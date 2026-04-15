from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from . import config, db
from .llm import get_llm

_SCORE_PROMPT = """You are a short-form video content analyst. Based on the analysis below, score this video on 4 dimensions (0-10 scale, decimals ok).

## Video Analysis
{analysis_json}

## Scoring Criteria
- hook_strength (0-10): How compelling is the opening? Does it grab attention in the first 1-2 seconds?
- information_density (0-10): How much value per second? Is every moment purposeful?
- emotional_impact (0-10): Does it evoke emotion? Will viewers feel something?
- shareability (0-10): Would someone share this? Is it remarkable or useful enough to forward?

## Output Format (JSON only)
{{
  "hook_strength": 0.0,
  "information_density": 0.0,
  "emotional_impact": 0.0,
  "shareability": 0.0,
  "overall": 0.0,
  "reasoning": "1-2 sentence explanation"
}}

The overall score is the weighted average: hook_strength*0.3 + information_density*0.25 + emotional_impact*0.25 + shareability*0.2

Return ONLY valid JSON."""


@dataclass
class VideoScore:
    hook_strength: float = 0.0
    information_density: float = 0.0
    emotional_impact: float = 0.0
    shareability: float = 0.0
    overall: float = 0.0
    reasoning: str = ""
    model_used: str = ""


def score_video(
    conn: db.sqlite3.Connection,
    video_id: str,
    llm_backend: Optional[str] = None,
) -> VideoScore:
    """Score a video using LLM analysis."""
    analysis = db.get_analysis(conn, video_id)
    if not analysis:
        raise ValueError("No analysis found for video: %s" % video_id)

    analysis_json = analysis["full_json"] or "{}"
    prompt = _SCORE_PROMPT.format(analysis_json=analysis_json)

    llm = get_llm(llm_backend)
    result_text = llm.complete(prompt, max_tokens=300, temperature=0.2)

    # Parse JSON response
    try:
        data = json.loads(result_text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", result_text)
        if m:
            data = json.loads(m.group())
        else:
            data = {}

    score = VideoScore(
        hook_strength=float(data.get("hook_strength", 0)),
        information_density=float(data.get("information_density", 0)),
        emotional_impact=float(data.get("emotional_impact", 0)),
        shareability=float(data.get("shareability", 0)),
        overall=float(data.get("overall", 0)),
        reasoning=str(data.get("reasoning", "")),
        model_used=llm_backend or config.LLM_BACKEND,
    )

    # Save to DB
    db.save_score(conn, video_id, score)
    return score
