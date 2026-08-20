"""A video that never processed must not enter the corpus as a 0.0.

`RwyLahUuGcc` (a 4h11m livestream) held a full set of 0.0 craft scores, a
`status` of 'analyzed', and an `analyses.full_json` that was literally
``{"summary": "{\\n", "topics": [], "error": "failed to parse JSON"}``. Zero
keyframes had been extracted; the merge stage had nothing visual to read; the
scorer's own reasoning field said "impossible to score the video accurately" --
and the number stored next to that sentence was 0.0.

Nothing crashed. One row in 111 was fiction, and it averaged into every
aggregate that touched it.

The tests below come in pairs on purpose. Marking a real video invalid is worse
than leaving one bad row in place, so each "this gets caught" case is matched by
a "this must not be" case — most importantly a genuinely awful video that scores
0.0 across the board *with* frames, which the guard has to let through.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest

from reel_scout import cli, db, patterns, stats, validity


# --- fixtures --------------------------------------------------------------

def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn, path


def _video(conn, pid, uploader="Chan One", duration=60.0):
    return db.upsert_video(
        conn, platform="youtube", platform_id=pid, url="https://y/%s" % pid,
        title=pid, uploader=uploader, duration_sec=duration)


def _transcript(conn, vid, chars=500):
    db.save_transcript(conn, vid, language="zh", text_full="x" * chars,
                       segments_json="[]", whisper_model="large-v3",
                       duration_sec=60.0)


def _keyframes(conn, vid, n=8):
    db.save_keyframes(conn, vid, [
        {"frame_index": i, "timestamp_sec": float(i), "file_path": "f%d.jpg" % i,
         "strategy": "scene"} for i in range(n)])


def _analysis(conn, vid, full=None):
    full = full or {"content_type": "educational",
                    "content_structure": "listicle",
                    "style": {"format": "talking_head", "pacing": "fast"},
                    "hook": {"opening_type": "question", "cta_type": "follow"}}
    db.save_analysis(conn, vid, summary="s", topics_json="[]",
                     hooks_json=json.dumps(full.get("hook", {})),
                     style_json=json.dumps(full.get("style", {})),
                     engagement_signals_json="{}", full_json=json.dumps(full))


def _score(conn, vid, dims):
    conn.execute(
        "INSERT INTO scores (video_id, hook_strength, visual_storytelling, "
        "pacing, structure, overall) VALUES (?,?,?,?,?,?)", (vid, *dims))
    conn.commit()


# --- the predicate, in matched pairs ---------------------------------------

def test_zero_keyframes_with_a_transcript_is_invalid():
    """The exact shape of RwyLahUuGcc: speech came out, no frames did."""
    conn, path = _fresh_db()
    try:
        vid = _video(conn, "bad", duration=15035.0)
        _transcript(conn, vid, chars=55521)
        reason = validity.invalid_reason(conn, vid)
        assert reason is not None
        # The reason has to carry both measurements, or the person reading it
        # later cannot disagree with the call.
        assert "0 frames" in reason
        assert "55521" in reason
    finally:
        conn.close()
        os.unlink(path)


def test_a_normal_video_is_not_flagged():
    conn, path = _fresh_db()
    try:
        vid = _video(conn, "good")
        _transcript(conn, vid)
        _keyframes(conn, vid, n=8)
        assert validity.invalid_reason(conn, vid) is None
    finally:
        conn.close()
        os.unlink(path)


def test_a_genuinely_terrible_video_scoring_all_zeros_is_not_flagged():
    """The false positive that would actually hurt.

    Five zeros are corroboration, never the test. A real video that bottoms out
    on every dimension still extracted frames, and the guard must not touch it —
    marking real work invalid is worse than leaving one bad row in the corpus.
    """
    conn, path = _fresh_db()
    try:
        vid = _video(conn, "awful")
        _transcript(conn, vid)
        _keyframes(conn, vid, n=12)
        _score(conn, vid, (0.0, 0.0, 0.0, 0.0, 0.0))
        assert validity.invalid_reason(conn, vid) is None
    finally:
        conn.close()
        os.unlink(path)


def test_a_freshly_downloaded_video_is_not_flagged():
    """No frames and no transcript: nothing has been established either way."""
    conn, path = _fresh_db()
    try:
        vid = _video(conn, "fresh")
        assert validity.invalid_reason(conn, vid) is None
    finally:
        conn.close()
        os.unlink(path)


def test_a_silent_video_with_frames_is_not_flagged():
    """Frames but no speech is an ordinary silent clip, not a contradiction."""
    conn, path = _fresh_db()
    try:
        vid = _video(conn, "silent")
        _keyframes(conn, vid, n=6)
        assert validity.invalid_reason(conn, vid) is None
    finally:
        conn.close()
        os.unlink(path)


def test_an_empty_transcript_row_does_not_count_as_speech():
    """A transcript row that exists but holds nothing proves no decode."""
    conn, path = _fresh_db()
    try:
        vid = _video(conn, "emptytext")
        db.save_transcript(conn, vid, language="zh", text_full="",
                           segments_json="[]", whisper_model="large-v3",
                           duration_sec=60.0)
        assert validity.invalid_reason(conn, vid) is None
    finally:
        conn.close()
        os.unlink(path)


# --- marking ---------------------------------------------------------------

def test_marking_sets_the_status_and_keeps_the_reason_readable():
    conn, path = _fresh_db()
    try:
        vid = _video(conn, "bad")
        _transcript(conn, vid, chars=55521)
        validity.mark_invalid(conn, vid, validity.invalid_reason(conn, vid))
        row = db.get_video(conn, vid)
        assert row["status"] == validity.INVALID_STATUS
        assert "55521" in row["error_message"]
        assert validity.is_invalid(conn, vid)
    finally:
        conn.close()
        os.unlink(path)


def test_marking_deletes_nothing():
    """Per the no-auto-delete rule: the mark is additive and reversible."""
    conn, path = _fresh_db()
    try:
        vid = _video(conn, "bad")
        _transcript(conn, vid, chars=900)
        _analysis(conn, vid)
        _score(conn, vid, (0.0, 0.0, 0.0, 0.0, 0.0))
        validity.mark_invalid(conn, vid, "because")

        assert db.get_transcript(conn, vid) is not None
        assert db.get_analysis(conn, vid) is not None
        assert db.get_score(conn, vid) is not None
        assert db.get_video(conn, vid) is not None
    finally:
        conn.close()
        os.unlink(path)


# --- scan is an audit, not an actuator -------------------------------------

def test_scan_finds_the_shape_and_writes_nothing():
    conn, path = _fresh_db()
    try:
        bad = _video(conn, "bad", duration=15035.0)
        _transcript(conn, bad, chars=55521)
        good = _video(conn, "good")
        _transcript(conn, good)
        _keyframes(conn, good)

        hits = validity.scan(conn)
        assert [h["id"] for h in hits] == [bad]
        # Read-only: the row it just reported is still untouched.
        assert db.get_video(conn, bad)["status"] != validity.INVALID_STATUS
    finally:
        conn.close()
        os.unlink(path)


# --- the aggregates it exists to protect -----------------------------------

def test_stats_drops_the_invalid_row_and_reports_the_exclusion():
    conn, path = _fresh_db()
    try:
        a = _video(conn, "a")
        _transcript(conn, a); _keyframes(conn, a); _analysis(conn, a)
        _score(conn, a, (8.0, 8.0, 8.0, 8.0, 8.0))
        b = _video(conn, "b")
        _transcript(conn, b); _keyframes(conn, b); _analysis(conn, b)
        _score(conn, b, (6.0, 6.0, 6.0, 6.0, 6.0))

        before = stats.compute_stats(conn)
        assert before["score_aggregates"]["overall"]["count"] == 2
        assert abs(before["score_aggregates"]["overall"]["avg"] - 7.0) < 1e-9

        # Now the fake row lands, exactly as RwyLahUuGcc did.
        bad = _video(conn, "bad", duration=15035.0)
        _transcript(conn, bad, chars=55521); _analysis(conn, bad)
        _score(conn, bad, (0.0, 0.0, 0.0, 0.0, 0.0))

        polluted = stats.compute_stats(conn)
        assert polluted["score_aggregates"]["overall"]["count"] == 3
        assert polluted["score_aggregates"]["overall"]["min"] == 0.0

        validity.mark_invalid(conn, bad, validity.invalid_reason(conn, bad))

        after = stats.compute_stats(conn)
        assert after["score_aggregates"]["overall"]["count"] == 2
        assert abs(after["score_aggregates"]["overall"]["avg"] - 7.0) < 1e-9
        assert after["score_aggregates"]["overall"]["min"] == 6.0
        assert after["total_videos"] == 2
        assert after["analyzed_videos"] == 2
        # Excluded, not silently vanished.
        assert after["excluded_invalid"] == 1
        assert "1 invalid" in stats.format_stats(after)
    finally:
        conn.close()
        os.unlink(path)


def test_per_model_score_groups_also_drop_the_invalid_row():
    """The pooled block and the per-`model_used` block are two separate queries.

    Excluding the fake row from only one of them is worse than excluding it from
    neither, because the two blocks would then disagree and neither would say
    why. (This is exactly what a textual auto-merge produced when the grouping
    landed alongside this guard — the pooled query carried the exclusion and the
    grouped one silently did not.)
    """
    conn, path = _fresh_db()
    try:
        good = _video(conn, "good")
        _transcript(conn, good); _keyframes(conn, good); _analysis(conn, good)
        conn.execute(
            "INSERT INTO scores (video_id, hook_strength, visual_storytelling, "
            "pacing, structure, overall, model_used) VALUES (?,?,?,?,?,?,?)",
            (good, 8.0, 8.0, 8.0, 8.0, 8.0, "ollama"))

        bad = _video(conn, "bad", duration=15035.0)
        _transcript(conn, bad, chars=55521); _analysis(conn, bad)
        conn.execute(
            "INSERT INTO scores (video_id, hook_strength, visual_storytelling, "
            "pacing, structure, overall, model_used) VALUES (?,?,?,?,?,?,?)",
            (bad, 0.0, 0.0, 0.0, 0.0, 0.0, "ollama"))
        conn.commit()

        polluted = stats.compute_stats(conn)
        assert polluted["score_sources"]["ollama"] == 2
        assert polluted["score_aggregates_by_model"]["ollama"]["overall"]["min"] == 0.0

        validity.mark_invalid(conn, bad, validity.invalid_reason(conn, bad))

        after = stats.compute_stats(conn)
        # Census and per-model aggregate both drop it...
        assert after["score_sources"]["ollama"] == 1
        by_model = after["score_aggregates_by_model"]["ollama"]["overall"]
        assert by_model["count"] == 1
        assert by_model["min"] == 8.0
        assert by_model["avg"] == 8.0
        # ...and agree with the pooled block, which is the point.
        pooled = after["score_aggregates"]["overall"]
        assert pooled["count"] == by_model["count"]
        assert pooled["avg"] == by_model["avg"]
        assert after["mixed_score_sources"] is False
    finally:
        conn.close()
        os.unlink(path)


def test_stats_channel_scope_counts_only_that_channels_invalid_rows():
    """The scoped exclusion query binds two parameters in order (status, then
    uploader). Swapping them silently returns 0 for every channel, which reads
    as 'nothing was excluded' — the exact failure this whole change is about."""
    conn, path = _fresh_db()
    try:
        mine = _video(conn, "mine", uploader="Chan One")
        _transcript(conn, mine, chars=900)
        validity.mark_invalid(conn, mine, "because")

        theirs = _video(conn, "theirs", uploader="Chan Two")
        _transcript(conn, theirs, chars=900)
        validity.mark_invalid(conn, theirs, "because")

        assert stats.compute_stats(conn)["excluded_invalid"] == 2
        assert stats.compute_stats(conn, "Chan One")["excluded_invalid"] == 1
        assert stats.compute_stats(conn, "Chan Two")["excluded_invalid"] == 1
        assert stats.compute_stats(conn, "Nobody")["excluded_invalid"] == 0
    finally:
        conn.close()
        os.unlink(path)


def test_stats_csv_carries_the_exclusion_count():
    conn, path = _fresh_db()
    try:
        bad = _video(conn, "bad")
        _transcript(conn, bad, chars=900)
        validity.mark_invalid(conn, bad, "because")
        rows = stats.to_csv_rows(stats.compute_stats(conn))
        assert ["count", "videos", "excluded_invalid", 1] in rows
    finally:
        conn.close()
        os.unlink(path)


def test_patterns_drops_the_invalid_row_from_the_high_low_split():
    """The split is where a fake 0.0 does the most damage: it anchors the
    bottom half and makes the channel's real floor look worse than it is."""
    conn, path = _fresh_db()
    try:
        for pid, dims in (("a", (8.0,) * 5), ("b", (7.0,) * 5), ("c", (6.0,) * 5)):
            v = _video(conn, pid, uploader="Chan One")
            _transcript(conn, v); _keyframes(conn, v); _analysis(conn, v)
            _score(conn, v, dims)

        bad = _video(conn, "bad", uploader="Chan One", duration=15035.0)
        _transcript(conn, bad, chars=55521); _analysis(conn, bad)
        _score(conn, bad, (0.0, 0.0, 0.0, 0.0, 0.0))

        polluted = patterns.compute_patterns(conn, "Chan One")
        assert polluted["high_vs_low"]["scored"] == 4
        assert polluted["total_videos"] == 4
        assert polluted["high_vs_low"]["low"]["avg_overall"] == 3.0

        validity.mark_invalid(conn, bad, validity.invalid_reason(conn, bad))

        after = patterns.compute_patterns(conn, "Chan One")
        assert after["high_vs_low"]["scored"] == 3
        assert after["total_videos"] == 3
        assert after["analyzed_videos"] == 3
        assert after["high_vs_low"]["low"]["avg_overall"] == 6.0
        # The 4.2h outlier also stops dragging the average duration.
        assert after["avg_duration_sec"] == 60.0
    finally:
        conn.close()
        os.unlink(path)


# --- the audit command ------------------------------------------------------

class _Args(object):
    db_command = "check-invalid"
    apply = False

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _seed_the_real_shape(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    vid = db.upsert_video(
        conn, platform="youtube", platform_id="RwyLahUuGcc",
        url="https://y/RwyLahUuGcc",
        title="土星的轉折點，大家都辛苦了 #唐綺陽8月運勢直播",
        uploader="唐綺陽官方專屬頻道", duration_sec=15035.0)
    db.save_transcript(conn, vid, language="zh", text_full="字" * 55521,
                       segments_json="[]", whisper_model="large-v3",
                       duration_sec=15035.0)
    conn.close()
    return vid


def test_check_invalid_reports_without_writing(temp_db, capsys):
    vid = _seed_the_real_shape(temp_db)
    cli._cmd_db(_Args(apply=False))
    out = capsys.readouterr().out

    assert "1 video(s) match" in out
    assert "RwyLahUuGcc" in out
    # Content, not just exit code: a CJK title that came back as `???` or
    # mojibake would still pass an exit-code assertion, and this repo has been
    # bitten by exactly that. Assert the characters survived the round trip.
    assert "唐綺陽官方專屬頻道" in out
    assert "土星的轉折點" in out
    assert "nothing was written" in out

    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        assert db.get_video(conn, vid)["status"] != validity.INVALID_STATUS
    finally:
        conn.close()


def test_check_invalid_apply_marks_but_deletes_nothing(temp_db, capsys):
    vid = _seed_the_real_shape(temp_db)
    cli._cmd_db(_Args(apply=True))
    out = capsys.readouterr().out
    assert "Marked 1 video(s)" in out
    assert "Nothing was deleted" in out

    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        assert db.get_video(conn, vid)["status"] == validity.INVALID_STATUS
        assert db.get_transcript(conn, vid) is not None
    finally:
        conn.close()


def test_check_invalid_on_a_clean_library_says_so(temp_db, capsys):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    vid = _video(conn, "good")
    _transcript(conn, vid)
    _keyframes(conn, vid)
    conn.close()

    cli._cmd_db(_Args(apply=True))
    assert "No videos match" in capsys.readouterr().out


# --- the scorer must not re-fabricate it by a second route -----------------

def test_scorer_refuses_an_invalid_video():
    from reel_scout import scorer

    conn, path = _fresh_db()
    try:
        vid = _video(conn, "bad")
        _transcript(conn, vid, chars=55521)
        _analysis(conn, vid)
        validity.mark_invalid(conn, vid, validity.invalid_reason(conn, vid))

        with pytest.raises(ValueError) as exc:
            scorer.score_video(conn, vid)
        assert "marked invalid" in str(exc.value)
        # And it refused before writing anything.
        assert db.get_score(conn, vid) is None
    finally:
        conn.close()
        os.unlink(path)
