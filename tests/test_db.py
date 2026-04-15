from __future__ import annotations

import os
import sqlite3
import tempfile

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
