from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional

from .. import db


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
            "language": transcript["language"] if transcript else None,
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
