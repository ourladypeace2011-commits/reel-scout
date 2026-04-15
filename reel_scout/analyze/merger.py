from __future__ import annotations

import json
import sqlite3
import urllib.request
from typing import Optional

from .. import config, db

_MERGE_PROMPT_TEMPLATE = """You are analyzing a short-form video. Based on the transcript and visual descriptions below, produce a structured JSON analysis.

## Video Metadata
- Title: {title}
- Platform: {platform}
- Duration: {duration_sec}s
- Uploader: {uploader}

## Transcript
{transcript}

## Visual Descriptions (keyframes)
{vision_descriptions}

## Output Format (JSON only, no markdown)
{{
  "summary": "1-2 sentence summary of the video content",
  "topics": ["topic1", "topic2"],
  "hook": {{
    "opening_type": "question|statement|visual|music|none",
    "opening_text": "first few words or description",
    "cta_type": "follow|like|comment|link|none",
    "cta_text": "CTA text if any"
  }},
  "style": {{
    "format": "talking_head|montage|tutorial|reaction|skit|vlog|slideshow",
    "pacing": "fast|medium|slow",
    "has_captions": true/false,
    "has_background_music": true/false,
    "text_overlay_count": 0
  }},
  "engagement_signals": {{
    "face_visible": true/false,
    "face_count": 0,
    "emotion": "enthusiastic|calm|serious|humorous|neutral",
    "spoken_language": "language code",
    "subtitle_language": "language code or empty"
  }},
  "content_type": "educational|entertainment|promotional|review|story|news"
}}

Return ONLY valid JSON, no explanation."""


def merge_analysis(
    conn: sqlite3.Connection,
    video_id: str,
) -> None:
    video = db.get_video(conn, video_id)
    transcript = db.get_transcript(conn, video_id)
    keyframes = db.get_keyframes(conn, video_id)

    # Gather vision descriptions
    vision_texts = []
    for kf in keyframes:
        cur = conn.execute(
            "SELECT * FROM vision_descriptions WHERE keyframe_id = ?",
            (kf["id"],),
        )
        vd = cur.fetchone()
        if vd:
            vision_texts.append(
                f"[{kf['timestamp_sec']:.1f}s] {vd['description']}"
            )

    transcript_text = transcript["text_full"] if transcript else "(no transcript)"
    vision_text = "\n".join(vision_texts) if vision_texts else "(no vision data)"

    prompt = _MERGE_PROMPT_TEMPLATE.format(
        title=video["title"] or "(untitled)",
        platform=video["platform"],
        duration_sec=video["duration_sec"] or 0,
        uploader=video["uploader"] or "(unknown)",
        transcript=transcript_text,
        vision_descriptions=vision_text,
    )

    # Call local LLM via oMLX (OpenAI-compatible)
    result_json = _call_llm(prompt)

    try:
        data = json.loads(result_json)
    except json.JSONDecodeError:
        # Try to extract JSON from response
        import re
        m = re.search(r"\{[\s\S]*\}", result_json)
        if m:
            data = json.loads(m.group())
        else:
            data = {"summary": result_json, "topics": [], "error": "failed to parse JSON"}

    db.save_analysis(
        conn, video_id,
        summary=data.get("summary", ""),
        topics_json=json.dumps(data.get("topics", []), ensure_ascii=False),
        hooks_json=json.dumps(data.get("hook", {}), ensure_ascii=False),
        style_json=json.dumps(data.get("style", {}), ensure_ascii=False),
        engagement_signals_json=json.dumps(
            data.get("engagement_signals", {}), ensure_ascii=False
        ),
        full_json=json.dumps(data, ensure_ascii=False),
    )


def _call_llm(prompt: str) -> str:
    """Call local LLM via OpenAI-compatible API."""
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 800,
        "temperature": 0.1,
    }
    if config.VLM_MODEL:
        payload["model"] = config.VLM_MODEL

    url = f"{config.OMLX_BASE_URL}/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    return result["choices"][0]["message"]["content"]
