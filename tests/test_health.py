"""Corpus health surface (roadmap §Phase 7A)."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from unittest.mock import patch

from reel_scout import db, health, validity
from reel_scout.shots import Shot


def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn, path


def _clip(conn, pid, status="analyzed", dur=10.0, path="/tmp/x.mp4"):
    vid = db.upsert_video(conn, "youtube", pid, "https://y/%s" % pid, title=pid)
    conn.execute("UPDATE videos SET status=?, duration_sec=?, file_path=? WHERE id=?",
                 (status, dur, path, vid))
    conn.commit()
    return vid


def _rows(conn):
    return {r["label"]: r for r in health.findings(health.collect(conn))}


def test_a_marked_invalid_clip_is_not_reported_as_an_open_gap():
    # Measured on the first real run: `validity.scan` returns everything
    # matching the shape, marked or not, and reporting its raw length showed the
    # clip we had already dealt with as outstanding. A dashboard that shows a
    # gap which is not there trains people to stop reading it.
    conn, path = _temp_db()
    try:
        vid = _clip(conn, "a", status=validity.INVALID_STATUS)
        with patch.object(validity, "scan", return_value=[{"id": vid}]):
            h = health.collect(conn)
        assert h["invalid_marked"] == 1
        assert h["invalid_unmarked"] == 0
    finally:
        conn.close(); os.unlink(path)


def test_an_unmarked_match_is_reported_with_its_remedy():
    conn, path = _temp_db()
    try:
        vid = _clip(conn, "a")
        with patch.object(validity, "scan", return_value=[{"id": vid}]):
            r = _rows(conn)["invalid rows"]
        assert r["actionable"] and "check-invalid" in r["fix"]
    finally:
        conn.close(); os.unlink(path)


def test_media_gone_is_flagged_but_never_actionable():
    # Nothing anyone runs here brings the file back; filing it next to work that
    # can be done makes a finishable list look hopeless.
    conn, path = _temp_db()
    try:
        _clip(conn, "a", path="/definitely/not/here.mp4")
        r = _rows(conn)["media"]
        assert not r["ok"] and not r["actionable"]
    finally:
        conn.close(); os.unlink(path)


def test_a_clip_whose_media_is_gone_is_not_a_shot_table_gap():
    # "measurable" has to mean measurable *here*.
    conn, path = _temp_db()
    try:
        _clip(conn, "a", path="/definitely/not/here.mp4")
        h = health.collect(conn)
        assert h["measurable"] == 0
        assert not _rows(conn)["shot table"]["actionable"]
    finally:
        conn.close(); os.unlink(path)


def test_a_measurable_clip_without_spans_is_actionable():
    conn, path = _temp_db()
    try:
        _clip(conn, "a")
        with patch("os.path.exists", return_value=True):
            r = _rows(conn)["shot table"]
        assert r["actionable"] and "backfill-shots" in r["fix"]
    finally:
        conn.close(); os.unlink(path)


def test_a_filled_shot_table_closes_the_gap():
    conn, path = _temp_db()
    try:
        vid = _clip(conn, "a")
        db.save_shots(conn, vid, [Shot(0, 0.0, 10.0, 10.0)])
        with patch("os.path.exists", return_value=True):
            r = _rows(conn)["shot table"]
        assert r["ok"] and not r["actionable"]
    finally:
        conn.close(); os.unlink(path)


def test_relative_paths_are_actionable_and_name_the_command():
    conn, path = _temp_db()
    try:
        _clip(conn, "a", path="./data/videos/a.mp4")
        r = _rows(conn)["paths"]
        assert r["actionable"] and "normalize-paths" in r["fix"]
    finally:
        conn.close(); os.unlink(path)


def test_invalid_clips_are_excluded_from_every_count():
    # Same exclusion `stats` applies. Otherwise a clip that was correctly
    # retired keeps showing up as missing spans, missing evidence and a
    # relative path forever.
    conn, path = _temp_db()
    try:
        _clip(conn, "bad", status=validity.INVALID_STATUS, path="./data/x.mp4")
        h = health.collect(conn)
        assert h["videos"] == 0
        assert h["relative_video_paths"] == 0
        assert h["measurable"] == 0
    finally:
        conn.close(); os.unlink(path)


def test_every_gap_row_carries_a_remedy():
    # A number with no next action tells the reader something is wrong and
    # leaves them to guess what to do about it.
    conn, path = _temp_db()
    try:
        _clip(conn, "a", path="./data/videos/a.mp4")
        for r in health.findings(health.collect(conn)):
            if r["actionable"]:
                assert r["fix"], "%s is actionable with no remedy named" % r["label"]
    finally:
        conn.close(); os.unlink(path)


def test_report_says_when_nothing_is_waiting():
    conn, path = _temp_db()
    try:
        h = health.collect(conn)
        text = health.format_report(h, health.findings(h))
        assert "Nothing here is waiting" in text
    finally:
        conn.close(); os.unlink(path)


def test_report_counts_only_the_actionable_gaps():
    conn, path = _temp_db()
    try:
        _clip(conn, "a", path="/gone.mp4")          # not actionable
        _clip(conn, "b", path="./data/videos/b.mp4")  # actionable (relative)
        h = health.collect(conn)
        text = health.format_report(h, health.findings(h))
        assert "1 gap(s)" in text
    finally:
        conn.close(); os.unlink(path)
