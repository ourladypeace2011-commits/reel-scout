from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import config

SCHEMA_VERSION = 2

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS videos (
    id              TEXT PRIMARY KEY,
    platform        TEXT NOT NULL,
    platform_id     TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT,
    uploader        TEXT,
    duration_sec    REAL,
    upload_date     TEXT,
    file_path       TEXT,
    file_size_bytes INTEGER,
    status          TEXT DEFAULT 'downloaded',
    error_message   TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transcripts (
    video_id        TEXT PRIMARY KEY REFERENCES videos(id),
    language        TEXT,
    text_full       TEXT,
    segments_json   TEXT,
    whisper_model   TEXT,
    duration_sec    REAL,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS keyframes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id        TEXT REFERENCES videos(id),
    frame_index     INTEGER,
    timestamp_sec   REAL,
    file_path       TEXT,
    strategy        TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vision_descriptions (
    keyframe_id     INTEGER PRIMARY KEY REFERENCES keyframes(id),
    description     TEXT,
    objects_json    TEXT,
    text_in_frame   TEXT,
    vlm_backend     TEXT,
    vlm_model       TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS analyses (
    video_id        TEXT PRIMARY KEY REFERENCES videos(id),
    summary         TEXT,
    topics_json     TEXT,
    hooks_json      TEXT,
    style_json      TEXT,
    engagement_signals_json TEXT,
    full_json       TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batches (
    id              TEXT PRIMARY KEY,
    source          TEXT,
    total_urls      INTEGER,
    completed       INTEGER DEFAULT 0,
    failed          INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'running',
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batch_items (
    batch_id        TEXT REFERENCES batches(id),
    url             TEXT,
    video_id        TEXT REFERENCES videos(id),
    status          TEXT DEFAULT 'pending',
    error_message   TEXT,
    PRIMARY KEY (batch_id, url)
);

CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_platform ON videos(platform);
CREATE INDEX IF NOT EXISTS idx_batch_items_status ON batch_items(status);
"""


def _video_id(platform: str, platform_id: str) -> str:
    raw = f"{platform}:{platform_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def get_connection() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add audio_events table (schema v1 -> v2)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS audio_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id        TEXT REFERENCES videos(id),
            event_type      TEXT NOT NULL,
            label           TEXT,
            start_sec       REAL,
            end_sec         REAL,
            confidence      REAL,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_audio_events_video ON audio_events(video_id);
    """)
    conn.execute(
        "UPDATE schema_version SET version = 2 WHERE version = 1"
    )
    conn.commit()


def init_db(conn: Optional[sqlite3.Connection] = None) -> sqlite3.Connection:
    if conn is None:
        conn = get_connection()
    conn.executescript(_SCHEMA_SQL)
    # Set schema version if not exists
    cur = conn.execute("SELECT version FROM schema_version LIMIT 1")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
    else:
        current_ver = row[0] if row else 0
        if current_ver < 2:
            _migrate_v1_to_v2(conn)
    # Always ensure audio_events table exists for fresh installs
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS audio_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id        TEXT REFERENCES videos(id),
            event_type      TEXT NOT NULL,
            label           TEXT,
            start_sec       REAL,
            end_sec         REAL,
            confidence      REAL,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_audio_events_video ON audio_events(video_id);
    """)
    conn.commit()
    return conn


# --- Video CRUD ---

def get_video(conn: sqlite3.Connection, video_id: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,))
    return cur.fetchone()


def get_video_by_url(conn: sqlite3.Connection, url: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM videos WHERE url = ?", (url,))
    return cur.fetchone()


def upsert_video(
    conn: sqlite3.Connection,
    platform: str,
    platform_id: str,
    url: str,
    title: Optional[str] = None,
    uploader: Optional[str] = None,
    duration_sec: Optional[float] = None,
    upload_date: Optional[str] = None,
    file_path: Optional[str] = None,
    file_size_bytes: Optional[int] = None,
) -> str:
    vid = _video_id(platform, platform_id)
    existing = get_video(conn, vid)
    if existing:
        conn.execute(
            """UPDATE videos SET title=COALESCE(?,title), uploader=COALESCE(?,uploader),
               duration_sec=COALESCE(?,duration_sec), upload_date=COALESCE(?,upload_date),
               file_path=COALESCE(?,file_path), file_size_bytes=COALESCE(?,file_size_bytes),
               updated_at=datetime('now')
               WHERE id=?""",
            (title, uploader, duration_sec, upload_date, file_path, file_size_bytes, vid),
        )
    else:
        conn.execute(
            """INSERT INTO videos (id, platform, platform_id, url, title, uploader,
               duration_sec, upload_date, file_path, file_size_bytes)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (vid, platform, platform_id, url, title, uploader,
             duration_sec, upload_date, file_path, file_size_bytes),
        )
    conn.commit()
    return vid


def update_video_status(
    conn: sqlite3.Connection, video_id: str, status: str,
    error: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE videos SET status=?, error_message=?, updated_at=datetime('now') WHERE id=?",
        (status, error, video_id),
    )
    conn.commit()


def list_videos(
    conn: sqlite3.Connection,
    status: Optional[str] = None,
    platform: Optional[str] = None,
    limit: int = 50,
) -> List[sqlite3.Row]:
    query = "SELECT * FROM videos WHERE 1=1"
    params = []  # type: List[Any]
    if status:
        query += " AND status = ?"
        params.append(status)
    if platform:
        query += " AND platform = ?"
        params.append(platform)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return conn.execute(query, params).fetchall()


# --- Transcript CRUD ---

def save_transcript(
    conn: sqlite3.Connection,
    video_id: str,
    language: str,
    text_full: str,
    segments_json: str,
    whisper_model: str,
    duration_sec: float,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO transcripts
           (video_id, language, text_full, segments_json, whisper_model, duration_sec)
           VALUES (?,?,?,?,?,?)""",
        (video_id, language, text_full, segments_json, whisper_model, duration_sec),
    )
    update_video_status(conn, video_id, "transcribed")
    conn.commit()


def get_transcript(conn: sqlite3.Connection, video_id: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM transcripts WHERE video_id = ?", (video_id,))
    return cur.fetchone()


# --- Keyframe CRUD ---

def save_keyframes(
    conn: sqlite3.Connection,
    video_id: str,
    keyframes: List[Dict[str, Any]],
) -> List[int]:
    ids = []
    for kf in keyframes:
        cur = conn.execute(
            """INSERT INTO keyframes (video_id, frame_index, timestamp_sec, file_path, strategy)
               VALUES (?,?,?,?,?)""",
            (video_id, kf["frame_index"], kf["timestamp_sec"],
             kf["file_path"], kf["strategy"]),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def get_keyframes(conn: sqlite3.Connection, video_id: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM keyframes WHERE video_id = ? ORDER BY timestamp_sec",
        (video_id,),
    ).fetchall()


# --- Vision CRUD ---

def save_vision_description(
    conn: sqlite3.Connection,
    keyframe_id: int,
    description: str,
    objects_json: str,
    text_in_frame: str,
    vlm_backend: str,
    vlm_model: str,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO vision_descriptions
           (keyframe_id, description, objects_json, text_in_frame, vlm_backend, vlm_model)
           VALUES (?,?,?,?,?,?)""",
        (keyframe_id, description, objects_json, text_in_frame, vlm_backend, vlm_model),
    )
    conn.commit()


# --- Audio Events CRUD ---

def save_audio_events(
    conn: sqlite3.Connection,
    video_id: str,
    events: List[Dict[str, Any]],
) -> None:
    """Bulk insert audio events for a video."""
    for ev in events:
        conn.execute(
            """INSERT INTO audio_events
               (video_id, event_type, label, start_sec, end_sec, confidence)
               VALUES (?,?,?,?,?,?)""",
            (
                video_id,
                ev["event_type"],
                ev.get("label", ""),
                ev.get("start_sec"),
                ev.get("end_sec"),
                ev.get("confidence"),
            ),
        )
    conn.commit()


def get_audio_events(
    conn: sqlite3.Connection, video_id: str
) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM audio_events WHERE video_id = ? ORDER BY start_sec",
        (video_id,),
    ).fetchall()


# --- Analysis CRUD ---

def save_analysis(
    conn: sqlite3.Connection,
    video_id: str,
    summary: str,
    topics_json: str,
    hooks_json: str,
    style_json: str,
    engagement_signals_json: str,
    full_json: str,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO analyses
           (video_id, summary, topics_json, hooks_json, style_json,
            engagement_signals_json, full_json)
           VALUES (?,?,?,?,?,?,?)""",
        (video_id, summary, topics_json, hooks_json, style_json,
         engagement_signals_json, full_json),
    )
    update_video_status(conn, video_id, "analyzed")
    conn.commit()


def get_analysis(conn: sqlite3.Connection, video_id: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM analyses WHERE video_id = ?", (video_id,))
    return cur.fetchone()


# --- Batch CRUD ---

def create_batch(
    conn: sqlite3.Connection, urls: List[str], source: str = "cli",
) -> str:
    batch_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO batches (id, source, total_urls) VALUES (?,?,?)",
        (batch_id, source, len(urls)),
    )
    for url in urls:
        conn.execute(
            "INSERT INTO batch_items (batch_id, url) VALUES (?,?)",
            (batch_id, url),
        )
    conn.commit()
    return batch_id


def get_pending_batch_items(
    conn: sqlite3.Connection, batch_id: str,
) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batch_items WHERE batch_id = ? AND status = 'pending'",
        (batch_id,),
    ).fetchall()


def get_latest_interrupted_batch(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM batches WHERE status = 'interrupted' ORDER BY updated_at DESC LIMIT 1"
    )
    return cur.fetchone()


def update_batch_item(
    conn: sqlite3.Connection,
    batch_id: str,
    url: str,
    status: str,
    video_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    conn.execute(
        """UPDATE batch_items SET status=?, video_id=?, error_message=?
           WHERE batch_id=? AND url=?""",
        (status, video_id, error, batch_id, url),
    )
    # Update batch counters
    if status == "done":
        conn.execute(
            "UPDATE batches SET completed=completed+1, updated_at=datetime('now') WHERE id=?",
            (batch_id,),
        )
    elif status == "error":
        conn.execute(
            "UPDATE batches SET failed=failed+1, updated_at=datetime('now') WHERE id=?",
            (batch_id,),
        )
    conn.commit()


def mark_batch_interrupted(conn: sqlite3.Connection, batch_id: str) -> None:
    conn.execute(
        "UPDATE batches SET status='interrupted', updated_at=datetime('now') WHERE id=?",
        (batch_id,),
    )
    conn.commit()


def mark_batch_completed(conn: sqlite3.Connection, batch_id: str) -> None:
    conn.execute(
        "UPDATE batches SET status='completed', updated_at=datetime('now') WHERE id=?",
        (batch_id,),
    )
    conn.commit()


# --- Stats ---

def db_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    stats = {}
    for table in ["videos", "transcripts", "keyframes", "vision_descriptions", "analyses", "audio_events", "batches"]:
        cur = conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 - table names are hardcoded
        stats[table] = cur.fetchone()[0]

    # Status breakdown
    rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM videos GROUP BY status"
    ).fetchall()
    stats["videos_by_status"] = {r["status"]: r["cnt"] for r in rows}

    # Platform breakdown
    rows = conn.execute(
        "SELECT platform, COUNT(*) as cnt FROM videos GROUP BY platform"
    ).fetchall()
    stats["videos_by_platform"] = {r["platform"]: r["cnt"] for r in rows}

    return stats
