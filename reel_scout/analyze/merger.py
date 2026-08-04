from __future__ import annotations

import json
import sqlite3
from typing import Optional

from .. import config, db
from ..llm import get_llm

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

## Audio Events
{audio_events}

## On-screen Text (burned-in captions, L3.5)
{onscreen_text}

## Output Format (JSON only, no markdown)
{{
  "summary": "1-2 sentence summary of the video content",
  "topics": ["topic1", "topic2"],
  "timeline": [
    {{"timestamp": "0-3s", "event": "hook/opening description"}},
    {{"timestamp": "3-15s", "event": "main content description"}},
    {{"timestamp": "15-20s", "event": "CTA or closing"}}
  ],
  "hook": {{
    "opening_type": "question|statement|visual|music|none",
    "opening_text": "first few words or description",
    "cta_type": "follow|like|comment|link|visit|none",
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
  "content_type": "educational|entertainment|promotional|review|story|news",
  "content_structure": "hook-body-cta|problem-solution|listicle|story-arc|raw-moment"
}}

IMPORTANT — content_structure: classify the video's overall structural template.
  - "hook-body-cta": grabs attention, delivers content, ends with a call to action.
  - "problem-solution": names a pain point then resolves it.
  - "listicle": enumerated points / steps / tips ("3 ways to…", "top 5…").
  - "story-arc": a narrative with setup → tension → payoff.
  - "raw-moment": an unstructured clip / candid moment with no deliberate arc.
  Pick the single best fit.

IMPORTANT — cta_type: read the LAST transcript segments and final frames before deciding. If the video closes by urging a real-world action — visit a shop, go try/eat somewhere, go check it out (e.g. "快去試試看", "大家快去吃", "就在XX路上") — set "cta_type": "visit" and copy the phrase into cta_text. follow/like/comment/link are ONLY for on-platform engagement. Use "none" ONLY when there is genuinely no closing call to action at all. Do not default to "none" for offline/visit CTAs.

The timeline should capture the narrative arc: how the video progresses from hook to main content to conclusion/CTA. Use approximate time ranges.

Return ONLY valid JSON, no explanation."""


_MEASURED_FIELDS = ("cuts_per_minute", "shot_count", "avg_shot_sec",
                    "audio_energy", "audio_bpm")


def _measured_from_metrics(sm) -> dict:
    """§4E measured pacing signals as a dict, dropping None fields; {} when there
    is no shot_metrics row."""
    if sm is None:
        return {}
    return {k: sm[k] for k in _MEASURED_FIELDS if sm[k] is not None}


def backfill_measured(conn: sqlite3.Connection, video_id: str) -> None:
    """Fold measured metrics into an EXISTING analysis when the merge step is
    skipped (re-analyzing a video whose analysis predates §4E). Without this a
    fresh Step 3.5 stores shot_metrics the scorer never sees — it only reads
    full_json.measured, and merge_analysis doesn't re-run for analyzed videos."""
    analysis = db.get_analysis(conn, video_id)
    if analysis is None:
        return
    measured = _measured_from_metrics(db.get_shot_metrics(conn, video_id))
    if not measured:
        return
    try:
        data = json.loads(analysis["full_json"] or "{}")
    except (ValueError, TypeError):
        return
    if data.get("measured") == measured:
        return  # already current — don't rewrite
    data["measured"] = measured
    db.save_analysis(
        conn, video_id,
        summary=analysis["summary"] or "",
        topics_json=analysis["topics_json"] or "[]",
        hooks_json=analysis["hooks_json"] or "{}",
        style_json=analysis["style_json"] or "{}",
        engagement_signals_json=analysis["engagement_signals_json"] or "{}",
        full_json=json.dumps(data, ensure_ascii=False),
    )


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

    # Build transcript with speaker labels if available
    # Three real states, not two: no row, a row with words, and a row whose ASR
    # found nothing. The last one used to fall through the `if transcript:`
    # branch and hand the prompt an empty string, so the model saw a "##
    # Transcript" heading with nothing under it -- indistinguishable from a
    # section that failed to render. It has no way to tell "nobody spoke" from
    # "the words are missing", and a clip whose only text is a title card gets
    # its opening classified as something that was said.
    if transcript and (transcript["text_full"] or "").strip():
        import json as _json
        segs = _json.loads(transcript["segments_json"] or "[]")
        if segs and segs[0].get("speaker"):
            transcript_text = "\n".join(
                "[%s] %s" % (s.get("speaker", ""), s.get("text", ""))
                for s in segs
            )
        else:
            transcript_text = transcript["text_full"]
    else:
        transcript_text = "(no transcript)"
    vision_text = "\n".join(vision_texts) if vision_texts else "(no vision data)"

    # Gather audio events
    audio_events = db.get_audio_events(conn, video_id)
    audio_text = "(no audio analysis)"
    if audio_events:
        audio_lines = []
        for ae in audio_events:
            audio_lines.append("[%.1fs-%.1fs] %s: %s (%.0f%%)" % (
                ae["start_sec"], ae["end_sec"], ae["event_type"],
                ae["label"], ae["confidence"] * 100))
        audio_text = "\n".join(audio_lines)

    # On-screen text (§4F, L3.5): burned-in captions read by the VLM/OCR, with
    # timestamps — carries the message for low-dialogue / pure-visual reels.
    ocr_rows = db.get_ocr_captions(conn, video_id)
    if ocr_rows:
        onscreen_text = "\n".join(
            "[%.1fs] %s" % (o["timestamp_sec"] or 0.0, o["text"]) for o in ocr_rows
        )
    else:
        onscreen_text = "(no on-screen text detected)"

    prompt = _MERGE_PROMPT_TEMPLATE.format(
        title=video["title"] or "(untitled)",
        platform=video["platform"],
        duration_sec=video["duration_sec"] or 0,
        uploader=video["uploader"] or "(unknown)",
        transcript=transcript_text,
        vision_descriptions=vision_text,
        audio_events=audio_text,
        onscreen_text=onscreen_text,
    )

    llm = get_llm()
    result_json = llm.complete(prompt, max_tokens=800, temperature=0.1)

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

    # §4E: fold measured pacing signals (cuts/min, shot count, audio energy/BPM)
    # into the analysis blob so the scorer reasons on evidence, not LLM vibes.
    # Only present when Step 3.5 measured them; None values are dropped.
    measured = _measured_from_metrics(db.get_shot_metrics(conn, video_id))
    if measured:
        data["measured"] = measured

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
