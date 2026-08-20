"""Write externally-produced judgment back into the DB — "the agent is the backend".

The pipeline's VLM and LLM stages both assume a local model endpoint (`oMLX` /
`ollama`). Someone without one is not actually stuck: keyframe extraction is
**ffmpeg, not a model**, so the frames exist on disk before the VLM stage ever
runs. An agent that can see images can describe those frames and apply the craft
rubric itself, and this module is how that result gets back into the database —
so it lands in `show`, `view`, `inspect` and the exported bundle like any other
analysis, instead of evaporating in a chat transcript.

Two rules here are load-bearing, not stylistic:

1. **Provenance is mandatory.** Craft scores are strongly model-dependent — the
   same clip scored 7.43 under qwen3-vl:8b and 5.5 under qwen2.5vl:7b. A row an
   agent produced must stay distinguishable from one a local VLM produced, or a
   classroom ends up comparing numbers that were never comparable. Every row
   written here is tagged `agent:<model>`.

2. **`overall` is recomputed, never accepted.** `scorer.py` deliberately discards
   the model's self-reported overall because it drifts from the weighted formula.
   We apply the identical weights, so an agent-scored video sits on the same
   scale as a locally-scored one on that axis at least.

The provenance stamp is what `stats` groups its score aggregates on, so a corpus
mixing agent-scored and locally-scored videos reports one block per source
instead of one blended average. Nothing downstream can recover the split if the
stamp is missing, which is why `provenance()` refuses an empty model name.
"""

from typing import Any, Dict, List, Optional, Tuple

from . import config, db

#: Re-exported from config so this module's long-standing names keep working.
#: These are NO LONGER a second copy — config.SCORE_WEIGHTS is the only
#: definition, and scorer builds its prompt sentence from that same dict.
_WEIGHTS = config.SCORE_WEIGHTS

_DIMENSIONS = config.SCORE_DIMENSIONS

#: Backend label for anything an agent produced rather than a local VLM/LLM.
AGENT_BACKEND = "agent"


def provenance(model: str) -> str:
    """`agent:<model>` — the string stamped into `model_used` / `vlm_model`."""
    model = (model or "").strip()
    if not model:
        raise ValueError("a model name is required so the row's origin stays traceable")
    return model if model.startswith(AGENT_BACKEND + ":") else "%s:%s" % (AGENT_BACKEND, model)


def normalize_weights(weights: Optional[Dict[str, float]]) -> Dict[str, float]:
    """Coerce arbitrary user weights into a set that sums to 1.0.

    Without this, a reader who drags every slider to the top gets an `overall`
    above 10 and the number silently stops meaning "a score out of 10". Scaling
    by the sum keeps the output on the same 0-10 axis as the stored default, so
    "yours vs default" stays an apples-to-apples comparison — which is the only
    reason the comparison is worth showing at all.

    Missing dimensions fall back to the configured default. Negative weights are
    clamped to 0. An all-zero set is meaningless, so it falls back to default
    rather than dividing by zero.
    """
    if not weights:
        return dict(config.SCORE_WEIGHTS)
    raw = {}
    for d in _DIMENSIONS:
        try:
            v = float(weights.get(d, config.SCORE_WEIGHTS[d]))
        except (TypeError, ValueError):
            v = config.SCORE_WEIGHTS[d]
        raw[d] = max(0.0, v)
    total = sum(raw.values())
    if total <= 0:
        return dict(config.SCORE_WEIGHTS)
    return {d: raw[d] / total for d in _DIMENSIONS}


def compute_overall(dims: Dict[str, float], weights: Optional[Dict[str, float]] = None) -> float:
    """Weighted overall, matching scorer.score_video exactly (2dp).

    `weights=None` uses the configured default — that is the number persisted to
    the DB and the only one any other surface should treat as authoritative.
    Passing custom weights is for read-side exploration (the inspector's
    sliders); it deliberately does not write anything back.
    """
    w = _WEIGHTS if weights is None else normalize_weights(weights)
    return round(sum(dims[d] * w[d] for d in _DIMENSIONS), 2)


def _as_score(value: Any, field: str) -> float:
    """A 0–10 number, or a hard error naming the field.

    Deliberately strict rather than clamping: a silently clamped 47 looks like a
    real 10 in the viewer, and the person who has to notice is a student.
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number 0-10, got %r" % (field, value))
    if num != num or num < 0 or num > 10:      # num != num catches NaN
        raise ValueError("%s must be within 0-10, got %r" % (field, value))
    return num


def resolve_keyframe(rows: List[Any], spec: Dict[str, Any]) -> int:
    """Map one frame entry to a keyframe id.

    Accepts `keyframe_id`, `frame_index`, or `file` (full path or bare basename),
    in that order. The producer here is a language model reading files off disk,
    so a single rigid key would fail in exactly the situation this path exists to
    serve; matching on what it can actually see is worth the extra branch.
    """
    if spec.get("keyframe_id") is not None:
        want = int(spec["keyframe_id"])
        for r in rows:
            if r["id"] == want:
                return want
        raise ValueError("keyframe_id %d does not belong to this video" % want)

    if spec.get("frame_index") is not None:
        want = int(spec["frame_index"])
        for r in rows:
            if r["frame_index"] == want:
                return r["id"]
        raise ValueError("no keyframe with frame_index %d for this video" % want)

    ref = spec.get("file") or spec.get("file_path")
    if ref:
        ref = str(ref)
        tail = ref.replace("\\", "/").rsplit("/", 1)[-1]
        for r in rows:
            path = (r["file_path"] or "").replace("\\", "/")
            if path == ref or path.rsplit("/", 1)[-1] == tail:
                return r["id"]
        raise ValueError("no keyframe matching file %r for this video" % ref)

    raise ValueError("each frame needs one of: keyframe_id, frame_index, file")


def ingest_vision(
    conn: Any,
    video_id: str,
    payload: Dict[str, Any],
    model: str = "",
) -> Tuple[int, List[str]]:
    """Write agent-produced frame descriptions. Returns (written, warnings).

    `payload` is `{"model": ..., "frames": [{<locator>, "description", ...}]}`.
    Frames that fail to resolve are collected as warnings rather than aborting the
    batch — a partial visual layer beats none, and the caller reports what missed.
    """
    frames = payload.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("payload needs a non-empty 'frames' list")

    stamp = provenance(model or payload.get("model", ""))
    rows = db.get_keyframes(conn, video_id)
    if not rows:
        raise ValueError(
            "no keyframes stored for %s — run `analyze --skip-vision` first so the "
            "frames exist on disk" % video_id)

    written, warnings = 0, []
    for i, spec in enumerate(frames):
        if not isinstance(spec, dict):
            warnings.append("frame %d: expected an object, got %s" % (i, type(spec).__name__))
            continue
        try:
            kf_id = resolve_keyframe(rows, spec)
        except ValueError as exc:
            warnings.append("frame %d: %s" % (i, exc))
            continue

        description = str(spec.get("description") or "").strip()
        if not description:
            warnings.append("frame %d: empty description, skipped" % i)
            continue

        objects = spec.get("objects") or []
        if isinstance(objects, str):
            objects = [objects]
        import json as _json
        db.save_vision_description(
            conn,
            keyframe_id=kf_id,
            description=description,
            objects_json=_json.dumps(list(objects), ensure_ascii=False),
            text_in_frame=str(spec.get("text_in_frame") or ""),
            vlm_backend=AGENT_BACKEND,
            vlm_model=stamp,
        )
        written += 1

    return written, warnings


#: Low-cardinality fields that `save_analysis` normalizes into their own columns,
#: which `stats` / `patterns` / `compare` then group and filter on. An agent that
#: invents a new value doesn't just mislabel one video — it silently adds a
#: one-member category to every aggregate. Same reasoning as the score bounds:
#: reject rather than accept-and-hope.
_ENUMS = {
    ("hook", "opening_type"): ("question", "statement", "visual", "music", "none"),
    ("hook", "cta_type"): ("follow", "like", "comment", "link", "visit", "none"),
    ("style", "format"): ("talking_head", "montage", "tutorial", "reaction",
                          "skit", "vlog", "slideshow"),
    ("style", "pacing"): ("fast", "medium", "slow"),
    ("engagement_signals", "emotion"): ("enthusiastic", "calm", "serious",
                                        "humorous", "neutral"),
    (None, "content_type"): ("educational", "entertainment", "promotional",
                             "review", "story", "news"),
    (None, "content_structure"): ("hook-body-cta", "problem-solution", "listicle",
                                  "story-arc", "raw-moment"),
}


def _check_enums(payload: Dict[str, Any]) -> List[str]:
    """Enum violations, as readable messages. Empty list means clean."""
    problems = []
    for (section, field), allowed in _ENUMS.items():
        holder = payload if section is None else payload.get(section) or {}
        if not isinstance(holder, dict):
            problems.append("%s must be an object" % section)
            continue
        value = holder.get(field)
        if value in (None, ""):
            continue
        if value not in allowed:
            where = field if section is None else "%s.%s" % (section, field)
            problems.append("%s=%r is not one of: %s" % (where, value, ", ".join(allowed)))
    return problems


def ingest_analysis(
    conn: Any,
    video_id: str,
    payload: Dict[str, Any],
    model: str = "",
) -> Dict[str, Any]:
    """Write an agent-produced structured analysis. Returns the stored blob.

    This is the third thing the pipeline can only get from a model, after the
    frame descriptions and the craft score. `merge_analysis` needs a reachable
    LLM; on a machine without one it fails with a connection error and the
    `analyses` row is simply never written — so the 4-beat structure, hook type
    and CTA type are all absent, which is most of what the tool is for.

    `payload` takes the same shape `merge_analysis` produces, so an agent can be
    handed the merge prompt's own output format and the two paths stay
    comparable. Provenance rides inside `full_json` as `_source`, because the
    table has no model column and inventing one is a migration this doesn't need.
    """
    import json as _json

    if not isinstance(payload, dict):
        raise ValueError("analysis payload must be a JSON object")
    if not str(payload.get("summary") or "").strip():
        raise ValueError("missing required field: summary")

    problems = _check_enums(payload)
    if problems:
        raise ValueError("invalid enum value(s):\n  - " + "\n  - ".join(problems))

    data = dict(payload)
    data["_source"] = provenance(model or payload.get("model", ""))
    data.pop("model", None)

    db.save_analysis(
        conn, video_id,
        summary=str(data.get("summary") or ""),
        topics_json=_json.dumps(data.get("topics") or [], ensure_ascii=False),
        hooks_json=_json.dumps(data.get("hook") or {}, ensure_ascii=False),
        style_json=_json.dumps(data.get("style") or {}, ensure_ascii=False),
        engagement_signals_json=_json.dumps(
            data.get("engagement_signals") or {}, ensure_ascii=False),
        full_json=_json.dumps(data, ensure_ascii=False),
    )
    return data


def ingest_score(
    conn: Any,
    video_id: str,
    payload: Dict[str, Any],
    model: str = "",
) -> Any:
    """Write an agent-produced craft score. Returns the stored VideoScore.

    Any `overall` in `payload` is ignored on purpose — see the module docstring.
    """
    from .scorer import VideoScore

    missing = [d for d in _DIMENSIONS if payload.get(d) is None]
    if missing:
        raise ValueError("missing required dimension(s): %s" % ", ".join(missing))

    dims = {d: _as_score(payload[d], d) for d in _DIMENSIONS}
    score = VideoScore(
        hook_strength=dims["hook_strength"],
        visual_storytelling=dims["visual_storytelling"],
        pacing=dims["pacing"],
        structure=dims["structure"],
        overall=compute_overall(dims),
        reasoning=str(payload.get("reasoning") or "").strip(),
        model_used=provenance(model or payload.get("model", "")),
    )
    db.save_score(conn, video_id, score)
    return score
