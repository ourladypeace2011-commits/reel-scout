from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import Any, Dict, List

from .. import config, db
from ..export.json_export import export_csv, export_json


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "crawl",
            "description": "Download short-form videos from YouTube, Instagram, or TikTok",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "urls": {"type": "array", "items": {"type": "string"}},
                    "cookies": {"type": "string"},
                },
                "required": ["urls"],
            },
        },
        {
            "name": "analyze",
            "description": "Full pipeline: download + transcribe + vision analysis + structured merge",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "urls": {"type": "array", "items": {"type": "string"}},
                    "skip_vision": {"type": "boolean", "default": False},
                    "skip_transcribe": {"type": "boolean", "default": False},
                    "wait": {"type": "boolean", "default": True},
                },
                "required": ["urls"],
            },
        },
        {
            "name": "list_videos",
            "description": "List analyzed videos with optional filters",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "platform": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        },
        {
            "name": "show_video",
            "description": "Show full analysis for a specific video",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                },
                "required": ["video_id"],
            },
        },
        {
            "name": "export",
            "description": "Export analyses to JSON or CSV",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": ["json", "csv"], "default": "json"},
                    "output": {"type": "string", "default": "./export"},
                },
            },
        },
    ]


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    handlers = {
        "crawl": _tool_crawl,
        "analyze": _tool_analyze,
        "list_videos": _tool_list_videos,
        "show_video": _tool_show_video,
        "export": _tool_export,
    }
    handler = handlers.get(name)
    if handler is None:
        return _error_result("Unknown tool: %s" % name)
    try:
        return handler(arguments)
    except Exception as exc:
        return _error_result("Error: %s" % exc)


def _text_result(payload: Any) -> Dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, indent=2),
            }
        ]
    }


def _error_result(message: str) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


def _tool_crawl(args: Dict[str, Any]) -> Dict[str, Any]:
    from ..crawl import get_crawler

    urls = args.get("urls") or []
    if not urls:
        return _error_result("urls is required")

    cookies = args.get("cookies")
    if cookies:
        os.environ["IG_COOKIES_FILE"] = cookies

    config.ensure_dirs()
    conn = db.init_db()
    results = []
    try:
        for url in urls:
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    crawler = get_crawler(url)
                    meta = crawler.download(url, config.VIDEOS_DIR)
                video_id = db.upsert_video(
                    conn,
                    platform=meta.platform,
                    platform_id=meta.platform_id,
                    url=url,
                    title=meta.title,
                    uploader=meta.uploader,
                    duration_sec=meta.duration_sec,
                    upload_date=meta.upload_date,
                    file_path=meta.file_path,
                    file_size_bytes=meta.file_size_bytes,
                )
                results.append(
                    {
                        "url": url,
                        "video_id": video_id,
                        "platform": meta.platform,
                        "title": meta.title,
                        "duration_sec": meta.duration_sec,
                        "status": "downloaded",
                    }
                )
            except Exception as exc:
                results.append({"url": url, "status": "error", "error": str(exc)})
    finally:
        conn.close()
    return _text_result(results)


def _tool_analyze(args: Dict[str, Any]) -> Dict[str, Any]:
    from ..analyze.pipeline import PipelineOptions, _process_single

    urls = args.get("urls") or []
    if not urls:
        return _error_result("urls is required")

    skip_vision = bool(args.get("skip_vision", False))
    skip_transcribe = bool(args.get("skip_transcribe", False))
    wait = bool(args.get("wait", True))

    config.ensure_dirs()
    conn = db.init_db()
    batch_id = db.create_batch(conn, urls, source="mcp")
    options = PipelineOptions(
        skip_vision=skip_vision,
        skip_transcribe=skip_transcribe,
    )
    if not wait:
        conn.close()
        return _text_result(
            {
                "batch_id": batch_id,
                "status": "queued",
                "warning": "wait=false stores the batch only; run analyze again with wait=true to process it.",
            }
        )

    processed = []
    try:
        for url in urls:
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    video_id = _process_single(conn, url, options)
                db.update_batch_item(conn, batch_id, url, "done", video_id=video_id)
                processed.append({"url": url, "video_id": video_id, "status": "done"})
            except Exception as exc:
                db.update_batch_item(conn, batch_id, url, "error", error=str(exc))
                processed.append({"url": url, "status": "error", "error": str(exc)})
        db.mark_batch_completed(conn, batch_id)
    finally:
        conn.close()
    return _text_result({"batch_id": batch_id, "items": processed})


def _tool_list_videos(args: Dict[str, Any]) -> Dict[str, Any]:
    config.ensure_dirs()
    conn = db.init_db()
    try:
        videos = db.list_videos(
            conn,
            status=args.get("status"),
            platform=args.get("platform"),
            limit=int(args.get("limit", 50)),
        )
        result = []
        for video in videos:
            result.append(
                {
                    "video_id": video["id"],
                    "platform": video["platform"],
                    "title": video["title"],
                    "status": video["status"],
                    "duration_sec": video["duration_sec"],
                    "url": video["url"],
                }
            )
        return _text_result(result)
    finally:
        conn.close()


def _tool_show_video(args: Dict[str, Any]) -> Dict[str, Any]:
    video_id = args.get("video_id", "")
    if not video_id:
        return _error_result("video_id is required")

    config.ensure_dirs()
    conn = db.init_db()
    try:
        video = db.get_video(conn, video_id)
        if video is None:
            return _error_result("Video not found: %s" % video_id)

        transcript = db.get_transcript(conn, video_id)
        analysis = db.get_analysis(conn, video_id)
        keyframes = db.get_keyframes(conn, video_id)
        payload = {
            "video": {
                "video_id": video["id"],
                "platform": video["platform"],
                "platform_id": video["platform_id"],
                "url": video["url"],
                "title": video["title"],
                "uploader": video["uploader"],
                "duration_sec": video["duration_sec"],
                "status": video["status"],
                "file_path": video["file_path"],
            },
            "transcript": None,
            "analysis": None,
            "keyframes": [],
        }
        if transcript is not None:
            payload["transcript"] = {
                "language": transcript["language"],
                "text_full": transcript["text_full"],
                "segments": json.loads(transcript["segments_json"] or "[]"),
                "whisper_model": transcript["whisper_model"],
                "duration_sec": transcript["duration_sec"],
            }
        if analysis is not None:
            payload["analysis"] = {
                "summary": analysis["summary"],
                "topics": json.loads(analysis["topics_json"] or "[]"),
                "hooks": json.loads(analysis["hooks_json"] or "[]"),
                "style": json.loads(analysis["style_json"] or "{}"),
                "engagement_signals": json.loads(analysis["engagement_signals_json"] or "[]"),
                "full": json.loads(analysis["full_json"] or "{}"),
            }
        for keyframe in keyframes:
            payload["keyframes"].append(
                {
                    "id": keyframe["id"],
                    "frame_index": keyframe["frame_index"],
                    "timestamp_sec": keyframe["timestamp_sec"],
                    "file_path": keyframe["file_path"],
                    "strategy": keyframe["strategy"],
                }
            )
        return _text_result(payload)
    finally:
        conn.close()


def _tool_export(args: Dict[str, Any]) -> Dict[str, Any]:
    fmt = args.get("format", "json")
    output = args.get("output", "./export")

    config.ensure_dirs()
    conn = db.init_db()
    try:
        if fmt == "json":
            count = export_json(conn, output)
            return _text_result({"format": fmt, "count": count, "output": output})
        if fmt == "csv":
            csv_output = output
            _, ext = os.path.splitext(output)
            if not ext:
                os.makedirs(output, exist_ok=True)
                csv_output = os.path.join(output, "export.csv")
            count = export_csv(conn, csv_output)
            return _text_result({"format": fmt, "count": count, "output": csv_output})
        return _error_result("Unsupported export format: %s" % fmt)
    finally:
        conn.close()
