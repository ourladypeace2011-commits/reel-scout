"""whisper.cpp backend — preconditions and the JSON-sidecar contract.

This backend had never run. Three defects kept it dead and none was reachable
by the suite, because nothing exercised it at all:

  1. `--output-json` writes `<wav>.json`; the old code parsed stdout.
  2. `-m` wants a ggml **file path**; the factory passed the faster-whisper
     model **name** ("large-v3").
  3. the default binary was "whisper"; upstream renamed it "whisper-cli".

There is no ggml model on the dev machine, so these tests deliberately do NOT
try to prove transcription works. They pin the two things that can be proven
without a model: that the backend refuses to start when a precondition is
missing and says which one, and that a successful run is read from the sidecar
file rather than stdout.
"""
from __future__ import annotations

import json
import os

import pytest

from reel_scout.transcribe import whisper_cpp
from reel_scout.transcribe.whisper_cpp import WhisperCppTranscriber, resolve_binary


# --- preflight --------------------------------------------------------------

def test_preflight_names_missing_binary_and_model(monkeypatch):
    monkeypatch.setattr(whisper_cpp.shutil, "which", lambda _n: None)
    t = WhisperCppTranscriber(model="", binary="")
    problems = t.preflight()
    assert len(problems) == 2
    joined = " ".join(problems)
    assert "binary not found" in joined
    assert "WHISPER_CPP_MODEL" in joined


def test_preflight_rejects_a_model_name_that_is_not_a_path(monkeypatch, tmp_path):
    """Defect 2: 'large-v3' is a faster-whisper name, not a ggml file."""
    monkeypatch.setattr(whisper_cpp.shutil, "which", lambda _n: "/usr/bin/whisper-cli")
    t = WhisperCppTranscriber(model="large-v3", binary="whisper-cli")
    problems = t.preflight()
    assert len(problems) == 1
    assert "does not exist" in problems[0]
    assert "large-v3" in problems[0]


def test_preflight_clean_when_both_present(monkeypatch, tmp_path):
    model = tmp_path / "ggml-large-v3.bin"
    model.write_bytes(b"not really a model")
    monkeypatch.setattr(whisper_cpp.shutil, "which", lambda _n: "/usr/bin/whisper-cli")
    t = WhisperCppTranscriber(model=str(model), binary="whisper-cli")
    assert t.preflight() == []


def test_transcribe_refuses_loudly_instead_of_running(monkeypatch, tmp_path):
    """A backend that cannot work must stop, not produce an empty transcript."""
    monkeypatch.setattr(whisper_cpp.shutil, "which", lambda _n: None)
    called = []
    monkeypatch.setattr(whisper_cpp.subprocess, "run",
                        lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
                            AssertionError("must not shell out")))
    t = WhisperCppTranscriber(model="", binary="")
    with pytest.raises(RuntimeError) as exc:
        t.transcribe(str(tmp_path / "clip.wav"))
    msg = str(exc.value)
    assert "not usable" in msg
    # the two capability gaps must travel with the refusal, or the next person
    # fixes the paths and silently loses the zh seed + guard
    assert "Traditional-Chinese" in msg
    assert "whisper-guard" in msg
    assert called == []


# --- binary resolution (defect 3) -------------------------------------------

def test_resolve_binary_prefers_the_current_upstream_name(monkeypatch):
    seen = []

    def fake_which(name):
        seen.append(name)
        return "/opt/homebrew/bin/whisper-cli" if name == "whisper-cli" else None

    monkeypatch.setattr(whisper_cpp.shutil, "which", fake_which)
    assert resolve_binary() == "/opt/homebrew/bin/whisper-cli"
    assert seen[0] == "whisper-cli"


def test_resolve_binary_falls_back_to_legacy_names(monkeypatch):
    monkeypatch.setattr(whisper_cpp.shutil, "which",
                        lambda n: "/usr/local/bin/main" if n == "main" else None)
    assert resolve_binary() == "/usr/local/bin/main"


# --- the sidecar contract (defect 1) ----------------------------------------

class _Ran:
    returncode = 0
    stdout = ""      # whisper.cpp prints progress here, never the JSON
    stderr = ""


def _wire(monkeypatch, tmp_path, payload, *, write_json=True):
    model = tmp_path / "ggml-large-v3.bin"
    model.write_bytes(b"stub")
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF")

    monkeypatch.setattr(whisper_cpp.shutil, "which", lambda _n: "/usr/bin/whisper-cli")
    monkeypatch.setattr("reel_scout.ffprobe.probe_duration", lambda _p: 61.0)

    def fake_run(cmd, **kwargs):
        if write_json:
            with open(str(wav) + ".json", "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
        return _Ran()

    monkeypatch.setattr(whisper_cpp.subprocess, "run", fake_run)
    return WhisperCppTranscriber(model=str(model), binary="whisper-cli"), wav


def test_result_is_read_from_the_sidecar_not_stdout(monkeypatch, tmp_path):
    payload = {
        "result": {"language": "zh"},
        "transcription": [
            {"text": " 第一句 ", "offsets": {"from": 0, "to": 1500}},
            {"text": "", "offsets": {"from": 1500, "to": 1600}},
            {"text": "第二句", "offsets": {"from": 1600, "to": 3000}},
        ],
    }
    t, wav = _wire(monkeypatch, tmp_path, payload)
    out = t.transcribe(str(wav))

    assert out.language == "zh"
    assert out.text_full == "第一句 第二句"      # blank segment dropped
    assert [s.start for s in out.segments] == [0.0, 1.6]
    assert out.segments[0].end == 1.5
    # 0.0 used to be hardcoded, which silently disabled the overrun detector
    assert out.duration_sec == 61.0
    assert out.model == "ggml-large-v3.bin"
    # sidecar cleaned up
    assert not os.path.exists(str(wav) + ".json")


def test_missing_sidecar_is_an_error_not_an_empty_transcript(monkeypatch, tmp_path):
    t, wav = _wire(monkeypatch, tmp_path, {}, write_json=False)
    with pytest.raises(RuntimeError) as exc:
        t.transcribe(str(wav))
    assert "wrote no JSON" in str(exc.value)


# --- timeouts (the 300s that made long files impossible) --------------------

def test_transcribe_timeout_scales_with_audio_length(monkeypatch, tmp_path):
    captured = {}
    payload = {"result": {"language": "en"}, "transcription": []}

    model = tmp_path / "ggml-large-v3.bin"
    model.write_bytes(b"stub")
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF")

    monkeypatch.setattr(whisper_cpp.shutil, "which", lambda _n: "/usr/bin/whisper-cli")
    # a 3h39m livestream -- the real input that exposed the flat 300s ceiling
    monkeypatch.setattr("reel_scout.ffprobe.probe_duration", lambda _p: 13170.0)

    def fake_run(cmd, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        with open(str(wav) + ".json", "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return _Ran()

    monkeypatch.setattr(whisper_cpp.subprocess, "run", fake_run)
    WhisperCppTranscriber(model=str(model), binary="whisper-cli").transcribe(str(wav))

    assert captured["timeout"] == pytest.approx(26340.0)   # 2x audio
    assert captured["timeout"] > 300                        # the old ceiling


def test_short_clip_still_gets_a_floor(monkeypatch, tmp_path):
    captured = {}
    payload = {"result": {"language": "en"}, "transcription": []}
    model = tmp_path / "ggml.bin"
    model.write_bytes(b"stub")
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(whisper_cpp.shutil, "which", lambda _n: "/usr/bin/whisper-cli")
    monkeypatch.setattr("reel_scout.ffprobe.probe_duration", lambda _p: 8.0)

    def fake_run(cmd, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        with open(str(wav) + ".json", "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return _Ran()

    monkeypatch.setattr(whisper_cpp.subprocess, "run", fake_run)
    WhisperCppTranscriber(model=str(model), binary="whisper-cli").transcribe(str(wav))
    # model load dominates on tiny inputs; 2x8s would kill a cold first run
    assert captured["timeout"] == 600.0


# --- ffmpeg rc (a failed convert used to look like silent audio) -------------

def test_failed_conversion_raises_instead_of_transcribing_silence(monkeypatch, tmp_path):
    model = tmp_path / "ggml.bin"
    model.write_bytes(b"stub")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00\x00")

    monkeypatch.setattr(whisper_cpp.shutil, "which", lambda _n: "/usr/bin/whisper-cli")
    monkeypatch.setattr("reel_scout.ffprobe.probe_duration", lambda _p: 30.0)

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "Invalid data found when processing input"

    monkeypatch.setattr(whisper_cpp.subprocess, "run", lambda *a, **k: _Failed())
    t = WhisperCppTranscriber(model=str(model), binary="whisper-cli")
    with pytest.raises(RuntimeError) as exc:
        t.transcribe(str(src))
    assert "could not convert" in str(exc.value)
