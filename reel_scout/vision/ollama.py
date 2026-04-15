from __future__ import annotations

import base64
import json
import urllib.request

from typing import Optional

from .base import BaseVLM, FrameDescription
from .prompts import get_frame_prompt


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
        }

        url = f"{self._base_url}/api/generate"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        text = result.get("response", "")
        return FrameDescription(description=text)
