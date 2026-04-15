from __future__ import annotations

import json
import urllib.request

from .base import BaseLLM


class OllamaLLM(BaseLLM):
    def __init__(self, base_url: str, model: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model or "llama3"

    def complete(
        self,
        prompt: str,
        max_tokens: int = 800,
        temperature: float = 0.1,
    ) -> str:
        payload = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }
        url = "%s/api/generate" % self._base_url
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result.get("response", "")
