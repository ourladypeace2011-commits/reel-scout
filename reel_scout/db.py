from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .utils.stderr import warn

SCHEMA_VERSION = 19

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

CREATE TABLE IF NOT EXISTS keyframe_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id          TEXT REFERENCES videos(id),
    strategy          TEXT,
    requested_max     INTEGER,
    selector_version  INTEGER DEFAULT 0,
    superseded_at     TEXT,
    created_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS keyframes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id        TEXT REFERENCES videos(id),
    run_id          INTEGER REFERENCES keyframe_runs(id),
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
    -- Normalized low-cardinality tags (derived from full_json) for filtering/stats.
    content_type    TEXT,
    opening_type    TEXT,
    cta_type        TEXT,
    style_format    TEXT,
    style_pacing    TEXT,
    emotion         TEXT,
    content_structure TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batches (
    id              TEXT PRIMARY KEY,
    source          TEXT,
    total_urls      INTEGER,
    completed       INTEGER DEFAULT 0,
    failed          INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'running',
    mode            TEXT,
    out_root        TEXT,
    pid             INTEGER,
    heartbeat_at    TEXT,
    cancel_requested INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batch_items (
    batch_id        TEXT REFERENCES batches(id),
    url             TEXT,
    video_id        TEXT REFERENCES videos(id),
    status          TEXT DEFAULT 'pending',
    error_message   TEXT,
    label           TEXT,
    slug            TEXT,
    bundle_dir      TEXT,
    PRIMARY KEY (batch_id, url)
);

-- User annotations. Deliberately NOT columns on `videos`: everything else in
-- this database is derived from the source clip and is safe to regenerate, but
-- these rows are the operator's own judgement. Keeping them in their own tables
-- is what lets re-crawl / re-analyze / re-score rewrite the pipeline's output
-- without ever touching a note someone typed.
CREATE TABLE IF NOT EXISTS annotation_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    sort_order      INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS video_annotations (
    video_id        TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
    note            TEXT,
    group_id        INTEGER REFERENCES annotation_groups(id) ON DELETE SET NULL,
    starred         INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- A mark is a point on one clip's timeline: "the cut at 0:07 is a semantic
-- turn". It hangs off a second, not off a keyframe row, because keyframes have
-- a run identity and get superseded (v12) while the second stays the second.
CREATE TABLE IF NOT EXISTS clip_marks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id        TEXT NOT NULL REFERENCES videos(id),
    t_sec           REAL NOT NULL,
    label           TEXT NOT NULL,
    note            TEXT,
    source          TEXT DEFAULT 'manual',
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_clip_marks_video ON clip_marks(video_id, t_sec);

CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_platform ON videos(platform);
CREATE INDEX IF NOT EXISTS idx_batch_items_status ON batch_items(status);
CREATE INDEX IF NOT EXISTS idx_annotations_group ON video_annotations(group_id);
CREATE INDEX IF NOT EXISTS idx_annotations_starred ON video_annotations(starred);
"""
# analyses tag indexes are created after migrations (see init_db) so they don't
# run against a pre-v5 DB whose analyses table lacks these columns yet.


# Low-cardinality analysis tags that are mirrored from full_json into indexed
# columns on `analyses` so they can be filtered/aggregated without JSON scans.
# (column name -> extractor) — full_json stays the source of truth.
def _extract_tag_columns(data: Dict[str, Any]) -> Dict[str, Any]:
    hook = data.get("hook") or {}
    style = data.get("style") or {}
    eng = data.get("engagement_signals") or {}
    return {
        "content_type": data.get("content_type"),
        "opening_type": hook.get("opening_type"),
        "cta_type": hook.get("cta_type"),
        "style_format": style.get("format"),
        "style_pacing": style.get("pacing"),
        "emotion": eng.get("emotion"),
        "content_structure": data.get("content_structure"),
    }


def _video_id(platform: str, platform_id: str) -> str:
    raw = f"{platform}:{platform_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def get_connection(timeout: float = 5.0) -> sqlite3.Connection:
    """A connection to the project database.

    `timeout` is sqlite's busy timeout. The 5s default is fine for a CLI command
    but not for anything running alongside a batch, where an analyze child can
    hold the write lock while it flushes a video's worth of keyframes.
    """
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=timeout)
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


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Add scores table (schema v2 -> v3)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scores (
            video_id        TEXT PRIMARY KEY REFERENCES videos(id),
            hook_strength   REAL,
            visual_storytelling REAL,
            pacing          REAL,
            structure       REAL,
            overall         REAL,
            reasoning       TEXT,
            model_used      TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.execute(
        "UPDATE schema_version SET version = 3 WHERE version = 2"
    )
    conn.commit()


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Rebuild scores table with Direction-A dimensions (schema v3 -> v4).
    Safe DROP: scores is regenerated by `reel-scout score`, not source data."""
    conn.executescript("""
        DROP TABLE IF EXISTS scores;
        CREATE TABLE scores (
            video_id        TEXT PRIMARY KEY REFERENCES videos(id),
            hook_strength   REAL,
            visual_storytelling REAL,
            pacing          REAL,
            structure       REAL,
            overall         REAL,
            reasoning       TEXT,
            model_used      TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.execute(
        "UPDATE schema_version SET version = 4 WHERE version = 3"
    )
    conn.commit()


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """Normalize low-cardinality analysis tags into indexed columns for
    filtering/stats (schema v4 -> v5), and backfill them from the existing
    full_json blobs. First migration in this repo to ALTER an existing
    data-bearing table (prior ones only added/rebuilt whole tables)."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(analyses)")}
    for name in ("content_type", "opening_type", "cta_type",
                 "style_format", "style_pacing", "emotion"):
        if name not in existing:
            conn.execute("ALTER TABLE analyses ADD COLUMN %s TEXT" % name)
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_analyses_content_type ON analyses(content_type);
        CREATE INDEX IF NOT EXISTS idx_analyses_style_format ON analyses(style_format);
        CREATE INDEX IF NOT EXISTS idx_analyses_opening_type ON analyses(opening_type);
        CREATE INDEX IF NOT EXISTS idx_analyses_cta_type ON analyses(cta_type);
    """)
    # Backfill from full_json (the source of truth) for rows analyzed pre-v5.
    for video_id, full_json in conn.execute(
        "SELECT video_id, full_json FROM analyses"
    ).fetchall():
        if not full_json:
            continue
        try:
            data = json.loads(full_json)
        except (ValueError, TypeError):
            continue
        tags = _extract_tag_columns(data)
        conn.execute(
            """UPDATE analyses SET content_type=?, opening_type=?, cta_type=?,
               style_format=?, style_pacing=?, emotion=? WHERE video_id=?""",
            (tags["content_type"], tags["opening_type"], tags["cta_type"],
             tags["style_format"], tags["style_pacing"], tags["emotion"], video_id),
        )
    conn.execute("UPDATE schema_version SET version = 5 WHERE version = 4")
    conn.commit()


def _migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    """Add the content_structure classification column + backfill from full_json
    (schema v5 -> v6). Rows analyzed before the merger emitted content_structure
    simply stay NULL — nothing to backfill for them."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(analyses)")}
    if "content_structure" not in existing:
        conn.execute("ALTER TABLE analyses ADD COLUMN content_structure TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_analyses_content_structure "
        "ON analyses(content_structure)"
    )
    for video_id, full_json in conn.execute(
        "SELECT video_id, full_json FROM analyses"
    ).fetchall():
        if not full_json:
            continue
        try:
            data = json.loads(full_json)
        except (ValueError, TypeError):
            continue
        cs = data.get("content_structure")
        if cs is not None:
            conn.execute(
                "UPDATE analyses SET content_structure=? WHERE video_id=?",
                (cs, video_id),
            )
    conn.execute("UPDATE schema_version SET version = 6 WHERE version = 5")
    conn.commit()


def _migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    """Add the shot_metrics table (schema v6 -> v7) for §4E evidence-based pacing:
    measured cut rhythm (cuts/min, shot count, avg shot length) + audio energy/BPM.
    A dedicated per-video table (like audio_events), not analyses columns — the
    numbers are measured signals, not derived tags, and stay directly queryable.
    No backfill: old videos simply have no measured metrics until re-analyzed."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS shot_metrics (
            video_id        TEXT PRIMARY KEY REFERENCES videos(id),
            shot_count      INTEGER,
            cuts_per_minute REAL,
            avg_shot_sec    REAL,
            audio_bpm       REAL,
            audio_energy    REAL,
            created_at      TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.execute("UPDATE schema_version SET version = 7 WHERE version = 6")
    conn.commit()


def _migrate_v7_to_v8(conn: sqlite3.Connection) -> None:
    """Add the ocr_captions table (schema v7 -> v8) for §4F: burned-in on-screen
    text (L3.5) with timestamps + engine provenance. Per-video, many rows (like
    keyframes/audio_events). No backfill — populated on (re-)analysis."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ocr_captions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id        TEXT REFERENCES videos(id),
            timestamp_sec   REAL,
            text            TEXT,
            engine          TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_ocr_captions_video ON ocr_captions(video_id);
    """)
    conn.execute("UPDATE schema_version SET version = 8 WHERE version = 7")
    conn.commit()


def _migrate_v8_to_v9(conn: sqlite3.Connection) -> None:
    """Add the performance table (schema v8 -> v9) for roadmap 4D: my own video's
    actual views/likes/comments, so its structure can be contrasted with the
    high-scoring corpus. One row per video (INSERT OR REPLACE)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS performance (
            video_id        TEXT PRIMARY KEY REFERENCES videos(id),
            views           INTEGER,
            likes           INTEGER,
            comments        INTEGER,
            notes           TEXT,
            recorded_at     TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.execute("UPDATE schema_version SET version = 9 WHERE version = 8")
    conn.commit()


def _migrate_v9_to_v10(conn: sqlite3.Connection) -> None:
    """Batch columns for the background runner (schema v9 -> v10).

    A batch started over MCP outlives the call that started it, so the row has to
    carry everything needed to report on it later and to recognise a worker that
    died: what it was told to do (mode, out_root), who is doing it (pid) and
    whether that is still true (heartbeat_at), plus a flag it can be asked to
    stop by. On batch_items, the label/slug/bundle_dir that used to live only in
    run_batch's return value, so progress survives the process.

    All nullable ADD COLUMN: no rewrite, no backfill, existing rows keep working
    and `analyze` -- the other writer of these tables -- is untouched.
    """
    for table, column, decl in (
        ("batches", "mode", "TEXT"),
        ("batches", "out_root", "TEXT"),
        ("batches", "pid", "INTEGER"),
        ("batches", "heartbeat_at", "TEXT"),
        ("batches", "cancel_requested", "INTEGER DEFAULT 0"),
        ("batch_items", "label", "TEXT"),
        ("batch_items", "slug", "TEXT"),
        ("batch_items", "bundle_dir", "TEXT"),
    ):
        try:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
        except sqlite3.OperationalError:
            pass  # already there
    conn.execute("UPDATE schema_version SET version = 10 WHERE version = 9")
    conn.commit()


def _migrate_v10_to_v11(conn: sqlite3.Connection) -> None:
    """User annotations: note / group / star (schema v10 -> v11).

    Two new tables, nothing altered. The pipeline tables are not touched at all,
    which is the point: a note survives every re-crawl, re-analyze and re-score,
    because none of those writers knows these tables exist.

    `group_id` is ON DELETE SET NULL rather than CASCADE — deleting a group must
    lose the grouping, never the note that was filed under it.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS annotation_groups (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL UNIQUE,
            sort_order      INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS video_annotations (
            video_id        TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
            note            TEXT,
            group_id        INTEGER REFERENCES annotation_groups(id) ON DELETE SET NULL,
            starred         INTEGER NOT NULL DEFAULT 0,
            updated_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_annotations_group ON video_annotations(group_id);
        CREATE INDEX IF NOT EXISTS idx_annotations_starred ON video_annotations(starred);
        """
    )
    conn.execute("UPDATE schema_version SET version = 11 WHERE version = 10")
    conn.commit()


def _migrate_v11_to_v12(conn: sqlite3.Connection) -> None:
    """Keyframe extractions become runs, so one can replace another (v11 -> v12).

    Re-extracting used to be impossible: `analyze` skipped it whenever any
    keyframe existed, which made `--keyframe-strategy` unreachable for anything
    already in the library. Simply overwriting was not an option either -- the
    scene extractor writes `<video_id>_scene_%03d.jpg` into the same directory
    every time, so a second pass would replace run 1's images in place while
    every row still pointed at the filename. Nothing deleted, and every old
    description now describing a picture that is no longer there.

    So an extraction gets an identity. New runs write into `r<run_id>/` and the
    previous run is marked superseded rather than removed: its rows, its images
    and the descriptions written against them all stay exactly where they are,
    which is what lets an earlier analysis still be checked against what it
    actually saw.

    Existing rows are backfilled into one run per video at selector_version 0 --
    0 meaning "sampled by the version that only ever looked at the opening".
    That number is how a re-run can tell what still needs doing.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS keyframe_runs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id          TEXT REFERENCES videos(id),
            strategy          TEXT,
            requested_max     INTEGER,
            selector_version  INTEGER DEFAULT 0,
            superseded_at     TEXT,
            created_at        TEXT DEFAULT (datetime('now'))
        );
        """
    )
    try:
        conn.execute("ALTER TABLE keyframes ADD COLUMN run_id INTEGER "
                     "REFERENCES keyframe_runs(id)")
    except sqlite3.OperationalError:
        pass  # already there

    rows = conn.execute(
        "SELECT video_id, MIN(strategy) AS strategy, COUNT(*) AS n "
        "FROM keyframes WHERE run_id IS NULL GROUP BY video_id"
    ).fetchall()
    for r in rows:
        cur = conn.execute(
            "INSERT INTO keyframe_runs (video_id, strategy, requested_max, "
            "selector_version) VALUES (?,?,?,0)",
            (r[0], r[1], r[2]),
        )
        conn.execute("UPDATE keyframes SET run_id = ? WHERE video_id = ? AND run_id IS NULL",
                     (cur.lastrowid, r[0]))
    conn.execute("UPDATE schema_version SET version = 12 WHERE version = 11")
    conn.commit()


def _migrate_v12_to_v13(conn: sqlite3.Connection) -> None:
    """Clips gain marks — points on the timeline someone put there (v12 -> v13).

    The decoded layers all answer "what is in this clip". None of them answer
    "which second is the one worth showing", and that answer is the working
    output of watching a reel with an intent: the cut at 0:07 is a semantic
    turn, the hook lands at 0:02, the caption everyone copies is at 0:11.

    It is not a note. `annotate.MAX_NOTE_LEN` caps a note at 500 characters and
    rejects rather than truncates, on purpose -- a note is one line describing a
    whole clip, and a timeline is a list of somewheres. Nor is it a keyframe
    row: a keyframe belongs to an extraction run and a later run supersedes it
    (see the v12 migration), so a mark hung off a keyframe id would stop
    pointing at anything the first time `--keyframe-strategy` was changed. A
    mark hangs off `t_sec`, which survives re-extraction because seven seconds
    into a clip is still seven seconds into that clip.

    `source` is what makes an importer safe to re-run. Rows written by
    `mark --import <file> --source teardown` carry `import:teardown`, and a
    re-import deletes only rows with that same source before writing the new
    ones. Marks someone typed by hand stay `manual` and are never in the blast
    radius, which is what lets the imported set be a regenerable projection of
    a file kept somewhere else without the two fighting over the same table.

    Nothing is backfilled: there is no earlier shape of this data to carry
    forward. The table starts empty and the index is the one the reads need --
    every query is "this video's marks, in time order".
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS clip_marks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id        TEXT NOT NULL REFERENCES videos(id),
            t_sec           REAL NOT NULL,
            label           TEXT NOT NULL,
            note            TEXT,
            source          TEXT DEFAULT 'manual',
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_clip_marks_video ON clip_marks(video_id, t_sec);
        """
    )
    conn.execute("UPDATE schema_version SET version = 13 WHERE version = 12")
    conn.commit()


def _migrate_v13_to_v14(conn: sqlite3.Connection) -> None:
    """A clip gains its shots — the spans between cuts (v13 -> v14).

    `shot_metrics` has counted cuts since v7, but only the aggregate:
    `shot_count`, `cuts_per_minute`, `avg_shot_sec`. The boundaries themselves
    were resolved out of the ffmpeg dump on every run and then discarded by a
    `len()`. So the library could say a clip has 50 shots and could not say
    where any one of them starts — and "one shot" is the unit every downstream
    question needs: which span is a close-up, which one holds still, which line
    of VO belongs to which frame.

    The table is additive and derived. Nothing reads it yet, existing rows are
    untouched, and a clip analyzed before v14 simply has no shots until it is
    re-analyzed. `(video_id, idx)` is unique so a re-run replaces rather than
    doubles, which is the failure mode a plain append would have.

    `shot_count` stays in `shot_metrics` rather than being recomputed from
    here: they are derived from the same boundary list in the same pass
    (`shots.compute_shot_table`), so they cannot disagree, and dropping the
    aggregate would break every existing scorer path for no gain.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS shots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id        TEXT REFERENCES videos(id),
            idx             INTEGER,
            start_sec       REAL,
            end_sec         REAL,
            dur_sec         REAL,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_shots_video ON shots(video_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shots_video_idx ON shots(video_id, idx);
        """
    )
    conn.execute("UPDATE schema_version SET version = 14")
    conn.commit()


def _migrate_v14_to_v15(conn: sqlite3.Connection) -> None:
    """A shot gains labels — and they hang off a timestamp, not a shot id (v15).

    Shot size is the first `kind`. It cannot live as a column on `shots`,
    because `save_shots` replaces a clip's spans wholesale on every re-analyze:
    every stored label would be deleted with the row it sat on, silently, and
    the next run would look like the clip had never been labelled.

    The v13 `marks` migration already settled this shape for the same reason —
    "a mark hangs off `t_sec`, which survives re-extraction because seven
    seconds into a clip is still seven seconds into that clip". A label is the
    same kind of thing: it describes what is on screen at a moment, and the
    moment outlives whichever partition happened to be current when someone
    looked.

    `source` and `model` are not decoration. A craft score taught this repo that
    a model-produced value without its origin is unusable the moment a second
    model touches the library: the same clip scores 7.43 under `qwen3-vl:8b` and
    5.5 under `qwen2.5vl:7b`. A shot size read by a VLM, a shot size pulled out
    of an old description, and a shot size a person typed are three different
    claims, and the uniqueness key includes `source` so all three can coexist
    rather than overwrite each other.

    ⚠️ **`model` is not in the key.** It records which model produced the row
    that is there; a second model under the same source replaces the first.
    What coexists is kinds of claim, not readings — see `label_shots`.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS shot_labels (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id        TEXT REFERENCES videos(id),
            t_sec           REAL NOT NULL,
            kind            TEXT NOT NULL,
            value           TEXT,
            source          TEXT,
            model           TEXT,
            prompt_hash     TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_shot_labels_video ON shot_labels(video_id, kind);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shot_labels_unique
            ON shot_labels(video_id, kind, t_sec, source);
        """
    )
    conn.execute("UPDATE schema_version SET version = 15")
    conn.commit()


def _migrate_v15_to_v16(conn: sqlite3.Connection) -> None:
    """Side-by-side translations of the free text (v15 -> v16).

    The decoded enums became translatable at the display layer because the model
    only picks from a closed list. Free text is different: a summary, a frame
    description, a line of transcript, a scoring rationale — those words are the
    model's own, and a translation is a second artifact, not a relabelling.

    So translations live in their own table and the original is never replaced.
    A reader sees both, and the row says which engine produced the Chinese.

    🔴 `source_hash` is the load-bearing column. Descriptions get re-run when a
    VLM changes; transcripts get re-cut when the language track is fixed. A
    translation stored without a fingerprint of what it translated would go on
    being displayed beside text it no longer matches — silently, and looking
    exactly like a current one. With the hash, a stale row can say so.

    `ref` addresses the piece inside a clip: a keyframe id, a transcript segment
    index, a timeline index, or '' for the whole-clip kinds (summary, reasoning).
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS translations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id        TEXT REFERENCES videos(id),
            kind            TEXT NOT NULL,
            ref             TEXT NOT NULL DEFAULT '',
            lang            TEXT NOT NULL,
            source_hash     TEXT NOT NULL,
            text            TEXT,
            engine          TEXT,
            model           TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_translations_video
            ON translations(video_id, kind, lang);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_translations_unique
            ON translations(video_id, kind, ref, lang);
        """
    )
    conn.execute("UPDATE schema_version SET version = 16")
    conn.commit()


def _migrate_v17_to_v18(conn: sqlite3.Connection) -> None:
    """Camera movement per shot, measured from motion vectors (v17 -> v18).

    Its own table rather than a `shot_labels` row, because it is not a label:
    no model answered, no prompt produced it. It is a measurement, like
    `shots`, and it carries the two numbers the class was derived from --
    `speed` and `agreement` -- so a reader can see why a shot was called what
    it was called. A class with no numbers under it is the shape §4E spent a
    page warning about.

    🔴 **Keyed on `t_sec`, never on `shots.id`.** `save_shots` replaces the span
    rows on every re-analyze, so anything hanging off a shot id disappears with
    them, silently. The same rule `shot_labels` was built on: seven seconds in
    is still seven seconds in.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS shot_motion (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id    TEXT NOT NULL REFERENCES videos(id),
            t_sec       REAL NOT NULL,
            dx          REAL,
            dy          REAL,
            speed       REAL,
            agreement   REAL,
            zoom        REAL,
            rotation    REAL,
            frames      INTEGER NOT NULL,
            movement    TEXT NOT NULL,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(video_id, t_sec)
        );
        CREATE INDEX IF NOT EXISTS idx_shot_motion_video ON shot_motion(video_id);
    """)
    conn.execute("UPDATE schema_version SET version = 18")
    conn.commit()


def _migrate_v16_to_v17(conn: sqlite3.Connection) -> None:
    """A shot label records the prompt that produced it (v16 -> v17).

    `source` and `model` were not enough. Measured on 24 frames from 18 clips:
    the same model on the same images returned `ECU` **ten** times under a
    prompt that listed the seven codes with their English names, and **once**
    under a prompt that defined each one by subject-to-frame ratio. Nothing in
    the stored rows distinguished the two runs — same source, same model, and a
    distribution that had moved by a factor of ten.

    A hash rather than a version string, for the same reason `translations`
    stores one: a number somebody has to remember to bump goes stale silently,
    a fingerprint of the actual prompt cannot. Existing rows get NULL, which
    reads correctly as "produced before this was recorded".
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(shot_labels)")]
    if "prompt_hash" not in cols:
        conn.execute("ALTER TABLE shot_labels ADD COLUMN prompt_hash TEXT")
    conn.execute("UPDATE schema_version SET version = 17")
    conn.commit()


def _migrate_v18_to_v19(conn: sqlite3.Connection) -> None:
    """Motion records scale and rotation, not just translation (v18 -> v19).

    v18 stored `dx`/`dy` and a speed derived from them, which is the whole of
    what a median motion vector can say. It cannot say *zoom*: the vector field
    of a push-in is radial, and its median is (0, 0) — so a shot that ended 45%
    tighter was stored as `speed 0.00, agreement 0.93` and read out as "the
    camera is locked off". Confidently, and wrongly.

    The two new columns are the fitted scale and rotation per shot. Existing
    rows get NULL, which reads correctly as "measured before this was
    recorded" — and, unlike a 0.0 default, does not claim the shot was checked
    for zoom and found to have none.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(shot_motion)")]
    for name in ("zoom", "rotation"):
        if name not in cols:
            conn.execute("ALTER TABLE shot_motion ADD COLUMN %s REAL" % name)
    conn.execute("UPDATE schema_version SET version = 19")
    conn.commit()


def init_db(conn: Optional[sqlite3.Connection] = None) -> sqlite3.Connection:
    if conn is None:
        # `REEL_SCOUT_DATA` defaults to "./data", i.e. relative to wherever the
        # process happens to start. That default is right — a student running
        # reel-scout inside their own project wants ./data to be *their* ./data.
        #
        # What is wrong is that opening a database that does not exist looks
        # exactly like opening one that does. Run a command from the wrong
        # directory and sqlite makes a fresh empty file, every query returns
        # nothing, and the output ("0 targets", "no videos found") is identical
        # to "everything is already up to date". Three separate incidents have
        # been logged against this shape.
        #
        # Creating on demand stays — first run needs it, and 40-odd call sites
        # including the whole test suite depend on it. Only the silence goes.
        fresh = not os.path.exists(config.DB_PATH)
        conn = get_connection()
        if fresh:
            warn(
                "  note: created a NEW empty database at %s\n"
                "        (expected existing data? check the cwd or set REEL_SCOUT_DATA)"
                % os.path.abspath(config.DB_PATH)
            )
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
            current_ver = 2
        if current_ver < 3:
            _migrate_v2_to_v3(conn)
            current_ver = 3
        if current_ver < 4:
            _migrate_v3_to_v4(conn)
            current_ver = 4
        if current_ver < 5:
            _migrate_v4_to_v5(conn)
            current_ver = 5
        if current_ver < 6:
            _migrate_v5_to_v6(conn)
            current_ver = 6
        if current_ver < 7:
            _migrate_v6_to_v7(conn)
            current_ver = 7
        if current_ver < 8:
            _migrate_v7_to_v8(conn)
            current_ver = 8
        if current_ver < 9:
            _migrate_v8_to_v9(conn)
            current_ver = 9
        if current_ver < 10:
            _migrate_v9_to_v10(conn)
            current_ver = 10
        if current_ver < 11:
            _migrate_v10_to_v11(conn)
            current_ver = 11
        if current_ver < 12:
            _migrate_v11_to_v12(conn)
            current_ver = 12
        if current_ver < 13:
            _migrate_v12_to_v13(conn)
            current_ver = 13
        if current_ver < 14:
            _migrate_v13_to_v14(conn)
            current_ver = 14
        if current_ver < 15:
            _migrate_v14_to_v15(conn)
            current_ver = 15
        if current_ver < 16:
            _migrate_v15_to_v16(conn)
            current_ver = 16
        if current_ver < 17:
            _migrate_v16_to_v17(conn)
            current_ver = 17
        if current_ver < 18:
            _migrate_v17_to_v18(conn)
            current_ver = 18
        if current_ver < 19:
            _migrate_v18_to_v19(conn)
    # Fresh installs never run the ladder, so every table added by a migration
    # also needs a CREATE IF NOT EXISTS here -- the same reason audio_events
    # below is repeated.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS shot_motion (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id    TEXT NOT NULL REFERENCES videos(id),
            t_sec       REAL NOT NULL,
            dx          REAL,
            dy          REAL,
            speed       REAL,
            agreement   REAL,
            zoom        REAL,
            rotation    REAL,
            frames      INTEGER NOT NULL,
            movement    TEXT NOT NULL,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(video_id, t_sec)
        );
        CREATE INDEX IF NOT EXISTS idx_shot_motion_video ON shot_motion(video_id);
    """)
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

        CREATE TABLE IF NOT EXISTS scores (
            video_id        TEXT PRIMARY KEY REFERENCES videos(id),
            hook_strength   REAL,
            visual_storytelling REAL,
            pacing          REAL,
            structure       REAL,
            overall         REAL,
            reasoning       TEXT,
            model_used      TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_analyses_content_type ON analyses(content_type);
        CREATE INDEX IF NOT EXISTS idx_analyses_style_format ON analyses(style_format);
        CREATE INDEX IF NOT EXISTS idx_analyses_opening_type ON analyses(opening_type);
        CREATE INDEX IF NOT EXISTS idx_analyses_cta_type ON analyses(cta_type);
        CREATE INDEX IF NOT EXISTS idx_analyses_content_structure ON analyses(content_structure);

        CREATE TABLE IF NOT EXISTS shot_metrics (
            video_id        TEXT PRIMARY KEY REFERENCES videos(id),
            shot_count      INTEGER,
            cuts_per_minute REAL,
            avg_shot_sec    REAL,
            audio_bpm       REAL,
            audio_energy    REAL,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS ocr_captions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id        TEXT REFERENCES videos(id),
            timestamp_sec   REAL,
            text            TEXT,
            engine          TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_ocr_captions_video ON ocr_captions(video_id);

        CREATE TABLE IF NOT EXISTS shots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id        TEXT REFERENCES videos(id),
            idx             INTEGER,
            start_sec       REAL,
            end_sec         REAL,
            dur_sec         REAL,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_shots_video ON shots(video_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shots_video_idx ON shots(video_id, idx);

        CREATE TABLE IF NOT EXISTS shot_labels (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id        TEXT REFERENCES videos(id),
            t_sec           REAL NOT NULL,
            kind            TEXT NOT NULL,
            value           TEXT,
            source          TEXT,
            model           TEXT,
            prompt_hash     TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_shot_labels_video ON shot_labels(video_id, kind);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shot_labels_unique
            ON shot_labels(video_id, kind, t_sec, source);

        CREATE TABLE IF NOT EXISTS translations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id        TEXT REFERENCES videos(id),
            kind            TEXT NOT NULL,
            ref             TEXT NOT NULL DEFAULT '',
            lang            TEXT NOT NULL,
            source_hash     TEXT NOT NULL,
            text            TEXT,
            engine          TEXT,
            model           TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_translations_video
            ON translations(video_id, kind, lang);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_translations_unique
            ON translations(video_id, kind, ref, lang);

        CREATE TABLE IF NOT EXISTS performance (
            video_id        TEXT PRIMARY KEY REFERENCES videos(id),
            views           INTEGER,
            likes           INTEGER,
            comments        INTEGER,
            notes           TEXT,
            recorded_at     TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    return conn


# --- Video CRUD ---

def get_video(conn: sqlite3.Connection, video_id: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,))
    return cur.fetchone()


def get_video_by_url(conn: sqlite3.Connection, url: str) -> Optional[sqlite3.Row]:
    """Find a clip by URL, matching on the identity the URL carries.

    An exact string match is tried first and is what almost every call hits. It
    is not enough on its own, because the URL stored at crawl time is whatever
    the operator pasted, and Instagram's share button appends a tracking
    parameter: one row in a real library is stored as
    `.../reel/DbLQuxQNP3l/?igsh=dmw0ZGYxdms2a3h4`. Paste the same reel's clean
    URL later and an exact match returns nothing -- and "no video found" is
    indistinguishable from "that clip was never crawled". The same miss happens
    for `/reels/` (plural, the share form), the account-scoped
    `instagram.com/<handle>/reel/<code>/`, and a missing trailing slash.

    So on a miss, fall back to the platform's own identity: the crawler that
    owns the URL extracts its id, and that id plus the platform is what the row
    is really keyed on (`_video_id`). Resolution is delegated rather than
    re-derived here -- `crawl` already knows every URL shape each platform
    accepts, and a second copy of those patterns in db.py would be one more
    thing to keep in step.
    """
    cur = conn.execute("SELECT * FROM videos WHERE url = ?", (url,))
    row = cur.fetchone()
    if row is not None:
        return row
    if not url or "://" not in url:
        # Not a URL at all -- an id or prefix. Its own resolver handles that,
        # and running the crawler registry over it would only waste the lookup.
        return None
    try:
        from .crawl import detect_platform, get_crawler

        platform = detect_platform(url)
        if platform is None:
            return None
        platform_id = get_crawler(url).extract_id(url)
    except (ImportError, ValueError, RuntimeError):
        # An unparseable URL is a miss, not a crash: this is a lookup.
        return None
    return conn.execute(
        "SELECT * FROM videos WHERE platform = ? AND platform_id = ?",
        (platform, platform_id)).fetchone()


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

# Twenty high-frequency characters per script, chosen so each one is exclusive:
# it appears in one script's ordinary prose and not the other's. Deliberately
# excluded: 後/后 and 麼/么, whose "simplified" form is also a real traditional
# character (皇后, 幺么) — a detector that fires on those would flag clean
# traditional text. Twenty is decisive on a paragraph and stays readable; a full
# conversion table would be a dependency, and this detects, it does not convert.
_TRAD_ONLY = "這個說時來對開發國學實體會過樣應點總經電"
_SIMP_ONLY = "这个说时来对开发国学实体会过样应点总经电"


def scan_script_mix(text: str) -> Tuple[int, int]:
    """(traditional-only hits, simplified-only hits) in `text`.

    Both non-zero means the file changes script partway through. That happens —
    one 28,331-char transcript flips at 83% with no interleaving — and it is
    invisible: a keyword search for 這 returns the first 83% and no error. Two of
    the 18 Chinese transcripts in the reference library are mixed like this.
    """
    trad = sum(text.count(c) for c in set(_TRAD_ONLY))
    simp = sum(text.count(c) for c in set(_SIMP_ONLY))
    return trad, simp


#: How far past the end of the media a transcript may run before it is worth
#: saying something. Whisper pads to its 30s decode window, so a little
#: overshoot is normal and nagging about it would train people to ignore the
#: warning. Measured on the reference library: 72 of 75 transcripts land within
#: 0.3% of the media duration, and the three that do not overshoot by 149%,
#: 249% and 369%. There is nothing in between.
_OVERRUN_RATIO = 1.1
_OVERRUN_SLACK_SEC = 0.5


def scan_transcript_overrun(segments_json: str, duration_sec: float):
    """(last_end, ratio) when the transcript runs past the media, else None.

    Whisper on speech-less audio does one of two things: it returns nothing, or
    it invents. In this library it returned nothing 23 times (all tagged `nn` --
    Norwegian Nynorsk, which is what Whisper guesses at silence) and invented 3
    times, and only the second kind reaches the database. The invented ones are
    recognisable without knowing the phrase: they claim to describe more media
    than exists.

    Detect, do not truncate -- same reason `scan_script_mix` does not convert.
    Clamping the timestamps would leave a plausible-looking transcript of a clip
    that has no speech in it, which is the harm, not the timestamps.
    """
    if not segments_json or not duration_sec or duration_sec <= 0:
        return None
    try:
        segs = json.loads(segments_json)
    except (ValueError, TypeError):
        return None
    if not segs:
        return None
    try:
        last_end = max(float(x.get("end", 0) or 0) for x in segs)
    except (AttributeError, TypeError, ValueError):
        return None
    if last_end <= duration_sec * _OVERRUN_RATIO + _OVERRUN_SLACK_SEC:
        return None
    return last_end, last_end / duration_sec


_script_mix_warned = set()


def warn_script_mix(text: str, where: str) -> None:
    """Report traditional/simplified mixing in generated text.

    `save_transcript` has watched transcripts for this since it was written, but
    the VLM and LLM write into the same database and were never looked at: 28
    frame descriptions, 2 object lists and 2 analysis blobs in the reference
    library carry simplified characters. Descriptions feed search, so a
    simplified one is a description that will not be found by a search typed the
    way everything else in the library is written.

    Deliberately NOT applied to `text_in_frame` or `ocr_captions`: that text is
    evidence of what the video actually showed. Flagging it as a mix would be
    treating the video's own choice as a defect.

    Warns once per surface per process. 12,582 description fields is not a
    thing to print 28 warnings about, and the second one adds nothing the first
    did not say.
    """
    if not text or where in _script_mix_warned:
        return
    trad, simp = scan_script_mix(text)
    if trad and simp:
        _script_mix_warned.add(where)
        warn("  ⚠️  %s mixes traditional and simplified (%d / %d) — generated "
             "text, not transcribed speech, so this is the model ignoring the "
             "prompt rather than the speaker switching. Reported once per run."
             % (where, trad, simp))


def save_transcript(
    conn: sqlite3.Connection,
    video_id: str,
    language: str,
    text_full: str,
    segments_json: str,
    whisper_model: str,
    duration_sec: float,
) -> None:
    # Detect, do not convert. Normalising would mean picking traditional or
    # simplified for every downstream consumer — a product decision, and one that
    # needs opencc, which core install deliberately does not carry. Reporting the
    # mix costs nothing and removes the part that actually hurts: not knowing.
    #
    # Not gated on `language`: that field is wrong often enough to matter (one
    # library clip is tagged zh with an English transcript), and gating on it
    # would skip exactly the mislabelled files this is meant to catch. Text with
    # no Chinese in it scores (0, 0) and says nothing, so the gate bought only
    # the illusion of one.
    #
    # Scanned before the write, reported after it. Anything that runs between
    # "we have the transcript" and "the transcript is stored" can lose the row,
    # and a detector that loses the data it was watching is worse than no
    # detector — see utils.stderr.warn for the console-encoding case that made
    # this concrete rather than theoretical.
    # A transcript that ends after the media does is describing audio that is
    # not there. Scanned here rather than in the backend because this is where
    # both halves are in scope: the segments and the measured duration.
    overrun = scan_transcript_overrun(segments_json, duration_sec)

    notice = None
    if text_full:
        trad, simp = scan_script_mix(text_full)
        if trad and simp:
            minor = min(trad, simp)
            total = trad + simp
            notice = (
                "  ⚠️  transcript mixes traditional and simplified "
                "(%d / %d, minority %.0f%%) — a keyword search will silently "
                "miss whichever half it is not written in" % (trad, simp, 100.0 * minor / total)
            )
    conn.execute(
        """INSERT OR REPLACE INTO transcripts
           (video_id, language, text_full, segments_json, whisper_model, duration_sec)
           VALUES (?,?,?,?,?,?)""",
        (video_id, language, text_full, segments_json, whisper_model, duration_sec),
    )
    update_video_status(conn, video_id, "transcribed")
    conn.commit()
    if notice:
        warn(notice)
    if overrun:
        last_end, ratio = overrun
        warn("  ⚠️  transcript runs %.1fs past a %.1fs file (%.0f%% of it) — "
             "Whisper hallucinating over speech-less audio looks exactly like "
             "this, and whisper-guard does not catch it. Check before trusting "
             "the text." % (last_end - duration_sec, duration_sec, 100.0 * ratio))


def get_transcript(conn: sqlite3.Connection, video_id: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM transcripts WHERE video_id = ?", (video_id,))
    return cur.fetchone()


# --- Keyframe CRUD ---

SELECTOR_VERSION = 1
"""Bumped when the sampling itself changes, not when the code around it moves.

0 is everything extracted before the detection pass stopped capping itself at
the frame budget -- those runs only ever looked at a clip's opening, so
"selector_version = 0" is the machine-checkable way to ask what still needs
re-sampling.
"""

_CURRENT_RUN = (
    "(k.run_id IS NULL OR k.run_id IN "
    " (SELECT id FROM keyframe_runs WHERE superseded_at IS NULL))"
)
# `run_id IS NULL` counts as current on purpose: any writer that does not know
# about runs still gets its frames read back. Losing rows to a filter they never
# opted into would be a silent hole, which is the shape of bug this package
# keeps paying for.

#: Whether the frame a `shot_labels` row (aliased `l`) hangs off still exists.
#:
#: 🔴 **Orphans are real, and deleting them was the wrong fix.** A label hangs
#: off a timestamp rather than a shot id *precisely* so that re-analysis cannot
#: destroy it — a decision this repo wrote down and a test guards. But
#: `--force-keyframes` moves the timestamps, and a label left behind by that is
#: unreachable: nothing will ever ask about 2.0s again once the frame moved to
#: 2.5s. Unreachable is not the same as wrong. So the row stays, and every
#: reader that would otherwise mistake it for a live reading asks this instead.
LIVE_FRAME_SQL = (
    "EXISTS (SELECT 1 FROM keyframes k WHERE k.video_id = l.video_id "
    "        AND k.timestamp_sec = l.t_sec AND " + _CURRENT_RUN + ")"
)


def begin_keyframe_run(
    conn: sqlite3.Connection,
    video_id: str,
    strategy: str,
    requested_max: int,
    selector_version: int = SELECTOR_VERSION,
) -> int:
    """Open a run and return its id. Supersedes nothing yet -- see commit."""
    cur = conn.execute(
        "INSERT INTO keyframe_runs (video_id, strategy, requested_max, selector_version) "
        "VALUES (?,?,?,?)",
        (video_id, strategy, requested_max, selector_version),
    )
    conn.commit()
    return cur.lastrowid


def commit_keyframe_run(conn: sqlite3.Connection, run_id: int, video_id: str) -> None:
    """Make `run_id` the current one, superseding this video's earlier runs.

    Deliberately separate from `begin_keyframe_run` and called only once frames
    are actually on disk. Superseding first would mean an extraction that dies
    halfway leaves the video with no current run at all -- old evidence retired
    before new evidence exists, which is worse than either state on its own.
    """
    conn.execute(
        "UPDATE keyframe_runs SET superseded_at = datetime('now') "
        "WHERE video_id = ? AND id != ? AND superseded_at IS NULL",
        (video_id, run_id),
    )
    conn.commit()


def current_keyframe_run(conn: sqlite3.Connection, video_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM keyframe_runs WHERE video_id = ? AND superseded_at IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (video_id,),
    ).fetchone()


def save_keyframes(
    conn: sqlite3.Connection,
    video_id: str,
    keyframes: List[Dict[str, Any]],
    run_id: Optional[int] = None,
) -> List[int]:
    ids = []
    for kf in keyframes:
        cur = conn.execute(
            """INSERT INTO keyframes (video_id, run_id, frame_index, timestamp_sec,
                                      file_path, strategy)
               VALUES (?,?,?,?,?,?)""",
            (video_id, run_id, kf["frame_index"], kf["timestamp_sec"],
             kf["file_path"], kf["strategy"]),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def get_keyframes(
    conn: sqlite3.Connection, video_id: str, include_superseded: bool = False
) -> List[sqlite3.Row]:
    sql = "SELECT k.* FROM keyframes k WHERE k.video_id = ?"
    if not include_superseded:
        sql += " AND " + _CURRENT_RUN
    return conn.execute(sql + " ORDER BY k.timestamp_sec", (video_id,)).fetchall()


def get_keyframes_with_descriptions(
    conn: sqlite3.Connection, video_id: str
) -> List[sqlite3.Row]:
    """Keyframes joined with their vision descriptions (LEFT JOIN, so frames
    without a description still appear), ordered by timestamp. Used by the
    read-only viewer to render each frame + what the VLM saw in it."""
    return conn.execute(
        """SELECT k.id, k.frame_index, k.timestamp_sec, k.file_path, k.strategy,
                  vd.description, vd.text_in_frame, vd.objects_json
           FROM keyframes k
           LEFT JOIN vision_descriptions vd ON vd.keyframe_id = k.id
           WHERE k.video_id = ? AND """ + _CURRENT_RUN + """
           ORDER BY k.timestamp_sec""",
        (video_id,),
    ).fetchall()


def get_described_keyframe_ids(conn: sqlite3.Connection, video_id: str) -> set:
    """Keyframe ids for this video that already have a vision description.
    Used to backfill only the missing frames on re-run."""
    rows = conn.execute(
        """SELECT vd.keyframe_id FROM vision_descriptions vd
           JOIN keyframes k ON k.id = vd.keyframe_id
           WHERE k.video_id = ? AND """ + _CURRENT_RUN,
        (video_id,),
    ).fetchall()
    return {r[0] for r in rows}


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
    # `text_in_frame` is excluded on purpose -- see warn_script_mix.
    warn_script_mix(description, "frame description")


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


# --- Shot Metrics CRUD (§4E measured pacing) ---

def save_shot_metrics(
    conn: sqlite3.Connection,
    video_id: str,
    shot_count: Optional[int] = None,
    cuts_per_minute: Optional[float] = None,
    avg_shot_sec: Optional[float] = None,
    audio_bpm: Optional[float] = None,
    audio_energy: Optional[float] = None,
) -> None:
    """Insert or replace the measured pacing metrics for a video."""
    conn.execute(
        """INSERT OR REPLACE INTO shot_metrics
           (video_id, shot_count, cuts_per_minute, avg_shot_sec, audio_bpm, audio_energy)
           VALUES (?,?,?,?,?,?)""",
        (video_id, shot_count, cuts_per_minute, avg_shot_sec, audio_bpm, audio_energy),
    )
    conn.commit()


def get_shot_metrics(conn: sqlite3.Connection, video_id: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM shot_metrics WHERE video_id = ?", (video_id,))
    return cur.fetchone()


def save_shots(conn: sqlite3.Connection, video_id: str, shots: List[Any]) -> int:
    """Replace this clip's shot table. Returns the number of rows written.

    Replace, not append: a second analyze pass measures the same clip again, and
    appending would leave two overlapping partitions behind one `video_id` with
    nothing marking which is current. The delete and the insert share one
    transaction so a crash between them cannot leave the clip with no shots
    while `shot_metrics` still claims a count.

    An empty list clears the table for that clip and is a legitimate call: a
    clip whose duration could not be probed has no partition, and saying so
    beats leaving a stale one from a previous run.
    """
    rows = [
        (video_id, s.index, s.start_sec, s.end_sec, s.dur_sec)
        for s in shots
    ]
    with conn:
        conn.execute("DELETE FROM shots WHERE video_id = ?", (video_id,))
        if rows:
            conn.executemany(
                "INSERT INTO shots (video_id, idx, start_sec, end_sec, dur_sec) "
                "VALUES (?,?,?,?,?)",
                rows,
            )
    return len(rows)


def get_shots(conn: sqlite3.Connection, video_id: str) -> List[sqlite3.Row]:
    """This clip's shots in clip order. Empty list when none were stored —
    which is the honest answer for anything analyzed before schema v14."""
    cur = conn.execute(
        "SELECT * FROM shots WHERE video_id = ? ORDER BY idx", (video_id,)
    )
    return cur.fetchall()

def save_shot_motion(conn: sqlite3.Connection, video_id: str,
                     rows: list) -> int:
    """Replace this clip's motion rows with `rows`, returning how many landed.

    Replace, not append: motion is derived wholly from the current shot spans,
    so a re-run with different spans must not leave the old rows beside the new
    ones. `shot_labels` appends because a label can come from several sources
    that coexist; this has exactly one source.
    """
    conn.execute("DELETE FROM shot_motion WHERE video_id = ?", (video_id,))
    for r in rows:
        conn.execute(
            "INSERT INTO shot_motion (video_id, t_sec, dx, dy, speed, "
            "agreement, zoom, rotation, frames, movement) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (video_id, r["t_sec"], r["dx"], r["dy"], r["speed"],
             r["agreement"], r.get("zoom"), r.get("rotation"),
             r["frames"], r["movement"]))
    conn.commit()
    return len(rows)


def get_shot_motion(conn: sqlite3.Connection, video_id: str) -> list:
    """This clip's motion rows in time order."""
    return conn.execute(
        "SELECT * FROM shot_motion WHERE video_id = ? ORDER BY t_sec",
        (video_id,)).fetchall()


def save_shot_label(conn: sqlite3.Connection, video_id: str, t_sec: float,
                    kind: str, value: Optional[str], source: str,
                    model: Optional[str] = None,
                    prompt_hash: Optional[str] = None,
                    supersedes: Tuple[str, ...] = ()) -> int:
    """Upsert one label. Keyed by (video, kind, timestamp, source) so a VLM pass
    and a human correction sit side by side instead of clobbering each other.

    `supersedes` names sources this write *replaces* rather than sits beside,
    and returns how many rows that removed. Side-by-side is the right default
    for a person's correction and a model's guess -- they are different kinds
    of claim. It is the wrong default for two answers from the same asking:
    one frame gets one verdict per producer, and a producer that answers a
    frame a second way has changed its mind, not added a second opinion.
    Caller supplies the sources because which ones are one producer is
    vocabulary, and vocabulary does not live in here.

    `prompt_hash` travels with the value because the prompt moves the answer as
    much as the model does — measured, ten-to-one on the same images."""
    removed = 0
    with conn:
        others = tuple(x for x in supersedes if x != source)
        if others:
            cur = conn.execute(
                "DELETE FROM shot_labels WHERE video_id = ? AND kind = ? "
                "AND t_sec = ? AND source IN (%s)"
                % ",".join("?" * len(others)),
                (video_id, kind, float(t_sec)) + others)
            removed = cur.rowcount or 0
        conn.execute(
            "INSERT INTO shot_labels (video_id, t_sec, kind, value, source, model, "
            "prompt_hash) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(video_id, kind, t_sec, source) DO UPDATE SET "
            "value = excluded.value, model = excluded.model, "
            "prompt_hash = excluded.prompt_hash, created_at = datetime('now')",
            (video_id, float(t_sec), kind, value, source, model, prompt_hash),
        )
    return removed


def get_shot_labels(conn: sqlite3.Connection, video_id: str,
                    kind: Optional[str] = None) -> List[sqlite3.Row]:
    """Labels for a clip, in time order. Every row carries its own source and
    model — a caller choosing between two labels for the same moment has to be
    able to see which produced which.

    The `source` in the ORDER BY is a tie-break for stable output, **not** a
    precedence. A caller that wants the person's call to win has to say so;
    reading it off the alphabet worked only as long as the sources happened to
    sort the right way, and `gate` sorts before `supplied`."""
    sql = "SELECT * FROM shot_labels WHERE video_id = ?"
    params: List[Any] = [video_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    return conn.execute(sql + " ORDER BY t_sec, source", params).fetchall()


def live_label_timestamps(conn: sqlite3.Connection, video_id: str) -> set:
    """Timestamps of this clip's labels whose keyframe still exists.

    Its own query rather than a column on :func:`get_shot_labels`, because a
    generic accessor that joins `keyframes` stops working on a database where
    that table has not been created yet -- which a migration test does, and
    which is a coupling worth not having for one caller's convenience.
    """
    rows = conn.execute(
        "SELECT DISTINCT l.t_sec FROM shot_labels l WHERE l.video_id = ? "
        "AND " + LIVE_FRAME_SQL, (video_id,)).fetchall()
    return {r["t_sec"] for r in rows}

def source_fingerprint(text: Optional[str]) -> str:
    """Stable fingerprint of the text a translation was made from."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def save_translation(conn: sqlite3.Connection, video_id: str, kind: str, ref: str,
                     lang: str, source_text: Optional[str], text: Optional[str],
                     engine: str, model: Optional[str] = None) -> None:
    """Upsert one translation, fingerprinting the source it was made from."""
    with conn:
        conn.execute(
            "INSERT INTO translations (video_id, kind, ref, lang, source_hash, "
            "text, engine, model) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(video_id, kind, ref, lang) DO UPDATE SET "
            "source_hash = excluded.source_hash, text = excluded.text, "
            "engine = excluded.engine, model = excluded.model, "
            "created_at = datetime('now')",
            (video_id, kind, str(ref), lang, source_fingerprint(source_text),
             text, engine, model),
        )


def get_translations(conn: sqlite3.Connection, video_id: str,
                     kind: Optional[str] = None,
                     lang: str = "zh") -> List[sqlite3.Row]:
    sql = "SELECT * FROM translations WHERE video_id = ? AND lang = ?"
    params: List[Any] = [video_id, lang]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    return conn.execute(sql + " ORDER BY kind, ref", params).fetchall()




# --- OCR Captions CRUD (§4F on-screen text / L3.5) ---

def save_ocr_captions(
    conn: sqlite3.Connection,
    video_id: str,
    captions: List[Dict[str, Any]],
) -> None:
    """Replace the on-screen captions for a video (idempotent re-run)."""
    conn.execute("DELETE FROM ocr_captions WHERE video_id = ?", (video_id,))
    for c in captions:
        conn.execute(
            """INSERT INTO ocr_captions (video_id, timestamp_sec, text, engine)
               VALUES (?,?,?,?)""",
            (video_id, c.get("timestamp_sec"), c.get("text", ""), c.get("engine", "")),
        )
    conn.commit()


def get_ocr_captions(conn: sqlite3.Connection, video_id: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ocr_captions WHERE video_id = ? ORDER BY timestamp_sec",
        (video_id,),
    ).fetchall()


# --- Performance CRUD (roadmap 4D: my own video's actual metrics) ---

def save_performance(
    conn: sqlite3.Connection,
    video_id: str,
    views: Optional[int] = None,
    likes: Optional[int] = None,
    comments: Optional[int] = None,
    notes: Optional[str] = None,
) -> None:
    # Upsert that PRESERVES fields you don't pass this call (COALESCE), so
    # `track --views 1000` then `track --likes 50` keeps both, rather than the
    # second call wiping views back to NULL (INSERT OR REPLACE would).
    if get_performance(conn, video_id) is not None:
        conn.execute(
            """UPDATE performance SET
               views=COALESCE(?, views), likes=COALESCE(?, likes),
               comments=COALESCE(?, comments), notes=COALESCE(?, notes),
               recorded_at=datetime('now') WHERE video_id=?""",
            (views, likes, comments, notes, video_id),
        )
    else:
        conn.execute(
            """INSERT INTO performance (video_id, views, likes, comments, notes)
               VALUES (?,?,?,?,?)""",
            (video_id, views, likes, comments, notes),
        )
    conn.commit()


def get_performance(conn: sqlite3.Connection, video_id: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM performance WHERE video_id = ?", (video_id,))
    return cur.fetchone()


# --- Score CRUD ---

def save_score(
    conn: sqlite3.Connection,
    video_id: str,
    score: Any,
) -> None:
    """Insert or replace a video score. Accepts a VideoScore dataclass."""
    conn.execute(
        """INSERT OR REPLACE INTO scores
           (video_id, hook_strength, visual_storytelling, pacing,
            structure, overall, reasoning, model_used)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            video_id,
            score.hook_strength,
            score.visual_storytelling,
            score.pacing,
            score.structure,
            score.overall,
            score.reasoning,
            score.model_used,
        ),
    )
    conn.commit()


def get_score(conn: sqlite3.Connection, video_id: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM scores WHERE video_id = ?", (video_id,))
    return cur.fetchone()


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
    # Derive the normalized tag columns from full_json (the source of truth) so
    # callers stay unchanged and the columns can never drift from the blob.
    try:
        data = json.loads(full_json) if full_json else {}
    except (ValueError, TypeError):
        data = {}
    tags = _extract_tag_columns(data)
    warn_script_mix(summary, "analysis summary")
    conn.execute(
        """INSERT OR REPLACE INTO analyses
           (video_id, summary, topics_json, hooks_json, style_json,
            engagement_signals_json, full_json,
            content_type, opening_type, cta_type, style_format, style_pacing,
            emotion, content_structure)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (video_id, summary, topics_json, hooks_json, style_json,
         engagement_signals_json, full_json,
         tags["content_type"], tags["opening_type"], tags["cta_type"],
         tags["style_format"], tags["style_pacing"], tags["emotion"],
         tags["content_structure"]),
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
    for table in ["videos", "transcripts", "keyframes", "vision_descriptions", "analyses", "audio_events", "shot_metrics", "ocr_captions", "performance", "scores", "batches"]:
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


def set_batch_meta(conn: sqlite3.Connection, batch_id: str, **fields: Any) -> None:
    """Set mode / out_root / pid / status on a batch row."""
    allowed = ("mode", "out_root", "pid", "status")
    sets = [(k, v) for k, v in fields.items() if k in allowed]
    if not sets:
        return
    conn.execute(
        "UPDATE batches SET %s, updated_at = datetime('now') WHERE id = ?"
        % ", ".join("%s = ?" % k for k, _ in sets),
        [v for _, v in sets] + [batch_id],
    )
    conn.commit()


def set_batch_item_progress(conn: sqlite3.Connection, batch_id: str, url: str,
                            **fields: Any) -> None:
    """Move one item along without touching the batch counters.

    Separate from update_batch_item on purpose: that one increments
    batches.completed / failed on every call, so routing intermediate states
    through it would inflate the counters by however many stages an item passes
    through. update_batch_item stays the terminal transition, called once.
    """
    allowed = ("status", "label", "slug", "video_id", "bundle_dir", "error_message")
    sets = [(k, v) for k, v in fields.items() if k in allowed]
    if not sets:
        return
    conn.execute(
        "UPDATE batch_items SET %s WHERE batch_id = ? AND url = ?"
        % ", ".join("%s = ?" % k for k, _ in sets),
        [v for _, v in sets] + [batch_id, url],
    )
    conn.commit()


def touch_batch_heartbeat(conn: sqlite3.Connection, batch_id: str) -> None:
    """Say the worker is still alive.

    The only way to tell 'running' from 'the process died and took the job with
    it': there is no shutdown hook, so a killed worker leaves status='running'
    forever unless something notices the clock.
    """
    conn.execute(
        "UPDATE batches SET heartbeat_at = datetime('now'), "
        "updated_at = datetime('now') WHERE id = ?", (batch_id,))
    conn.commit()


def request_batch_cancel(conn: sqlite3.Connection, batch_id: str) -> None:
    conn.execute(
        "UPDATE batches SET cancel_requested = 1, updated_at = datetime('now') "
        "WHERE id = ?", (batch_id,))
    conn.commit()


def batch_cancel_requested(conn: sqlite3.Connection, batch_id: str) -> bool:
    row = conn.execute(
        "SELECT cancel_requested FROM batches WHERE id = ?", (batch_id,)).fetchone()
    return bool(row and row[0])


def get_batch(conn: sqlite3.Connection, batch_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()


def get_batch_items(conn: sqlite3.Connection, batch_id: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batch_items WHERE batch_id = ? ORDER BY rowid", (batch_id,)
    ).fetchall()


def get_latest_batch(conn: sqlite3.Connection,
                     source: Optional[str] = None) -> Optional[sqlite3.Row]:
    """Newest batch, optionally restricted to one source.

    So a caller that has lost the batch_id across a long conversation -- the
    situation a background runner exists for -- can still ask about it.
    """
    if source:
        return conn.execute(
            "SELECT * FROM batches WHERE source = ? ORDER BY created_at DESC, rowid DESC "
            "LIMIT 1", (source,)).fetchone()
    return conn.execute(
        "SELECT * FROM batches ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()


# --- Annotation CRUD (note / group / star) ---
# Storage primitives only. Name resolution, validation and the shape the CLI /
# MCP / HTTP surfaces agree on live in annotate.py, so all three write the same
# way and there is one place to change the rules.

def list_groups(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT g.*, ("
        "  SELECT COUNT(*) FROM video_annotations a WHERE a.group_id = g.id"
        ") AS video_count "
        "FROM annotation_groups g ORDER BY g.sort_order, g.name"
    ).fetchall()


def get_group(conn: sqlite3.Connection, group_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM annotation_groups WHERE id = ?", (group_id,)).fetchone()


def get_group_by_name(conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM annotation_groups WHERE name = ? COLLATE NOCASE",
        (name,)).fetchone()


def create_group(conn: sqlite3.Connection, name: str,
                 sort_order: int = 0) -> sqlite3.Row:
    conn.execute(
        "INSERT INTO annotation_groups (name, sort_order) VALUES (?, ?)",
        (name, sort_order))
    conn.commit()
    row = get_group_by_name(conn, name)
    assert row is not None  # just inserted
    return row


def rename_group(conn: sqlite3.Connection, group_id: int, name: str) -> bool:
    cur = conn.execute(
        "UPDATE annotation_groups SET name = ? WHERE id = ?", (name, group_id))
    conn.commit()
    return cur.rowcount > 0


def delete_group(conn: sqlite3.Connection, group_id: int) -> bool:
    """Drop a group. Rows filed under it keep their note and star; only the
    grouping is cleared (the FK is ON DELETE SET NULL). Done explicitly as well,
    because PRAGMA foreign_keys is per-connection and a caller may have it off."""
    conn.execute(
        "UPDATE video_annotations SET group_id = NULL WHERE group_id = ?", (group_id,))
    cur = conn.execute("DELETE FROM annotation_groups WHERE id = ?", (group_id,))
    conn.commit()
    return cur.rowcount > 0


def get_annotation(conn: sqlite3.Connection, video_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM video_annotations WHERE video_id = ?", (video_id,)).fetchone()


def list_annotations(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """Every annotation, keyed by video_id, with the group name joined in.

    One query for the whole library: the list page needs a row per video and
    would otherwise issue N+1 of them.
    """
    rows = conn.execute(
        "SELECT a.video_id, a.note, a.group_id, a.starred, a.updated_at, "
        "       g.name AS group_name "
        "FROM video_annotations a "
        "LEFT JOIN annotation_groups g ON g.id = a.group_id"
    ).fetchall()
    return {r["video_id"]: dict(r) for r in rows}


def set_annotation(conn: sqlite3.Connection, video_id: str,
                   note: Optional[str] = None,
                   group_id: Optional[int] = None,
                   starred: Optional[bool] = None,
                   clear_group: bool = False) -> Dict[str, Any]:
    """Upsert one video's annotation. Only the fields passed are written.

    `None` means "leave alone", which is why clearing the group needs its own
    flag — otherwise there would be no way to express "file this under nothing"
    through the same call that leaves the note untouched.
    """
    existing = get_annotation(conn, video_id)
    if existing is None:
        conn.execute(
            "INSERT INTO video_annotations (video_id, note, group_id, starred) "
            "VALUES (?, ?, ?, ?)",
            (video_id, note, None if clear_group else group_id,
             1 if starred else 0))
    else:
        sets, params = [], []  # type: List[str], List[Any]
        if note is not None:
            sets.append("note = ?")
            params.append(note)
        if clear_group:
            sets.append("group_id = NULL")
        elif group_id is not None:
            sets.append("group_id = ?")
            params.append(group_id)
        if starred is not None:
            sets.append("starred = ?")
            params.append(1 if starred else 0)
        if sets:
            sets.append("updated_at = datetime('now')")
            params.append(video_id)
            conn.execute(
                "UPDATE video_annotations SET %s WHERE video_id = ?" % ", ".join(sets),
                params)
    conn.commit()
    row = get_annotation(conn, video_id)
    return dict(row) if row else {}


# --- Clip marks (see the v13 migration) ---

def add_clip_mark(conn: sqlite3.Connection, video_id: str, t_sec: float,
                  label: str, note: Optional[str] = None,
                  source: str = "manual") -> Dict[str, Any]:
    cur = conn.execute(
        "INSERT INTO clip_marks (video_id, t_sec, label, note, source) "
        "VALUES (?, ?, ?, ?, ?)",
        (video_id, float(t_sec), label, note, source))
    conn.commit()
    row = conn.execute("SELECT * FROM clip_marks WHERE id = ?",
                       (cur.lastrowid,)).fetchone()
    return dict(row) if row else {}


def list_clip_marks(conn: sqlite3.Connection, video_id: str,
                    source: Optional[str] = None) -> List[sqlite3.Row]:
    """One clip's marks in time order — the order every reader wants them in.

    `id` breaks ties so two marks on the same second do not swap places between
    calls; a list that reorders itself looks like the data changed.
    """
    sql = "SELECT * FROM clip_marks WHERE video_id = ?"
    params: List[Any] = [video_id]
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    return conn.execute(sql + " ORDER BY t_sec, id", params).fetchall()


def delete_clip_mark(conn: sqlite3.Connection, mark_id: int) -> bool:
    cur = conn.execute("DELETE FROM clip_marks WHERE id = ?", (int(mark_id),))
    conn.commit()
    return cur.rowcount > 0


def delete_clip_marks(conn: sqlite3.Connection, video_id: str,
                      source: Optional[str] = None) -> int:
    """Delete a clip's marks, optionally only those from one source.

    `source=None` means every mark on the clip. Callers that mean "just the ones
    I wrote" must say so — see `marks.import_marks`, where getting this wrong
    would let a re-import take someone's hand-typed marks with it.
    """
    if source is None:
        cur = conn.execute("DELETE FROM clip_marks WHERE video_id = ?", (video_id,))
    else:
        cur = conn.execute("DELETE FROM clip_marks WHERE video_id = ? AND source = ?",
                           (video_id, source))
    conn.commit()
    return cur.rowcount


def normalize_media_paths(
    conn: sqlite3.Connection, dry_run: bool = False
) -> "Tuple[int, List[str]]":
    """Rewrite stored media paths into the portable data-root-relative form.

    A live database holds both shapes — ``./data/videos/x.mp4`` written when the
    process ran from the repo root, and absolute paths written from anywhere
    else. Reads resolve both, so this is cleanup rather than a prerequisite.

    Rows whose path resolves to nothing are counted and returned but never
    rewritten: rewriting a path we cannot verify turns a recoverable row into a
    confidently wrong one.
    """
    from .utils import paths as media_paths

    changed = 0
    missing: List[str] = []

    for table in ("videos", "keyframes"):
        rows = conn.execute(
            "SELECT id, file_path FROM %s "  # noqa: S608 - table names are hardcoded
            "WHERE file_path IS NOT NULL AND file_path != ''" % table
        ).fetchall()
        for row in rows:
            stored = row["file_path"]
            if not media_paths.exists(stored):
                missing.append("%s:%s -> %s" % (table, row["id"], stored))
                continue
            portable = media_paths.to_storage_path(
                media_paths.resolve_media_path(stored)
            )
            if portable == stored:
                continue
            changed += 1
            if not dry_run:
                conn.execute(
                    "UPDATE %s SET file_path = ? WHERE id = ?" % table,  # noqa: S608
                    (portable, row["id"]),
                )

    if not dry_run:
        conn.commit()
    return changed, missing
