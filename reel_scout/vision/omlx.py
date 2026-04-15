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


class OmlxVLM(BaseVLM):
    def __init__(self, base_url: str, model: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model

    def describe_frame(self, image_path: str) -> FrameDescription:
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
                        {"type": "text", "text": _PROMPT},
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
