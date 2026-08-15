from __future__ import annotations

import base64
import json
import urllib.request

from typing import Optional

from .. import config
from ..utils.stderr import warn
from .base import BaseVLM, FrameDescription
from .parse import parse_frame_reply
from .prompts import get_frame_prompt


_model_avail_cache: dict = {}


def model_available(base_url: str, name: str) -> bool:
    """True if `name` is installed in the local Ollama (matched by tag or base
    name). Cached per process. Lets the fallback path skip cleanly when its model
    isn't pulled instead of erroring (404) once per failed frame. (arkiv #83)"""
    if not name:
        return False
    if name in _model_avail_cache:
        return _model_avail_cache[name]
    avail = False
    try:
        url = f"{base_url.rstrip('/')}/api/tags"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        installed = {m.get("name", "") for m in data.get("models", [])}
        base = name.split(":")[0]
        avail = name in installed or any(n.split(":")[0] == base for n in installed)
    except Exception:
        avail = False
    _model_avail_cache[name] = avail
    return avail


def _ran_out_before_answering(result: dict) -> bool:
    """The reply is empty *because* the ceiling was hit, not because the model
    had nothing to say.

    Both come back as an empty string, and they want opposite responses: one is
    worth another attempt with more room, the other would return the same empty
    answer more slowly. `done_reason` is what separates them.
    """
    return (not (result.get("response") or "").strip()
            and result.get("done_reason") == "length")


class OllamaVLM(BaseVLM):
    def __init__(self, base_url: str, model: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model or "llava"

    def _generate(self, prompt: str, img_b64: str, num_predict: int) -> dict:
        payload = {
            "model": self._model,
            "prompt": prompt,
            "images": [img_b64],
            "stream": False,
            # keep the model resident between frames so we don't pay the
            # ~30s cold-load on every keyframe (which blew the old 60s timeout)
            "keep_alive": "10m",
            # bound per-frame generation length so a single frame can't run away
            "options": {"num_predict": num_predict},
        }
        req = urllib.request.Request(
            f"{self._base_url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        # cold-load of a vision model + generation can exceed a minute on the
        # first frame; 300s covers cold start, keep_alive keeps later frames fast
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def describe_frame(
        self,
        image_path: str,
        frame_index: Optional[int] = None,
        total_frames: Optional[int] = None,
        timestamp_sec: Optional[float] = None,
        video_duration_sec: Optional[float] = None,
    ) -> FrameDescription:
        prompt = get_frame_prompt(frame_index, total_frames, timestamp_sec, video_duration_sec)

        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        budget = config.VLM_NUM_PREDICT
        result = self._generate(prompt, img_b64, budget)
        text = result.get("response", "")

        if _ran_out_before_answering(result):
            # Buy room once, for the frames that need it, instead of buying it
            # for every frame forever. On a model that reasons first the budget
            # covers reasoning *and* answer, and the reasoning length varies far
            # more than the answer does -- measured ~440 tokens typical against a
            # tail past 1200. A frame that hit the ceiling is precisely the one
            # worth a second, larger attempt; a frame that finished on its own is
            # not, and retrying it would return the same thing more slowly.
            bigger = min(int(budget * config.VLM_RETRY_MULTIPLIER),
                         config.VLM_NUM_PREDICT_MAX)
            if bigger > budget:
                result = self._generate(prompt, img_b64, bigger)
                text = result.get("response", "")
                budget = bigger

        if _ran_out_before_answering(result):
            # Say how far it already got, or whoever reads this goes and raises
            # a number that has already been raised.
            warn(
                "  VLM still answered nothing at %d tokens (retried from %d);"
                " frame left undescribed: %s"
                % (budget, config.VLM_NUM_PREDICT, image_path)
            )
        prose, in_frame, objects = parse_frame_reply(text)
        return FrameDescription(
            description=prose, text_in_frame=in_frame, objects=objects
        )
