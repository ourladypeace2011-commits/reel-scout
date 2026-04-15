from __future__ import annotations

import base64
import json
import urllib.request

from typing import Optional

from .base import BaseVLM, FrameDescription
from .prompts import get_frame_prompt


class OmlxVLM(BaseVLM):
    def __init__(self, base_url: str, model: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model

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

        # Detect mime type
        ext = image_path.rsplit(".", 1)[-1].lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(
            ext, "image/jpeg"
        )

        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{img_b64}"
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "max_tokens": 300,
        }
        if self._model:
            payload["model"] = self._model

        url = f"{self._base_url}/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        text = result["choices"][0]["message"]["content"]
        return FrameDescription(description=text)
