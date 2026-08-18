"""Timeline marks: storage, import semantics, inspector rendering, export, CLI.

Four boundaries are defended here, and each one is a specific way this feature
could look like it works while quietly not:

1. The pipeline may rewrite everything it produced and must never reach a mark
   — the same red line the annotations tests draw, for the same reason.
2. `import_marks` validates the whole batch BEFORE deleting anything. Reversed,
   one bad row in a generated file destroys the good set it was replacing.
3. The inspector must actually CALL the renderer. A function that returns
   correct HTML nobody inserts passes every unit test and ships a blank page.
4. Marks stay out of an exported bundle unless asked for. The default is the
   safety property; a default nobody pins is a default that drifts.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from reel_scout import bundle, cli, db, inspector, marks, scorer


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
          title: str = "Fried Chicken Reel", full=None,
          duration_sec: float = 12.0) -> str:
    vid = db.upsert_video(conn, platform="instagram", platform_id=platform_id,
                          url="https://instagram.com/reel/%s/" % platform_id,
                          title=title, uploader="someone", duration_sec=duration_sec)
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

def test_migration_adds_the_table_to_an_existing_db(temp_db):
    """A v12 database gains clip_marks without losing anything it already had."""
    c = sqlite3.connect(temp_db)
    c.row_factory = sqlite3.Row
    try:
        vid = _seed(c)
        c.execute("DROP TABLE clip_marks")
        c.execute("UPDATE schema_version SET version = 12")
        c.commit()

        db.init_db(c)

        # Against SCHEMA_VERSION rather than a literal, so a later migration
        # does not turn this into an edit made to silence a failure.
        assert (c.execute("SELECT version FROM schema_version").fetchone()[0]
                == db.SCHEMA_VERSION)
        assert c.execute("SELECT COUNT(*) FROM clip_marks").fetchone()[0] == 0
        assert db.get_video(c, vid) is not None
        assert db.get_analysis(c, vid) is not None

        marks.add(c, vid, 7.0, "still works")
        assert [m["label"] for m in marks.list_for(c, vid)] == ["still works"]
    finally:
        c.close()


def test_marks_come_back_in_time_order(conn):
    vid = _seed(conn)
    for t, label in [(9.0, "c"), (2.0, "a"), (5.0, "b")]:
        marks.add(conn, vid, t, label)
    assert [m["label"] for m in marks.list_for(conn, vid)] == ["a", "b", "c"]


def test_reanalyzing_and_rescoring_leave_the_marks_alone(conn):
    """The whole reason marks live in their own table."""
    vid = _seed(conn)
    marks.add(conn, vid, 7.0, "semantic turn", note="eye close-up")
    marks.import_marks(conn, vid, [{"t": 2.0, "label": "hook"}], source="teardown")

    # Re-analyze: a completely different analysis over the same video.
    _seed(conn, full={
        "summary": "Re-analyzed, different everything.",
        "topics": ["other"], "content_type": "educational",
        "content_structure": "problem-solution",
        "timeline": [{"timestamp": "0-5s", "event": "new"}],
    })
    # Re-score.
    db.save_score(conn, vid, scorer.VideoScore(
        hook_strength=8.0, visual_storytelling=7.0, pacing=6.0, structure=5.0,
        overall=6.5, reasoning="rescored", model_used="test"))
    # Re-extract keyframes into a new run, superseding the old one — the v12
    # shape a mark must survive, since keyframe rows do not.
    run_id = db.begin_keyframe_run(conn, vid, strategy="scene", requested_max=4)
    db.save_keyframes(conn, vid, [{"frame_index": 0, "timestamp_sec": 1.0,
                                   "file_path": "/tmp/x.jpg", "strategy": "scene"}],
                      run_id=run_id)
    db.commit_keyframe_run(conn, run_id, vid)
    conn.commit()

    rows = marks.list_for(conn, vid)
    assert [(m["t_sec"], m["label"], m["source"]) for m in rows] == [
        (2.0, "hook", "import:teardown"),
        (7.0, "semantic turn", "manual"),
    ]


# --- import: validate before delete ---

def test_one_bad_row_rejects_the_batch_and_leaves_the_old_set_intact(conn):
    """Reverse the validate/delete order and this is the test that goes red."""
    vid = _seed(conn)
    marks.import_marks(conn, vid, [{"t": 1.0, "label": "keep me"},
                                   {"t": 4.0, "label": "me too"}],
                       source="teardown")

    with pytest.raises(marks.MarkError) as exc:
        marks.import_marks(conn, vid, [{"t": 2.0, "label": "fine"},
                                       {"t": 999.0, "label": "past the end"}],
                           source="teardown")
    assert "past the end" in str(exc.value)

    # Not "the batch failed" — the PREVIOUS batch is still all there.
    assert [m["label"] for m in marks.list_for(conn, vid)] == ["keep me", "me too"]


@pytest.mark.parametrize("bad, why", [
    ([{"t": 1.0, "label": ""}], "label is empty"),
    ([{"t": 1.0, "label": "x" * 61}], "label is longer"),
    ([{"t": -1.0, "label": "before"}], "before the clip starts"),
    ([{"t": 99.0, "label": "after"}], "past the end"),
    ([{"t": "soon", "label": "vague"}], "not a number"),
    ([{"label": "no time"}], "has no time"),
    ([{"t": 1.0, "label": "long note", "note": "n" * 501}], "note is longer"),
    (["not an object"], "is not an object"),
])
def test_input_validation_rejects_rather_than_writing_something_wrong(conn, bad, why):
    vid = _seed(conn)
    marks.add(conn, vid, 3.0, "survivor")
    with pytest.raises(marks.MarkError) as exc:
        marks.import_marks(conn, vid, bad, source="teardown")
    assert why in str(exc.value)
    assert [m["label"] for m in marks.list_for(conn, vid)] == ["survivor"]


def test_over_the_per_video_limit_is_refused(conn):
    vid = _seed(conn)
    too_many = [{"t": 0.0, "label": "m%d" % i}
                for i in range(marks.MAX_MARKS_PER_VIDEO + 1)]
    with pytest.raises(marks.MarkError) as exc:
        marks.import_marks(conn, vid, too_many, source="teardown")
    assert "over the limit" in str(exc.value)
    assert marks.list_for(conn, vid) == []


def test_reimporting_the_same_source_replaces_rather_than_accumulates(conn):
    vid = _seed(conn)
    first = marks.import_marks(conn, vid, [{"t": 1.0, "label": "v1 a"},
                                           {"t": 2.0, "label": "v1 b"}],
                               source="teardown")
    assert (first["removed"], first["added"]) == (0, 2)

    second = marks.import_marks(conn, vid, [{"t": 3.0, "label": "v2 only"}],
                                source="teardown")
    assert (second["removed"], second["added"]) == (2, 1)
    assert [m["label"] for m in marks.list_for(conn, vid)] == ["v2 only"]


def test_an_import_never_touches_the_hand_typed_marks(conn):
    vid = _seed(conn)
    marks.add(conn, vid, 7.0, "typed by hand")
    marks.import_marks(conn, vid, [{"t": 1.0, "label": "generated"}], source="teardown")
    marks.import_marks(conn, vid, [{"t": 5.0, "label": "regenerated"}], source="teardown")
    marks.import_marks(conn, vid, [{"t": 6.0, "label": "other tool"}], source="beats")

    rows = marks.list_for(conn, vid)
    assert [(m["label"], m["source"]) for m in rows] == [
        ("regenerated", "import:teardown"),
        ("other tool", "import:beats"),
        ("typed by hand", "manual"),
    ]


def test_an_importer_may_not_claim_the_manual_source(conn):
    """Otherwise every re-import would delete exactly what `source` protects."""
    vid = _seed(conn)
    marks.add(conn, vid, 7.0, "typed by hand")
    with pytest.raises(marks.MarkError) as exc:
        marks.import_marks(conn, vid, [{"t": 1.0, "label": "sneaky"}], source="manual")
    assert "reserved" in str(exc.value)
    assert [m["label"] for m in marks.list_for(conn, vid)] == ["typed by hand"]


def test_a_bare_source_name_means_the_same_row_everywhere(conn):
    """`--import --source teardown` then `--clear --source teardown` must match.

    They are stored as `import:teardown`. Normalizing on the way in but not on
    the way out would make the clear a no-op that reports "removed 0" — true,
    useless, and indistinguishable from "there was nothing to remove".
    """
    vid = _seed(conn)
    marks.import_marks(conn, vid, [{"t": 1.0, "label": "a"}], source="teardown")
    assert [m["label"] for m in marks.list_for(conn, vid, source="teardown")] == ["a"]
    assert [m["label"] for m in marks.list_for(conn, vid, source="import:teardown")] == ["a"]
    assert marks.clear(conn, vid, source="teardown") == 1
    assert marks.list_for(conn, vid) == []


def test_a_clip_with_no_recorded_duration_says_so_instead_of_guessing(conn):
    vid = _seed(conn, platform_id="nodur", duration_sec=None)
    result = marks.import_marks(conn, vid, [{"t": 9999.0, "label": "unbounded"}],
                                source="teardown")
    assert result["duration_checked"] is False
    assert result["added"] == 1


def test_clear_without_a_source_takes_everything(conn):
    vid = _seed(conn)
    marks.add(conn, vid, 1.0, "manual one")
    marks.import_marks(conn, vid, [{"t": 2.0, "label": "imported"}], source="teardown")
    assert marks.clear(conn, vid) == 2
    assert marks.list_for(conn, vid) == []


def test_removing_a_mark_is_scoped_to_the_clip_you_named(conn):
    """Ids are global. Naming one clip and deleting another's row is the kind of
    success message that is worse than an error."""
    a = _seed(conn, platform_id="clipA")
    b = _seed(conn, platform_id="clipB")
    mark_b = marks.add(conn, b, 1.0, "belongs to B")

    with pytest.raises(marks.MarkError) as exc:
        marks.remove(conn, mark_b["id"], video_id=a)
    assert "not on" in str(exc.value)
    assert [m["label"] for m in marks.list_for(conn, b)] == ["belongs to B"]

    assert marks.remove(conn, mark_b["id"], video_id=b) is True
    assert marks.list_for(conn, b) == []


def test_marks_on_an_unknown_video_are_404(conn):
    with pytest.raises(marks.MarkError) as exc:
        marks.add(conn, "no-such-video", 1.0, "x")
    assert exc.value.status == 404
    with pytest.raises(marks.MarkError) as exc:
        marks.remove(conn, 12345)
    assert exc.value.status == 404


# --- inspector rendering ---

def _render(conn, vid):
    view = inspector.build_inspect_view(conn, vid)
    return view, inspector.render_inspector(view)


def test_a_clip_with_no_marks_renders_no_marks_section(conn):
    vid = _seed(conn)
    _, html = _render(conn, vid)
    assert 'id="marks"' not in html
    assert 'class="mkrow"' not in html
    assert 'data-i18n="marks"' not in html


def test_marks_render_on_the_waveform_and_as_a_clickable_list(conn):
    """Both halves, because this is exactly where a mock silently rendered
    nothing: the section was built correctly and never inserted into the page."""
    vid = _seed(conn)
    marks.add(conn, vid, 7.0, "semantic turn", note="eye close-up")
    marks.add(conn, vid, 2.0, "hook lands")

    view, html = _render(conn, vid)
    assert [m["label"] for m in view["marks"]] == ["hook lands", "semantic turn"]

    # the waveform layer
    assert 'id="marks"' in html
    assert html.count('class="mktick"') == 2
    # ticks sit inside the waveform svg, before the playhead so it draws on top
    svg = html.split('id="wfsvg"', 1)[1].split("</svg>", 1)[0]
    assert 'id="marks"' in svg
    assert svg.index('id="marks"') < svg.index('id="head"')

    # the list layer
    assert html.count('class="mkrow"') == 2
    assert 'data-ts="7.000"' in html
    assert "semantic turn" in html and "eye close-up" in html
    assert 'data-i18n="marks"' in html

    # and the script that makes a click seek
    assert "mkRows" in html and "seek(+r.dataset.ts)" in html


def test_mark_text_is_escaped_not_injected(conn):
    vid = _seed(conn)
    marks.add(conn, vid, 1.0, '"><script>alert(1)</script>')
    _, html = _render(conn, vid)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_tick_x_is_in_the_waveforms_own_coordinate_space(conn):
    """Half way through a 12s clip is half way across the viewBox, not 6px."""
    vid = _seed(conn)
    marks.add(conn, vid, 6.0, "midpoint")
    _, html = _render(conn, vid)
    expected = 6.0 / 12.0 * inspector._WAVEFORM_BINS
    assert 'x1="%.3f"' % expected in html


# --- export ---

def _read_pages(out_dir: str) -> str:
    text = []
    for name in os.listdir(out_dir):
        with open(os.path.join(out_dir, name), encoding="utf-8") as f:
            text.append(f.read())
    return "\n".join(text)


def test_export_bundle_carries_no_marks_by_default(conn):
    """Marks are working state; the take-home file must not ship them."""
    vid = _seed(conn)
    marks.add(conn, vid, 7.0, "semantic turn", note="do not ship this")

    page = bundle.build_reel_page(conn, vid)

    assert page["ok"]
    assert "semantic turn" not in page["html"]
    assert "do not ship this" not in page["html"]
    assert 'class="mkrow"' not in page["html"]
    assert 'id="marks"' not in page["html"]
    assert "Fried Chicken" in page["html"]          # the analysis is still there


def test_with_marks_is_what_puts_them_in(conn):
    vid = _seed(conn)
    marks.add(conn, vid, 7.0, "semantic turn", note="ship this one")

    page = bundle.build_reel_page(conn, vid, with_marks=True)

    assert "semantic turn" in page["html"]
    assert "ship this one" in page["html"]
    assert 'class="mkrow"' in page["html"]


def test_build_bundle_passes_the_gate_through(conn, tmp_path):
    """The flag has to survive the whole call chain, not just the leaf."""
    vid = _seed(conn)
    marks.add(conn, vid, 7.0, "semantic turn")

    out = str(tmp_path / "off")
    bundle.build_bundle(conn, out, video_ids=[vid])
    assert "semantic turn" not in _read_pages(out)

    out_on = str(tmp_path / "on")
    bundle.build_bundle(conn, out_on, video_ids=[vid], with_marks=True)
    assert "semantic turn" in _read_pages(out_on)


def test_cli_export_defaults_to_withholding_marks(temp_db, capsys, tmp_path):
    """Pins the CLI default itself, not just the library's."""
    c = sqlite3.connect(temp_db)
    c.row_factory = sqlite3.Row
    vid = _seed(c)
    marks.add(c, vid, 7.0, "semantic turn")
    c.close()

    out = str(tmp_path / "bundle-default")
    cli.main(["export", "--format", "bundle", "--output", out])
    capsys.readouterr()
    assert "semantic turn" not in _read_pages(out)

    out_on = str(tmp_path / "bundle-marks")
    cli.main(["export", "--format", "bundle", "--output", out_on, "--with-marks"])
    capsys.readouterr()
    assert "semantic turn" in _read_pages(out_on)


# --- CLI ---

def test_cli_mark_add_list_import_and_clear(temp_db, capsys, tmp_path):
    c = sqlite3.connect(temp_db)
    c.row_factory = sqlite3.Row
    vid = _seed(c)
    c.close()

    cli.main(["mark", vid, "--at", "7.0", "--label", "semantic turn",
              "--note", "eye close-up"])
    assert "semantic turn" in capsys.readouterr().out

    cli.main(["mark", vid, "--list", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert [m["label"] for m in out["marks"]] == ["semantic turn"]
    assert out["marks"][0]["source"] == "manual"

    src = tmp_path / "marks.json"
    src.write_text(json.dumps({"marks": [{"t": 2.0, "label": "hook lands"},
                                         {"t": 11.0, "label": "cta"}]}),
                   encoding="utf-8")
    cli.main(["mark", vid, "--import", str(src), "--source", "teardown"])
    assert "added 2" in capsys.readouterr().out

    # A URL resolves the same as an id.
    cli.main(["mark", "https://instagram.com/reel/abc123/", "--list", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert [m["label"] for m in out["marks"]] == ["hook lands", "semantic turn", "cta"]

    # Scoped clear leaves the hand-typed one alone.
    cli.main(["mark", vid, "--clear", "--source", "teardown"])
    assert "removed 2" in capsys.readouterr().out
    cli.main(["mark", vid, "--list", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert [m["label"] for m in out["marks"]] == ["semantic turn"]

    cli.main(["mark", vid, "--rm", str(out["marks"][0]["id"])])
    assert "removed mark" in capsys.readouterr().out
    cli.main(["mark", vid, "--list"])
    assert "no marks" in capsys.readouterr().out


@pytest.mark.parametrize("argv, expect", [
    (["mark", "does-not-exist", "--at", "1", "--label", "x"], "no video found"),
    (["mark", "abc", "--at", "1", "--label", ""], "label is empty"),
])
def test_cli_mark_failures_exit_non_zero_with_a_message(temp_db, argv, expect):
    """#68 fixed exactly this hole in batch/analyze. Not reopening it here."""
    c = sqlite3.connect(temp_db)
    c.row_factory = sqlite3.Row
    vid = _seed(c)
    c.close()
    argv = [a if a != "abc" else vid for a in argv]

    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert expect in str(exc.value)
    # SystemExit("msg") exits 1; what must never happen is a silent 0.
    assert exc.value.code != 0


def test_cli_mark_bad_import_file_returns_non_zero(temp_db, capsys, tmp_path):
    """A generator feeding this must be able to tell that nothing was written."""
    c = sqlite3.connect(temp_db)
    c.row_factory = sqlite3.Row
    vid = _seed(c)
    marks.add(c, vid, 3.0, "already here")
    c.close()

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(["mark", vid, "--import", str(broken), "--source", "teardown"])
    assert exc.value.code == 1
    assert "not valid JSON" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        cli.main(["mark", vid, "--import", str(tmp_path / "nope.json"),
                  "--source", "teardown"])
    assert exc.value.code == 1

    c = sqlite3.connect(temp_db)
    c.row_factory = sqlite3.Row
    assert [m["label"] for m in marks.list_for(c, vid)] == ["already here"]
    c.close()


def test_cli_label_without_a_time_is_refused(temp_db, capsys):
    c = sqlite3.connect(temp_db)
    c.row_factory = sqlite3.Row
    vid = _seed(c)
    c.close()
    with pytest.raises(SystemExit) as exc:
        cli.main(["mark", vid, "--label", "no time given"])
    assert exc.value.code == 1
    assert "needs --at" in capsys.readouterr().out


# --- MCP ---

def test_mcp_mark_round_trip(temp_db):
    from reel_scout.mcp import tools

    c = sqlite3.connect(temp_db)
    c.row_factory = sqlite3.Row
    vid = _seed(c)
    c.close()

    res = tools.call_tool("mark", {"video_id": vid, "t_sec": 7.0,
                                   "label": "semantic turn", "note": "eye close-up"})
    assert "isError" not in res
    payload = json.loads(res["content"][0]["text"])
    assert payload["mark"]["label"] == "semantic turn"

    res = tools.call_tool("mark", {"video_id": vid, "source": "teardown",
                                   "marks": [{"t": 2.0, "label": "hook lands"}]})
    payload = json.loads(res["content"][0]["text"])
    assert (payload["removed"], payload["added"]) == (0, 1)
    assert [m["label"] for m in payload["marks"]] == ["hook lands", "semantic turn"]

    # Listing is the default action.
    res = tools.call_tool("mark", {"video_id": vid})
    assert len(json.loads(res["content"][0]["text"])["marks"]) == 2

    # An import with no source is an error, not a silent wipe of everything.
    res = tools.call_tool("mark", {"video_id": vid, "marks": [{"t": 1.0, "label": "x"}]})
    assert res.get("isError")
    res = tools.call_tool("mark", {"video_id": vid})
    assert len(json.loads(res["content"][0]["text"])["marks"]) == 2

    # Bad input is a reported error, not an exception and not a partial write.
    res = tools.call_tool("mark", {"video_id": vid, "t_sec": 999.0, "label": "late"})
    assert res.get("isError")

    res = tools.call_tool("mark", {"video_id": vid, "clear": True,
                                   "source": "teardown"})
    payload = json.loads(res["content"][0]["text"])
    assert payload["removed"] == 1
    assert [m["label"] for m in payload["marks"]] == ["semantic turn"]


def test_mcp_mark_is_both_visible_and_callable():
    """The two registries that decide this are separate and nothing makes them
    agree — missing from one is invisible, missing from the other is unknown."""
    from reel_scout.mcp import tools

    assert "mark" in [t["name"] for t in tools.list_tools()]
    assert "mark" in tools._HANDLERS
