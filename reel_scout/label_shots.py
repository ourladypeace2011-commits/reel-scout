"""Produce shot-size labels for a clip's shots (roadmap §6B, the producer half).

`shot_size.py` owns the vocabulary and refuses to guess; this owns the pass that
actually asks something. Two ways in, because the answer to "which model" is not
this module's to make:

* **`--from-json`** — labels supplied from outside (an agent, a person, another
  tool), the same L1 shape `ingest` already uses. No local model, no GPU.
* **a local VLM** — the constrained prompt against ollama.

Every row is stamped with `source` and `model`. That is not bookkeeping: the
same clip scores 7.43 under `qwen3-vl:8b` and 5.5 under `qwen2.5vl:7b`, and a
shot size is no less model-dependent than a craft score. Two passes with
different models coexist rather than overwrite, and a reader choosing between
them can see which is which.

**Measured cost** (2026-09-02, M2 Max, `qwen2.5vl:7b`, one representative frame
per shot): 39 frames in 2.4 minutes ≈ 3.7 s/frame. The whole 2,872-frame library
is therefore ≈ 3 hours, not the 9.5 estimated before measuring — the constrained
answer caps output at a dozen tokens, where a full description does not.

**Known weakness, measured on the same run**: a title card or a graphic gets a
subject-based code instead of `UNKNOWN`. One of three spot-checked frames was a
title card labelled `LS`. Treat `UNKNOWN` as under-reported.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from . import config, db
from .shot_size import (SOURCE_VLM, UNKNOWN, classification_prompt, parse_answer)
from .shots import Shot, bind_frames_to_shots

#: Enough for a code. A budget that allows a sentence invites one, and a
#: sentence is exactly what `parse_answer` refuses.
_NUM_PREDICT = 12


def representative_frames(conn, video_id: str) -> List[Tuple[Shot, Any]]:
    """(shot, its representative keyframe) for shots that have one."""
    rows = db.get_shots(conn, video_id)
    if not rows:
        return []
    spans = [Shot(r["idx"], r["start_sec"], r["end_sec"], r["dur_sec"]) for r in rows]
    per, _ = bind_frames_to_shots(spans, db.get_keyframes_with_descriptions(conn, video_id) or [])
    return [(s.shot, s.representative) for s in per if s.representative is not None]


def classify_with_ollama(image_path: str, model: str,
                         base_url: Optional[str] = None) -> Optional[str]:
    """One frame, one code. None on a refusal, a malformed answer, or a failure.

    None is not an error state the caller should retry blindly — a model that
    will not follow the constraint will not follow it the second time either.
    """
    url = (base_url or getattr(config, "OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
    try:
        with open(image_path, "rb") as f:
            img = base64.b64encode(f.read()).decode("utf-8")
    except OSError:
        return None
    body = json.dumps({
        "model": model,
        "prompt": classification_prompt(),
        "images": [img],
        "stream": False,
        # Reasoning models otherwise spend the whole budget thinking and answer
        # nothing; the same guard the vision path already needs.
        "think": False,
        "options": {"num_predict": _NUM_PREDICT, "temperature": 0},
    }).encode("utf-8")
    req = urllib.request.Request(url + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return parse_answer(payload.get("response"))


def label_video(conn, video_id: str, model: str,
                base_url: Optional[str] = None,
                supplied: Optional[Dict[str, str]] = None) -> Dict[str, int]:
    """Label every shot that has a representative frame. Returns a tally.

    `supplied` maps a keyframe id (as a string, since it arrives from JSON) to a
    code, and skips the model entirely. Anything not in it is asked, unless the
    caller passed `supplied` for every frame.
    """
    # `missing` is its own bucket on purpose. The first run of this command
    # reported "39 refused" when every single frame file was simply unreadable
    # -- blaming the model for a path problem is the exact conflation this
    # library keeps paying for, and it took one run to build a fresh one.
    tally = {"labelled": 0, "unknown": 0, "refused": 0, "skipped": 0, "missing": 0}
    for shot, frame in representative_frames(conn, video_id):
        code = None
        source = SOURCE_VLM
        used_model: Optional[str] = model
        if supplied is not None:
            code = parse_answer(supplied.get(str(frame["id"])))
            source, used_model = "supplied", None
            if code is None:
                tally["skipped"] += 1
                continue
        else:
            if not os.path.exists(frame["file_path"]):
                tally["missing"] += 1
                continue
            code = classify_with_ollama(frame["file_path"], model, base_url)
            if code is None:
                tally["refused"] += 1
                continue
        # The label hangs off the frame's timestamp, not the shot id: spans are
        # replaced on every re-analyze, timestamps are not.
        db.save_shot_label(conn, video_id, frame["timestamp_sec"],
                           "shot_size", code, source, used_model)
        tally["unknown" if code == UNKNOWN else "labelled"] += 1
    return tally
