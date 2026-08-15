"""A model that thinks before answering needs room to do both.

`num_predict` was 384, which is enough for a model that answers directly and far
too little for one that reasons first. Measured on qwen3-vl:8b: the reply comes
back `done_reason: "length"`, `eval_count: 384`, and **zero characters** -- the
entire budget went on reasoning and the answer had not started. The same frame
at 1200 stops on its own after 934 tokens with a full description.

What made it expensive to find is that nothing said so. The HTTP call succeeds,
the parse yields an empty string, the pipeline files the frame under "failed"
next to the ones that raised, and the only visible trace is a count: one clip
came back 23-of-40 empty with not a line in the log explaining why. That is
indistinguishable from a model that simply had nothing to say about the frame.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from reel_scout import config
from reel_scout.vision.ollama import OllamaVLM


class _Resp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def frame(tmp_path):
    p = tmp_path / "f.jpg"
    p.write_bytes(b"\xff\xd8\xff\xdb" + b"x" * 64)  # enough to base64
    return str(p)


def test_the_generation_budget_comes_from_config(frame, monkeypatch):
    monkeypatch.setattr(config, "VLM_NUM_PREDICT", 1234)
    seen = {}

    def _urlopen(req, timeout=None):
        seen["payload"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"response": "A shot.\nON_SCREEN_TEXT: NONE\nOBJECTS: a, b"})

    with patch("urllib.request.urlopen", side_effect=_urlopen):
        OllamaVLM("http://localhost:11434", "m").describe_frame(frame)

    assert seen["payload"]["options"]["num_predict"] == 1234, (
        "a budget that cannot be raised from outside is a budget nobody can fix")


def _urlopen_seq(*payloads):
    """Return each payload in turn, recording the num_predict it was asked with."""
    seen = []
    it = iter(payloads)

    def _open(req, timeout=None):
        seen.append(json.loads(req.data.decode("utf-8"))["options"]["num_predict"])
        return _Resp(next(it))

    return _open, seen


def test_hitting_the_ceiling_buys_more_room_once(frame, monkeypatch):
    """The frames that need a bigger budget are exactly the ones that hit it.

    Raising the default for everyone would race a distribution with no upper
    bound and charge every frame for a tail that reaches one in seven. The VLM
    is the most expensive step here, so the room gets bought per frame, on
    evidence, once.
    """
    monkeypatch.setattr(config, "VLM_NUM_PREDICT", 1000)
    monkeypatch.setattr(config, "VLM_RETRY_MULTIPLIER", 3)
    monkeypatch.setattr(config, "VLM_NUM_PREDICT_MAX", 9999)

    _open, seen = _urlopen_seq(
        {"response": "", "done_reason": "length", "eval_count": 1000},
        {"response": "A market.\nON_SCREEN_TEXT: NONE\nOBJECTS: stalls",
         "done_reason": "stop", "eval_count": 1400},
    )
    with patch("urllib.request.urlopen", side_effect=_open):
        out = OllamaVLM("http://localhost:11434", "m").describe_frame(frame)

    assert seen == [1000, 3000], "second attempt must actually ask for more room"
    assert out.description, "and the answer from it is the one that is kept"


def test_a_model_that_stopped_on_its_own_is_not_retried(frame, monkeypatch, capsys):
    # Empty because it hit the ceiling and empty because it had nothing to say
    # look identical in `response` and want opposite responses. Retrying the
    # second returns the same nothing, more slowly.
    monkeypatch.setattr(config, "VLM_NUM_PREDICT", 1000)
    _open, seen = _urlopen_seq({"response": "", "done_reason": "stop", "eval_count": 12})
    with patch("urllib.request.urlopen", side_effect=_open):
        OllamaVLM("http://localhost:11434", "m").describe_frame(frame)

    assert seen == [1000], "one attempt only"
    assert "answered nothing" not in capsys.readouterr().err


def test_the_retry_is_capped(frame, monkeypatch):
    monkeypatch.setattr(config, "VLM_NUM_PREDICT", 1200)
    monkeypatch.setattr(config, "VLM_RETRY_MULTIPLIER", 10)
    monkeypatch.setattr(config, "VLM_NUM_PREDICT_MAX", 2000)

    _open, seen = _urlopen_seq(
        {"response": "", "done_reason": "length"},
        {"response": "ok\nON_SCREEN_TEXT: NONE\nOBJECTS: a", "done_reason": "stop"},
    )
    with patch("urllib.request.urlopen", side_effect=_open):
        OllamaVLM("http://localhost:11434", "m").describe_frame(frame)

    assert seen == [1200, 2000], "the ceiling is a ceiling, not a suggestion"


def test_still_empty_after_the_retry_says_it_already_tried(frame, monkeypatch, capsys):
    monkeypatch.setattr(config, "VLM_NUM_PREDICT", 1200)
    monkeypatch.setattr(config, "VLM_RETRY_MULTIPLIER", 3)
    monkeypatch.setattr(config, "VLM_NUM_PREDICT_MAX", 9999)

    _open, _ = _urlopen_seq(
        {"response": "", "done_reason": "length"},
        {"response": "", "done_reason": "length"},
    )
    with patch("urllib.request.urlopen", side_effect=_open):
        out = OllamaVLM("http://localhost:11434", "m").describe_frame(frame)

    assert out.description == ""
    err = capsys.readouterr().err
    assert "3600" in err, "say how far it already got"
    assert "retried from 1200" in err, (
        "or whoever reads this raises a number that has already been raised")
    assert frame in err, "and which frame, or it cannot be picked up later"


def test_a_model_with_genuinely_nothing_to_say_is_not_reported_as_a_budget_problem(
    frame, capsys
):
    # `done_reason: "stop"` means the model finished on its own. An empty answer
    # there is a different fact, and blaming the budget for it would send someone
    # to raise a number that was never the problem.
    resp = _Resp({"response": "", "done_reason": "stop", "eval_count": 12})
    with patch("urllib.request.urlopen", return_value=resp):
        OllamaVLM("http://localhost:11434", "m").describe_frame(frame)
    assert "without answering" not in capsys.readouterr().err


def test_a_normal_answer_stays_quiet(frame, capsys):
    resp = _Resp({"response": "A market.\nON_SCREEN_TEXT: NONE\nOBJECTS: stalls",
                  "done_reason": "stop", "eval_count": 900})
    with patch("urllib.request.urlopen", return_value=resp):
        out = OllamaVLM("http://localhost:11434", "m").describe_frame(frame)
    assert out.description
    assert "without answering" not in capsys.readouterr().err


# The measurement this default was set from, kept next to the number it set.
MEASURED_TOKENS_TO_FINISH = 934  # qwen3-vl:8b, one library frame, done_reason "stop"


def test_the_default_budget_clears_what_a_thinking_model_actually_needs():
    """Pins a calibration to the evidence that produced it, not to a magic number.

    384 was not slightly low, it was below the point where the answer *starts*:
    the model evaluated all 384 tokens reasoning and returned nothing at all.
    A default under the measured requirement means every frame on that model
    comes back empty, and comes back empty quietly.
    """
    assert config.VLM_NUM_PREDICT > MEASURED_TOKENS_TO_FINISH, (
        "the default has to leave room for a model that reasons before it answers; "
        "%d tokens was measured as the point one finishes on its own"
        % MEASURED_TOKENS_TO_FINISH)


def test_a_truncated_answer_is_kept_rather_than_bought_again(frame, monkeypatch):
    """Hitting the ceiling *with* an answer is a different trade.

    The model can fill the budget and still have said something useful; the tail
    of that sentence is missing, not the whole description. Retrying would buy a
    cleaner ending at the price of a second full VLM call -- the most expensive
    step in this pipeline -- on a frame that already has content. Empty is worth
    paying to fix; truncated is not.
    """
    monkeypatch.setattr(config, "VLM_NUM_PREDICT", 1000)
    _open, seen = _urlopen_seq({
        "response": "A market scene with stalls and\nON_SCREEN_TEXT: NONE\nOBJECTS: a",
        "done_reason": "length"})
    with patch("urllib.request.urlopen", side_effect=_open):
        out = OllamaVLM("http://localhost:11434", "m").describe_frame(frame)

    assert seen == [1000], "an answer in hand is not re-bought"
    assert out.description
