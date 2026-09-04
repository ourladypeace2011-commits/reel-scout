"""Interactive single-clip inspector web app (player + waveform + filmstrip)."""
from __future__ import annotations

import array
import json
import os
import re
import sqlite3
import threading
import urllib.request

from reel_scout import config, db, inspector


def _tiny_jpeg(path: str) -> None:
    data = bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300"
        + "08060607060508070707090909"
    ) + b"\xff\xd9"
    with open(path, "wb") as f:
        f.write(data)


_SEGMENTS = [
    {"start": 0.0, "end": 2.4, "text": "Hungry right now?", "confidence": 0.9},
    {"start": 2.4, "end": 6.0, "text": "Watch this.", "confidence": 0.9},
    {"start": 6.0, "end": 8.4, "text": "Come try it.", "confidence": 0.9},
]

_FULL = {
    "summary": "A punchy fried-chicken reel.",
    "topics": ["food"],
    "content_structure": "hook-body-cta",
    "hook": {"opening_type": "question", "opening_text": "Hungry?",
             "cta_type": "visit", "cta_text": "come try it"},
    "style": {"format": "montage", "pacing": "fast"},
}


def _seed(conn, kf_path=None, duration_sec=8.4, segments=None, with_score=True,
          text_full="Hungry right now? Watch this. Come try it.", video_file=None):
    vid = db.upsert_video(conn, platform="instagram", platform_id="xyz",
                          url="https://www.instagram.com/reel/xyz/", title="Fried Chicken",
                          uploader="waffle", duration_sec=duration_sec,
                          file_path=video_file)
    db.save_transcript(
        conn, vid, language="en", text_full=text_full,
        segments_json=json.dumps(_SEGMENTS if segments is None else segments),
        whisper_model="x", duration_sec=duration_sec or 0.0)
    if with_score:
        conn.execute("INSERT INTO scores (video_id, hook_strength, visual_storytelling, "
                     "pacing, structure, overall, reasoning) VALUES (?,?,?,?,?,?,?)",
                     (vid, 8.0, 7.0, 9.0, 6.5, 7.6, "strong open, weak close"))
    if kf_path:
        db.save_keyframes(conn, vid, [
            {"frame_index": 0, "timestamp_sec": 0.5, "file_path": kf_path, "strategy": "scene"},
            {"frame_index": 1, "timestamp_sec": 7.9, "file_path": kf_path, "strategy": "scene"},
        ])
        first = db.get_keyframes(conn, vid)[0]
        conn.execute("INSERT INTO vision_descriptions (keyframe_id, description, "
                     "text_in_frame, objects_json, vlm_backend, vlm_model) VALUES (?,?,?,?,?,?)",
                     (first["id"], "close-up of fried chicken", "SO GOOD", "[]", "ollama", "x"))
    db.save_analysis(conn, vid, summary=_FULL["summary"], topics_json=json.dumps(_FULL["topics"]),
                     hooks_json=json.dumps(_FULL["hook"]), style_json=json.dumps(_FULL["style"]),
                     engagement_signals_json="{}", full_json=json.dumps(_FULL))
    conn.commit()
    return vid


def _conn(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    return conn


def _make_video_file(name="clip.mp4", body=b"0123456789" * 1000):
    os.makedirs(config.VIDEOS_DIR, exist_ok=True)
    path = os.path.join(config.VIDEOS_DIR, name)
    with open(path, "wb") as f:
        f.write(body)
    return name, path, body


# --- data assembly ---

def test_build_inspect_view_parses_segments(temp_db):
    conn = _conn(temp_db)
    try:
        vid = _seed(conn)
        view = inspector.build_inspect_view(conn, vid)
        assert view is not None
        assert len(view["segments"]) == 3
        assert view["segments"][0]["text"] == "Hungry right now?"
        assert view["has_video"] is False  # no file_path
        assert view["language"] == "en"
    finally:
        conn.close()


def test_build_inspect_view_detects_video_file(temp_db):
    name, _, _ = _make_video_file()
    conn = _conn(temp_db)
    try:
        vid = _seed(conn, video_file=name)
        view = inspector.build_inspect_view(conn, vid)
        assert view["has_video"] is True
    finally:
        conn.close()


def test_build_inspect_view_unknown(temp_db):
    conn = _conn(temp_db)
    try:
        assert inspector.build_inspect_view(conn, "deadbeef") is None
    finally:
        conn.close()


def test_duration_falls_back_when_video_duration_missing(temp_db):
    kf = os.path.join(config.KEYFRAMES_DIR, "kf.jpg")
    os.makedirs(config.KEYFRAMES_DIR, exist_ok=True)
    _tiny_jpeg(kf)
    conn = _conn(temp_db)
    try:
        vid = _seed(conn, kf_path=kf, duration_sec=None)
        view = inspector.build_inspect_view(conn, vid)
        assert abs(view["duration"] - 8.4) < 1e-6  # last segment/keyframe
    finally:
        conn.close()


# --- waveform ---

def test_compute_waveform_peaks_normalized(temp_db, monkeypatch):
    pcm = array.array("h", [100, -200, 30000, -32768]).tobytes()

    class _R:
        returncode = 0
        stdout = pcm

    monkeypatch.setattr(inspector.subprocess, "run", lambda *a, **k: _R())
    peaks = inspector.compute_waveform("whatever.mp4", bins=2)
    assert len(peaks) == 2
    assert abs(peaks[0] - 200 / 32768.0) < 1e-6
    assert abs(peaks[1] - 1.0) < 1e-6


def test_compute_waveform_ffmpeg_failure_returns_none(temp_db, monkeypatch):
    class _R:
        returncode = 1
        stdout = b""

    monkeypatch.setattr(inspector.subprocess, "run", lambda *a, **k: _R())
    assert inspector.compute_waveform("x.mp4", bins=8) is None


def test_waveform_payload_flat_without_file(temp_db):
    conn = _conn(temp_db)
    try:
        vid = _seed(conn)  # no video file
        payload = inspector._waveform_payload(conn, vid, bins=16)
        assert payload["bins"] == 16
        assert payload["peaks"] == [0.0] * 16
    finally:
        conn.close()


# --- rendering ---

def test_render_has_player_waveform_filmstrip(temp_db):
    name, _, _ = _make_video_file()
    kf = os.path.join(config.KEYFRAMES_DIR, "kf.jpg")
    os.makedirs(config.KEYFRAMES_DIR, exist_ok=True)
    _tiny_jpeg(kf)
    conn = _conn(temp_db)
    try:
        vid = _seed(conn, kf_path=kf, video_file=name)
        page = inspector.render_inspector(inspector.build_inspect_view(conn, vid))
        assert '<video id="player"' in page and "/api/stream/%s" % vid in page
        assert 'id="wf"' in page and 'id="wfsvg"' in page          # waveform
        assert 'class="strip"' in page and 'class="cell"' in page  # filmstrip
        assert 'class="seg"' in page and 'data-start="0.000"' in page
        assert 'id="setin"' in page and 'id="srt"' in page         # in/out + export
        assert '"reference, not authority"' in page or "reference, not authority" in page
        assert '"hasVideo": true' in page or '"hasVideo":true' in page
        assert page.count("<html") == 1 and page.count("</html>") == 1
    finally:
        conn.close()


def test_render_no_video_shows_note(temp_db):
    conn = _conn(temp_db)
    try:
        vid = _seed(conn)  # no file
        page = inspector.render_inspector(inspector.build_inspect_view(conn, vid))
        assert "video file not on disk" in page
        assert '<video id="player"' not in page
    finally:
        conn.close()


# --- server ---

def test_server_serves_inspect_waveform_stream_keyframe(temp_db):
    name, _, body = _make_video_file()
    kf = os.path.join(config.KEYFRAMES_DIR, "kf.jpg")
    os.makedirs(config.KEYFRAMES_DIR, exist_ok=True)
    _tiny_jpeg(kf)
    conn = _conn(temp_db)
    vid = _seed(conn, kf_path=kf, video_file=name)
    kf_id = db.get_keyframes(conn, vid)[0]["id"]
    conn.close()

    httpd = inspector.make_inspect_server(port=0, default_id=vid)
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        # inspect page
        page = urllib.request.urlopen(base + "/inspect/%s" % vid, timeout=5).read().decode()
        assert '<video id="player"' in page
        # root serves the default clip
        root = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        assert "reel-scout inspect" in root
        # waveform json (no ffmpeg needed: real tiny file may fail decode -> flat)
        wf = json.loads(urllib.request.urlopen(base + "/api/waveform/%s?bins=32" % vid, timeout=5).read())
        assert wf["bins"] == 32 and len(wf["peaks"]) == 32
        # keyframe image
        img = urllib.request.urlopen(base + "/keyframe/%s" % kf_id, timeout=5)
        assert img.headers["Content-Type"] == "image/jpeg"
        # unknown clip -> 404
        try:
            urllib.request.urlopen(base + "/inspect/nope", timeout=5)
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_stream_honors_range(temp_db):
    name, _, body = _make_video_file(body=b"ABCDEFGHIJ" * 500)  # 5000 bytes
    conn = _conn(temp_db)
    vid = _seed(conn, video_file=name)
    conn.close()

    httpd = inspector.make_inspect_server(port=0, default_id=vid)
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        # full request advertises range support
        full = urllib.request.urlopen(base + "/api/stream/%s" % vid, timeout=5)
        assert full.headers["Accept-Ranges"] == "bytes"
        assert len(full.read()) == len(body)
        # partial request
        req = urllib.request.Request(base + "/api/stream/%s" % vid,
                                     headers={"Range": "bytes=0-99"})
        resp = urllib.request.urlopen(req, timeout=5)
        assert resp.status == 206
        assert resp.headers["Content-Range"] == "bytes 0-99/%d" % len(body)
        chunk = resp.read()
        assert len(chunk) == 100 and chunk == body[:100]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_parse_range_edge_cases():
    assert inspector._parse_range(None, 1000) is None
    assert inspector._parse_range("bytes=0-99", 1000) == (0, 99)
    assert inspector._parse_range("bytes=100-", 1000) == (100, 999)
    assert inspector._parse_range("bytes=-100", 1000) == (900, 999)  # suffix
    assert inspector._parse_range("bytes=500-4000", 1000) == (500, 999)  # clamp end
    assert inspector._parse_range("bytes=900-100", 1000) is None  # start>end


def test_cmd_inspect_unknown_ref_exits(temp_db, capsys):
    class Args:
        video = "nomatch"
        host = "127.0.0.1"
        port = 0
        open_browser = False

    import pytest
    from reel_scout.cli import _cmd_inspect
    with pytest.raises(SystemExit):
        _cmd_inspect(Args())
    assert "No video matches" in capsys.readouterr().out


def test_back_link_only_when_there_is_an_index(temp_db):
    """`view` mode: "/" is the library, so offer a way back. `inspect <id>` pins
    "/" to this very page, so a back link there would loop to itself."""
    import sqlite3 as _sq
    import threading
    import urllib.request

    conn = _sq.connect(temp_db)
    conn.row_factory = _sq.Row
    vid = _seed(conn)
    conn.close()

    for default_id, expect_back in ((None, True), (vid, False)):
        httpd = inspector.make_inspect_server(port=0, default_id=default_id)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            page = urllib.request.urlopen(
                "http://127.0.0.1:%d/inspect/%s" % (port, vid), timeout=5).read().decode()
            assert ('class="back"' in page) is expect_back, (
                "default_id=%r should%s render a back link" % (
                    default_id, "" if expect_back else " not"))
        finally:
            httpd.shutdown()
            httpd.server_close()


# --- keyframe descriptions reach the exported page ---

def test_filmstrip_carries_what_was_seen_in_each_frame(temp_db):
    """The bundle used to render thumbnails and a score with no observations
    behind it — the evidence layer stayed in the DB and the reader had to take
    the number on faith. At L1 those descriptions ARE the analysis."""
    conn = _conn(temp_db)
    try:
        kf = os.path.join(config.KEYFRAMES_DIR, "f.jpg")
        os.makedirs(config.KEYFRAMES_DIR, exist_ok=True)
        _tiny_jpeg(kf)
        vid = _seed(conn, kf_path=kf)
        page = inspector.render_inspector(inspector.build_inspect_view(conn, vid))
        assert "close-up of fried chicken" in page
        assert 'data-desc="close-up of fried chicken"' in page
        assert 'id="kfdesc"' in page          # the caption slot the JS fills
        # The described count is shown; the label word is now a translatable span,
        # so the count and the word are adjacent rather than one literal phrase.
        assert '1 <span data-i18n="described">described</span>' in page
    finally:
        conn.close()


def test_no_descriptions_means_no_empty_caption_slot(temp_db):
    """Nothing described yet -> don't render a caption element that stays blank."""
    conn = _conn(temp_db)
    try:
        kf = os.path.join(config.KEYFRAMES_DIR, "g.jpg")
        os.makedirs(config.KEYFRAMES_DIR, exist_ok=True)
        _tiny_jpeg(kf)
        vid = db.upsert_video(conn, platform="instagram", platform_id="nodesc",
                              url="https://www.instagram.com/reel/nodesc/",
                              title="Bare", duration_sec=5.0)
        db.save_keyframes(conn, vid, [
            {"frame_index": 0, "timestamp_sec": 1.0, "file_path": kf, "strategy": "scene"}])
        conn.commit()
        page = inspector.render_inspector(inspector.build_inspect_view(conn, vid))
        assert 'id="kfdesc"' not in page
        # No described-count span in the filmstrip. (The word "described" now also
        # lives in the embedded i18n dictionary, so assert on the rendered span,
        # not the bare word.)
        assert 'data-i18n="described"' not in page
    finally:
        conn.close()


def test_zero_weight_guard_blanks_the_overall_meter():
    """Regression: the all-zero branch must clear the meter, not just message.

    Found by driving the real sliders in a browser, NOT by the JS/Python parity
    check — that only compared `computeOverall` return values, so it could not
    see that the early `return` left the previously-computed overall sitting in
    the DOM. The panel then said "all weights at zero — no verdict" while still
    displaying a verdict (8.0), which is worse than either state alone.

    Asserted at source level because there is no JS test runner here; it is a
    tripwire against the blanking being deleted, not a behavioural test.
    """
    src = inspector._SCRIPT
    guard = src[src.index("if(v==null){"):src.index("var def=isDefault();")]
    assert "oval.textContent='—'" in guard, (
        "the zero-weight branch no longer blanks the overall value — the panel "
        "will claim 'no verdict' while still showing the last computed one")
    assert "obar.style.width='0%'" in guard, (
        "the zero-weight branch no longer collapses the overall bar")


def test_i18n_chrome_translated_but_model_content_untouched(temp_db):
    """The language toggle swaps interface chrome only; model output stays put.

    Guards the boundary that makes the toggle honest — reasoning, transcript and
    decoded-structure VALUES come from the model and must never be silently
    "translated" (that would mean re-running the model), while every label a
    reader reads as chrome must carry a data-i18n key so applyLang can swap it.
    """
    conn = _conn(temp_db)
    try:
        vid = _seed(conn)  # has a scored video with reasoning + segments
        page = inspector.render_inspector(inspector.build_inspect_view(conn, vid))

        # Both dictionaries ship with the page (offline-capable toggle).
        assert '"zh"' in page and '"en"' in page
        assert "工藝評分" in page and "重新加權" in page   # zh chrome present in boot
        # The toggle control exists.
        assert 'class="langbtn" data-lang="zh"' in page

        # Chrome labels carry keys.
        for key in ("brand", "waveform", "craftScores", "reset",
                    "dim.overall", "row.Structure", "reweightSummary"):
            assert 'data-i18n="%s"' % key in page

        # Model content is NOT wrapped for translation.
        assert 'data-i18n' not in page.split('class="reasoning"')[1][:40]
        # applyLang + persistence wired.
        assert "applyLang" in page and "localStorage.setItem('rs_lang'" in page
    finally:
        conn.close()


def test_i18n_dicts_have_identical_keys():
    """en and zh must cover the same keys, or a switch leaves stale text behind."""
    assert set(inspector.I18N["en"]) == set(inspector.I18N["zh"])


# --- evidence: the header must not name a language nobody spoke ----------------
#
# Whisper reports a language even when it transcribed nothing: 22 of the 34
# silent clips in the live corpus are labelled `nn` (Norwegian Nynorsk), its
# classic output on silence. The header said `instagram / @x / nn / 0:32` for a
# clip with zero words, which reads as "we heard Norwegian", not "we heard
# nothing".

def test_empty_transcript_gets_a_note_not_a_missing_section(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        vid = _seed(conn, segments=[], text_full="")
        page = inspector.render_inspector(inspector.build_inspect_view(conn, vid))
    finally:
        conn.close()
    assert 'data-i18n="noTranscript"' in page
    assert 'data-i18n="noTranscriptNote"' in page
    assert 'class="segs"' not in page


def test_language_chip_is_hidden_when_no_words_were_transcribed(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        vid = _seed(conn, segments=[], text_full="")
        page = inspector.render_inspector(inspector.build_inspect_view(conn, vid))
    finally:
        conn.close()
    # Scope to the meta line AND match the separator: a bare "en" false-positives
    # on the `rel="noopener"` sitting in the same element.
    meta = re.search(r'<p class="meta">.*?</p>', page, re.S)
    assert meta is not None
    assert " / en" not in meta.group(0)


def test_language_chip_still_shows_when_there_are_words(temp_db):
    """Negative control for the guard above."""
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        vid = _seed(conn)
        page = inspector.render_inspector(inspector.build_inspect_view(conn, vid))
    finally:
        conn.close()
    meta = re.search(r'<p class="meta">.*?</p>', page, re.S)
    assert meta is not None and " / en" in meta.group(0)


def test_the_score_block_is_unchanged_when_the_transcript_is_empty(temp_db):
    """The owner's call was label-only: say the evidence was thin, never quietly
    withhold or discount the number. This encodes that so a later contributor
    cannot decide to be clever and grey it out."""
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        vid = _seed(conn, segments=[], text_full="", with_score=True)
        page = inspector.render_inspector(inspector.build_inspect_view(conn, vid))
    finally:
        conn.close()
    for key in ("dim.overall", "dim.hook_strength", "dim.visual_storytelling",
                "dim.pacing", "dim.structure"):
        assert 'data-i18n="%s"' % key in page
    meters = re.search(r'<div class="meters">.*?</div>\s*</section>', page, re.S)
    if meters:
        assert "noTranscriptNote" not in meters.group(0)


def test_segments_win_over_an_empty_text_full(temp_db):
    """Defensive: never render segments and a 'no transcript' note together."""
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        vid = _seed(conn, segments=[{"start": 0.0, "end": 1.0, "text": "hi"}],
                    text_full="")
        view = inspector.build_inspect_view(conn, vid)
        page = inspector.render_inspector(view)
    finally:
        conn.close()
    assert view["has_transcript"] is True
    assert 'data-i18n="noTranscriptNote"' not in page


# --- Shot grammar block (roadmap §6B/§6C on the page, 2026-09-03) -----------

def _grammar_db(sizes=(), motions=(), shots=((0, 0.0, 5.0), (1, 5.0, 10.0))):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    conn.execute("INSERT INTO videos (id, platform, platform_id, url, file_path) "
                 "VALUES ('v1', 'p', 'v1', 'u', 'f.mp4')")
    for idx, a, b in shots:
        conn.execute("INSERT INTO shots (video_id, idx, start_sec, end_sec) "
                     "VALUES ('v1', ?, ?, ?)", (idx, a, b))
    for t_sec, value in sizes:
        db.save_shot_label(conn, "v1", t_sec, "shot_size", value, "vlm", "m")
    if motions:
        db.save_shot_motion(conn, "v1", [
            {"t_sec": t, "dx": 0.0, "dy": 0.0, "speed": sp,
             "agreement": ag, "frames": 30, "movement": mv}
            for t, mv, sp, ag in motions])
    conn.commit()
    return conn, path


def test_a_size_is_matched_by_the_span_it_falls_in_not_by_equality():
    # A size hangs off its representative frame, which sits *inside* the span.
    # Matching on equality would leave almost every shot blank.
    conn, path = _grammar_db(sizes=((2.4, "MCU"), (7.9, "LS")))
    try:
        g = inspector._shot_grammar(conn, "v1")
        assert [r["size"] for r in g["rows"]] == ["MCU", "LS"]
    finally:
        conn.close(); os.unlink(path)


def test_the_analysable_ratio_is_rendered_beside_the_codes():
    # 40 of 94 labelled clips have no frame the vocabulary applies to. Codes
    # without the ratio would present those as confidently as a clean one.
    conn, path = _grammar_db(sizes=((2.4, "MCU"), (7.9, "UNKNOWN")))
    try:
        html = inspector._render_shot_grammar(
            {"shot_grammar": inspector._shot_grammar(conn, "v1")})
        assert 'data-i18n="analysable"' in html
        assert "50% (1/2)" in html
    finally:
        conn.close(); os.unlink(path)


def test_a_mostly_unanalysable_clip_is_marked_not_just_numbered():
    conn, path = _grammar_db(
        sizes=((2.4, "UNKNOWN"), (7.9, "UNKNOWN")),
        shots=((0, 0.0, 5.0), (1, 5.0, 10.0)))
    try:
        html = inspector._render_shot_grammar(
            {"shot_grammar": inspector._shot_grammar(conn, "v1")})
        assert "warn" in html
    finally:
        conn.close(); os.unlink(path)


def test_a_movement_class_carries_the_numbers_it_came_from():
    # A class with no measurement under it is the shape this project keeps
    # paying for; the page must not be where the evidence is dropped.
    conn, path = _grammar_db(motions=((0.0, "camera_moves", 3.34, 0.72),))
    try:
        html = inspector._render_shot_grammar(
            {"shot_grammar": inspector._shot_grammar(conn, "v1")})
        assert "3.3 px" in html and "72%" in html
    finally:
        conn.close(); os.unlink(path)


def test_a_clip_with_neither_size_nor_movement_renders_no_block():
    conn, path = _grammar_db()
    try:
        assert inspector._render_shot_grammar(
            inspector._shot_grammar(conn, "v1")) == ""
    finally:
        conn.close(); os.unlink(path)


def test_a_hand_supplied_size_outranks_the_model_on_the_page():
    conn, path = _grammar_db(sizes=((2.4, "MCU"),))
    try:
        db.save_shot_label(conn, "v1", 2.4, "shot_size", "CU", "supplied", None)
        g = inspector._shot_grammar(conn, "v1")
        assert g["rows"][0]["size"] == "CU"
    finally:
        conn.close(); os.unlink(path)


def test_a_hand_supplied_size_outranks_a_gated_unknown_too():
    # The test above passes on the alphabet, not on the rule: 'supplied' sorts
    # before 'vlm', so it lands first and the model row never overwrites it --
    # with or without the precedence clause. 'gate' sorts before 'supplied',
    # which is the case that actually asks the question, and the one a gate
    # pass makes ordinary: a frame the gate rejected, then corrected by hand.
    conn, path = _grammar_db()
    try:
        db.save_shot_label(conn, "v1", 2.4, "shot_size", "UNKNOWN", "gate", "m")
        db.save_shot_label(conn, "v1", 2.4, "shot_size", "CU", "supplied", None)
        assert inspector._shot_grammar(conn, "v1")["rows"][0]["size"] == "CU"
    finally:
        conn.close(); os.unlink(path)
