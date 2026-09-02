"""Fill in shot tables for clips analyzed before schema v14.

`shots` is an additive migration: a clip measured before v14 carries its cut
*count* in `shot_metrics` and no spans at all, and only a fresh measurement
fills them. Re-running `analyze` would do it, but at the cost of redoing
transcription, vision and the merge — and re-running the merge would change
analyses that are already correct. This pass touches nothing but `shots`.

It is cheap: the scene-detect pass decodes without writing frames, measured at
~63× realtime on an M2 Max, so six hours of footage costs about six minutes.

A clip whose media is no longer on disk is **skipped, not failed**. Those two
are different states — one is "this machine cannot answer", the other is "the
measurement was attempted and did not work" — and reporting them together is
how a library ends up looking broken when it is merely incomplete.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional

from . import db
from .shots import compute_shot_table


def candidates(conn) -> list:
    """Analyzed clips with a duration but no spans, oldest schema first."""
    return conn.execute(
        "SELECT v.id, v.file_path, v.duration_sec, v.title FROM videos v "
        "WHERE v.status = 'analyzed' AND v.duration_sec > 0 "
        "AND NOT EXISTS (SELECT 1 FROM shots s WHERE s.video_id = v.id) "
        "ORDER BY v.duration_sec"
    ).fetchall()


def backfill(conn, dry_run: bool = False, limit: Optional[int] = None,
             progress: bool = False) -> Dict[str, Any]:
    rows = candidates(conn)
    total = len(rows)
    if limit:
        rows = rows[:limit]
    out = {"candidates": total, "filled": 0, "skipped_no_media": 0, "failed": 0}
    for i, r in enumerate(rows, 1):
        path = r["file_path"]
        if not path or not os.path.exists(path):
            out["skipped_no_media"] += 1
            continue
        if dry_run:
            out["filled"] += 1
            continue
        table = compute_shot_table(path, duration_sec=r["duration_sec"])
        if not table:
            # compute_shot_table returns None on an unreadable file or a failed
            # pass. Counting that as a fill would put an empty partition behind
            # a clip that was never measured.
            out["failed"] += 1
            continue
        _metrics, spans = table
        db.save_shots(conn, r["id"], spans)
        out["filled"] += 1
        if progress and i % 20 == 0:
            print("  ...%d/%d" % (i, len(rows)), file=sys.stderr, flush=True)
    return out
