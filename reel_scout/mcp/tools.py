from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import Any, Dict, List

from .. import config, db, ingest
from ..export.json_export import export_csv, export_json
from ..utils import paths as media_paths

# `ingest` is imported at module level, against the deferred-import convention the
# handlers below follow, because `_enum()` reads its whitelists while `list_tools()`
# is being built. It is cheap -- ingest imports only db, which is already here.
# Making it lazy would break the schema at list time.


def _enum(section, field) -> Dict[str, Any]:
    """A string property whose allowed values come from ingest's own validator.

    Retyping the seven whitelists here would create a second copy that drifts
    from the one that actually rejects payloads, and the schema is the only
    place the model learns them before a failed round-trip.
    """
    return {"type": "string", "enum": list(ingest._ENUMS[(section, field)])}


#: Frames returned by `keyframes` when the caller does not say.
_KEYFRAMES_DEFAULT_MAX = 8

#: Total base64 budget for one `keyframes` reply. Native frames are unbounded and
#: base64 inflates them by 4/3, so a long clip could otherwise return tens of MB
#: into a context window. Truncation is reported, never silent.
_KEYFRAMES_MAX_BYTES = 4 * 1024 * 1024


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
            "name": "keyframes",
            "description": (
                "Return the extracted keyframe images themselves so you can look at "
                "them. `show_video` only gives you file paths, which are useless "
                "without filesystem access — this is how you actually see the frames. "
                "Call it before `ingest_vision`: describe what you saw, never what you "
                "assumed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "video_id": {
                        "type": "string",
                        "description": "Video id or a unique prefix.",
                    },
                    "frame_indexes": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "Specific frame_index values. Omit for an even spread "
                            "across the clip."
                        ),
                    },
                    "max_frames": {
                        "type": "integer",
                        "default": _KEYFRAMES_DEFAULT_MAX,
                        "description": "Cap on how many frames come back (default 8).",
                    },
                },
                "required": ["video_id"],
            },
        },
        {
            "name": "ingest_vision",
            "description": (
                "Write your own keyframe descriptions into the DB — this is how the "
                "visual layer gets produced when there is no local VLM (L1). Call "
                "`keyframes` first and describe what you actually saw. Address each "
                "frame by keyframe_id, frame_index, or file."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string", "description": "Video id or a unique prefix."},
                    "model": {
                        "type": "string",
                        "description": (
                            "Your model name. Stamped as agent:<model> so the row's "
                            "origin stays traceable — craft scores vary a lot between "
                            "models."
                        ),
                    },
                    "frames": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "keyframe_id": {"type": "integer"},
                                "frame_index": {"type": "integer"},
                                "file": {"type": "string",
                                         "description": "Full path or bare basename."},
                                "description": {"type": "string"},
                                "objects": {"type": "array", "items": {"type": "string"}},
                                "text_in_frame": {"type": "string"},
                            },
                            "required": ["description"],
                        },
                    },
                },
                "required": ["video_id", "model", "frames"],
            },
        },
        {
            "name": "ingest_analysis",
            "description": (
                "Write the structured analysis (4-beat timeline, hook, style, "
                "content type) into the DB. The low-cardinality fields are validated "
                "enums that `stats` and `patterns` group on: omit any you cannot "
                "determine — never invent a value, since a coined one adds a "
                "one-member category to every aggregate."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "model": {"type": "string"},
                    "summary": {"type": "string"},
                    "topics": {"type": "array", "items": {"type": "string"}},
                    "timeline": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "timestamp": {"type": "string", "description": "e.g. '0-3s'"},
                                "event": {"type": "string"},
                            },
                        },
                    },
                    "hook": {
                        "type": "object",
                        "properties": {
                            "opening_type": _enum("hook", "opening_type"),
                            "opening_text": {"type": "string"},
                            "cta_type": _enum("hook", "cta_type"),
                            "cta_text": {"type": "string"},
                        },
                    },
                    "style": {
                        "type": "object",
                        "properties": {
                            "format": _enum("style", "format"),
                            "pacing": _enum("style", "pacing"),
                            "has_captions": {"type": "boolean"},
                            "has_background_music": {"type": "boolean"},
                            "text_overlay_count": {"type": "integer"},
                        },
                    },
                    "engagement_signals": {
                        "type": "object",
                        "properties": {
                            "face_visible": {"type": "boolean"},
                            "face_count": {"type": "integer"},
                            "emotion": _enum("engagement_signals", "emotion"),
                            "spoken_language": {"type": "string"},
                            "subtitle_language": {"type": "string"},
                        },
                    },
                    "content_type": _enum(None, "content_type"),
                    "content_structure": _enum(None, "content_structure"),
                },
                # Only summary: every enum stays optional on purpose. Marking them
                # required would push the model to invent a value rather than omit
                # the field, which is exactly what the whitelist exists to prevent.
                "required": ["video_id", "model", "summary"],
            },
        },
        {
            "name": "ingest_score",
            "description": (
                "Write your rubric score (L1). Do NOT send `overall` — it is "
                "recomputed as hook*0.3 + visual*0.25 + pacing*0.2 + structure*0.25 "
                "and anything you send is discarded. Values outside 0-10 are "
                "rejected, not clamped. Read the prompt pack first so the four "
                "dimensions mean what they mean everywhere else."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "model": {"type": "string"},
                    "hook_strength": {"type": "number", "minimum": 0, "maximum": 10},
                    "visual_storytelling": {"type": "number", "minimum": 0, "maximum": 10},
                    "pacing": {"type": "number", "minimum": 0, "maximum": 10},
                    "structure": {"type": "number", "minimum": 0, "maximum": 10},
                    "reasoning": {"type": "string"},
                },
                "required": [
                    "video_id", "model",
                    "hook_strength", "visual_storytelling", "pacing", "structure",
                ],
            },
        },
        {
            "name": "batch_start",
            "description": (
                "Analyze a list of short-form videos in the background and export a "
                "self-contained bundle for each. Returns immediately with a "
                "batch_id; poll `batch_status`. Roughly 20 seconds per video, and "
                "the job outlives this conversation. IMPORTANT: if the result says "
                "status 'needs_mode_choice', present the listed choices to the user "
                "and wait for their answer — do not pick a mode for them."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array", "items": {"type": "string"},
                        "description": "The links the user pasted. Instagram reels, "
                                       "TikTok, YouTube Shorts; anything else is ignored.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Raw pasted text or CSV. Links are extracted "
                                       "from it and a preceding name becomes the label.",
                    },
                    "doc": {
                        "type": "string",
                        "description": "Google Doc/Sheet URL (sharing must be "
                                       "'anyone with the link', which makes its "
                                       "contents public) or any text/CSV URL.",
                    },
                    "file": {
                        "type": "string",
                        "description": "A local .txt/.csv on the machine running this server.",
                    },
                    "mode": {"type": "string", "enum": ["full", "agent", "transcript"]},
                    "out": {"type": "string",
                            "description": "Output root. Defaults to a timestamped "
                                           "directory under ~/reel-scout-batch."},
                    "limit": {"type": "integer", "default": 0},
                    "force": {"type": "boolean", "default": False,
                              "description": "Start even though another batch is running."},
                },
            },
        },
        {
            "name": "batch_status",
            "description": (
                "Progress of a background batch. Call with no arguments for the most "
                "recent one. Safe to call repeatedly; roughly every 30 seconds is "
                "plenty."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"batch_id": {"type": "string"}},
            },
        },
        {
            "name": "batch_cancel",
            "description": (
                "Ask a running batch to stop after the video it is currently on. "
                "Everything already finished is kept."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"batch_id": {"type": "string"}},
            },
        },
        {
            "name": "inspect",
            "description": (
                "Open the interactive inspector for a clip — video player, waveform, "
                "keyframe filmstrip and transcript, all synced to the playhead — and "
                "return a localhost URL for the user to click. Starts a local server "
                "on first use and reuses it afterwards. Local only; it stops when "
                "this session ends."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "video_id": {
                        "type": "string",
                        "description": "Video id or a unique prefix. Omit to land on "
                                       "the library index.",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["open", "status", "stop"],
                        "default": "open",
                    },
                },
            },
        },
        {
            "name": "export",
            "description": "Export analyses to JSON, CSV, or skeleton (beat/rhythm "
                           "hand-off JSON for downstream auto-editing)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": ["json", "csv", "skeleton"], "default": "json"},
                    "output": {"type": "string", "default": "./export"},
                    "video": {"type": "string", "description": "skeleton: single video id (exact or unique prefix)"},
                },
            },
        },
        {
            "name": "patterns",
            "description": "Per-channel pattern analysis (length, hook/CTA/structure mix, high-vs-low, cadence). Read-only.",
            "inputSchema": {
                "type": "object",
                "properties": {"channel": {"type": "string"}},
                "required": ["channel"],
            },
        },
        {
            "name": "inspire",
            "description": "Generate a fresh content variant (titles/hook/structure) from a high-scoring video",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "based_on": {"type": "string"},
                    "angle": {"type": "string"},
                },
                "required": ["based_on"],
            },
        },
        {
            "name": "research",
            "description": "Competitor research: browse the given channels, aggregate niche patterns, return the report data. analyze=true also analyzes each video first (slow); analyze=false skips per-video analysis but still browses the channels (needs network), aggregating from the existing DB.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "niche": {"type": "string"},
                    "channels": {"type": "array", "items": {"type": "string"}},
                    "depth": {"type": "integer", "default": 20},
                    "analyze": {"type": "boolean", "default": False},
                },
                "required": ["niche", "channels"],
            },
        },
        {
            "name": "annotate",
            "description": (
                "Set the operator's own layer on a video — a note about what it is "
                "for, which group it files under, whether it is starred. Stored "
                "separately from the analysis, so re-analyzing never overwrites it. "
                "Omitted fields are left alone."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string",
                                 "description": "URL, video id, or unique id prefix"},
                    "note": {"type": "string"},
                    "group": {"type": "string",
                              "description": "Group name or id; empty string removes the grouping"},
                    "create_group": {"type": "boolean", "default": False},
                    "starred": {"type": "boolean"},
                },
                "required": ["video_id"],
            },
        },
        {
            "name": "list_annotations",
            "description": (
                "Every note / group / star in the library, plus the group list. "
                "Use it to find what you filed under a group or marked to revisit."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "group": {"type": "string", "description": "Only this group (name or id)"},
                    "starred_only": {"type": "boolean", "default": False},
                },
            },
        },
    ]


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    handler = _HANDLERS.get(name)
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


def _image_result(payload: Any, images: List[Dict[str, str]]) -> Dict[str, Any]:
    """A JSON block followed by one image block per frame.

    The text block first, deliberately: it carries the keyframe_id / frame_index
    locators, and a model that reads top-to-bottom should have them in hand
    before it starts describing what it sees.
    """
    content = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]
    for image in images:
        content.append(
            {
                "type": "image",
                "data": image["data"],
                "mimeType": image.get("mime_type", "image/jpeg"),
            }
        )
    return {"content": content}


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
        score = db.get_score(conn, video_id)
        keyframes = db.get_keyframes_with_descriptions(conn, video_id)
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
                "file_path": media_paths.resolve_media_path(video["file_path"]),
            },
            "transcript": None,
            "analysis": None,
            "score": None,
            "keyframes": [],
        }
        if score is not None:
            # model_used is the point: agent-scored and locally-scored rows are
            # averaged together by `stats`, so a score without its origin is
            # unattributable. Over MCP this is the only place it surfaces.
            payload["score"] = {
                "overall": score["overall"],
                "hook_strength": score["hook_strength"],
                "visual_storytelling": score["visual_storytelling"],
                "pacing": score["pacing"],
                "structure": score["structure"],
                "reasoning": score["reasoning"],
                "model_used": score["model_used"],
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
                    "file_path": media_paths.resolve_media_path(keyframe["file_path"]),
                    "strategy": keyframe["strategy"],
                    # Without these the agent can write a visual layer and then
                    # have no way to read it back: over MCP `show_video` is the
                    # only view it has.
                    "description": keyframe["description"],
                    "text_in_frame": keyframe["text_in_frame"],
                    "objects": json.loads(keyframe["objects_json"] or "[]"),
                }
            )
        return _text_result(payload)
    finally:
        conn.close()


def _tool_patterns(args: Dict[str, Any]) -> Dict[str, Any]:
    channel = args.get("channel", "")
    if not channel:
        return _error_result("channel is required")
    from .. import patterns as patterns_mod

    config.ensure_dirs()
    conn = db.init_db()
    try:
        return _text_result(patterns_mod.compute_patterns(conn, channel))
    finally:
        conn.close()


def _tool_inspire(args: Dict[str, Any]) -> Dict[str, Any]:
    based_on = args.get("based_on", "")
    if not based_on:
        return _error_result("based_on is required")
    from .. import inspire as inspire_mod

    config.ensure_dirs()
    conn = db.init_db()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            data = inspire_mod.generate_inspiration(conn, based_on, angle=args.get("angle", ""))
        return _text_result(data)
    finally:
        conn.close()


def _tool_research(args: Dict[str, Any]) -> Dict[str, Any]:
    niche = args.get("niche", "")
    channels = args.get("channels") or []
    if not niche or not channels:
        return _error_result("niche and channels are required")
    if not isinstance(channels, list):
        return _error_result("channels must be an array of URLs")
    from .. import research as research_mod

    config.ensure_dirs()
    conn = db.init_db()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            agg = research_mod.run_research(
                conn, niche, channels,
                depth=int(args.get("depth", 20)),
                do_analyze=bool(args.get("analyze", False)),
            )
        return _text_result(agg)
    finally:
        conn.close()


def _pick_frames(rows: List[Any], wanted: Any, max_frames: int) -> List[Any]:
    """Which keyframe rows to return: the ones asked for, or an even spread.

    An even spread rather than the first N — the first N of a 40-frame clip is
    the first few seconds, which tells you nothing about how the clip resolves.
    """
    if wanted:
        want = {int(w) for w in wanted}
        return [r for r in rows if r["frame_index"] in want][:max_frames]
    if len(rows) <= max_frames:
        return list(rows)
    step = (len(rows) - 1) / float(max_frames - 1) if max_frames > 1 else 0
    return [rows[int(round(i * step))] for i in range(max_frames)]


def _tool_keyframes(args: Dict[str, Any]) -> Dict[str, Any]:
    import base64

    from ..compare import resolve_ref
    from ..viewer import _keyframe_path

    video_ref = args.get("video_id") or ""
    if not video_ref:
        return _error_result("video_id is required")
    max_frames = int(args.get("max_frames", _KEYFRAMES_DEFAULT_MAX))
    if max_frames < 1:
        return _error_result("max_frames must be at least 1")

    config.ensure_dirs()
    conn = db.init_db()
    try:
        video_id, matches = resolve_ref(conn, video_ref)
        if video_id is None:
            if len(matches) > 1:
                return _error_result(
                    "Ambiguous video id %r — matches: %s" % (video_ref, ", ".join(matches))
                )
            return _error_result("Video not found: %s" % video_ref)

        rows = db.get_keyframes(conn, video_id)
        if not rows:
            return _error_result(
                "No keyframes stored for %s — run `analyze --skip-vision` first so the "
                "frames exist on disk." % video_id
            )

        selected = _pick_frames(rows, args.get("frame_indexes"), max_frames)
        described = set(db.get_described_keyframe_ids(conn, video_id))

        frames, images, warnings = [], [], []
        budget = _KEYFRAMES_MAX_BYTES
        truncated = False
        for row in selected:
            path = _keyframe_path(conn, str(row["id"]))
            if not path:
                warnings.append(
                    "keyframe %s: %s is not on disk" % (row["id"], row["file_path"])
                )
                continue
            with open(path, "rb") as handle:
                raw = handle.read()
            encoded = base64.b64encode(raw).decode("ascii")
            if len(encoded) > budget:
                truncated = True
                break
            budget -= len(encoded)
            frames.append(
                {
                    "keyframe_id": row["id"],
                    "frame_index": row["frame_index"],
                    "timestamp_sec": row["timestamp_sec"],
                    "file_path": row["file_path"],
                    "already_described": row["id"] in described,
                }
            )
            images.append({"data": encoded, "mime_type": "image/jpeg"})

        payload = {
            "video_id": video_id,
            "keyframes_total": len(rows),
            "returned": len(frames),
            "frames": frames,
            "next_step": (
                "Look at the images below, then call ingest_vision with one entry per "
                "frame, addressed by keyframe_id."
            ),
        }
        if warnings:
            payload["warnings"] = warnings
        if truncated:
            payload["truncated"] = (
                "Stopped at %d frame(s) to stay inside the reply size budget. Ask for "
                "specific frame_indexes to see the rest." % len(frames)
            )
        return _image_result(payload, images)
    finally:
        conn.close()


def _resolve_video(conn: Any, ref: str):
    """(video_id, error_result). Exactly one of the two is None.

    ingest.py has no existence check of its own, and without one a bad id reaches
    save_analysis / save_score as a foreign-key violation -- sqlite3.IntegrityError,
    not ValueError, so it would sail past the handler's error mapping and surface
    as "FOREIGN KEY constraint failed".
    """
    from ..compare import resolve_ref

    video_id, matches = resolve_ref(conn, ref)
    if video_id is not None:
        return video_id, None
    if len(matches) > 1:
        return None, _error_result(
            "Ambiguous video id %r — matches: %s" % (ref, ", ".join(matches))
        )
    return None, _error_result("Video not found: %s" % ref)


def _ingest_payload(args: Dict[str, Any]) -> Dict[str, Any]:
    """Everything except the two routing keys.

    `video_id` in particular has to go: ingest_analysis stores whatever it is
    handed inside full_json, and it only pops `model` itself.
    """
    return {k: v for k, v in args.items() if k not in ("video_id", "model")}


def _tool_ingest_vision(args: Dict[str, Any]) -> Dict[str, Any]:
    video_ref = args.get("video_id") or ""
    if not video_ref:
        return _error_result("video_id is required")
    frames = args.get("frames")
    if not isinstance(frames, list) or not frames:
        return _error_result("frames must be a non-empty list")

    config.ensure_dirs()
    conn = db.init_db()
    try:
        video_id, err = _resolve_video(conn, video_ref)
        if err is not None:
            return err
        try:
            written, warnings = ingest.ingest_vision(
                conn, video_id, _ingest_payload(args), model=args.get("model", "")
            )
        except ValueError as exc:
            return _error_result(str(exc))

        total = len(db.get_keyframes(conn, video_id))
        described = len(db.get_described_keyframe_ids(conn, video_id))
        payload = {
            "video_id": video_id,
            "written": written,
            "submitted": len(frames),
            "source": ingest.provenance(args.get("model", "")),
            "keyframes_total": total,
            "keyframes_described": described,
        }
        # Partial success is by design, so a bare count would let the caller
        # believe every frame landed.
        if warnings:
            payload["warnings"] = warnings
        if described < total:
            payload["still_undescribed"] = total - described
        return _text_result(payload)
    finally:
        conn.close()


def _tool_ingest_analysis(args: Dict[str, Any]) -> Dict[str, Any]:
    video_ref = args.get("video_id") or ""
    if not video_ref:
        return _error_result("video_id is required")

    config.ensure_dirs()
    conn = db.init_db()
    try:
        video_id, err = _resolve_video(conn, video_ref)
        if err is not None:
            return err
        payload_in = _ingest_payload(args)
        try:
            stored = ingest.ingest_analysis(
                conn, video_id, payload_in, model=args.get("model", "")
            )
        except ValueError as exc:
            return _error_result(str(exc))

        hook = stored.get("hook") or {}
        style = stored.get("style") or {}
        signals = stored.get("engagement_signals") or {}
        landed = {
            "content_type": stored.get("content_type"),
            "content_structure": stored.get("content_structure"),
            "opening_type": hook.get("opening_type"),
            "cta_type": hook.get("cta_type"),
            "style_format": style.get("format"),
            "style_pacing": style.get("pacing"),
            "emotion": signals.get("emotion"),
        }
        payload = {
            "video_id": video_id,
            "source": stored.get("_source"),
            "summary": (stored.get("summary") or "")[:200],
            "stored": landed,
        }
        # Omission is the documented right answer for an undeterminable enum, so
        # make it visible rather than silent.
        omitted = sorted(k for k, v in landed.items() if v in (None, ""))
        if omitted:
            payload["omitted"] = omitted
        return _text_result(payload)
    finally:
        conn.close()


def _tool_ingest_score(args: Dict[str, Any]) -> Dict[str, Any]:
    import dataclasses

    video_ref = args.get("video_id") or ""
    if not video_ref:
        return _error_result("video_id is required")

    config.ensure_dirs()
    conn = db.init_db()
    try:
        video_id, err = _resolve_video(conn, video_ref)
        if err is not None:
            return err
        try:
            score = ingest.ingest_score(
                conn, video_id, _ingest_payload(args), model=args.get("model", "")
            )
        except ValueError as exc:
            return _error_result(str(exc))

        payload = dataclasses.asdict(score)
        payload["video_id"] = video_id
        payload["note"] = (
            "overall was recomputed from the four dimensions; any value you sent "
            "was discarded. This is an L1 (agent-scored) row — it is not directly "
            "comparable with locally-scored videos in the same corpus."
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
        if fmt == "skeleton":
            from ..export.skeleton import export_skeleton
            video_id = None
            ref = args.get("video")
            if ref:
                from ..compare import resolve_ref
                video_id, _ = resolve_ref(conn, ref)
                if video_id is None:
                    return _error_result("Video not found: %s" % ref)
            count = export_skeleton(conn, output, video_id=video_id)
            return _text_result({"format": fmt, "count": count, "output": output})
        return _error_result("Unsupported export format: %s" % fmt)
    finally:
        conn.close()


#: How long a batch may go without a heartbeat before we stop believing it.
#: Generous on purpose: 18s/video is the happy path, but one bad link plus
#: whisper on a long clip can legitimately run minutes, and crying "stalled" at
#: a job that is working is worse than being slow to notice a dead one.
_BATCH_STALE_AFTER_SEC = 900


def _batch_entries_from_args(args: Dict[str, Any]):
    """(entries, error). Normalise every source to text, then parse_rows it.

    Not special-casing `urls` buys dedup for free -- which matters more than it
    sounds: batch_items is keyed (batch_id, url) and create_batch INSERTs each
    one, so the same reel pasted twice is an IntegrityError, not a duplicate.
    It also drops Drive links and long-form YouTube, which is exactly the mess a
    chat paste contains, and picks up "Amy Wu https://..." as a label.
    """
    from .. import batch as batch_mod

    sources = [k for k in ("urls", "text", "doc", "file") if args.get(k)]
    if not sources:
        return None, _error_result("one of urls / text / doc / file is required")
    if len(sources) > 1:
        return None, _error_result(
            "give exactly one of urls / text / doc / file, not %s" % ", ".join(sources))

    kind = sources[0]
    if kind == "urls":
        text = "\n".join(str(u) for u in args["urls"])
    elif kind == "text":
        text = str(args["text"])
    elif kind == "doc":
        try:
            with contextlib.redirect_stdout(sys.stderr):
                text = batch_mod.fetch(str(args["doc"]))
        except (RuntimeError, OSError) as exc:
            return None, _error_result(str(exc))
    else:
        try:
            with open(str(args["file"]), encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            return None, _error_result("could not read %s: %s" % (args["file"], exc))

    entries = batch_mod.parse_rows(text)
    limit = int(args.get("limit", 0) or 0)
    if limit:
        entries = entries[:limit]
    if not entries:
        return None, _error_result(
            "no Instagram / TikTok / YouTube Shorts links found in that input")
    return entries, None


def _mode_choice_result(message: str, caps: Dict[str, bool], parsed: int) -> Dict[str, Any]:
    """A question, returned as a success.

    Not isError: an error invites the client to treat the call as transient and
    retry, and a retry loop is precisely how a mode gets chosen without the user
    ever being asked.
    """
    return _text_result({
        "status": "needs_mode_choice",
        "capabilities": caps,
        "message": message,
        "parsed_count": parsed,
        "choices": [
            {"mode": "agent", "recommended": True,
             "label": "You read the keyframes",
             "detail": "reel-scout extracts the frames; you look at them with the "
                       "`keyframes` tool and write descriptions and a score back. "
                       "No local model, no API key, no extra cost."},
            {"mode": "transcript",
             "label": "Transcript and structure only",
             "detail": "No visual layer and no craft score."},
            {"mode": "full",
             "label": "A local VLM does it",
             "detail": "Needs a local VLM running first (e.g. `ollama serve`)."},
        ],
        "action_required": (
            "Show these options to the user and ask which they want. Do not choose "
            "for them. Then call batch_start again with the same source plus `mode`."
        ),
    })


def _spawn_worker(batch_id: str, log_path: str) -> int:
    """Start the detached worker. Returns its pid."""
    import subprocess

    kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL,
                              "stdout": subprocess.DEVNULL,
                              "close_fds": True}
    if sys.platform == "win32":
        flags = 0
        for name in ("CREATE_NEW_PROCESS_GROUP", "DETACHED_PROCESS"):
            flags |= getattr(subprocess, name, 0)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True

    handle = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "reel_scout.mcp.batch_worker", "--batch-id", batch_id],
            stderr=handle, **kwargs)
    finally:
        handle.close()
    return proc.pid


def _batch_state(row: Any, has_pending: bool) -> str:
    """What to call a batch, given that nothing marks a killed worker's row."""
    import datetime

    status = row["status"] or "running"
    if status != "running":
        return status
    beat = row["heartbeat_at"]
    if not beat:
        return "starting"
    try:
        last = datetime.datetime.strptime(beat, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return status
    # sqlite's datetime('now') is UTC and naive; match it without utcnow(),
    # which is deprecated from 3.12.
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    age = (now - last).total_seconds()
    return "stalled" if age > _BATCH_STALE_AFTER_SEC else "running"


def _tool_batch_start(args: Dict[str, Any]) -> Dict[str, Any]:
    from .. import batch as batch_mod
    from .batch_worker import BATCH_SOURCE

    entries, err = _batch_entries_from_args(args)
    if err is not None:
        return err

    requested = args.get("mode") or None
    # probe() shells out to ffmpeg and yt-dlp and makes two HTTP probes -- up to
    # ~16s. resolve_mode ignores caps for these two, so do not pay for it.
    if requested in ("agent", "transcript"):
        caps = {"vlm": False, "whisper": True}
    else:
        with contextlib.redirect_stdout(sys.stderr):
            caps = batch_mod.probe()
    mode, message = batch_mod.resolve_mode(requested, caps)
    if mode is None:
        return _mode_choice_result(message, caps, len(entries))

    config.ensure_dirs()
    conn = db.init_db()
    try:
        live = db.get_latest_batch(conn, source=BATCH_SOURCE)
        if live is not None and not args.get("force") and _batch_state(live, False) == "running":
            return _error_result(
                "batch %s is still running (%d/%d done). Wait for it, ask "
                "batch_status about it, or pass force=true." % (
                    live["id"], live["completed"] or 0, live["total_urls"] or 0))

        out_root = args.get("out") or os.path.join(
            os.path.expanduser("~"), "reel-scout-batch", _batch_stamp(conn))
        try:
            os.makedirs(out_root, exist_ok=True)
        except OSError as exc:
            return _error_result("cannot create output directory %s: %s" % (out_root, exc))

        urls = [url for _label, url in entries]
        batch_id = db.create_batch(conn, urls, source=BATCH_SOURCE)
        for label, url in entries:
            db.set_batch_item_progress(conn, batch_id, url, label=label)
        db.set_batch_meta(conn, batch_id, mode=mode, out_root=out_root)

        pid = _spawn_worker(batch_id, os.path.join(out_root, "batch.log"))
        db.set_batch_meta(conn, batch_id, pid=pid)

        return _text_result({
            "status": "started",
            "batch_id": batch_id,
            "mode": mode,
            "count": len(entries),
            "out": out_root,
            "estimated_seconds": len(entries) * 18,
            "poll_with": "batch_status",
            "suggested_poll_interval_sec": 30,
            "note": "Running in the background; it survives this conversation. "
                    "Worker stderr goes to batch.log under `out`.",
        })
    finally:
        conn.close()


def _batch_stamp(conn: Any) -> str:
    """A per-run subdirectory name, from sqlite rather than the clock.

    An agent will start several batches in one session, and a flat root makes
    them collide on manifest.json and on slug directories.
    """
    return conn.execute(
        "SELECT strftime('%Y-%m-%d-%H%M%S', 'now')").fetchone()[0]


def _tool_batch_status(args: Dict[str, Any]) -> Dict[str, Any]:
    from .. import batch as batch_mod
    from .batch_worker import BATCH_SOURCE

    config.ensure_dirs()
    # get_connection, not init_db: init_db runs executescript over the whole
    # schema, which takes the schema lock, and a batch's analyze child holds the
    # write lock for long stretches. A reader on WAL never blocks on a writer.
    conn = db.get_connection(timeout=30)
    try:
        batch_id = args.get("batch_id")
        row = (db.get_batch(conn, batch_id) if batch_id
               else db.get_latest_batch(conn, source=BATCH_SOURCE))
        if row is None:
            return _error_result(
                "no such batch: %s" % batch_id if batch_id
                else "no batch has been started yet")
        batch_id = row["id"]

        items = db.get_batch_items(conn, batch_id)
        done, failed, pending, needs_vision = [], [], [], []
        current = None
        for item in items:
            status = item["status"] or "pending"
            entry = {"label": item["label"], "url": item["url"],
                     "video_id": item["video_id"], "bundle_dir": item["bundle_dir"]}
            if status == "done":
                done.append(entry)
            elif status == "error":
                failed.append({"label": item["label"], "url": item["url"],
                               "reason": item["error_message"]})
            elif status == "pending":
                pending.append(entry)
            else:
                current = {"label": item["label"], "url": item["url"], "stage": status}
            if item["video_id"] and batch_mod.needs_completion(conn, item["video_id"]):
                needs_vision.append({"label": item["label"], "slug": item["slug"],
                                     "video_id": item["video_id"]})

        state = _batch_state(row, bool(needs_vision))
        out_root = row["out_root"] or ""
        payload = {
            "batch_id": batch_id,
            "state": state,
            "mode": row["mode"],
            "out": out_root,
            "counts": {"total": len(items), "done": len(done), "failed": len(failed),
                       "pending": len(pending), "needs_visual_layer": len(needs_vision)},
            "current": current,
            "done": done,
            "failed": failed,
            "needs_visual_layer": needs_vision,
            "manifest_present": bool(out_root) and os.path.isfile(
                os.path.join(out_root, "manifest.json")),
        }
        if needs_vision:
            payload["next_steps"] = (
                "For each entry in needs_visual_layer: call `keyframes` to see the "
                "frames, then `ingest_vision`, `ingest_analysis` and `ingest_score` "
                "to write your findings back. Re-export afterwards to refresh the "
                "bundle."
            )
        if state == "stalled":
            payload["note"] = (
                "The worker has stopped updating this batch, so the %d item(s) still "
                "pending will not run. Completed bundles on disk are unaffected and "
                "are listed above." % len(pending)
            )
        elif state == "cancelled":
            payload["note"] = "Cancelled. Everything already finished is listed above."
        return _text_result(payload)
    finally:
        conn.close()


def _tool_batch_cancel(args: Dict[str, Any]) -> Dict[str, Any]:
    from .batch_worker import BATCH_SOURCE

    config.ensure_dirs()
    conn = db.get_connection(timeout=30)
    try:
        batch_id = args.get("batch_id")
        row = (db.get_batch(conn, batch_id) if batch_id
               else db.get_latest_batch(conn, source=BATCH_SOURCE))
        if row is None:
            return _error_result("no such batch: %s" % (batch_id or "(none started)"))
        db.request_batch_cancel(conn, row["id"])
        return _text_result({
            "status": "cancel_requested",
            "batch_id": row["id"],
            "note": "The video currently being processed will finish first; the "
                    "batch stops before the next one. Nothing already done is lost.",
        })
    finally:
        conn.close()


def _link_result(headline: str, payload: Any) -> Dict[str, Any]:
    """A plain-text block carrying the bare URL, then the JSON.

    A URL buried in a JSON blob does not reliably become a clickable link in a
    chat client, and the whole point of this tool is that the user clicks it.
    """
    return {
        "content": [
            {"type": "text", "text": headline},
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)},
        ]
    }


def _tool_inspect(args: Dict[str, Any]) -> Dict[str, Any]:
    from . import live

    action = args.get("action") or "open"

    if action == "stop":
        return _text_result({"stopped": live.stop_inspector(),
                             "note": "The inspector is no longer listening."})

    if action == "status":
        return _text_result({"running": live.is_running(), "base_url": live.base_url()})

    if action != "open":
        return _error_result("action must be one of: open, status, stop")

    was_running = live.is_running()
    video_ref = args.get("video_id") or ""

    config.ensure_dirs()
    conn = db.init_db()
    try:
        video_id, title = None, None
        if video_ref:
            video_id, err = _resolve_video(conn, video_ref)
            if err is not None:
                return err
            row = db.get_video(conn, video_id)
            title = row["title"] if row else None
    finally:
        conn.close()

    try:
        base = live.ensure_inspector()
    except OSError as exc:
        return _error_result("could not start the inspector: %s" % exc)

    url = "%s/inspect/%s" % (base, video_id) if video_id else base + "/"
    payload = {
        "url": url,
        "base_url": base,
        "video_id": video_id,
        "title": title,
        "reused": was_running,
        "note": "Local only (127.0.0.1, no authentication). It stops when this "
                "MCP session ends, so ask again rather than bookmarking it.",
    }
    headline = ("Inspector ready: %s\nOpen that link in your browser — video "
                "player, waveform, keyframe filmstrip and transcript, all "
                "synced to the playhead." % url)
    return _link_result(headline, payload)


#: Name -> handler. Defined here, after every handler exists, and kept module-level
#: so a test can assert it against `list_tools()`: the two are the only things that
#: decide whether a tool is callable and whether it is visible, and nothing about
#: the code makes them agree. A tool missing here answers "Unknown tool"; a tool
#: missing there is invisible but callable.
def _tool_annotate(args: Dict[str, Any]) -> Dict[str, Any]:
    from .. import annotate as ann_mod, db

    conn = db.init_db()
    try:
        video_id, err = _resolve_video(conn, args.get("video_id") or "")
        if err is not None:
            return err
        group = args.get("group")
        try:
            ann = ann_mod.set_annotation(
                conn, video_id,
                note=args.get("note"),
                group=None if group == "" else group,
                starred=args.get("starred"),
                clear_group=(group == ""),
                create_group=bool(args.get("create_group")),
            )
        except ann_mod.AnnotateError as exc:
            return _error_result(str(exc))
        return _text_result({"video_id": video_id, "annotation": ann})
    finally:
        conn.close()


def _tool_list_annotations(args: Dict[str, Any]) -> Dict[str, Any]:
    from .. import annotate as ann_mod, db

    conn = db.init_db()
    try:
        groups = ann_mod.list_groups(conn)
        rows = list(ann_mod.all_annotations(conn).values())
        if args.get("starred_only"):
            rows = [r for r in rows if r.get("starred")]
        want = args.get("group")
        if want not in (None, ""):
            try:
                gid = ann_mod.resolve_group(conn, want)
            except ann_mod.AnnotateError as exc:
                return _error_result(str(exc))
            rows = [r for r in rows if r.get("group_id") == gid]
        return _text_result({"groups": groups, "annotations": rows})
    finally:
        conn.close()


_HANDLERS = {
    "crawl": _tool_crawl,
    "annotate": _tool_annotate,
    "list_annotations": _tool_list_annotations,
    "analyze": _tool_analyze,
    "list_videos": _tool_list_videos,
    "show_video": _tool_show_video,
    "keyframes": _tool_keyframes,
    "ingest_vision": _tool_ingest_vision,
    "ingest_analysis": _tool_ingest_analysis,
    "ingest_score": _tool_ingest_score,
    "batch_start": _tool_batch_start,
    "batch_status": _tool_batch_status,
    "batch_cancel": _tool_batch_cancel,
    "inspect": _tool_inspect,
    "export": _tool_export,
    "patterns": _tool_patterns,
    "inspire": _tool_inspire,
    "research": _tool_research,
}
