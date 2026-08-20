"""Corpus statistics (roadmap 3D).

Aggregates the normalized tag columns (3C) and the craft scores across the
analyzed library — tag distributions + score avg/min/max — optionally scoped to
one channel. Pure DB read path (no crawler, no LLM), like compare.py.

Channel scoping keys on the free-text `videos.uploader` (there is no channel
table / id), so `--channel` is a substring match, not an exact key.

Scores are grouped by `model_used`, because a craft score only means something
relative to the model that produced it: the same clip scored 7.43 under
`qwen3-vl:8b` and 5.5 under `qwen2.5vl:7b`, and an agent-supplied score
(`agent:<model>`, see ingest.py) is a third ruler again. Pooling them yields a
mean that can describe no video in the corpus. The pooled block is still
reported — it is the long-standing output shape and dropping it would break
readers — but when more than one source is present it is labelled as pooled and
`mixed_score_sources` is set, so nobody reads it as a single scale.
"""
from __future__ import annotations

import csv as _csv
from typing import Any, Dict, List, Optional, Tuple

from . import db

# Normalized enum columns on `analyses` (added in schema v5/v6).
TAG_COLUMNS = [
    "content_type", "content_structure", "style_format",
    "style_pacing", "opening_type", "cta_type", "emotion",
]
# Numeric craft dimensions on `scores`.
SCORE_COLUMNS = ["overall", "hook_strength", "visual_storytelling", "pacing", "structure"]

#: Label for a score row that carries no `model_used` — legacy rows written
#: before the column was populated. Same string `show` prints, so the two
#: surfaces name the same thing the same way.
UNKNOWN_MODEL = "(unknown)"


def _channel_clause(channel: Optional[str]) -> Tuple[str, List[Any]]:
    if channel:
        return " AND v.uploader LIKE ?", ["%" + channel + "%"]
    return "", []


def _agg(avg: Any, minimum: Any, maximum: Any, count: Any) -> Dict[str, Any]:
    """One column's aggregate block. Shared by the pooled and the per-model path
    so both round identically — a per-model average that rounded differently
    from the pooled one would read as a bug in the grouping."""
    return {
        "avg": round(avg, 2) if avg is not None else None,
        "min": minimum,
        "max": maximum,
        "count": count,
    }


def _score_groups(
    conn: db.sqlite3.Connection, where: str, params: List[Any]
) -> Tuple[Dict[str, int], Dict[str, Dict[str, Any]]]:
    """Per-`model_used` score aggregates plus a row census, in one query.

    Grouping is on the exact `model_used` string, which is the finest grain that
    is actually correct: two local VLMs are no more comparable to each other than
    an agent is to either. A coarser agent-vs-local split is derivable from the
    `agent:` prefix; the reverse is not, so the fine grain is what gets stored.
    """
    selects = [
        # NULL and '' both mean "origin never recorded" and belong in one
        # bucket, so the coalescing happens in SQL — merging two groups'
        # averages afterwards would need re-weighting by hand.
        "COALESCE(NULLIF(s.model_used, ''), ?) AS src",
        "COUNT(*) AS row_count",
    ]
    for col in SCORE_COLUMNS:
        # Column names come from the hardcoded SCORE_COLUMNS list, never input.
        selects.append(
            "AVG(s.{c}) AS {c}_avg, MIN(s.{c}) AS {c}_min, "
            "MAX(s.{c}) AS {c}_max, COUNT(s.{c}) AS {c}_cnt".format(c=col))
    sql = ("SELECT " + ", ".join(selects) +
           " FROM scores s JOIN videos v ON s.video_id = v.id "
           "WHERE 1=1" + where + " GROUP BY src ORDER BY row_count DESC, src")
    # The UNKNOWN_MODEL placeholder sits in the SELECT list, which SQLite binds
    # before the WHERE clause's — so it has to lead the parameter list.
    rows = conn.execute(sql, [UNKNOWN_MODEL] + params).fetchall()

    census: Dict[str, int] = {}
    by_model: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        src = r["src"]
        census[src] = r["row_count"]
        by_model[src] = {
            col: _agg(r["%s_avg" % col], r["%s_min" % col],
                      r["%s_max" % col], r["%s_cnt" % col])
            for col in SCORE_COLUMNS
        }
    return census, by_model


def compute_stats(conn: db.sqlite3.Connection, channel: Optional[str] = None) -> Dict[str, Any]:
    where, params = _channel_clause(channel)

    total = conn.execute(
        "SELECT COUNT(*) FROM videos v WHERE 1=1" + where, params
    ).fetchone()[0]
    analyzed = conn.execute(
        "SELECT COUNT(*) FROM analyses a JOIN videos v ON a.video_id = v.id "
        "WHERE 1=1" + where, params
    ).fetchone()[0]

    # Tag distributions. Column names come from the hardcoded TAG_COLUMNS list
    # (never user input); the channel value is bound as a parameter.
    tag_distributions: Dict[str, Dict[str, int]] = {}
    for col in TAG_COLUMNS:
        rows = conn.execute(
            "SELECT a.{c} AS val, COUNT(*) AS cnt FROM analyses a "
            "JOIN videos v ON a.video_id = v.id "
            "WHERE a.{c} IS NOT NULL{w} GROUP BY a.{c} ORDER BY cnt DESC, val".format(
                c=col, w=where),
            params,
        ).fetchall()
        tag_distributions[col] = {r["val"]: r["cnt"] for r in rows}

    score_aggregates: Dict[str, Dict[str, Any]] = {}
    for col in SCORE_COLUMNS:
        row = conn.execute(
            "SELECT AVG(s.{c}) AS avg, MIN(s.{c}) AS min, MAX(s.{c}) AS max, "
            "COUNT(s.{c}) AS cnt FROM scores s JOIN videos v ON s.video_id = v.id "
            "WHERE s.{c} IS NOT NULL{w}".format(c=col, w=where),
            params,
        ).fetchone()
        score_aggregates[col] = _agg(row["avg"], row["min"], row["max"], row["cnt"])

    score_sources, score_aggregates_by_model = _score_groups(conn, where, params)

    return {
        "channel": channel,
        "total_videos": total,
        "analyzed_videos": analyzed,
        "tag_distributions": tag_distributions,
        # Pooled across every source. Safe to read on its own only when
        # `mixed_score_sources` is False.
        "score_aggregates": score_aggregates,
        "score_sources": score_sources,
        "score_aggregates_by_model": score_aggregates_by_model,
        "mixed_score_sources": len(score_sources) > 1,
    }


def _score_lines(agg_by_col: Dict[str, Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for col, agg in agg_by_col.items():
        if agg["count"] == 0:
            continue
        lines.append("%-20s %s / %s-%s  (n=%d)" % (
            col,
            "%.1f" % agg["avg"] if agg["avg"] is not None else "—",
            agg["min"], agg["max"], agg["count"]))
    return lines


def format_stats(stats: Dict[str, Any]) -> str:
    lines: List[str] = []
    scope = "channel ~ '%s'" % stats["channel"] if stats["channel"] else "all channels"
    lines.append("Corpus stats (%s)" % scope)
    lines.append("=" * 40)
    lines.append("Videos: %d total, %d analyzed" % (
        stats["total_videos"], stats["analyzed_videos"]))

    lines.append("\n-- Tag distributions --")
    for col, dist in stats["tag_distributions"].items():
        if not dist:
            continue
        parts = ", ".join("%s:%d" % (k, v) for k, v in dist.items())
        lines.append("%-18s %s" % (col, parts))

    sources = stats.get("score_sources") or {}
    by_model = stats.get("score_aggregates_by_model") or {}
    mixed = bool(stats.get("mixed_score_sources"))

    if mixed:
        census = ", ".join("%s n=%d" % (k, v) for k, v in sources.items())
        lines.append(
            "\n!! %d scoring sources in this corpus (%s). Craft scores are "
            "model-dependent, so the pooled figures below average rulers that "
            "were never the same ruler; read the per-source blocks instead."
            % (len(sources), census))
        lines.append("\n-- Score aggregates, POOLED across %d sources — "
                     "not a single scale (avg / min-max, n) --" % len(sources))
    elif sources:
        lines.append("\n-- Score aggregates · source: %s (avg / min-max, n) --"
                     % next(iter(sources)))
    else:
        lines.append("\n-- Score aggregates (avg / min-max, n) --")
    lines.extend(_score_lines(stats["score_aggregates"]))

    if mixed:
        for src, agg_by_col in by_model.items():
            lines.append("\n-- Score aggregates · %s (avg / min-max, n) --" % src)
            lines.extend(_score_lines(agg_by_col))
    return "\n".join(lines)


def to_csv_rows(stats: Dict[str, Any]) -> List[List[Any]]:
    """Long-format rows: (metric, dimension, key, value) — one value per row so
    distributions and aggregates share a single flat schema.

    The per-model score rows carry the model label in `dimension` and
    `<column>.<stat>` in `key`, which keeps the grouping inside the existing
    four columns: a spreadsheet built against the old schema still opens."""
    rows: List[List[Any]] = [["metric", "dimension", "key", "value"]]
    rows.append(["count", "videos", "total", stats["total_videos"]])
    rows.append(["count", "videos", "analyzed", stats["analyzed_videos"]])
    for col, dist in stats["tag_distributions"].items():
        for key, val in dist.items():
            rows.append(["tag", col, key, val])
    for col, agg in stats["score_aggregates"].items():
        for key in ("avg", "min", "max", "count"):
            rows.append(["score", col, key, agg[key]])
    for src, cnt in (stats.get("score_sources") or {}).items():
        rows.append(["count", "score_source", src, cnt])
    # Emitted even when False: a reader parsing the CSV should not have to infer
    # "single scale" from the absence of a row.
    rows.append(["flag", "score_source", "mixed",
                 int(bool(stats.get("mixed_score_sources")))])
    for src, agg_by_col in (stats.get("score_aggregates_by_model") or {}).items():
        for col, agg in agg_by_col.items():
            for key in ("avg", "min", "max", "count"):
                rows.append(["score_by_model", src, "%s.%s" % (col, key), agg[key]])
    return rows


def write_csv(stats: Dict[str, Any], path: str) -> int:
    rows = to_csv_rows(stats)
    with open(path, "w", newline="", encoding="utf-8") as f:
        _csv.writer(f).writerows(rows)
    return len(rows) - 1  # excluding header
