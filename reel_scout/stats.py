"""Corpus statistics (roadmap 3D).

Aggregates the normalized tag columns (3C) and the craft scores across the
analyzed library — tag distributions + score avg/min/max — optionally scoped to
one channel. Pure DB read path (no crawler, no LLM), like compare.py.

Channel scoping keys on the free-text `videos.uploader` (there is no channel
table / id), so `--channel` is a substring match, not an exact key.
"""
from __future__ import annotations

import csv as _csv
from typing import Any, Dict, List, Optional, Tuple

from . import db, validity

#: Rows whose media never really processed are dropped from every aggregate
#: here. They are not low scores — they are non-measurements that happen to be
#: shaped like measurements, and a 0.0 that means "the run died" averages in
#: exactly like a 0.0 that means "this video is bad". The count is reported
#: rather than silently swallowed, because a corpus quietly getting smaller is
#: the same class of defect this guard exists to stop.
_NOT_INVALID = validity.exclude_sql("v")

# Normalized enum columns on `analyses` (added in schema v5/v6).
TAG_COLUMNS = [
    "content_type", "content_structure", "style_format",
    "style_pacing", "opening_type", "cta_type", "emotion",
]
# Numeric craft dimensions on `scores`.
SCORE_COLUMNS = ["overall", "hook_strength", "visual_storytelling", "pacing", "structure"]


def _channel_clause(channel: Optional[str]) -> Tuple[str, List[Any]]:
    if channel:
        return " AND v.uploader LIKE ?", ["%" + channel + "%"]
    return "", []


def compute_stats(conn: db.sqlite3.Connection, channel: Optional[str] = None) -> Dict[str, Any]:
    where, params = _channel_clause(channel)

    total = conn.execute(
        "SELECT COUNT(*) FROM videos v WHERE 1=1" + where + _NOT_INVALID, params
    ).fetchone()[0]
    analyzed = conn.execute(
        "SELECT COUNT(*) FROM analyses a JOIN videos v ON a.video_id = v.id "
        "WHERE 1=1" + where + _NOT_INVALID, params
    ).fetchone()[0]
    excluded_invalid = conn.execute(
        "SELECT COUNT(*) FROM videos v WHERE v.status = ?" + where,
        [validity.INVALID_STATUS] + params
    ).fetchone()[0]

    # Tag distributions. Column names come from the hardcoded TAG_COLUMNS list
    # (never user input); the channel value is bound as a parameter.
    tag_distributions: Dict[str, Dict[str, int]] = {}
    for col in TAG_COLUMNS:
        rows = conn.execute(
            "SELECT a.{c} AS val, COUNT(*) AS cnt FROM analyses a "
            "JOIN videos v ON a.video_id = v.id "
            "WHERE a.{c} IS NOT NULL{w}{x} GROUP BY a.{c} ORDER BY cnt DESC, val".format(
                c=col, w=where, x=_NOT_INVALID),
            params,
        ).fetchall()
        tag_distributions[col] = {r["val"]: r["cnt"] for r in rows}

    score_aggregates: Dict[str, Dict[str, Any]] = {}
    for col in SCORE_COLUMNS:
        row = conn.execute(
            "SELECT AVG(s.{c}) AS avg, MIN(s.{c}) AS min, MAX(s.{c}) AS max, "
            "COUNT(s.{c}) AS cnt FROM scores s JOIN videos v ON s.video_id = v.id "
            "WHERE s.{c} IS NOT NULL{w}{x}".format(c=col, w=where, x=_NOT_INVALID),
            params,
        ).fetchone()
        score_aggregates[col] = {
            "avg": round(row["avg"], 2) if row["avg"] is not None else None,
            "min": row["min"],
            "max": row["max"],
            "count": row["cnt"],
        }

    return {
        "channel": channel,
        "total_videos": total,
        "analyzed_videos": analyzed,
        "excluded_invalid": excluded_invalid,
        "tag_distributions": tag_distributions,
        "score_aggregates": score_aggregates,
    }


def format_stats(stats: Dict[str, Any]) -> str:
    lines: List[str] = []
    scope = "channel ~ '%s'" % stats["channel"] if stats["channel"] else "all channels"
    lines.append("Corpus stats (%s)" % scope)
    lines.append("=" * 40)
    lines.append("Videos: %d total, %d analyzed" % (
        stats["total_videos"], stats["analyzed_videos"]))
    if stats.get("excluded_invalid"):
        lines.append(
            "Excluded: %d invalid (media never processed; see "
            "`reel-scout db check-invalid`)" % stats["excluded_invalid"])

    lines.append("\n-- Tag distributions --")
    for col, dist in stats["tag_distributions"].items():
        if not dist:
            continue
        parts = ", ".join("%s:%d" % (k, v) for k, v in dist.items())
        lines.append("%-18s %s" % (col, parts))

    lines.append("\n-- Score aggregates (avg / min-max, n) --")
    for col, agg in stats["score_aggregates"].items():
        if agg["count"] == 0:
            continue
        lines.append("%-20s %s / %s-%s  (n=%d)" % (
            col,
            "%.1f" % agg["avg"] if agg["avg"] is not None else "—",
            agg["min"], agg["max"], agg["count"]))
    return "\n".join(lines)


def to_csv_rows(stats: Dict[str, Any]) -> List[List[Any]]:
    """Long-format rows: (metric, dimension, key, value) — one value per row so
    distributions and aggregates share a single flat schema."""
    rows: List[List[Any]] = [["metric", "dimension", "key", "value"]]
    rows.append(["count", "videos", "total", stats["total_videos"]])
    rows.append(["count", "videos", "analyzed", stats["analyzed_videos"]])
    rows.append(["count", "videos", "excluded_invalid",
                 stats.get("excluded_invalid", 0)])
    for col, dist in stats["tag_distributions"].items():
        for key, val in dist.items():
            rows.append(["tag", col, key, val])
    for col, agg in stats["score_aggregates"].items():
        for key in ("avg", "min", "max", "count"):
            rows.append(["score", col, key, agg[key]])
    return rows


def write_csv(stats: Dict[str, Any], path: str) -> int:
    rows = to_csv_rows(stats)
    with open(path, "w", newline="", encoding="utf-8") as f:
        _csv.writer(f).writerows(rows)
    return len(rows) - 1  # excluding header
