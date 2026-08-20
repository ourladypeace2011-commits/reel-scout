"""Per-channel pattern analysis (roadmap 3B second half).

Answers "what does this channel tend to do?" — average length, hook/CTA mix,
structure mix, how the top-scored half differs structurally from the bottom half,
and posting cadence. Pure DB read path (no crawler, no LLM), like stats.py /
compare.py, so it keeps working when platform access is down.

Channel scoping keys on the free-text `videos.uploader` (there is no channel
table / id), so `--channel` is a substring match, not an exact key.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from . import db, validity

#: Same exclusion stats.py applies, for the same reason: a video whose media
#: never processed is a non-measurement, and the high/low split is exactly where
#: a fake 0.0 does the most damage — it anchors the bottom half of the channel.
_NOT_INVALID = validity.exclude_sql("v")        # queries that alias videos as v
_NOT_INVALID_BARE = validity.exclude_sql("")    # queries that select FROM videos

# Distribution columns surfaced per channel (subset of the normalized tags most
# useful for "how does this channel build videos").
_DIST_COLUMNS = ["opening_type", "cta_type", "content_structure",
                 "style_format", "style_pacing"]


def _dist(conn, col: str, uploader: str) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT a.{c} AS val, COUNT(*) AS cnt FROM analyses a "
        "JOIN videos v ON a.video_id = v.id "
        "WHERE a.{c} IS NOT NULL AND v.uploader LIKE ?{x} "
        "GROUP BY a.{c} ORDER BY cnt DESC, val".format(c=col, x=_NOT_INVALID),
        ("%" + uploader + "%",),
    ).fetchall()
    return {r["val"]: r["cnt"] for r in rows}


def _parse_upload_date(raw: Optional[str]) -> Optional[datetime]:
    """yt-dlp upload dates are 'YYYYMMDD' strings; parse defensively."""
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:8], "%Y%m%d")
    except (ValueError, TypeError):
        return None


def _cadence(dates: List[datetime]) -> Dict[str, Any]:
    """Posting cadence from sorted upload dates: post count with dates + average
    gap in days. Returns None gaps when fewer than 2 dated posts."""
    ds = sorted(dates)
    if len(ds) < 2:
        return {"dated_posts": len(ds), "avg_gap_days": None,
                "first": ds[0].strftime("%Y-%m-%d") if ds else None,
                "last": ds[-1].strftime("%Y-%m-%d") if ds else None}
    gaps = [(ds[i] - ds[i - 1]).days for i in range(1, len(ds))]
    return {
        "dated_posts": len(ds),
        "avg_gap_days": round(sum(gaps) / len(gaps), 1),
        "first": ds[0].strftime("%Y-%m-%d"),
        "last": ds[-1].strftime("%Y-%m-%d"),
    }


def _high_low_split(conn, uploader: str) -> Dict[str, Any]:
    """Split scored videos at the median overall score and contrast the top vs
    bottom half's structure/pacing mix — what the channel's winners do differently."""
    rows = conn.execute(
        "SELECT s.overall AS overall, a.content_structure AS structure, "
        "a.style_pacing AS pacing FROM scores s "
        "JOIN videos v ON s.video_id = v.id "
        "LEFT JOIN analyses a ON a.video_id = s.video_id "
        "WHERE s.overall IS NOT NULL AND v.uploader LIKE ?{x} "
        "ORDER BY s.overall".format(x=_NOT_INVALID),
        ("%" + uploader + "%",),
    ).fetchall()
    if len(rows) < 2:
        return {"scored": len(rows), "high": None, "low": None}
    mid = len(rows) // 2
    low_rows, high_rows = rows[:mid], rows[mid:]

    def summarize(group):
        overalls = [r["overall"] for r in group]
        structures: Dict[str, int] = {}
        pacings: Dict[str, int] = {}
        for r in group:
            if r["structure"]:
                structures[r["structure"]] = structures.get(r["structure"], 0) + 1
            if r["pacing"]:
                pacings[r["pacing"]] = pacings.get(r["pacing"], 0) + 1
        return {
            "n": len(group),
            "avg_overall": round(sum(overalls) / len(overalls), 2),
            "structure": structures,
            "pacing": pacings,
        }

    return {"scored": len(rows), "low": summarize(low_rows), "high": summarize(high_rows)}


def compute_patterns(conn: db.sqlite3.Connection, channel: str) -> Dict[str, Any]:
    like = "%" + channel + "%"
    total = conn.execute(
        "SELECT COUNT(*) FROM videos WHERE uploader LIKE ?" + _NOT_INVALID_BARE,
        (like,)).fetchone()[0]
    analyzed = conn.execute(
        "SELECT COUNT(*) FROM analyses a JOIN videos v ON a.video_id = v.id "
        "WHERE v.uploader LIKE ?" + _NOT_INVALID, (like,)).fetchone()[0]
    dur_row = conn.execute(
        "SELECT AVG(duration_sec) AS avg, COUNT(duration_sec) AS n "
        "FROM videos WHERE uploader LIKE ? AND duration_sec IS NOT NULL"
        + _NOT_INVALID_BARE, (like,)
    ).fetchone()
    dates = [
        d for d in (
            _parse_upload_date(r[0]) for r in conn.execute(
                "SELECT upload_date FROM videos WHERE uploader LIKE ?"
                + _NOT_INVALID_BARE, (like,)).fetchall()
        ) if d is not None
    ]

    return {
        "channel": channel,
        "total_videos": total,
        "analyzed_videos": analyzed,
        "avg_duration_sec": round(dur_row["avg"], 1) if dur_row["avg"] is not None else None,
        "distributions": {c: _dist(conn, c, channel) for c in _DIST_COLUMNS},
        "high_vs_low": _high_low_split(conn, channel),
        "cadence": _cadence(dates),
    }


def format_patterns(p: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("Channel patterns (uploader ~ '%s')" % p["channel"])
    lines.append("=" * 44)
    lines.append("Videos: %d total, %d analyzed" % (
        p["total_videos"], p["analyzed_videos"]))
    if p["avg_duration_sec"] is not None:
        lines.append("Avg duration: %.1fs" % p["avg_duration_sec"])

    lines.append("\n-- Structure / hook / CTA mix --")
    for col, dist in p["distributions"].items():
        if dist:
            parts = ", ".join("%s:%d" % (k, v) for k, v in dist.items())
            lines.append("%-18s %s" % (col, parts))

    hl = p["high_vs_low"]
    if hl.get("high") and hl.get("low"):
        lines.append("\n-- Top half vs bottom half (by overall score) --")
        for label, g in (("HIGH", hl["high"]), ("LOW", hl["low"])):
            struct = ", ".join("%s:%d" % (k, v) for k, v in g["structure"].items()) or "—"
            lines.append("%-4s n=%d  avg=%.2f  structure=[%s]" % (
                label, g["n"], g["avg_overall"], struct))

    c = p["cadence"]
    if c["dated_posts"]:
        gap = "%.1f days" % c["avg_gap_days"] if c["avg_gap_days"] is not None else "n/a"
        lines.append("\n-- Cadence --")
        lines.append("Dated posts: %d  (%s → %s)  avg gap: %s" % (
            c["dated_posts"], c["first"], c["last"], gap))
    return "\n".join(lines)
