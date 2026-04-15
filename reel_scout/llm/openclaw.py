from __future__ import annotations

import json
import os
import urllib.request

from .base import BaseLLM


class OpenClawLLM(BaseLLM):
    def __init__(self, base_url: str, model: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = os.getenv("OPENCLAW_API_KEY", "")

    def complete(
        self,
        prompt: str,
        max_tokens: int = 800,
        temperature: float = 0.1,
    ) -> str:
        payload = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if self._model:
            payload["model"] = self._model

        url = "%s/chat/completions" % self._base_url
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = "Bearer %s" % self._api_key

        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result["choices"][0]["message"]["content"]
