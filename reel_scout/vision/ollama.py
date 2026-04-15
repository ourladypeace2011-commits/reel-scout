from __future__ import annotations

import base64
import json
import urllib.request

from .base import BaseVLM, FrameDescription

_PROMPT = (
    "Describe this frame from a short-form video. Include:\n"
    "1. Visual elements (people, objects, text overlays, backgrounds)\n"
    "2. Any on-screen text (OCR)\n"
    "3. Estimated mood/energy level\n"
    "4. Production style (talking head, b-roll, screen recording, etc.)\n"
    "Be concise. 2-3 sentences max."
)


class OllamaVLM(BaseVLM):
    def __init__(self, base_url: str, model: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model or "llava"

    def describe_frame(self, image_path: str) -> FrameDescription:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        payload = {
            "model": self._model,
            "prompt": _PROMPT,
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
