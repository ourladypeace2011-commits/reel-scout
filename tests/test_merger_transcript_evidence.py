"""The merge prompt must say when nothing was transcribed.

`analyze` produces the blob the scorer later reads, so this is the one place
where "we pretend we heard words" is a behaviour rather than a rendering. A
`transcripts` row whose `text_full` is empty used to hand the prompt an empty
string, giving the model a "## Transcript" heading with nothing under it --
indistinguishable from a section that failed to render, and the reason a clip
whose only text is a title card can have its opening classified as spoken.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

from reel_scout import db
from reel_scout.analyze import merger

_LLM_REPLY = json.dumps({"summary": "s", "topics": [], "style": {}})


def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn, path


def _merge_and_capture_prompt(conn, vid):
    """Run merge_analysis against a mock LLM and return the prompt it received."""
    mock_llm = MagicMock()
    mock_llm.complete.return_value = _LLM_REPLY
    with patch("reel_scout.analyze.merger.get_llm", return_value=mock_llm):
        merger.merge_analysis(conn, vid)
    return mock_llm.complete.call_args[0][0]


def _seed(conn, platform_id, *, text_full=None, segments_json="[]"):
    vid = db.upsert_video(
        conn, platform="youtube", platform_id=platform_id,
        url="https://youtube.com/shorts/%s" % platform_id,
        title="T", duration_sec=12.0,
    )
    if text_full is not None:
        db.save_transcript(conn, vid, language="en", text_full=text_full,
                           segments_json=segments_json, whisper_model="x",
                           duration_sec=12.0)
    return vid


def test_an_empty_transcript_row_reads_as_no_transcript_in_the_prompt():
    conn, path = _temp_db()
    try:
        vid = _seed(conn, "empty_row", text_full="")
        prompt = _merge_and_capture_prompt(conn, vid)
    finally:
        conn.close()
        os.unlink(path)
    assert "(no transcript)" in prompt


def test_a_whitespace_only_transcript_also_reads_as_no_transcript():
    conn, path = _temp_db()
    try:
        vid = _seed(conn, "ws_row", text_full="  \n\t ")
        prompt = _merge_and_capture_prompt(conn, vid)
    finally:
        conn.close()
        os.unlink(path)
    assert "(no transcript)" in prompt


def test_a_missing_transcript_row_still_reads_as_no_transcript():
    """The half that already worked -- pinned so the fix cannot break it."""
    conn, path = _temp_db()
    try:
        vid = _seed(conn, "no_row")
        prompt = _merge_and_capture_prompt(conn, vid)
    finally:
        conn.close()
        os.unlink(path)
    assert "(no transcript)" in prompt


def test_real_words_are_passed_through_untouched():
    """Negative control: the marker must not displace an actual transcript."""
    conn, path = _temp_db()
    try:
        vid = _seed(conn, "has_words", text_full="Hungry? Come try our chicken.")
        prompt = _merge_and_capture_prompt(conn, vid)
    finally:
        conn.close()
        os.unlink(path)
    assert "Hungry? Come try our chicken." in prompt
    assert "(no transcript)" not in prompt


def test_speaker_labels_still_win_when_there_are_words():
    """The diarized path sits inside the branch this change guards -- prove the
    guard did not amputate it."""
    conn, path = _temp_db()
    segs = json.dumps([{"start": 0.0, "end": 1.0, "text": "hi", "speaker": "S1"},
                       {"start": 1.0, "end": 2.0, "text": "yes", "speaker": "S2"}])
    try:
        vid = _seed(conn, "diarized", text_full="hi yes", segments_json=segs)
        prompt = _merge_and_capture_prompt(conn, vid)
    finally:
        conn.close()
        os.unlink(path)
    assert "[S1] hi" in prompt and "[S2] yes" in prompt


# --- the schema example must never be stored as if it were analysis -----------
#
# 94 of the first 96 clips analyzed carried the prompt's own worked example in
# `timeline` -- 75 of them verbatim and entire. The model adapted the
# timestamps but not the prose, because unlike every other field in that block
# the example read like a plausible value rather than a specification.
#
# It is not a cosmetic defect: `scorer.py` names the timeline as evidence for
# BOTH `hook_strength` and `structure`, so a constant was being scored as if it
# were each clip's narrative arc, and the viewer rendered it to the reader.

def test_the_prompt_no_longer_ships_a_copyable_timeline_example():
    """The schema block must specify the shape, not demonstrate a plausible value."""
    from reel_scout.analyze.merger import _MERGE_PROMPT_TEMPLATE as tpl
    for echoed in ("hook/opening description", "main content description",
                   "CTA or closing"):
        assert echoed not in tpl, "%r is still copyable out of the prompt" % echoed
    assert "<what actually happens" in tpl


def test_echoed_template_entries_are_dropped_not_stored():
    from reel_scout.analyze.merger import strip_template_timeline
    kept, dropped = strip_template_timeline([
        {"timestamp": "0-3s", "event": "hook/opening description"},
        {"timestamp": "3-15s", "event": "main content description"},
        {"timestamp": "15-20s", "event": "CTA or closing"},
    ])
    assert kept == [] and dropped == 3


def test_real_events_survive_the_guard():
    """Negative control -- the guard must not eat genuine analysis."""
    from reel_scout.analyze.merger import strip_template_timeline
    real = [{"timestamp": "0-4s", "event": "close-up of the shutter, no speech"},
            {"timestamp": "4-20s", "event": "walks through the three settings"}]
    kept, dropped = strip_template_timeline(list(real))
    assert kept == real and dropped == 0


def test_the_guard_matches_case_and_whitespace_insensitively():
    from reel_scout.analyze.merger import strip_template_timeline
    kept, dropped = strip_template_timeline([
        {"timestamp": "0-3s", "event": "  Hook/Opening Description  "},
    ])
    assert kept == [] and dropped == 1


def test_a_malformed_timeline_degrades_instead_of_raising():
    from reel_scout.analyze.merger import strip_template_timeline
    assert strip_template_timeline(None) == ([], 0)
    assert strip_template_timeline("not a list") == ([], 0)


def test_merge_stores_no_template_timeline_even_if_the_model_echoes_it():
    """End-to-end: the guard sits between the model and the database."""
    conn, path = _temp_db()
    try:
        vid = _seed(conn, "echoer", text_full="real words here")
        mock_llm = MagicMock()
        mock_llm.complete.return_value = json.dumps({
            "summary": "s", "topics": [],
            "timeline": [{"timestamp": "0-3s", "event": "hook/opening description"},
                         {"timestamp": "3-9s", "event": "she demonstrates focus pulling"}],
        })
        with patch("reel_scout.analyze.merger.get_llm", return_value=mock_llm):
            merger.merge_analysis(conn, vid)
        stored = json.loads(db.get_analysis(conn, vid)["full_json"])["timeline"]
    finally:
        conn.close()
        os.unlink(path)
    assert len(stored) == 1
    assert stored[0]["event"] == "she demonstrates focus pulling"
