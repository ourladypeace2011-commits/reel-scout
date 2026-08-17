from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from reel_scout import config
from reel_scout.llm import get_llm
from reel_scout.llm.ollama import OllamaLLM
from reel_scout.llm.omlx import OmlxLLM
from reel_scout.llm.openclaw import OpenClawLLM


class _MockResponse(object):
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_get_llm_omlx(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "omlx")
    monkeypatch.setattr(config, "LLM_MODEL", "text-model")
    llm = get_llm()
    assert isinstance(llm, OmlxLLM)


def test_get_llm_ollama(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "ollama")
    monkeypatch.setattr(config, "LLM_MODEL", "ollama-model")
    llm = get_llm()
    assert isinstance(llm, OllamaLLM)


def test_get_llm_openclaw(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "openclaw")
    monkeypatch.setattr(config, "OPENCLAW_MODEL", "claude-sonnet")
    llm = get_llm()
    assert isinstance(llm, OpenClawLLM)


def test_get_llm_unknown():
    with pytest.raises(ValueError):
        get_llm("bad-backend")


def test_omlx_complete():
    captured = {}

    def _mock_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _MockResponse({"choices": [{"message": {"content": "omlx ok"}}]})

    llm = OmlxLLM(base_url="http://localhost:8000/v1", model="text-model")
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
        result = llm.complete("hello", max_tokens=123, temperature=0.3)

    assert result == "omlx ok"
    assert captured["url"] == "http://localhost:8000/v1/chat/completions"
    assert captured["body"]["model"] == "text-model"
    assert captured["body"]["messages"][0]["content"] == "hello"
    assert captured["body"]["max_tokens"] == 123
    assert captured["body"]["temperature"] == 0.3


def test_ollama_complete():
    captured = {}

    def _mock_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _MockResponse({"response": "ollama ok"})

    llm = OllamaLLM(base_url="http://localhost:11434", model="llama3.2")
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
        result = llm.complete("prompt text", max_tokens=77, temperature=0.4)

    assert result == "ollama ok"
    assert captured["url"] == "http://localhost:11434/api/generate"
    assert captured["body"]["model"] == "llama3.2"
    assert captured["body"]["prompt"] == "prompt text"
    assert captured["body"]["stream"] is False
    assert captured["body"]["options"]["num_predict"] == 77
    assert captured["body"]["options"]["temperature"] == 0.4


def test_openclaw_complete(monkeypatch):
    captured = {}
    monkeypatch.setenv("OPENCLAW_API_KEY", "secret-key")

    def _mock_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _MockResponse({"choices": [{"message": {"content": "openclaw ok"}}]})

    llm = OpenClawLLM(base_url="http://localhost:18789/v1", model="claude-sonnet")
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
        result = llm.complete("review me")

    assert result == "openclaw ok"
    assert captured["url"] == "http://localhost:18789/v1/chat/completions"
    assert captured["body"]["model"] == "claude-sonnet"
    assert captured["headers"]["Authorization"] == "Bearer secret-key"


def test_openclaw_no_key(monkeypatch):
    captured = {}
    monkeypatch.delenv("OPENCLAW_API_KEY", raising=False)

    def _mock_urlopen(request, timeout=0):
        captured["headers"] = dict(request.header_items())
        return _MockResponse({"choices": [{"message": {"content": "no key"}}]})

    llm = OpenClawLLM(base_url="http://localhost:18789/v1", model="")
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
        result = llm.complete("review me")

    assert result == "no key"
    assert "Authorization" not in captured["headers"]


# --- LLM_TIMEOUT / retry (2026-08-15) ------------------------------------
#
# The 600s timeout used to be a literal in ollama.py. The failure it hid:
# 2026-08-11 a 96-clip re-merge lost 3 clips to Ollama contention, and the same
# clips re-ran fine at 84/125/101s on an idle box. Contention is an external
# condition; a fixed constant is an internal choice. These tests pin both halves
# of the fix — the timeout is reachable from config, and a timeout is retried
# while a non-timeout is not.


def test_ollama_uses_config_timeout(monkeypatch):
    monkeypatch.setattr(config, "LLM_TIMEOUT", 12.5)
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 0)
    seen = {}

    def _mock_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return _MockResponse({"response": "ok"})

    llm = OllamaLLM("http://localhost:11434", "m")
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
        assert llm.complete("x") == "ok"
    # Not 600 — the whole point is that this number now comes from outside.
    assert seen["timeout"] == 12.5


def test_ollama_retries_timeout_then_succeeds(monkeypatch):
    import socket as _socket

    monkeypatch.setattr(config, "LLM_TIMEOUT", 1)
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 2)
    monkeypatch.setattr(config, "LLM_RETRY_BACKOFF", 0)  # keep the test instant
    calls = {"n": 0}

    def _mock_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _socket.timeout("timed out")
        return _MockResponse({"response": "late but fine"})

    llm = OllamaLLM("http://localhost:11434", "m")
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
        assert llm.complete("x") == "late but fine"
    assert calls["n"] == 3


def test_ollama_gives_up_after_max_retries(monkeypatch):
    import socket as _socket

    monkeypatch.setattr(config, "LLM_TIMEOUT", 1)
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "LLM_RETRY_BACKOFF", 0)
    calls = {"n": 0}

    def _mock_urlopen(req, timeout=None):
        calls["n"] += 1
        raise _socket.timeout("timed out")

    llm = OllamaLLM("http://localhost:11434", "m")
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
        with pytest.raises(_socket.timeout):
            llm.complete("x")
    # 1 retry == 2 attempts, and the final failure is raised rather than
    # swallowed — a silently empty merge is what this change exists to prevent.
    assert calls["n"] == 2


def test_ollama_does_not_retry_non_timeout(monkeypatch):
    import urllib.error

    monkeypatch.setattr(config, "LLM_TIMEOUT", 1)
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 3)
    monkeypatch.setattr(config, "LLM_RETRY_BACKOFF", 0)
    calls = {"n": 0}

    def _mock_urlopen(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError("u", 404, "model not found", {}, None)

    llm = OllamaLLM("http://localhost:11434", "m")
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
        with pytest.raises(urllib.error.HTTPError):
            llm.complete("x")
    # A 404 fails identically on attempt four; retrying it only delays the
    # error by LLM_TIMEOUT x LLM_MAX_RETRIES.
    assert calls["n"] == 1


def test_ollama_says_so_when_a_200_carries_no_response_field(monkeypatch, capsys):
    # An empty `response` is legitimate: the model produced nothing. A *missing*
    # one means the 200 is not a generate reply at all — an error envelope, a
    # proxy in front of Ollama, a schema that moved. Both return "", so without
    # this line the merger gets an empty result that reads as a successful call.
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 0)

    llm = OllamaLLM("http://localhost:11434", "m")
    with patch("urllib.request.urlopen", return_value=_MockResponse({"error": "nope"})):
        assert llm.complete("x") == ""
    err = capsys.readouterr().err
    assert "no 'response' field" in err
    assert "error" in err, "name the keys that did arrive, or there is nothing to debug"


def test_ollama_stays_quiet_when_the_model_legitimately_returns_nothing(
    monkeypatch, capsys
):
    # The guard must not fire on the normal empty answer, or it is noise.
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 0)

    llm = OllamaLLM("http://localhost:11434", "m")
    with patch("urllib.request.urlopen", return_value=_MockResponse({"response": ""})):
        assert llm.complete("x") == ""
    assert "no 'response' field" not in capsys.readouterr().err
