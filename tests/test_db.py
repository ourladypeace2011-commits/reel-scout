from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

import pytest

from reel_scout import db


def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn, path


def test_init_db():
    conn, path = _temp_db()
    cur = conn.execute("SELECT version FROM schema_version")
    assert cur.fetchone()[0] == db.SCHEMA_VERSION
    conn.close()
    os.unlink(path)


def test_shot_metrics_table_and_roundtrip():
    conn, path = _temp_db()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "shot_metrics" in tables
    vid = db.upsert_video(
        conn, platform="youtube", platform_id="sm_rt",
        url="https://youtube.com/shorts/sm_rt",
    )
    db.save_shot_metrics(
        conn, vid, shot_count=10, cuts_per_minute=18.0,
        avg_shot_sec=3.0, audio_bpm=120.0, audio_energy=0.25,
    )
    row = db.get_shot_metrics(conn, vid)
    assert row["shot_count"] == 10
    assert row["cuts_per_minute"] == 18.0
    assert row["audio_bpm"] == 120.0
    assert row["audio_energy"] == 0.25
    # Replaceable (re-run overwrites, not duplicates).
    db.save_shot_metrics(conn, vid, shot_count=5)
    assert db.get_shot_metrics(conn, vid)["shot_count"] == 5
    conn.close()
    os.unlink(path)


def test_migrate_v6_to_v7_on_legacy_db():
    """A DB stamped at v6 without shot_metrics should migrate to v7 and gain the
    table (exercises the migration chain, not just the fresh-install path)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    # Simulate a pre-v7 DB.
    conn.execute("DROP TABLE IF EXISTS shot_metrics")
    conn.execute("UPDATE schema_version SET version = 6")
    conn.commit()

    db.init_db(conn)  # runs the chain from v6 up to the current schema

    assert conn.execute(
        "SELECT version FROM schema_version").fetchone()[0] == db.SCHEMA_VERSION
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "shot_metrics" in tables
    conn.close()
    os.unlink(path)


def test_ocr_captions_roundtrip_and_replace():
    conn, path = _temp_db()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ocr_captions" in tables
    vid = db.upsert_video(
        conn, platform="youtube", platform_id="ocr_rt",
        url="https://youtube.com/shorts/ocr_rt",
    )
    db.save_ocr_captions(conn, vid, [
        {"timestamp_sec": 2.0, "text": "B", "engine": "vlm"},
        {"timestamp_sec": 1.0, "text": "A", "engine": "vlm"},
    ])
    rows = db.get_ocr_captions(conn, vid)
    assert [r["text"] for r in rows] == ["A", "B"]  # ordered by timestamp
    # Re-save replaces (idempotent), not appends.
    db.save_ocr_captions(conn, vid, [
        {"timestamp_sec": 0.5, "text": "C", "engine": "tesseract"},
    ])
    rows = db.get_ocr_captions(conn, vid)
    assert len(rows) == 1
    assert rows[0]["text"] == "C"
    assert rows[0]["engine"] == "tesseract"
    conn.close()
    os.unlink(path)


def test_migrate_v7_to_v8_adds_ocr_captions():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    conn.execute("DROP TABLE IF EXISTS ocr_captions")
    conn.execute("UPDATE schema_version SET version = 7")
    conn.commit()

    db.init_db(conn)  # should run _migrate_v7_to_v8

    assert conn.execute(
        "SELECT version FROM schema_version").fetchone()[0] == db.SCHEMA_VERSION
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ocr_captions" in tables
    conn.close()
    os.unlink(path)


def test_upsert_video():
    conn, path = _temp_db()
    vid = db.upsert_video(
        conn, platform="youtube", platform_id="abc123",
        url="https://youtube.com/shorts/abc123",
        title="Test Video", duration_sec=30.0,
    )
    assert len(vid) == 16
    video = db.get_video(conn, vid)
    assert video["title"] == "Test Video"
    assert video["platform"] == "youtube"

    # Upsert same video — should update
    db.upsert_video(
        conn, platform="youtube", platform_id="abc123",
        url="https://youtube.com/shorts/abc123",
        title="Updated Title",
    )
    video = db.get_video(conn, vid)
    assert video["title"] == "Updated Title"

    conn.close()
    os.unlink(path)


def test_batch_lifecycle():
    conn, path = _temp_db()
    urls = ["https://example.com/1", "https://example.com/2"]
    batch_id = db.create_batch(conn, urls)
    pending = db.get_pending_batch_items(conn, batch_id)
    assert len(pending) == 2

    db.update_batch_item(conn, batch_id, urls[0], "done", video_id="v1")
    db.update_batch_item(conn, batch_id, urls[1], "error", error="fail")

    pending = db.get_pending_batch_items(conn, batch_id)
    assert len(pending) == 0

    conn.close()
    os.unlink(path)


def test_list_videos():
    conn, path = _temp_db()
    db.upsert_video(conn, "youtube", "a1", "https://yt/a1", title="YT 1")
    db.upsert_video(conn, "instagram", "b1", "https://ig/b1", title="IG 1")

    all_vids = db.list_videos(conn)
    assert len(all_vids) == 2

    yt_only = db.list_videos(conn, platform="youtube")
    assert len(yt_only) == 1
    assert yt_only[0]["platform"] == "youtube"

    conn.close()
    os.unlink(path)


# --- v10: the columns a background batch runner needs -------------------------

def _v9_database(path):
    """A v9 batches/batch_items pair with a row in each, as it shipped."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        CREATE TABLE videos (id TEXT PRIMARY KEY);
        CREATE TABLE batches (
            id TEXT PRIMARY KEY, source TEXT, total_urls INTEGER,
            completed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE batch_items (
            batch_id TEXT, url TEXT, video_id TEXT,
            status TEXT DEFAULT 'pending', error_message TEXT,
            PRIMARY KEY (batch_id, url));
        INSERT INTO schema_version VALUES (9);
        INSERT INTO batches (id, source, total_urls) VALUES ('b1', 'cli', 2);
        INSERT INTO batch_items (batch_id, url, status)
            VALUES ('b1', 'https://x/1', 'done');
    """)
    conn.commit()
    conn.close()


def test_a_v9_database_migrates_without_losing_batches(tmp_path):
    path = str(tmp_path / "v9.db")
    _v9_database(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db._migrate_v9_to_v10(conn)

    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 10
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM batch_items").fetchone()[0] == "done"
    cols = {r[1] for r in conn.execute("PRAGMA table_info(batches)")}
    assert {"mode", "out_root", "pid", "heartbeat_at", "cancel_requested"} <= cols
    item_cols = {r[1] for r in conn.execute("PRAGMA table_info(batch_items)")}
    assert {"label", "slug", "bundle_dir"} <= item_cols
    conn.close()


def test_the_migration_is_idempotent(tmp_path):
    """init_db runs the ladder on every connection, so a half-applied ALTER must
    not become a hard failure on the next open."""
    path = str(tmp_path / "v9.db")
    _v9_database(path)
    conn = sqlite3.connect(path)
    db._migrate_v9_to_v10(conn)
    conn.execute("UPDATE schema_version SET version = 9")
    conn.commit()
    db._migrate_v9_to_v10(conn)  # must not raise
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 10
    conn.close()


def test_a_fresh_database_has_the_same_columns_as_a_migrated_one(tmp_path, monkeypatch):
    """_SCHEMA_SQL and the migration are two independent definitions of the same
    tables; they drift the moment only one is edited."""
    fresh = str(tmp_path / "fresh.db")
    conn = sqlite3.connect(fresh)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    fresh_cols = {r[1] for r in conn.execute("PRAGMA table_info(batches)")}
    fresh_items = {r[1] for r in conn.execute("PRAGMA table_info(batch_items)")}
    conn.close()

    migrated = str(tmp_path / "old.db")
    _v9_database(migrated)
    conn = sqlite3.connect(migrated)
    db._migrate_v9_to_v10(conn)
    old_cols = {r[1] for r in conn.execute("PRAGMA table_info(batches)")}
    old_items = {r[1] for r in conn.execute("PRAGMA table_info(batch_items)")}
    conn.close()

    assert fresh_cols == old_cols
    assert fresh_items == old_items


# --- the counter bug this split exists to avoid -------------------------------

def test_item_progress_does_not_touch_the_batch_counters(temp_db):
    """update_batch_item increments completed/failed on every call. Routing an
    item's intermediate states through it would count one video several times,
    so progress writes go through a separate function and the counting call
    happens exactly once, at the terminal transition."""
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        batch_id = db.create_batch(conn, ["https://x/1"], source="mcp-batch")

        for status in ("analyzing", "analyzing", "needs_vision", "exporting", "exporting"):
            db.set_batch_item_progress(conn, batch_id, "https://x/1", status=status)
        assert conn.execute(
            "SELECT completed FROM batches WHERE id = ?", (batch_id,)).fetchone()[0] == 0

        db.update_batch_item(conn, batch_id, "https://x/1", "done", video_id="v1")
        assert conn.execute(
            "SELECT completed FROM batches WHERE id = ?", (batch_id,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_item_progress_records_the_fields_status_needs(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        batch_id = db.create_batch(conn, ["https://x/1"], source="mcp-batch")
        db.set_batch_item_progress(
            conn, batch_id, "https://x/1",
            label="Amy Wu", slug="amy-wu", bundle_dir="/out/amy-wu", status="done")
        row = db.get_batch_items(conn, batch_id)[0]
        assert (row["label"], row["slug"], row["bundle_dir"]) == (
            "Amy Wu", "amy-wu", "/out/amy-wu")
    finally:
        conn.close()


# --- liveness and cancellation -------------------------------------------------

def test_heartbeat_is_what_separates_running_from_abandoned(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        batch_id = db.create_batch(conn, ["https://x/1"], source="mcp-batch")
        assert db.get_batch(conn, batch_id)["heartbeat_at"] is None
        db.touch_batch_heartbeat(conn, batch_id)
        assert db.get_batch(conn, batch_id)["heartbeat_at"] is not None
    finally:
        conn.close()


def test_cancel_is_a_flag_the_worker_polls(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        batch_id = db.create_batch(conn, ["https://x/1"], source="mcp-batch")
        assert db.batch_cancel_requested(conn, batch_id) is False
        db.request_batch_cancel(conn, batch_id)
        assert db.batch_cancel_requested(conn, batch_id) is True
    finally:
        conn.close()


def test_set_batch_meta_records_what_the_job_was_told_to_do(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        batch_id = db.create_batch(conn, ["https://x/1"], source="mcp-batch")
        db.set_batch_meta(conn, batch_id, mode="agent", out_root="/out", pid=4242)
        row = db.get_batch(conn, batch_id)
        assert (row["mode"], row["out_root"], row["pid"]) == ("agent", "/out", 4242)
    finally:
        conn.close()


def test_latest_batch_can_be_found_by_source(temp_db):
    """A caller that lost the batch_id across a long conversation is exactly the
    situation a background runner creates."""
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        db.create_batch(conn, ["https://x/1"], source="cli")
        mine = db.create_batch(conn, ["https://x/2"], source="mcp-batch")
        assert db.get_latest_batch(conn, source="mcp-batch")["id"] == mine
    finally:
        conn.close()


# --- "brand new empty DB" must be audible (2026-08-15) --------------------
#
# REEL_SCOUT_DATA defaults to "./data", relative to wherever the process starts.
# That default is correct for a student running inside their own project. What
# was wrong is that opening a database that does not exist looked exactly like
# opening one that does: sqlite makes the file, every query returns nothing, and
# "0 targets" is indistinguishable from "already up to date". Three incidents
# have been logged against this shape.


def test_init_db_announces_a_freshly_created_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db.config, "DB_PATH", str(tmp_path / "reel_scout.db"))
    monkeypatch.setattr(db.config, "ensure_dirs", lambda: None)

    conn = db.init_db()
    conn.close()

    err = capsys.readouterr().err
    assert "NEW empty database" in err
    # The absolute path is the whole point — "./data" tells you nothing about
    # which ./data you just made.
    assert str(tmp_path) in err


def test_init_db_is_silent_when_the_database_already_exists(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(db.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db.config, "DB_PATH", str(tmp_path / "reel_scout.db"))
    monkeypatch.setattr(db.config, "ensure_dirs", lambda: None)

    db.init_db().close()
    capsys.readouterr()          # drop the first-run notice

    db.init_db().close()

    # A notice on every command would be noise, and noise gets filtered out —
    # which is how the signal would be lost a second time.
    assert "NEW empty database" not in capsys.readouterr().err


# --- mixed traditional/simplified transcripts (2026-08-15) ----------------
#
# A transcript can change script partway through: one 28,331-char transcript
# flips at 83% with no interleaving. Searching it for 這 returns the first 83%
# and no error. 2 of the 18 Chinese transcripts in the reference library are
# mixed like this, and the detector below scores 18/18 against them — 0 false
# positives, 0 misses. It reports; it does not convert, because converting means
# picking a script for every downstream consumer.

_TRAD = "這個說時來對開發國學實體會過樣應點總經電"
_SIMP = "这个说时来对开发国学实体会过样应点总经电"


def test_scan_script_mix_flags_only_actual_mixtures():
    assert db.scan_script_mix(_TRAD)[1] == 0
    assert db.scan_script_mix(_SIMP)[0] == 0
    trad, simp = db.scan_script_mix(_TRAD + _SIMP)
    assert trad and simp


def test_scan_script_mix_ignores_chars_that_exist_in_both():
    # 后 is a real traditional character (皇后) and 么 is too (幺么). A detector
    # that counted them would flag clean traditional prose as mixed, and a
    # warning that fires on good input is a warning people learn to skip.
    trad, simp = db.scan_script_mix("皇后幺么這個說")
    assert simp == 0, "后/么 must not read as simplified"
    assert trad > 0


def test_save_transcript_warns_on_mixed_script(temp_db, capsys):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        db.init_db(conn)
        vid = db.upsert_video(conn, platform="yt", platform_id="p1", url="u", title="t")
        db.save_transcript(conn, vid, "zh", _TRAD + _SIMP, "[]", "w", 1.0)
        err = capsys.readouterr().err
        assert "mixes traditional and simplified" in err
        assert "silently miss" in err
    finally:
        conn.close()


def test_save_transcript_is_quiet_on_single_script_and_on_non_chinese(temp_db, capsys):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        db.init_db(conn)
        v1 = db.upsert_video(conn, platform="yt", platform_id="p1", url="u", title="t")
        db.save_transcript(conn, v1, "zh", _TRAD * 3, "[]", "w", 1.0)
        assert "mixes traditional" not in capsys.readouterr().err

        # English stays quiet because it contains no Chinese, not because of its
        # language tag. The scan deliberately runs on every transcript: one clip
        # in the library is tagged zh with an English transcript, so trusting
        # that field would skip precisely the mislabelled files this catches.
        v2 = db.upsert_video(conn, platform="yt", platform_id="p2", url="u2", title="t")
        db.save_transcript(conn, v2, "en", "hello world", "[]", "w", 1.0)
        assert "mixes traditional" not in capsys.readouterr().err

        # ...and a mislabelled file is still caught: language says English, the
        # text is mixed Chinese, and the warning fires anyway.
        v3 = db.upsert_video(conn, platform="yt", platform_id="p3", url="u3", title="t")
        db.save_transcript(conn, v3, "en", _TRAD + _SIMP, "[]", "w", 1.0)
        assert "mixes traditional" in capsys.readouterr().err
    finally:
        conn.close()


def test_a_console_that_cannot_print_the_warning_does_not_cost_the_transcript(
    temp_db, monkeypatch
):
    # The warning carries an emoji and an em dash, and printing it used to happen
    # *before* the INSERT — so on a console that could not encode it, a detector
    # whose whole job was to report a problem instead deleted the row it was
    # reporting on. `utils.stderr.warn` absorbs the encoding case, but it catches
    # a deliberately bounded set; this pins the ordering itself, using a failure
    # warn does NOT absorb. Even then the transcript has to be on disk already.
    class _HostileConsole:
        def write(self, s):
            raise RuntimeError("a stream wrapper broken in a way warn() cannot absorb")

        def flush(self):
            pass

    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        db.init_db(conn)
        vid = db.upsert_video(conn, platform="yt", platform_id="p9", url="u9", title="t")
        monkeypatch.setattr(sys, "stderr", _HostileConsole())
        with pytest.raises(RuntimeError):
            db.save_transcript(conn, vid, "zh", _TRAD + _SIMP, "[]", "w", 1.0)
        monkeypatch.undo()

        row = conn.execute(
            "SELECT text_full FROM transcripts WHERE video_id=?", (vid,)
        ).fetchone()
        assert row is not None, "scan, write, then talk — talking must come last"
        assert row["text_full"] == _TRAD + _SIMP
    finally:
        conn.close()


# --- URL lookup resolves on identity, not on string equality ---

def _seed_url(conn, url, platform="instagram", platform_id="DbLQuxQNP3l"):
    return db.upsert_video(conn, platform=platform, platform_id=platform_id,
                           url=url, title="Reel", duration_sec=29.7)


@pytest.mark.parametrize("asked", [
    "https://www.instagram.com/reel/DbLQuxQNP3l/",          # clean, canonical
    "https://www.instagram.com/reels/DbLQuxQNP3l/",         # plural, share form
    "https://instagram.com/reel/DbLQuxQNP3l",               # no www, no slash
    "https://www.instagram.com/adhiraj.anand/reel/DbLQuxQNP3l/",   # account-scoped
    "https://www.instagram.com/reel/DbLQuxQNP3l/?igsh=OTHER",      # different tracker
])
def test_a_stored_tracking_parameter_does_not_hide_the_clip(asked):
    """The row was crawled from a share link, so its stored URL carries `?igsh=`.

    Asking with any other spelling of the same reel used to return nothing, and
    "no video found" reads exactly like "never crawled" — the caller cannot tell
    a lookup miss from an empty library.
    """
    conn, path = _temp_db()
    try:
        vid = _seed_url(conn, "https://www.instagram.com/reel/DbLQuxQNP3l/?igsh=dmw0")
        row = db.get_video_by_url(conn, asked)
        assert row is not None and row["id"] == vid
    finally:
        conn.close()
        os.unlink(path)


def test_a_clip_that_was_never_crawled_is_still_a_miss():
    """The fallback resolves identity; it must not start inventing matches."""
    conn, path = _temp_db()
    try:
        _seed_url(conn, "https://www.instagram.com/reel/DbLQuxQNP3l/")
        assert db.get_video_by_url(
            conn, "https://www.instagram.com/reel/NEVERSEEN1/") is None
        # A different platform with a colliding id is a different clip.
        assert db.get_video_by_url(
            conn, "https://www.tiktok.com/@x/video/DbLQuxQNP3l") is None
    finally:
        conn.close()
        os.unlink(path)


def test_a_non_url_reference_is_not_run_through_the_crawler_registry():
    """Ids and prefixes reach here too; they have their own resolver."""
    conn, path = _temp_db()
    try:
        vid = _seed_url(conn, "https://www.instagram.com/reel/DbLQuxQNP3l/")
        assert db.get_video_by_url(conn, vid[:8]) is None
        assert db.get_video_by_url(conn, "") is None
    finally:
        conn.close()
        os.unlink(path)


# --- 2026-08-25 audit of the real library ---------------------------------


def test_a_transcript_that_outlives_its_media_is_reported():
    """Whisper on speech-less audio either returns nothing or invents, and only
    the invented kind reaches the database.

    Three transcripts in the reference library ran past the end of the file --
    one 6.4s clip carried a single segment 0.0-30.0 reading "Thanks for
    watching!". whisper-guard was installed and enabled the whole time. The
    tell that does not need the phrase: it claims to describe more media than
    exists.
    """
    from reel_scout import db

    hit = db.scan_transcript_overrun(
        json.dumps([{"start": 0.0, "end": 30.0, "text": "Thanks for watching!"}]), 6.4)
    assert hit is not None
    last_end, ratio = hit
    assert last_end == 30.0
    assert ratio > 4


def test_whispers_normal_window_padding_is_not_reported():
    """The rule has to survive the normal case or it trains people to ignore it.

    72 of 75 transcripts in the reference library land within 0.3% of the media
    duration; a decode window that runs a few tenths long is not a finding.
    """
    from reel_scout import db

    assert db.scan_transcript_overrun(
        json.dumps([{"start": 0.0, "end": 60.2}]), 60.0) is None
    assert db.scan_transcript_overrun(
        json.dumps([{"start": 0.0, "end": 827.0}]), 827.014) is None


def test_overrun_scan_returns_none_rather_than_guessing():
    """No segments, no duration, or unparseable JSON is "cannot tell" -- it must
    not surface as either a finding or a clean bill of health."""
    from reel_scout import db

    assert db.scan_transcript_overrun("", 10.0) is None
    assert db.scan_transcript_overrun("[]", 10.0) is None
    assert db.scan_transcript_overrun("not json", 10.0) is None
    assert db.scan_transcript_overrun(json.dumps([{"end": 99.0}]), 0) is None


def test_generated_text_is_watched_for_script_mixing_once_per_surface(capsys):
    """`save_transcript` has watched transcripts for this since it was written.
    The VLM writes into the same database and was never looked at -- 28 frame
    descriptions in the reference library carry simplified characters.

    Once per surface, not once per row: 12,582 description fields is not a
    thing to print 28 warnings about.
    """
    from reel_scout import db

    db._script_mix_warned.clear()
    mixed = "這個場景" + "这个场景"
    db.warn_script_mix(mixed, "frame description")
    first = capsys.readouterr().err
    db.warn_script_mix(mixed, "frame description")
    second = capsys.readouterr().err

    assert "frame description" in first
    assert second == ""
    db._script_mix_warned.clear()


def test_text_that_is_all_one_script_says_nothing(capsys):
    from reel_scout import db

    db._script_mix_warned.clear()
    db.warn_script_mix("這個場景很好", "frame description")
    assert capsys.readouterr().err == ""
    db._script_mix_warned.clear()


# --- shots table (schema v14, roadmap Phase 6A) ----------------------------

def _shot(i, a, b):
    from reel_scout.shots import Shot
    return Shot(index=i, start_sec=a, end_sec=b, dur_sec=round(b - a, 3))


def test_save_shots_round_trips_in_clip_order():
    conn, path = _temp_db()
    try:
        vid = db.upsert_video(conn, "youtube", "s1", "https://yt/s1", title="S1")
        n = db.save_shots(conn, vid, [_shot(0, 0.0, 2.0), _shot(1, 2.0, 7.5),
                                      _shot(2, 7.5, 10.0)])
        assert n == 3
        rows = db.get_shots(conn, vid)
        assert [r["idx"] for r in rows] == [0, 1, 2]
        assert rows[1]["start_sec"] == 2.0 and rows[1]["dur_sec"] == 5.5
    finally:
        conn.close()
        os.unlink(path)


def test_save_shots_replaces_rather_than_appends():
    # A second analyze pass measures the same clip again. Appending would leave
    # two overlapping partitions behind one video_id with nothing marking which
    # is current.
    conn, path = _temp_db()
    try:
        vid = db.upsert_video(conn, "youtube", "s2", "https://yt/s2", title="S2")
        db.save_shots(conn, vid, [_shot(0, 0.0, 5.0), _shot(1, 5.0, 10.0)])
        db.save_shots(conn, vid, [_shot(0, 0.0, 10.0)])
        rows = db.get_shots(conn, vid)
        assert len(rows) == 1
        assert rows[0]["end_sec"] == 10.0
    finally:
        conn.close()
        os.unlink(path)


def test_save_shots_empty_clears_instead_of_leaving_a_stale_partition():
    conn, path = _temp_db()
    try:
        vid = db.upsert_video(conn, "youtube", "s3", "https://yt/s3", title="S3")
        db.save_shots(conn, vid, [_shot(0, 0.0, 5.0)])
        assert db.save_shots(conn, vid, []) == 0
        assert db.get_shots(conn, vid) == []
    finally:
        conn.close()
        os.unlink(path)


def test_save_shots_is_scoped_to_one_video():
    conn, path = _temp_db()
    try:
        a = db.upsert_video(conn, "youtube", "s4", "https://yt/s4", title="S4")
        b = db.upsert_video(conn, "youtube", "s5", "https://yt/s5", title="S5")
        db.save_shots(conn, a, [_shot(0, 0.0, 3.0)])
        db.save_shots(conn, b, [_shot(0, 0.0, 4.0), _shot(1, 4.0, 8.0)])
        db.save_shots(conn, b, [])          # clearing b must not touch a
        assert len(db.get_shots(conn, a)) == 1
        assert db.get_shots(conn, b) == []
    finally:
        conn.close()
        os.unlink(path)


def test_get_shots_is_empty_for_a_clip_analyzed_before_v14():
    # Not an error state - the honest answer for a pre-migration clip.
    conn, path = _temp_db()
    try:
        vid = db.upsert_video(conn, "youtube", "s6", "https://yt/s6", title="S6")
        assert db.get_shots(conn, vid) == []
    finally:
        conn.close()
        os.unlink(path)


def test_migration_creates_shots_on_a_v13_database():
    conn, path = _temp_db()
    try:
        # Simulate v13: move the table aside (a rename, so nothing is destroyed)
        # and wind the version back, then let init_db run the ladder.
        conn.execute("ALTER TABLE shots RENAME TO shots_v13_stash")
        conn.execute("UPDATE schema_version SET version = 13")
        conn.commit()
        db.init_db(conn)
        # Against SCHEMA_VERSION, not a literal: the ladder runs every later
        # migration too, and pinning 14 here breaks the moment one is added.
        assert (conn.execute("SELECT version FROM schema_version").fetchone()[0]
                == db.SCHEMA_VERSION)
        vid = db.upsert_video(conn, "youtube", "s7", "https://yt/s7", title="S7")
        assert db.save_shots(conn, vid, [_shot(0, 0.0, 1.0)]) == 1
        assert len(db.get_shots(conn, vid)) == 1
    finally:
        conn.close()
        os.unlink(path)


def test_migration_is_idempotent():
    conn, path = _temp_db()
    try:
        vid = db.upsert_video(conn, "youtube", "s8", "https://yt/s8", title="S8")
        db.save_shots(conn, vid, [_shot(0, 0.0, 1.0)])
        db._migrate_v13_to_v14(conn)      # running it again must not lose rows
        assert len(db.get_shots(conn, vid)) == 1
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 14
        db.init_db(conn)                  # and the ladder puts it back to current
        assert (conn.execute("SELECT version FROM schema_version").fetchone()[0]
                == db.SCHEMA_VERSION)
    finally:
        conn.close()
        os.unlink(path)
