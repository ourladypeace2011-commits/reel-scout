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


class OllamaVLM(BaseVLM):
    def __init__(self, base_url: str, model: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model or "llava"

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

        payload = {
            "model": self._model,
            "prompt": prompt,
            "images": [img_b64],
            "stream": False,
            # keep the model resident between frames so we don't pay the
            # ~30s cold-load on every keyframe (which blew the old 60s timeout)
            "keep_alive": "10m",
            # bound per-frame generation length so a single frame can't run away
            "options": {"num_predict": config.VLM_NUM_PREDICT},
        }

        url = f"{self._base_url}/api/generate"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )

        # cold-load of a vision model + generation can exceed a minute on the
        # first frame; 300s covers cold start, keep_alive keeps later frames fast
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        text = result.get("response", "")
        if not text.strip() and result.get("done_reason") == "length":
            # The generation budget ran out before the model said anything. On a
            # model that reasons before answering, that is the whole budget spent
            # on reasoning -- measured on qwen3-vl:8b at num_predict=384: 384
            # tokens evaluated, zero characters returned.
            #
            # Worth a line of its own, because everything downstream reads this
            # as "the frame failed" and files it with the ones that raised. One
            # clip came back 23-of-40 empty with nothing in the log saying why,
            # which looks identical to a model that simply had nothing to say.
            warn(
                "  VLM spent its whole %d-token budget without answering"
                " (raise VLM_NUM_PREDICT); frame left undescribed: %s"
                % (config.VLM_NUM_PREDICT, image_path)
            )
        prose, in_frame, objects = parse_frame_reply(text)
        return FrameDescription(
            description=prose, text_in_frame=in_frame, objects=objects
        )
