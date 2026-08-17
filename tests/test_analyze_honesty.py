"""`analyze` had the two holes `batch` was just fixed for, and I walked into both.

Backfilling descriptions for two clips, `reel-scout analyze <url>` printed
`Batch <id> completed.` and exited 0 while the single item in each batch had
errored on the line above. Nothing landed. A wrapper reading `$?` is told the
work is done.

The second hole was in the message that explained the failure. The vision
fallback is gated on four conditions and the log named only the last one:

    1 frame(s) failed; fallback 'qwen3-vl:8b' unavailable

The backend was omlx, so the first condition was already false and the model
was never even checked -- and qwen3-vl:8b was installed and answering. Acting
on that sentence means installing a model that is already there.

Both are the same defect as the batch one: the run reports fine and the result
is quietly missing.
"""

from __future__ import annotations

import pytest

from reel_scout import cli
from reel_scout.analyze import pipeline


# --- exit code ------------------------------------------------------------

def _args_ok(monkeypatch, errors):
    monkeypatch.setattr(pipeline, "run", lambda urls, options: errors)


def test_analyze_with_a_failed_item_does_not_exit_zero(monkeypatch):
    _args_ok(monkeypatch, errors=1)
    with pytest.raises(SystemExit) as exc:
        cli.main(["analyze", "https://www.instagram.com/reel/AbCdEfG0001/"])
    assert exc.value.code == 1


def test_analyze_where_every_item_failed_does_not_exit_zero(monkeypatch):
    _args_ok(monkeypatch, errors=3)
    with pytest.raises(SystemExit) as exc:
        cli.main(["analyze", "https://www.instagram.com/reel/AbCdEfG0001/"])
    assert exc.value.code == 1


def test_analyze_where_everything_landed_exits_zero(monkeypatch):
    _args_ok(monkeypatch, errors=0)
    cli.main(["analyze", "https://www.instagram.com/reel/AbCdEfG0001/"])


def test_analyze_with_no_urls_at_all_does_not_exit_zero(monkeypatch):
    # Reaching the pipeline would itself be the bug, so make that loud.
    monkeypatch.setattr(
        pipeline, "run",
        lambda urls, options: pytest.fail("pipeline ran with no URLs"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["analyze"])
    assert exc.value.code == 1


# --- the batch line has to agree with what happened ------------------------

def test_a_batch_with_failures_does_not_call_itself_plain_completed(
        monkeypatch, capsys, tmp_path):
    """The word on screen and the exit code must not disagree."""
    calls = {"n": 0}

    def _boom(conn, url, options):
        calls["n"] += 1
        raise RuntimeError("Local file not found: %s" % url)

    monkeypatch.setattr(pipeline, "_process_single", _boom)
    monkeypatch.setattr(pipeline.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(pipeline.config, "DB_PATH", str(tmp_path / "t.db"))

    errors = pipeline.run(["https://www.instagram.com/reel/AbCdEfG0001/",
                           "https://www.instagram.com/reel/AbCdEfG0002/"])
    out = capsys.readouterr().out

    assert calls["n"] == 2
    assert errors == 2
    assert "completed with 2/2 failed" in out
    # The bare word is what a reader skims for; it must not be there alone.
    assert "completed.\n" not in out


# --- the fallback message names the reason that actually applied -----------

def _never(*_a, **_k):
    raise AssertionError("model_available probed when a cheaper reason applied")


def test_a_non_ollama_backend_is_not_reported_as_a_missing_model():
    why = pipeline.fallback_blocked_because(
        "omlx", "qwen3-vl:8b", "qwen2.5vl:7b", "http://localhost:11434", _never)
    assert "omlx" in why
    # The old message. It was false here and cost a round of chasing.
    assert "not installed" not in why


def test_an_unset_fallback_says_so():
    why = pipeline.fallback_blocked_because(
        "ollama", "", "qwen2.5vl:7b", "http://localhost:11434", _never)
    assert "VLM_FALLBACK_MODEL" in why


def test_a_fallback_equal_to_the_primary_says_so():
    why = pipeline.fallback_blocked_because(
        "ollama", "qwen2.5vl:7b", "qwen2.5vl:7b", "http://localhost:11434", _never)
    assert "just failed" in why


def test_a_genuinely_missing_model_still_says_not_installed():
    why = pipeline.fallback_blocked_because(
        "ollama", "qwen3-vl:8b", "qwen2.5vl:7b", "http://localhost:11434",
        lambda base, name: False)
    assert "not installed" in why
    assert "http://localhost:11434" in why  # where it looked, so it can be checked


def test_nothing_blocks_it_when_all_four_hold():
    why = pipeline.fallback_blocked_because(
        "ollama", "qwen3-vl:8b", "qwen2.5vl:7b", "http://localhost:11434",
        lambda base, name: True)
    assert why == ""
