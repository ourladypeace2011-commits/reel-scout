"""Corpus health surface (roadmap §Phase 7A)."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from unittest.mock import patch

from reel_scout import config, db, health, validity
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


def test_relative_paths_are_reported_but_never_actionable():
    # `utils.paths.resolve_media_path` reads both forms from any cwd, so a mixed
    # library is untidy rather than broken. Four modules reported a healthy
    # library as broken in one day by calling `os.path.exists` on the stored
    # string instead — the gap was in the readers, and a dashboard pointing at
    # the data would have sent the next person to rewrite 2,986 rows for nothing.
    conn, path = _temp_db()
    try:
        _clip(conn, "a", path="./data/videos/a.mp4")
        r = _rows(conn)["paths"]
        assert not r["actionable"]
        assert "resolve both" in r["detail"]
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
        _clip(conn, "a", path="/gone.mp4")   # media gone — never actionable
        with patch.object(validity, "scan", return_value=[{"id": _clip(conn, "b")}]):
            h = health.collect(conn)
            text = health.format_report(h, health.findings(h))
        assert "1 gap(s)" in text
    finally:
        conn.close(); os.unlink(path)


# --- the resolver, tested where it actually differs -------------------------
#
# Every test above uses absolute paths or patches `os.path.exists`, and in that
# world the resolver and the raw call agree — which is why five mutations that
# reverted the resolver were all MISSED on the first pass. The difference only
# shows with a *relative* stored path read from a *different* working
# directory, which is exactly the situation four modules got wrong in one day.

def _data_root_clip(conn, tmp_root, pid="rel"):
    """A clip whose media really exists, stored the legacy `./data/...` way."""
    videos = os.path.join(tmp_root, "data", "videos")
    os.makedirs(videos, exist_ok=True)
    real = os.path.join(videos, "%s.mp4" % pid)
    with open(real, "wb") as f:
        f.write(b"0")
    vid = db.upsert_video(conn, "youtube", pid, "https://y/%s" % pid, title=pid)
    conn.execute(
        "UPDATE videos SET status='analyzed', duration_sec=10.0, file_path=? WHERE id=?",
        ("./data/videos/%s.mp4" % pid, vid))
    conn.commit()
    return vid


def test_a_relative_path_resolves_from_an_unrelated_cwd(monkeypatch, tmp_path):
    # The whole point: `os.path.exists("./data/videos/x.mp4")` is False from
    # anywhere but the checkout that wrote it, and the library is full of them.
    conn, path = _temp_db()
    root = str(tmp_path)
    try:
        _data_root_clip(conn, root)
        monkeypatch.setattr(config, "DATA_DIR", os.path.join(root, "data"))
        elsewhere = tmp_path / "somewhere-else"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        assert not os.path.exists("./data/videos/rel.mp4"), "cwd must not resolve it"
        h = health.collect(conn)
        assert h["missing_media"] == 0, "the resolver has to find it"
        assert h["measurable"] == 1, "and it has to count as measurable"
    finally:
        conn.close(); os.unlink(path)


def test_paths_row_is_not_actionable_even_when_every_path_is_relative():
    conn, path = _temp_db()
    try:
        _clip(conn, "a", path="./data/videos/a.mp4")
        _clip(conn, "b", path="./data/videos/b.mp4")
        rows = health.findings(health.collect(conn))
        paths_row = {r["label"]: r for r in rows}["paths"]
        assert paths_row["actionable"] is False
        # And it must not inflate the "waiting on a command" count either.
        assert sum(1 for r in rows if r["actionable"]) == 0
    finally:
        conn.close(); os.unlink(path)

def test_health_flags_labels_made_by_a_superseded_prompt():
    # The prompt moved the answer ten-to-one on the same images, so a mixed
    # column is two different measurements sharing a name.
    from reel_scout.shot_size import prompt_fingerprint
    conn, path = _temp_db()
    try:
        vid = _clip(conn, "a")
        db.save_shot_label(conn, vid, 1.0, "shot_size", "ECU", "vlm", "m", "OLD")
        db.save_shot_label(conn, vid, 2.0, "shot_size", "MCU", "vlm", "m",
                           prompt_fingerprint())
        r = _rows(conn)["shot-size prompt"]
        assert r["actionable"] and "1 label" in r["detail"]
    finally:
        conn.close(); os.unlink(path)


def test_health_is_quiet_when_every_label_is_from_the_current_prompt():
    from reel_scout.shot_size import prompt_fingerprint
    conn, path = _temp_db()
    try:
        vid = _clip(conn, "a")
        db.save_shot_label(conn, vid, 1.0, "shot_size", "MCU", "vlm", "m",
                           prompt_fingerprint())
        assert _rows(conn)["shot-size prompt"]["actionable"] is False
    finally:
        conn.close(); os.unlink(path)


# --- a prescription that cannot work is worse than none (2026-09-05) ---------

def _findings_with(**overrides):
    """Real snapshot, then the one or two numbers this test is about."""
    conn, path = _temp_db()
    try:
        h = dict(health.collect(conn), **overrides)
        return health.findings(h)
    finally:
        conn.close(); os.unlink(path)


def test_the_stale_prompt_fix_names_force_because_interviews_are_skipped():
    # `shot-size <video>` returns having done nothing on a talking_head clip,
    # so the row stayed red forever while telling the operator to run a command
    # that could not clear it. Interviews are also the clips most likely to be
    # holding old labels, which made this a no-op in practice, not in theory.
    row = [r for r in _findings_with(labels_from_an_old_prompt=3)
           if r["label"] == "shot-size prompt"]
    assert row, "the shot-size prompt finding should be present"
    assert "--force" in (row[0]["fix"] or ""), row[0]["fix"]


def test_an_orphaned_label_is_reported_with_no_remedy():
    # There is nothing to run: the frame moved, so the reading can neither be
    # refreshed nor reached. Offering a command would be the same lie the row
    # above was telling.
    row = [r for r in _findings_with(orphaned_shot_labels=2)
           if r["label"] == "shot-size frames"]
    assert row, "the orphan finding should be present"
    assert row[0]["fix"] is None and row[0]["actionable"] is False
    assert row[0]["ok"] is False
