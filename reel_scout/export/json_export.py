from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional

from .. import db


def export_html(
    conn: sqlite3.Connection,
    output_path: str,
    video_id: Optional[str] = None,
) -> str:
    """Write a self-contained HTML viewer (keyframes base64-embedded, zero
    external assets) for one or all analyzed videos. If output_path is a
    directory (or lacks a .html suffix) the file is named reel-scout-viewer.html
    inside it. Returns the file path written."""
    from ..viewer import render_bundle

    if not output_path.endswith(".html"):
        os.makedirs(output_path, exist_ok=True)
        output_path = os.path.join(output_path, "reel-scout-viewer.html")
    else:
        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(render_bundle(conn, video_id=video_id))
    return output_path


def _parse_segments(segments_json: Optional[str]) -> list:
    """Project stored transcript segments to [{start,end,text,speaker?}] — the
    timecoded structure the flat `text_full` drops (R-seg). Never raises: a corrupt
    or missing column degrades to []."""
    if not segments_json:
        return []
    try:
        rows = json.loads(segments_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        seg = {
            "start": r.get("start"),
            "end": r.get("end"),
            "text": (r.get("text") or "").strip(),
        }
        if r.get("speaker") is not None:   # only when diarization ran
            seg["speaker"] = r["speaker"]
        out.append(seg)
    return out


def export_json(
    conn: sqlite3.Connection,
    output_dir: str,
    video_id: Optional[str] = None,
) -> int:
    """Export analyses as individual JSON files. Returns count exported."""
    os.makedirs(output_dir, exist_ok=True)

    if video_id:
        videos = [db.get_video(conn, video_id)]
        videos = [v for v in videos if v is not None]
    else:
        videos = db.list_videos(conn, status="analyzed", limit=9999)

    count = 0
    for video in videos:
        vid = video["id"]
        analysis = db.get_analysis(conn, vid)
        transcript = db.get_transcript(conn, vid)
        if not analysis:
            continue

        record = {
            "video_id": vid,
            "platform": video["platform"],
            "platform_id": video["platform_id"],
            "url": video["url"],
            "title": video["title"],
            "uploader": video["uploader"],
            "duration_sec": video["duration_sec"],
            "upload_date": video["upload_date"],
            "transcript": transcript["text_full"] if transcript else None,
            "has_transcript": (
                bool((transcript["text_full"] or "").strip()) if transcript else False
            ),
            "language": transcript["language"] if transcript else None,
            # R-seg: keep the timecoded (and, when diarized, speaker-tagged)
            # segments, not just the flat text_full — so a JSON consumer can locate
            # and cut on a line, not only read the words.
            "segments": _parse_segments(transcript["segments_json"] if transcript else None),
            "analysis": json.loads(analysis["full_json"]) if analysis["full_json"] else {},
        }

        fpath = os.path.join(output_dir, f"{vid}.json")
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        count += 1

    return count


def export_csv(
    conn: sqlite3.Connection,
    output_path: str,
) -> int:
    """Export flat CSV summary. Returns count exported."""
    import csv

    videos = db.list_videos(conn, status="analyzed", limit=9999)
    if not videos:
        return 0

    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "video_id", "platform", "url", "title", "uploader",
            "duration_sec", "upload_date", "language", "summary",
            "topics", "content_type", "format", "pacing",
        ])

        count = 0
        for video in videos:
            vid = video["id"]
            analysis = db.get_analysis(conn, vid)
            transcript = db.get_transcript(conn, vid)
            if not analysis:
                continue

            full = json.loads(analysis["full_json"]) if analysis["full_json"] else {}
            style = full.get("style", {})

            writer.writerow([
                vid,
                video["platform"],
                video["url"],
                video["title"],
                video["uploader"],
                video["duration_sec"],
                video["upload_date"],
                transcript["language"] if transcript else "",
                analysis["summary"],
                ", ".join(json.loads(analysis["topics_json"] or "[]")),
                full.get("content_type", ""),
                style.get("format", ""),
                style.get("pacing", ""),
            ])
            count += 1

    return count
