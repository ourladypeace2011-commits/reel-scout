from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from . import config, db, ingest, validity
from .llm import get_llm

_SCORE_PROMPT = """You are a short-form video content analyst. Based on the analysis below, score this video on 4 dimensions (0-10 scale, decimals ok).

## Video Analysis
{analysis_json}
{measured_metrics}
## Scoring Criteria
- hook_strength (0-10): How compelling is the opening? Does it grab attention in the first 1-2 seconds? (signal: hook.opening_type, timeline 0-3s)
- visual_storytelling (0-10): Do the visuals carry the story on their own — shot variety, framing, visual information — readable even with the sound off? (signal: keyframe visual descriptions, style.format, faces)
- pacing (0-10): Does the rhythm hold attention? When "Measured Signals" are present, PREFER them as evidence: cuts_per_minute and avg_shot_sec are objective measurements of cut rhythm (higher cuts/min and shorter shots = snappier pacing), and audio_energy/audio_bpm indicate musical drive. Only fall back to the qualitative signals (style.pacing, text_overlay_count, timeline segment count) when no measured signal is given. Do not contradict a strong measured cut rhythm with a vibe-based guess.
- structure (0-10): Is there a complete arc (setup -> build -> payoff) that lands its ending? A clear call-to-action at the close is a plus. (signal: timeline narrative arc, hook.cta_type/cta_text)

## Output Format (JSON only)
{{
  "hook_strength": 0.0,
  "visual_storytelling": 0.0,
  "pacing": 0.0,
  "structure": 0.0,
  "overall": 0.0,
  "reasoning": "1-2 sentence explanation"
}}

The overall score is the weighted average: {weight_formula}

Return ONLY valid JSON."""


def _fmt_weight(w: float) -> str:
    """Render a weight the way a human writes it (0.3, not 0.30) so the prompt
    sentence reads naturally and stays byte-comparable with the config dict."""
    return ("%g" % w)


def weight_formula(weights: Optional[dict] = None) -> str:
    """`hook_strength*0.3 + visual_storytelling*0.25 + ...` built from config.

    Generated rather than hardcoded: the prompt and ingest.compute_overall used
    to hold separate copies of these numbers, and the only thing preventing a
    silent divergence was a test. Now there is one dict and this renderer.
    """
    w = weights or config.SCORE_WEIGHTS
    return " + ".join("%s*%s" % (d, _fmt_weight(w[d])) for d in config.SCORE_DIMENSIONS)


def _measured_block(analysis_json: str) -> str:
    """Render the measured pacing signals (§4E) as an explicit, emphasized prompt
    section so the LLM anchors the pacing score on evidence. Returns '' when the
    analysis has no measured metrics (older rows / measurement disabled), leaving
    the prompt behaviorally identical to before."""
    try:
        data = json.loads(analysis_json)
    except (ValueError, TypeError):
        return ""
    m = (data or {}).get("measured") or {}
    if not m:
        return ""
    lines = ["", "## Measured Signals (objective — prefer these for pacing)"]
    labels = [
        ("cuts_per_minute", "cuts_per_minute"),
        ("shot_count", "shot_count"),
        ("avg_shot_sec", "avg_shot_sec"),
        ("audio_energy", "audio_energy"),
        ("audio_bpm", "audio_bpm"),
    ]
    for key, label in labels:
        if m.get(key) is not None:
            lines.append("- %s: %s" % (label, m[key]))
    return "\n".join(lines) + "\n"


@dataclass
class VideoScore:
    hook_strength: float = 0.0
    visual_storytelling: float = 0.0
    pacing: float = 0.0
    structure: float = 0.0
    overall: float = 0.0
    reasoning: str = ""
    model_used: str = ""


def score_video(
    conn: db.sqlite3.Connection,
    video_id: str,
    llm_backend: Optional[str] = None,
) -> VideoScore:
    """Score a video using LLM analysis."""
    # A video already known to be unusable must not acquire a score by a second
    # route. The pipeline stops before this call, but `score` is also reachable
    # directly, and the failure mode being prevented is precisely a real-looking
    # number produced from nothing — see reel_scout/validity.py.
    if validity.is_invalid(conn, video_id):
        raise ValueError(
            "refusing to score %s: it is marked invalid (%s). Scoring it would "
            "put a fabricated number back into the corpus aggregates."
            % (video_id, db.get_video(conn, video_id)["error_message"]))

    analysis = db.get_analysis(conn, video_id)
    if not analysis:
        raise ValueError("No analysis found for video: %s" % video_id)

    analysis_json = analysis["full_json"] or "{}"
    prompt = _SCORE_PROMPT.format(
        analysis_json=analysis_json,
        measured_metrics=_measured_block(analysis_json),
        weight_formula=weight_formula(),
    )

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

    hook = float(data.get("hook_strength", 0))
    visual = float(data.get("visual_storytelling", 0))
    pacing = float(data.get("pacing", 0))
    structure = float(data.get("structure", 0))
    # overall is COMPUTED from the dimensions, never read from the model's own
    # "overall" field (it drifts ~0.1 from the formula). Delegating to
    # ingest.compute_overall means the agent-ingest path and this path are the
    # same arithmetic over the same weights, by construction rather than by test.
    overall = ingest.compute_overall({
        "hook_strength": hook,
        "visual_storytelling": visual,
        "pacing": pacing,
        "structure": structure,
    })
    score = VideoScore(
        hook_strength=hook,
        visual_storytelling=visual,
        pacing=pacing,
        structure=structure,
        overall=overall,
        reasoning=str(data.get("reasoning", "")),
        model_used=llm_backend or config.LLM_BACKEND,
    )

    # Save to DB
    db.save_score(conn, video_id, score)
    return score
