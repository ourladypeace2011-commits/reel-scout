"""Cross-video comparison (roadmap 3B).

Reads already-stored analyses/scores out of the DB and lays them side by side —
duration, format, pacing, hook/CTA type, content type, and the craft scores — so
you can eyeball what high performers share. Pure read path: it touches no crawler
and no LLM, so it keeps working even when platform access is down.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from . import db

# (key, human label). Order = row order in the human table.
FIELDS: List[Tuple[str, str]] = [
    ("title", "Title"),
    ("platform", "Platform"),
    ("duration_sec", "Duration"),
    ("format", "Format"),
    ("pacing", "Pacing"),
    ("opening_type", "Hook type"),
    ("cta_type", "CTA type"),
    ("content_type", "Content type"),
    # Directly above the scores on purpose: this table is where a reader is most
    # likely to turn two numbers into "A is better than B", and until now it
    # never once looked at whether either clip had words to score.
    ("has_transcript", "Transcript"),
    ("hook_strength", "Hook score"),
    ("visual_storytelling", "Visual score"),
    ("pacing_score", "Pacing score"),
    ("structure", "Structure score"),
    ("overall", "Overall score"),
]

_MISSING = "—"


def resolve_ref(conn: db.sqlite3.Connection, ref: str) -> Tuple[Optional[str], List[str]]:
    """Resolve a video reference to an id. Accepts an exact id or a unique
    prefix (ids are 16 hex chars — typing several in full is painful). Returns
    (resolved_id_or_None, all_prefix_matches) so the caller can report ambiguity.
    """
    if db.get_video(conn, ref) is not None:
        return ref, [ref]
    cur = conn.execute("SELECT id FROM videos WHERE id LIKE ? ORDER BY id", (ref + "%",))
    ids = [r[0] for r in cur.fetchall()]
    if len(ids) == 1:
        return ids[0], ids
    return None, ids


def collect_video(conn: db.sqlite3.Connection, video_id: str) -> Dict[str, Any]:
    """Gather the comparison fields for one video. Missing analysis/score leaves
    those fields as None (rendered as an em dash), never fabricated."""
    video = db.get_video(conn, video_id)
    analysis = db.get_analysis(conn, video_id)
    score = db.get_score(conn, video_id)
    transcript = db.get_transcript(conn, video_id)

    row: Dict[str, Any] = {
        "video_id": video_id,
        "title": video["title"] or None,
        "platform": video["platform"],
        "duration_sec": video["duration_sec"],
        "format": None,
        "pacing": None,
        "opening_type": None,
        "cta_type": None,
        "content_type": None,
        "has_transcript": (
            bool((transcript["text_full"] or "").strip())
            if transcript is not None else False
        ),
        "hook_strength": None,
        "visual_storytelling": None,
        "pacing_score": None,
        "structure": None,
        "overall": None,
    }

    if analysis is not None:
        full = json.loads(analysis["full_json"]) if analysis["full_json"] else {}
        style = full.get("style", {}) or {}
        hook = full.get("hook", {}) or {}
        row["format"] = style.get("format")
        row["pacing"] = style.get("pacing")
        row["opening_type"] = hook.get("opening_type")
        row["cta_type"] = hook.get("cta_type")
        row["content_type"] = full.get("content_type")

    if score is not None:
        row["hook_strength"] = score["hook_strength"]
        row["visual_storytelling"] = score["visual_storytelling"]
        row["pacing_score"] = score["pacing"]
        row["structure"] = score["structure"]
        row["overall"] = score["overall"]

    return row


def build_comparison(
    conn: db.sqlite3.Connection, refs: List[str]
) -> Dict[str, Any]:
    """Resolve refs and collect each video's fields. Returns
    {"videos": [row, ...], "errors": ["...", ...]} — unresolved/ambiguous refs
    go to errors instead of aborting the whole comparison."""
    videos: List[Dict[str, Any]] = []
    errors: List[str] = []
    for ref in refs:
        resolved, matches = resolve_ref(conn, ref)
        if resolved is None:
            if matches:
                errors.append(
                    "Ambiguous ref '%s' matches %d videos: %s"
                    % (ref, len(matches), ", ".join(matches[:5]))
                )
            else:
                errors.append("Video not found: %s" % ref)
            continue
        videos.append(collect_video(conn, resolved))
    return {"videos": videos, "errors": errors}


def _fmt(key: str, value: Any) -> str:
    # Checked before the None/"" fold: False here means "we looked, there were
    # no words", which is a finding. _MISSING means "unknown" everywhere else in
    # this table, and collapsing the two would erase the distinction the column
    # exists to draw.
    if key == "has_transcript":
        return "yes" if value else "NO WORDS"
    if value is None or value == "":
        return _MISSING
    if key == "duration_sec":
        return "%.0fs" % value
    if key in ("hook_strength", "visual_storytelling", "pacing_score",
               "structure", "overall") and isinstance(value, (int, float)):
        return "%.1f" % value
    return str(value)


def format_table(comparison: Dict[str, Any], width: int = 22) -> str:
    """Transposed table: one row per field, one column per video (videos are
    few, fields are many, so this reads better than a wide row-per-video grid)."""
    videos = comparison["videos"]
    lines: List[str] = []
    if not videos:
        lines.append("(no videos to compare)")
    else:
        label_w = max(len(label) for _, label in FIELDS) + 2
        header = "Field".ljust(label_w) + "".join(
            v["video_id"][:12].ljust(width) for v in videos
        )
        lines.append(header)
        lines.append("-" * len(header.rstrip()))
        for key, label in FIELDS:
            cells = "".join(_fmt(key, v.get(key))[:width - 1].ljust(width) for v in videos)
            lines.append(label.ljust(label_w) + cells)
    for err in comparison["errors"]:
        lines.append("! " + err)
    return "\n".join(lines)
