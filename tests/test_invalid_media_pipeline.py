"""The guard has to fire inside the pipeline, not just in a helper.

`RwyLahUuGcc` did not get its fake 0.0 from a bad predicate — nobody had a
predicate. Keyframe extraction returned nothing, and the three stages downstream
each turned that nothing into something plausible: an unparseable merge stored as
an analysis, a model refusal stored as 0.0, and `status='analyzed'` on top.

So the assertion that matters is not "the reason string is right" — it is that
the run *stops*, and that the score table is still empty afterwards. These tests
drive `_process_single` with the keyframe extractor stubbed to return nothing,
which is exactly what ffmpeg did on the 4h11m file.

Paired, as always: the same harness with a working extractor must run through
untouched, or the guard is just an outage.
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import tempfile

import pytest

from reel_scout import db, validity
from reel_scout.analyze import pipeline


def _harness(tmp_path, monkeypatch, duration=15035.0):
    """A video already downloaded and transcribed, sitting at the keyframe stage."""
    data_dir = str(tmp_path)
    monkeypatch.setattr(pipeline.config, "DATA_DIR", data_dir)
    monkeypatch.setattr(pipeline.config, "KEYFRAMES_DIR",
                        os.path.join(data_dir, "keyframes"))
    monkeypatch.setattr(pipeline.config, "VIDEOS_DIR", os.path.join(data_dir, "videos"))
    # Best-effort measurement stages are noise for this test.
    monkeypatch.setattr(pipeline.config, "SHOT_METRICS_ENABLED", False)
    monkeypatch.setattr(pipeline.config, "OCR_ENABLED", False)

    media = os.path.join(data_dir, "clip.mp4")
    with open(media, "wb") as f:
        f.write(b"\x00")

    db_path = os.path.join(data_dir, "t.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)

    url = "https://www.youtube.com/watch?v=RwyLahUuGcc"
    vid = db.upsert_video(
        conn, platform="youtube", platform_id="RwyLahUuGcc", url=url,
        title="a 4h livestream", uploader="Chan One", duration_sec=duration,
        file_path=media, file_size_bytes=1)
    db.save_transcript(conn, vid, language="zh", text_full="x" * 55521,
                       segments_json="[]", whisper_model="large-v3",
                       duration_sec=duration)
    return conn, url, vid


def _options(**kw):
    opts = pipeline.PipelineOptions(skip_transcribe=True, skip_vision=True)
    for k, v in kw.items():
        setattr(opts, k, v)
    return opts


# --- the bad case ----------------------------------------------------------

def test_extraction_producing_no_frames_marks_the_video_invalid(
        tmp_path, monkeypatch, capsys):
    conn, url, vid = _harness(tmp_path, monkeypatch)
    monkeypatch.setattr(pipeline, "extract_keyframes",
                        lambda *a, **k: [])          # what ffmpeg did on the 4h file
    # If either of these is reached the guard failed to stop the run.
    monkeypatch.setattr(pipeline, "merge_analysis",
                        lambda *a, **k: pytest.fail("merged an empty visual layer"))

    try:
        with pytest.raises(validity.InvalidMediaError) as exc:
            pipeline._process_single(conn, url, _options(score=True))

        assert exc.value.video_id == vid
        row = db.get_video(conn, vid)
        assert row["status"] == validity.INVALID_STATUS
        assert "55521" in row["error_message"]

        # The whole point: no fabricated row on either table.
        assert db.get_score(conn, vid) is None
        assert db.get_analysis(conn, vid) is None

        err = capsys.readouterr().err
        assert "INVALID" in err
        assert "Nothing was deleted" in err
    finally:
        conn.close()


def test_the_transcript_and_media_survive_the_marking(tmp_path, monkeypatch):
    """No-auto-delete: the guard marks, it never reclaims."""
    conn, url, vid = _harness(tmp_path, monkeypatch)
    monkeypatch.setattr(pipeline, "extract_keyframes", lambda *a, **k: [])
    try:
        with pytest.raises(validity.InvalidMediaError):
            pipeline._process_single(conn, url, _options())
        assert db.get_transcript(conn, vid) is not None
        assert os.path.exists(os.path.join(str(tmp_path), "clip.mp4"))
    finally:
        conn.close()


def test_an_invalid_item_counts_as_a_failed_batch_item(tmp_path, monkeypatch):
    """It must not report success. A run that produced no analysis and exits 0
    is the same defect `test_analyze_honesty` was written about."""
    conn, url, vid = _harness(tmp_path, monkeypatch)
    monkeypatch.setattr(pipeline, "extract_keyframes", lambda *a, **k: [])
    monkeypatch.setattr(pipeline.config, "DB_PATH", os.path.join(str(tmp_path), "t.db"))
    conn.close()

    errors = pipeline.run([url], _options())
    assert errors == 1


# --- the paired good case --------------------------------------------------

def test_a_video_whose_frames_extract_is_untouched(tmp_path, monkeypatch):
    conn, url, vid = _harness(tmp_path, monkeypatch, duration=60.0)

    frames = [
        type("KF", (), {"frame_index": i, "timestamp_sec": float(i),
                        "file_path": "f%d.jpg" % i, "strategy": "scene"})()
        for i in range(6)
    ]
    monkeypatch.setattr(pipeline, "extract_keyframes", lambda *a, **k: frames)
    # Pre-seed the analysis so merge is skipped without reaching an LLM.
    db.save_analysis(conn, vid, summary="s", topics_json="[]", hooks_json="{}",
                     style_json="{}", engagement_signals_json="{}",
                     full_json=json.dumps({"content_type": "educational"}))

    try:
        out = pipeline._process_single(conn, url, _options())
        assert out == vid
        row = db.get_video(conn, vid)
        assert row["status"] != validity.INVALID_STATUS
        assert validity.invalid_reason(conn, vid) is None
        assert len(db.get_keyframes(conn, vid)) == 6
    finally:
        conn.close()


# --- structural: the gate must stay upstream of the fabricating stages -----

def _fn(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("no function %s" % name)


def test_the_gate_runs_before_merge_and_score():
    """Ordering is the whole mechanism. If the check drifts below the merge or
    score call it stops preventing anything — the fake row is already written."""
    src = open(pipeline.__file__, encoding="utf-8").read()
    proc = _fn(ast.parse(src), "_process_single")

    def first_line(pred):
        return min((n.lineno for n in ast.walk(proc) if pred(n)), default=None)

    gate = first_line(
        lambda n: isinstance(n, ast.Call)
        and getattr(n.func, "attr", None) == "invalid_reason")
    merge = first_line(
        lambda n: isinstance(n, ast.Call)
        and getattr(n.func, "id", None) == "merge_analysis")
    score = first_line(
        lambda n: isinstance(n, ast.Call)
        and getattr(n.func, "id", None) == "score_video")

    assert gate is not None, "the validity gate is gone from _process_single"
    assert merge is not None and gate < merge, (
        "validity gate (line %s) must precede merge_analysis (line %s)" % (gate, merge))
    assert score is not None and gate < score, (
        "validity gate (line %s) must precede score_video (line %s)" % (gate, score))
