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
