"""Shot-table backfill for pre-v14 clips."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from unittest.mock import patch

from reel_scout import backfill_shots, db
from reel_scout.shots import Shot, ShotMetrics


def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn, path


def _clip(conn, pid, dur=10.0, path="/tmp/x.mp4", status="analyzed"):
    vid = db.upsert_video(conn, "youtube", pid, "https://y/%s" % pid, title=pid)
    conn.execute("UPDATE videos SET status=?, duration_sec=?, file_path=? WHERE id=?",
                 (status, dur, path, vid))
    conn.commit()
    return vid


TABLE = (ShotMetrics(2, 6.0, 5.0, 10.0), [Shot(0, 0.0, 5.0, 5.0), Shot(1, 5.0, 10.0, 5.0)])


def test_candidates_are_analyzed_clips_with_a_duration_and_no_spans():
    conn, path = _temp_db()
    try:
        a = _clip(conn, "a")
        b = _clip(conn, "b")
        _clip(conn, "c", status="crawled")      # not analyzed
        _clip(conn, "d", dur=0)                 # no duration to divide by
        db.save_shots(conn, b, [Shot(0, 0.0, 10.0, 10.0)])   # already has spans
        assert [r["id"] for r in backfill_shots.candidates(conn)] == [a]
    finally:
        conn.close(); os.unlink(path)


def test_media_gone_is_skipped_not_failed():
    # Two different states: "this machine cannot answer" vs "the measurement was
    # attempted and did not work". Reporting them together makes a library look
    # broken when it is merely incomplete.
    conn, path = _temp_db()
    try:
        _clip(conn, "a", path="/definitely/not/here.mp4")
        r = backfill_shots.backfill(conn)
        assert r["skipped_no_media"] == 1 and r["failed"] == 0 and r["filled"] == 0
    finally:
        conn.close(); os.unlink(path)


def test_a_failed_measurement_writes_no_empty_partition():
    conn, path = _temp_db()
    try:
        vid = _clip(conn, "a")
        with patch("os.path.exists", return_value=True), \
             patch.object(backfill_shots, "compute_shot_table", return_value=None):
            r = backfill_shots.backfill(conn)
        assert r["failed"] == 1 and r["filled"] == 0
        assert db.get_shots(conn, vid) == []
    finally:
        conn.close(); os.unlink(path)


def test_fills_the_spans_it_measured():
    conn, path = _temp_db()
    try:
        vid = _clip(conn, "a")
        with patch("os.path.exists", return_value=True), \
             patch.object(backfill_shots, "compute_shot_table", return_value=TABLE):
            r = backfill_shots.backfill(conn)
        assert r["filled"] == 1
        assert [x["idx"] for x in db.get_shots(conn, vid)] == [0, 1]
    finally:
        conn.close(); os.unlink(path)


def test_dry_run_writes_nothing():
    conn, path = _temp_db()
    try:
        vid = _clip(conn, "a")
        with patch("os.path.exists", return_value=True), \
             patch.object(backfill_shots, "compute_shot_table") as m:
            r = backfill_shots.backfill(conn, dry_run=True)
        m.assert_not_called()          # a dry run must not even decode
        assert r["filled"] == 1
        assert db.get_shots(conn, vid) == []
    finally:
        conn.close(); os.unlink(path)


def test_limit_caps_the_work_but_candidates_reports_the_whole_backlog():
    # A run that says "2 candidates" after --limit 2 would hide the backlog.
    conn, path = _temp_db()
    try:
        for pid in ("a", "b", "c"):
            _clip(conn, pid)
        with patch("os.path.exists", return_value=True), \
             patch.object(backfill_shots, "compute_shot_table", return_value=TABLE):
            r = backfill_shots.backfill(conn, limit=2)
        assert r["candidates"] == 3 and r["filled"] == 2
    finally:
        conn.close(); os.unlink(path)


def test_rerunning_is_a_no_op_because_filled_clips_leave_the_candidate_set():
    conn, path = _temp_db()
    try:
        _clip(conn, "a")
        with patch("os.path.exists", return_value=True), \
             patch.object(backfill_shots, "compute_shot_table", return_value=TABLE):
            backfill_shots.backfill(conn)
            second = backfill_shots.backfill(conn)
        assert second["candidates"] == 0 and second["filled"] == 0
    finally:
        conn.close(); os.unlink(path)
