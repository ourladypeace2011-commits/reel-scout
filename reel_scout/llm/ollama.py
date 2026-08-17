from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

from .. import config
from ..utils.stderr import warn
from .base import BaseLLM


def _is_timeout(exc: BaseException) -> bool:
    """True when the call died waiting, not when it died being wrong.

    urllib raises a bare `socket.timeout` (an alias of `TimeoutError` since 3.10)
    for a read timeout, but wraps a connect timeout in `URLError`. Both mean "the
    box did not answer in time" and both are worth retrying; an HTTPError (400,
    404 model-not-found) is not — it will fail identically on attempt three.

    HTTPError needs no branch of its own even though it subclasses URLError: its
    `.reason` is the status message string, never a timeout, so the URLError test
    below already returns False for it. An explicit early return was written here
    first and removed — deleting it changed no behaviour, which is the tell that
    it was decoration. A guard a mutation can remove for free is not a guard; it
    just makes the mutation report look covered.
    """
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, (socket.timeout, TimeoutError))
    return False


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
        # Retry timeouts with backoff. Everything else is raised on the first
        # attempt — see _is_timeout for why.
        attempts = max(1, config.LLM_MAX_RETRIES + 1)
        for i in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                if "response" not in result:
                    # A 200 whose body is not a generate response: an error
                    # envelope, a proxy in front of Ollama, a schema that moved.
                    # An empty `response` is legitimate (the model produced
                    # nothing); a *missing* one means this is not the reply we
                    # think it is, and returning "" for it hands the merger an
                    # empty result that looks like a successful call. Not raised,
                    # because some compatible backends may legitimately differ
                    # and a hard error would break a setup that works today.
                    keys = ", ".join(sorted(result)) or "none"
                    warn(
                        "  LLM returned 200 with no 'response' field"
                        " (keys: %s); treating as empty" % keys
                    )
                return result.get("response", "")
            except Exception as exc:  # noqa: BLE001
                last = i == attempts - 1
                if last or not _is_timeout(exc):
                    raise
                wait = config.LLM_RETRY_BACKOFF * (2 ** i)
                warn(
                    "  LLM timed out after %gs (attempt %d/%d); retrying in %gs"
                    % (config.LLM_TIMEOUT, i + 1, attempts, wait)
                )
                time.sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover
