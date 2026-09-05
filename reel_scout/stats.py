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

`evidence_coverage` answers the question roadmap §4E left open: of the videos
carrying a craft score, how many have a `shot_metrics` row underneath it. With
one, the merger folds measurements into `full_json.measured` and the scorer
prompt prefers them for `pacing`; without one, `pacing` is the model reading the
vibe off the analysis text — the exact model-dependent guess §4E existed to
remove. The two print the same-looking number, so the difference is invisible at
the point of use.

`shot_table_videos` is the same question asked of the newest layer. `shots`
(v14) is additive, so a clip analyzed before it has the cut *count* and no
spans, and only a re-analyze fills them — a gap that would otherwise be
invisible until somebody queried the table by hand.

That gap cost six weeks in both directions. §4E was marked done in code while
`shot_metrics` held zero rows, and nothing said so; then the corpus filled to
116/117 and nothing said that either, so the roadmap carried a stale 🔴 until
somebody ran `SELECT COUNT(*)` by hand. **A capability in use that no surface
reports is indistinguishable from one that is not in use** — the same way round
as the original note, and the reason this block reports counts rather than a
bare percentage: 100% of two videos and 100% of two hundred are different claims.
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

    ⚠️ **That was true of the ingest path and false of the scorer until
    2026-09-05.** `scorer` wrote the *backend*, so 115 of 115 locally-scored
    rows read `ollama` and every model that ever scored under it was pooled
    into one yardstick — the exact thing this grouping exists to prevent, hidden
    behind a docstring that said otherwise. New rows carry `<backend>:<model>`;
    a bare backend name means the row predates that and cannot be compared at
    model grain.
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
           "WHERE 1=1" + where + _NOT_INVALID +
           " GROUP BY src ORDER BY row_count DESC, src")
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


def _evidence_coverage(
    conn: db.sqlite3.Connection, where: str, params: List[Any]
) -> Dict[str, Any]:
    """How many scored videos actually have measurements under the score.

    `pacing` is only evidence-based when `shot_metrics` exists for that video:
    the merger folds it into `full_json.measured` and the scorer prompt prefers
    it. With no row, the scorer falls back to reading the vibe off the analysis
    text — the exact model-dependent guess §4E existed to remove. Both cases
    print an identical-looking number, so the difference is invisible at the
    point of use; this is the surface that names it.

    Reported as a count, never as a bare percentage: 100% of two videos and
    100% of two hundred are not the same claim.
    """
    scored = conn.execute(
        "SELECT COUNT(*) FROM scores s JOIN videos v ON s.video_id = v.id "
        "WHERE 1=1" + where + _NOT_INVALID, params
    ).fetchone()[0]
    scored_with_measured = conn.execute(
        "SELECT COUNT(*) FROM scores s JOIN videos v ON s.video_id = v.id "
        "WHERE EXISTS (SELECT 1 FROM shot_metrics m WHERE m.video_id = s.video_id)"
        + where + _NOT_INVALID, params
    ).fetchone()[0]
    measured_videos = conn.execute(
        "SELECT COUNT(*) FROM shot_metrics m JOIN videos v ON m.video_id = v.id "
        "WHERE 1=1" + where + _NOT_INVALID, params
    ).fetchone()[0]
    ocr_videos = conn.execute(
        "SELECT COUNT(DISTINCT o.video_id) FROM ocr_captions o "
        "JOIN videos v ON o.video_id = v.id WHERE 1=1" + where + _NOT_INVALID, params
    ).fetchone()[0]
    # `shots` (schema v14) is the newest extraction layer and the one most
    # likely to sit empty unnoticed: it is additive, so every clip analyzed
    # before v14 has the cut *count* in shot_metrics and no spans at all, and
    # nothing else in the tool would ever mention the difference. This repo has
    # already lost six weeks twice to an extraction layer nobody was counting.
    shot_table_videos = conn.execute(
        "SELECT COUNT(DISTINCT sh.video_id) FROM shots sh "
        "JOIN videos v ON sh.video_id = v.id WHERE 1=1" + where + _NOT_INVALID, params
    ).fetchone()[0]
    return {
        "scored": scored,
        "scored_with_measured": scored_with_measured,
        "scored_without_measured": scored - scored_with_measured,
        "measured_videos": measured_videos,
        "on_screen_text_videos": ocr_videos,
        "shot_table_videos": shot_table_videos,
        "measured_without_shot_table": measured_videos - shot_table_videos,
    }


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
        score_aggregates[col] = _agg(row["avg"], row["min"], row["max"], row["cnt"])

    score_sources, score_aggregates_by_model = _score_groups(conn, where, params)

    return {
        "channel": channel,
        "total_videos": total,
        "analyzed_videos": analyzed,
        "excluded_invalid": excluded_invalid,
        "tag_distributions": tag_distributions,
        # Pooled across every source. Safe to read on its own only when
        # `mixed_score_sources` is False.
        "score_aggregates": score_aggregates,
        "score_sources": score_sources,
        "score_aggregates_by_model": score_aggregates_by_model,
        "mixed_score_sources": len(score_sources) > 1,
        "evidence_coverage": _evidence_coverage(conn, where, params),
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
    if stats.get("excluded_invalid"):
        lines.append(
            "Excluded: %d invalid (media never processed; see "
            "`reel-scout db check-invalid`)" % stats["excluded_invalid"])

    ev = stats.get("evidence_coverage") or {}
    if ev.get("scored"):
        lines.append("\n-- Evidence coverage --")
        lines.append("%-20s %5d" % ("scored", ev["scored"]))
        lines.append("%-20s %5d  (shot_metrics: cuts/min, avg shot, BPM/energy)"
                     % ("with measured", ev["scored_with_measured"]))
        if ev["scored_without_measured"]:
            lines.append("%-20s %5d  <- pacing on these is model judgement, not measurement"
                         % ("without measured", ev["scored_without_measured"]))
        lines.append("%-20s %5d  videos" % ("on-screen text L3.5",
                                            ev["on_screen_text_videos"]))
        lines.append("%-20s %5d  videos" % ("shot table (spans)",
                                            ev.get("shot_table_videos", 0)))
        gap = ev.get("measured_without_shot_table") or 0
        if gap:
            lines.append("%-20s %5d  <- have the cut count but not the spans; "
                         "re-analyze to fill" % ("  missing spans", gap))
    elif ev:
        lines.append("\n-- Evidence coverage --")
        lines.append("no scored videos in scope")

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
    rows.append(["count", "videos", "excluded_invalid",
                 stats.get("excluded_invalid", 0)])
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
    # Emitted even when every count is zero: "no evidence" and "this CSV predates
    # evidence reporting" must not look the same to a reader.
    for key, val in sorted((stats.get("evidence_coverage") or {}).items()):
        rows.append(["evidence", "coverage", key, val])
    return rows


def write_csv(stats: Dict[str, Any], path: str) -> int:
    rows = to_csv_rows(stats)
    with open(path, "w", newline="", encoding="utf-8") as f:
        _csv.writer(f).writerows(rows)
    return len(rows) - 1  # excluding header
