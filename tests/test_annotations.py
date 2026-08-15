"""User annotations: note / group / star across storage, API, list page and CLI.

The boundary these tests defend is the reason the feature has its own tables:
the pipeline may rewrite everything it produced, and must never touch what the
operator typed.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

from reel_scout import annotate, cli, config, db, viewer


_FULL = {
    "summary": "A punchy reel.",
    "topics": ["food"],
    "content_type": "promotional",
    "content_structure": "hook-body-cta",
    "hook": {"opening_type": "question", "opening_text": "Hungry?"},
    "style": {"format": "montage", "pacing": "fast"},
    "timeline": [{"timestamp": "0-3s", "event": "hook"}],
}


def _seed(conn: sqlite3.Connection, platform_id: str = "abc123",
          title: str = "Fried Chicken Reel", full=None) -> str:
    vid = db.upsert_video(conn, platform="instagram", platform_id=platform_id,
                          url="https://instagram.com/reel/%s/" % platform_id,
                          title=title, uploader="someone", duration_sec=12.0)
    db.update_video_status(conn, vid, "analyzed")
    data = full or _FULL
    db.save_analysis(conn, vid, summary=data["summary"],
                     topics_json=json.dumps(data["topics"]),
                     hooks_json=json.dumps(data.get("hook", {})),
                     style_json=json.dumps(data.get("style", {})),
                     engagement_signals_json="{}",
                     full_json=json.dumps(data))
    conn.commit()
    return vid


@pytest.fixture
def conn(temp_db):
    c = sqlite3.connect(temp_db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
    finally:
        c.close()


# --- storage ---

def test_migration_adds_tables_to_an_existing_db(temp_db):
    """A v10 database gains the tables without losing anything it already had."""
    c = sqlite3.connect(temp_db)
    c.row_factory = sqlite3.Row
    try:
        vid = _seed(c)
        c.execute("DROP TABLE video_annotations")
        c.execute("DROP TABLE annotation_groups")
        c.execute("UPDATE schema_version SET version = 10")
        c.commit()

        db.init_db(c)

        # Against SCHEMA_VERSION rather than a literal: what this asserts is
        # "migrating brings the database up to date", which stays true as
        # migrations are added. A hardcoded number turns every later migration
        # into an edit here, and an edit made to silence a failure is one nobody
        # thinks about.
        assert (c.execute("SELECT version FROM schema_version").fetchone()[0]
                == db.SCHEMA_VERSION)
        assert annotate.get(c, vid)["note"] is None      # table is there, empty
        assert db.get_video(c, vid) is not None          # nothing was dropped
    finally:
        c.close()


def test_partial_updates_do_not_clobber_the_other_fields(conn):
    vid = _seed(conn)
    annotate.set_annotation(conn, vid, note="course opener")
    annotate.set_annotation(conn, vid, starred=True)
    g = annotate.add_group(conn, "Course")
    annotate.set_annotation(conn, vid, group=g["id"])

    ann = annotate.get(conn, vid)
    assert ann["note"] == "course opener"
    assert ann["starred"] == 1
    assert ann["group_name"] == "Course"

    # A star toggle sent on its own must not erase the note beside it.
    annotate.set_annotation(conn, vid, starred=False)
    assert annotate.get(conn, vid)["note"] == "course opener"


def test_reanalyzing_a_video_leaves_the_annotation_alone(conn):
    """The whole reason these live in their own tables."""
    vid = _seed(conn)
    annotate.set_annotation(conn, vid, note="mine", starred=True,
                            group="Course", create_group=True)

    _seed(conn, full={
        "summary": "Re-analyzed, different everything.",
        "topics": ["other"], "content_type": "educational",
        "content_structure": "problem-solution",
        "timeline": [{"timestamp": "0-5s", "event": "new"}],
    })

    ann = annotate.get(conn, vid)
    assert (ann["note"], ann["starred"], ann["group_name"]) == ("mine", 1, "Course")


def test_deleting_a_group_keeps_the_note_and_star(conn):
    vid = _seed(conn)
    g = annotate.add_group(conn, "Scratch")
    annotate.set_annotation(conn, vid, note="keep me", starred=True, group=g["id"])

    annotate.remove_group(conn, int(g["id"]))

    ann = annotate.get(conn, vid)
    assert ann["group_id"] is None and ann["group_name"] is None
    assert ann["note"] == "keep me" and ann["starred"] == 1


def test_group_names_are_unique_case_insensitively(conn):
    annotate.add_group(conn, "Course")
    with pytest.raises(annotate.AnnotateError) as exc:
        annotate.add_group(conn, "course")
    assert exc.value.status == 409


def test_rejects_overlong_note_rather_than_truncating(conn):
    vid = _seed(conn)
    with pytest.raises(annotate.AnnotateError):
        annotate.set_annotation(conn, vid, note="x" * (annotate.MAX_NOTE_LEN + 1))
    assert annotate.get(conn, vid)["note"] is None


def test_annotating_an_unknown_video_is_404(conn):
    with pytest.raises(annotate.AnnotateError) as exc:
        annotate.set_annotation(conn, "nope", note="hi")
    assert exc.value.status == 404


# --- JSON API ---

def test_api_star_group_and_note_round_trip(conn):
    vid = _seed(conn)
    status, body = annotate.handle_api(conn, "POST", "/api/groups", {"name": "Course"})
    assert status == 200
    gid = body["group"]["id"]

    status, body = annotate.handle_api(
        conn, "POST", "/api/annotate/" + vid,
        {"note": "first lesson", "group_id": gid, "starred": True})
    assert status == 200
    assert body["annotation"]["group_name"] == "Course"
    assert body["annotation"]["starred"] == 1

    status, body = annotate.handle_api(conn, "GET", "/api/annotate/" + vid, None)
    assert body["annotation"]["note"] == "first lesson"


def test_api_explicit_null_group_clears_only_the_grouping(conn):
    vid = _seed(conn)
    g = annotate.add_group(conn, "Course")
    annotate.set_annotation(conn, vid, note="n", group=g["id"], starred=True)

    _, body = annotate.handle_api(conn, "POST", "/api/annotate/" + vid,
                                  {"group_id": None})
    assert body["annotation"]["group_id"] is None
    assert body["annotation"]["note"] == "n"        # untouched
    assert body["annotation"]["starred"] == 1


def test_api_reports_errors_with_a_usable_status(conn):
    status, body = annotate.handle_api(conn, "POST", "/api/annotate/missing",
                                       {"note": "x"})
    assert status == 404 and "error" in body

    annotate.add_group(conn, "Course")
    status, body = annotate.handle_api(conn, "POST", "/api/groups", {"name": " Course "})
    assert status == 409

    status, _ = annotate.handle_api(conn, "POST", "/api/groups", {"name": "  "})
    assert status == 400


# --- rendering ---

def test_served_list_is_a_table_with_the_controls(conn):
    vid = _seed(conn)
    g = annotate.add_group(conn, "Course")
    annotate.set_annotation(conn, vid, note="opener", group=g["id"], starred=True)

    html = viewer.render_index_page(conn)

    assert '<table class="library"' in html
    assert 'id="starfilter"' in html                       # the header star
    assert 'data-i18n="col.note"' in html
    assert 'class="noteinput"' in html and "opener" in html
    assert '<select class="groupsel">' in html
    assert 'value="%d" selected' % int(g["id"]) in html
    assert 'aria-pressed="true"' in html and "★" in html    # starred row
    assert "/api/annotate/" in html                        # the writer script


def test_group_delete_control_is_offered_and_scoped_to_the_toolbar(conn):
    """Deleting a group is a library-wide action, so it lives in the toolbar —
    not as an ✕ inside a per-video dropdown, where it would be one slip away
    from destroying a group when you meant to unfile one clip."""
    vid = _seed(conn)
    g = annotate.add_group(conn, "Course")
    annotate.set_annotation(conn, vid, group=g["id"])

    html = viewer.render_index_page(conn)

    assert 'id="delgroup"' in html and 'id="rmgroup"' in html
    # The picker shows how many rows a group holds — that count is the warning.
    assert "Course (1)" in html
    assert "/api/groups/" in html            # the delete call
    # No confirm() anywhere: a modal blocks the page and this is cheap to undo.
    assert "confirm(" not in html


def test_export_bundle_carries_no_annotations_and_no_writer(conn):
    """Annotations are working state; the take-home file must not ship them."""
    vid = _seed(conn)
    annotate.set_annotation(conn, vid, note="internal only", starred=True,
                            group="Course", create_group=True)

    bundle = viewer.render_bundle(conn)

    assert "internal only" not in bundle
    assert "Course" not in bundle
    assert "/api/annotate/" not in bundle
    assert 'class="noteinput"' not in bundle
    assert "Fried Chicken" in bundle                        # the analysis is still there


def test_note_is_escaped_not_injected(conn):
    vid = _seed(conn)
    annotate.set_annotation(conn, vid, note='"><script>alert(1)</script>')
    html = viewer.render_index_page(conn)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# --- CLI ---

def test_cli_note_and_group_commands(temp_db, capsys):
    c = sqlite3.connect(temp_db)
    c.row_factory = sqlite3.Row
    vid = _seed(c)
    c.commit()
    c.close()

    cli.main(["group", "add", "Course"])
    cli.main(["note", vid, "--text", "first lesson", "--group", "Course", "--star"])
    capsys.readouterr()

    cli.main(["note", vid, "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["note"] == "first lesson"
    assert out["group_name"] == "Course"
    assert out["starred"] == 1

    cli.main(["group", "list"])
    assert "Course" in capsys.readouterr().out

    # A URL resolves the same as an id.
    cli.main(["note", "https://instagram.com/reel/abc123/", "--unstar", "--json"])
    assert json.loads(capsys.readouterr().out)["starred"] == 0

    cli.main(["group", "rm", "Course"])
    assert "kept" in capsys.readouterr().out
    cli.main(["note", vid, "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["group_id"] is None and out["note"] == "first lesson"


def test_cli_unknown_video_exits_with_a_message(temp_db):
    with pytest.raises(SystemExit) as exc:
        cli.main(["note", "does-not-exist", "--text", "x"])
    assert "no video found" in str(exc.value)


# --- over a real socket ---

def _post(base: str, path: str, payload):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    resp = urllib.request.urlopen(req, timeout=5)
    return resp.status, json.loads(resp.read().decode())


def test_server_writes_annotations_and_refuses_everything_else(temp_db):
    """The write path end to end, and the shape of its refusals."""
    from reel_scout import inspector

    c = sqlite3.connect(temp_db)
    c.row_factory = sqlite3.Row
    vid = _seed(c)
    c.close()

    httpd = inspector.make_inspect_server(port=0, default_id=None)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        status, body = _post(base, "/api/groups", {"name": "Course"})
        assert status == 200
        gid = body["group"]["id"]

        status, body = _post(base, "/api/annotate/" + vid,
                             {"note": "opener", "group_id": gid, "starred": True})
        assert status == 200 and body["annotation"]["group_name"] == "Course"

        # It really landed in the database, not just in the response.
        c = sqlite3.connect(temp_db)
        c.row_factory = sqlite3.Row
        assert annotate.get(c, vid)["note"] == "opener"
        c.close()

        # ...and it shows up on the page the next time it renders.
        page = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        assert "opener" in page

        # POST anywhere else is not a write surface.
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(base, "/api/waveform/" + vid, {})
        assert exc.value.code == 404

        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(base, "/api/annotate/nope", {"note": "x"})
        assert exc.value.code == 404

        # Malformed body is rejected before it can reach the database.
        req = urllib.request.Request(
            base + "/api/annotate/" + vid, data=b"not json",
            headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 400
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- MCP ---

def test_mcp_annotate_round_trip(temp_db):
    from reel_scout.mcp import tools

    c = sqlite3.connect(temp_db)
    c.row_factory = sqlite3.Row
    vid = _seed(c)
    c.close()

    res = tools.call_tool("annotate", {
        "video_id": vid, "note": "for lesson 1",
        "group": "Course", "create_group": True, "starred": True})
    assert "isError" not in res
    ann = json.loads(res["content"][0]["text"])["annotation"]
    assert ann["note"] == "for lesson 1" and ann["group_name"] == "Course"

    res = tools.call_tool("list_annotations", {"starred_only": True})
    payload = json.loads(res["content"][0]["text"])
    assert [a["video_id"] for a in payload["annotations"]] == [vid]
    assert [g["name"] for g in payload["groups"]] == ["Course"]

    # An unknown group is an error, not a silently empty list.
    res = tools.call_tool("list_annotations", {"group": "nope"})
    assert res.get("isError")
