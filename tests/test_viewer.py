"""Read-only HTML viewer export (self-contained, base64 keyframes)."""
from __future__ import annotations

import json
import os
import sqlite3
import struct
import tempfile
import zlib

from reel_scout import config, db, i18n, viewer
from reel_scout.export.json_export import export_html


def _tiny_jpeg(path: str) -> None:
    # Smallest valid-ish JPEG bytes; enough for base64 embedding to succeed.
    data = bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300"
        + "08060607060508070707090909" * 1  # filler
    ) + b"\xff\xd9"
    with open(path, "wb") as f:
        f.write(data)


_FULL = {
    "summary": "A punchy fried-chicken reel.",
    "topics": ["food", "fried chicken"],
    "content_type": "promotional",
    "content_structure": "hook-body-cta",
    "hook": {"opening_type": "question", "opening_text": "Hungry?",
             "cta_type": "visit", "cta_text": "come try it"},
    "style": {"format": "montage", "pacing": "fast"},
    "timeline": [{"timestamp": "0-3s", "event": "hook"}, {"timestamp": "3-15s", "event": "food"}],
}


def _seed(conn, kf_path=None, text_full="Hungry? Come try our chicken.", platform_id="abc"):
    # Order mirrors the real pipeline (transcribe/keyframes first, merge last) so
    # the final video status is "analyzed" — save_transcript sets "transcribed".
    vid = db.upsert_video(conn, platform="youtube", platform_id=platform_id,
                          url="https://youtube.com/shorts/%s" % platform_id, title="Fried Chicken",
                          uploader="Waffle", duration_sec=20.0)
    db.save_transcript(conn, vid, language="en", text_full=text_full,
                       segments_json="[]", whisper_model="x", duration_sec=20.0)
    conn.execute("INSERT INTO scores (video_id, hook_strength, visual_storytelling, "
                 "pacing, structure, overall) VALUES (?,?,?,?,?,?)", (vid, 8.0, 7.0, 9.0, 6.5, 7.6))
    if kf_path:
        db.save_keyframes(conn, vid, [{"frame_index": 0, "timestamp_sec": 1.0,
                                       "file_path": kf_path, "strategy": "scene"}])
        kf = db.get_keyframes(conn, vid)[0]
        conn.execute("INSERT INTO vision_descriptions (keyframe_id, description, "
                     "text_in_frame, objects_json, vlm_backend, vlm_model) VALUES (?,?,?,?,?,?)",
                     (kf["id"], "close-up of fried chicken", "SO GOOD", "[]", "omlx", "x"))
    db.save_analysis(conn, vid, summary=_FULL["summary"], topics_json=json.dumps(_FULL["topics"]),
                     hooks_json=json.dumps(_FULL["hook"]), style_json=json.dumps(_FULL["style"]),
                     engagement_signals_json="{}", full_json=json.dumps(_FULL))
    conn.commit()
    return vid


def test_get_keyframes_with_descriptions_left_join(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    tmpjpg = os.path.join(config.KEYFRAMES_DIR, "kf.jpg")
    os.makedirs(config.KEYFRAMES_DIR, exist_ok=True)
    _tiny_jpeg(tmpjpg)
    try:
        vid = _seed(conn, kf_path=tmpjpg)
        rows = db.get_keyframes_with_descriptions(conn, vid)
        assert len(rows) == 1
        assert rows[0]["description"] == "close-up of fried chicken"
        assert rows[0]["text_in_frame"] == "SO GOOD"
    finally:
        conn.close()


def test_build_video_view_assembles_full_record(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        vid = _seed(conn)
        view = viewer.build_video_view(conn, vid)
        assert view["title"] == "Fried Chicken"
        assert view["content_structure"] == "hook-body-cta"
        assert view["hook"]["cta_type"] == "visit"
        assert view["score"]["overall"] == 7.6
        assert "chicken" in view["transcript"]
        assert view["timeline"][0]["event"] == "hook"
    finally:
        conn.close()


def test_export_html_is_self_contained_and_readonly(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    kf = os.path.join(config.KEYFRAMES_DIR, "abc", "abc_scene_000.jpg")
    os.makedirs(os.path.dirname(kf), exist_ok=True)
    _tiny_jpeg(kf)
    out = os.path.join(tempfile.mkdtemp(), "viewer.html")
    try:
        _seed(conn, kf_path=kf)
        path = export_html(conn, out)
        assert os.path.exists(path)
        content = open(path, encoding="utf-8").read()
        # self-contained: keyframe embedded, no external asset refs
        assert "data:image/jpeg;base64," in content
        assert "http://" not in content.replace("https://youtube.com", "")  # no external asset hosts
        assert "<script src=" not in content and 'link rel="stylesheet"' not in content
        # decoded structure + scores present
        assert "hook-body-cta" in content
        assert "Fried Chicken" in content
        assert "7.6" in content
        # read-only: no action surfaces, scores framed as reference
        assert "<form" not in content
        assert "reference, not authority" in content
    finally:
        conn.close()


def test_export_html_missing_keyframe_degrades_gracefully(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    out = os.path.join(tempfile.mkdtemp(), "viewer.html")
    try:
        _seed(conn, kf_path="/no/such/frame.jpg")  # recorded but file missing
        path = export_html(conn, out)
        content = open(path, encoding="utf-8").read()
        assert "image unavailable" in content  # placeholder, not a crash
    finally:
        conn.close()


def test_render_pages_use_url_keyframes_not_base64(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    kf = os.path.join(config.KEYFRAMES_DIR, "abc", "abc_scene_000.jpg")
    os.makedirs(os.path.dirname(kf), exist_ok=True)
    _tiny_jpeg(kf)
    try:
        vid = _seed(conn, kf_path=kf)
        idx = viewer.render_index_page(conn)
        assert '/video/%s' % vid in idx and "Fried Chicken" in idx
        page = viewer.render_video_page(conn, vid)
        assert '/keyframe/' in page          # server serves frames by URL
        assert 'data:image/jpeg;base64,' not in page   # NOT embedded on the server
        assert viewer.render_video_page(conn, "nope") is None
    finally:
        conn.close()


def test_view_server_serves_index_video_and_keyframe(temp_db):
    import threading
    import urllib.request

    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    kf = os.path.join(config.KEYFRAMES_DIR, "abc", "abc_scene_000.jpg")
    os.makedirs(os.path.dirname(kf), exist_ok=True)
    _tiny_jpeg(kf)
    vid = _seed(conn, kf_path=kf)
    kf_id = db.get_keyframes(conn, vid)[0]["id"]
    conn.close()  # server opens its own per-request connections via config.DB_PATH

    httpd = viewer.make_server(port=0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = "http://127.0.0.1:%d" % port
        index = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        assert "Fried Chicken" in index and "/video/%s" % vid in index

        vpage = urllib.request.urlopen("%s/video/%s" % (base, vid), timeout=5).read().decode()
        assert "hook-body-cta" in vpage and "reference, not authority" in vpage

        img = urllib.request.urlopen("%s/keyframe/%s" % (base, kf_id), timeout=5)
        assert img.status == 200
        assert img.read()[:2] == b"\xff\xd8"  # JPEG magic

        # unknown routes 404
        try:
            urllib.request.urlopen(base + "/video/nope", timeout=5)
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_view_server_serves_while_a_connection_is_held_open(temp_db):
    """A stalled connection must not block other requests.

    The plain single-threaded HTTPServer serialized everything, so one idle
    browser keep-alive socket stalled the whole viewer — and a video page, which
    fetches each keyframe over its own request, could hang until timeout.
    """
    import socket
    import threading
    import urllib.request

    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    _seed(conn)
    conn.close()

    httpd = viewer.make_server(port=0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        # Connect and leave the request unfinished (no terminating blank line),
        # the way an idle keep-alive socket sits there.
        hog = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            hog.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n")
            resp = urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5)
            assert resp.status == 200
        finally:
            hog.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_viewer_i18n_chrome_bilingual_and_model_content_untouched(temp_db):
    """The viewer's language toggle swaps chrome only; titles/values stay put.

    Same boundary the inspector holds: interface labels carry data-i18n and both
    dictionaries ship in the page (offline-capable toggle), but the video title,
    decoded-structure values and transcript are model output and never move.
    """
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    kf = os.path.join(config.KEYFRAMES_DIR, "abc", "abc_scene_000.jpg")
    os.makedirs(os.path.dirname(kf), exist_ok=True)
    _tiny_jpeg(kf)
    try:
        vid = _seed(conn, kf_path=kf)

        idx = viewer.render_index_page(conn)
        # toggle + both dicts + applyLang wiring present on the list page.
        assert 'class="langbtn" data-lang="zh"' in idx
        assert '"zh"' in idx and '"en"' in idx
        assert "解構分析" in idx and "工藝評分" in idx          # zh chrome in boot
        assert "applyLang" in idx and "localStorage.setItem('rs_lang'" in idx
        # The subtitle is chrome, and the served list says so with its own key:
        # that page writes annotations back, so it must not claim "read-only".
        assert 'data-i18n="subLive"' in idx
        assert 'data-i18n="sub"' not in idx
        assert "Fried Chicken" in idx                          # model title untranslated
        # The take-home export has no server to write to and keeps the old key.
        assert 'data-i18n="sub"' in viewer.render_bundle(conn)

        page = viewer.render_video_page(conn, vid)
        for key in ("decoded", "craftScores", "keyframes", "transcript",
                    "allVideos", "dim.overall", "row.Structure"):
            assert 'data-i18n="%s"' % key in page
        # baseline English still present (JS-off / string-contains safety).
        assert "Decoded structure" in page and "Craft scores" in page
    finally:
        conn.close()


def test_viewer_and_inspector_share_one_i18n_dict():
    """Both surfaces pull from reel_scout.i18n.STRINGS — no second copy to drift."""
    from reel_scout import i18n, inspector
    assert inspector.I18N is i18n.STRINGS
    assert set(i18n.STRINGS["en"]) == set(i18n.STRINGS["zh"])


# --- evidence: a score computed without a transcript must say so ---------------
#
# 36% of the live corpus (35 of 96 clips) has no usable transcript, and 32 of
# those still carry a four-dimension craft score. Every surface used to collapse
# "no words" into an empty string and then render nothing, so a score built on
# the visual layer alone was typographically identical to one built on
# everything. Mean overall for the two groups is 6.82 vs 6.88 -- the scorer is
# demonstrably not discounting, which is why the reader has to be told.

def _seed_no_row(conn):
    """A clip whose transcripts row was never written at all."""
    vid = db.upsert_video(conn, platform="instagram", platform_id="silent",
                          url="https://instagram.com/reel/silent/", title="Silent",
                          uploader="nobody", duration_sec=9.0)
    db.save_analysis(conn, vid, summary="", topics_json="[]", hooks_json="{}",
                     style_json="{}", engagement_signals_json="{}", full_json="{}")
    conn.commit()
    return vid


def test_build_video_view_flags_a_clip_with_no_words(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        spoken = _seed(conn)
        silent = _seed(conn, text_full="", platform_id="quiet")
        norow = _seed_no_row(conn)
        assert viewer.build_video_view(conn, spoken)["has_transcript"] is True
        assert viewer.build_video_view(conn, silent)["has_transcript"] is False
        # A missing row and an empty row mean the same thing to a reader.
        assert viewer.build_video_view(conn, norow)["has_transcript"] is False
    finally:
        conn.close()


def test_whitespace_only_transcript_counts_as_no_words(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        vid = _seed(conn, text_full="   \n  ")
        assert viewer.build_video_view(conn, vid)["has_transcript"] is False
    finally:
        conn.close()


def test_index_row_marks_only_the_clip_with_no_transcript(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        spoken = _seed(conn)
        silent = _seed(conn, text_full="", platform_id="quiet")
        views = [viewer.build_video_view(conn, spoken),
                 viewer.build_video_view(conn, silent)]
        page = viewer.render_index(views, href=lambda v: "/inspect/%s" % v)
    finally:
        conn.close()
    assert page.count('data-i18n="noTranscript"') == 1


def test_video_section_says_no_transcript_instead_of_rendering_nothing(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        vid = _seed(conn, text_full="")
        page = viewer.render_video_section(viewer.build_video_view(conn, vid),
                                           keyframe_src=lambda kf: "")
    finally:
        conn.close()
    assert 'data-i18n="noTranscript"' in page
    assert 'data-i18n="noTranscriptNote"' in page
    # The score still renders -- this labels evidence, it never withholds.
    assert "7.6" in page


def test_a_transcribed_clip_gets_no_marker(temp_db):
    """Negative control: the marker must not appear where words exist."""
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        vid = _seed(conn)
        page = viewer.render_video_section(viewer.build_video_view(conn, vid),
                                           keyframe_src=lambda kf: "")
    finally:
        conn.close()
    assert 'data-i18n="noTranscriptNote"' not in page
    assert 'data-i18n="transcript"' in page


def test_the_rendered_baseline_matches_the_english_dict(temp_db):
    """applyLang() swaps textContent wholesale, so a drifted English baseline
    would flip to different words the first time someone toggles language."""
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        vid = _seed(conn, text_full="")
        page = viewer.render_video_section(viewer.build_video_view(conn, vid),
                                           keyframe_src=lambda kf: "")
    finally:
        conn.close()
    assert i18n.STRINGS["en"]["noTranscriptNote"] in page


def test_row_carries_its_own_save_hint_and_flushes_pending_note_on_pagehide(temp_db):
    """Feedback has to land where the eye is, and a pending note has to survive.

    Two defects reported together, and they were the same defect wearing two
    hats: a save that works but looks like nothing happened. The header hint is
    one element at the top, so on a library longer than a screen it reports into
    off-screen space — which is why the annotation layer read as "is there
    supposed to be a submit button?". And `focusout` only fires when you leave
    the field; closing the tab inside the 600ms debounce fired neither it nor
    the timer, so the last note typed was dropped without a word.
    """
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    _seed(conn)
    page = viewer.render_index_page(conn)
    conn.close()

    # every row can report next to itself, not only into the header
    assert page.count('class="rowhint"') == page.count('class="noteinput"')
    assert 'say(t(\'saved\',\'saved\'), false, tr)' in page

    # The hint needs a slot or it is in the markup and never on screen — the
    # note input is width:100%. That slot comes from a flex wrapper INSIDE the
    # cell: putting display:flex on the <td> itself drops it out of the table
    # layout algorithm and collapses the column, which is what the first two
    # attempts at this did. Both halves are pinned because the markup alone
    # looks correct while rendering to nothing.
    assert page.count('class="notewrap"') == page.count('class="noteinput"')
    assert '.notewrap{display:flex' in page
    assert '.c-note{display:flex' not in page

    # the pending note leaves even when the page is going away
    assert "addEventListener('pagehide'" in page
    assert "navigator.sendBeacon('/api/annotate/'" in page

    # ...and the flush asks "does this row differ from what the server
    # confirmed", not "is a timer pending". A pending timer is one of three
    # ways a row can be dirty at that moment — the debounce may have fired
    # and left a request in flight, or an earlier save may have failed. The
    # first cut keyed on the timer and silently dropped the other two, so the
    # value comparison is the part worth pinning.
    assert "var known=(vid in saved) ? saved[vid] : inp.defaultValue" in page
    assert "if(inp.value===known) return" in page
    assert "for(var vid in timers)" not in page


def test_annotate_accepts_a_beacon_shaped_post(temp_db):
    """`sendBeacon` is the contract the pagehide flush rests on — pin it.

    A beacon sends a Blob, and the browser sets the Content-Type from it; the
    JS is free to pick one, but nothing stops that from drifting. The endpoint
    reads the body as JSON regardless, which is exactly why no separate beacon
    route was needed. If someone later makes the handler require
    `application/json`, notes typed in the last 600ms before a tab close start
    disappearing again — silently, and only for that one case. This test is the
    thing that would catch it.

    Drives `make_inspect_server`, which is what `reel-scout view` actually
    serves — `viewer.make_server` is the read-only GET-only server kept for
    tests, and pointing a write test at it only proves that it has no do_POST.
    """
    import threading
    import urllib.request

    from reel_scout.inspector import make_inspect_server

    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    vid = _seed(conn)
    conn.close()

    httpd = make_inspect_server(port=0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        for ctype in ("application/json", "text/plain;charset=UTF-8", None):
            body = json.dumps({"note": "beacon %s" % ctype}).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/annotate/%s" % (port, vid),
                data=body, method="POST")
            if ctype:
                req.add_header("Content-Type", ctype)
            assert urllib.request.urlopen(req, timeout=5).status == 200

            check = sqlite3.connect(temp_db)
            check.row_factory = sqlite3.Row
            got = check.execute(
                "SELECT note FROM video_annotations WHERE video_id=?", (vid,)).fetchone()
            check.close()
            assert got["note"] == "beacon %s" % ctype
    finally:
        httpd.shutdown()
        httpd.server_close()
