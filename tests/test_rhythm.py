from __future__ import annotations

import os
import struct
import tempfile
import wave

import pytest

from reel_scout.audio import rhythm


def _write_wav(path, samples, sr=16000):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        frames = b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in samples
        )
        wf.writeframes(frames)


def test_rms_silence():
    assert rhythm._rms([0.0] * 100) == 0.0


def test_rms_constant():
    assert rhythm._rms([0.5] * 100) == pytest.approx(0.5, abs=1e-6)


def test_rms_empty():
    assert rhythm._rms([]) == 0.0


def test_compute_rhythm_reads_wav_energy():
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        _write_wav(path, [0.3] * 16000)  # 1s of constant-amplitude tone
        r = rhythm.compute_rhythm(path)
        assert r["energy"] == pytest.approx(0.3, abs=0.01)
        assert "bpm" in r
    finally:
        os.unlink(path)


def test_compute_rhythm_bad_path():
    assert rhythm.compute_rhythm("/nonexistent-xyz.wav") == {
        "energy": None, "bpm": None, "candidate_bpm": None, "peak_ratio": None,
    }


def test_compute_rhythm_corrupt_wav_returns_none():
    # A text file with a .wav name makes wave.open raise wave.Error, which must be
    # swallowed into the None result rather than propagating.
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        with open(path, "w") as f:
            f.write("definitely not a RIFF/WAV file")
        assert rhythm.compute_rhythm(path) == {
            "energy": None, "bpm": None, "candidate_bpm": None, "peak_ratio": None,
        }
    finally:
        os.unlink(path)


def test_compute_rhythm_shape_is_uniform_across_paths():
    # Callers read the evidence keys without first working out which failure
    # they got, so every return path must carry the same keys.
    keys = {"energy", "bpm", "candidate_bpm", "peak_ratio"}
    assert set(rhythm.compute_rhythm("/nonexistent-xyz.wav")) == keys
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        _write_wav(path, [0.3] * 16000)
        assert set(rhythm.compute_rhythm(path)) == keys
    finally:
        os.unlink(path)


def test_estimate_bpm_in_range_when_present():
    pytest.importorskip("numpy")
    sr = 16000
    samples = [0.0] * (sr * 4)  # 4s
    # Click track: a short burst every 0.5s => ~120 BPM.
    for beat in range(8):
        idx = int(beat * 0.5 * sr)
        for k in range(200):
            if idx + k < len(samples):
                samples[idx + k] = 0.8
    bpm = rhythm.estimate_bpm(samples, sr)
    # Best-effort: may be None, but if it commits to a tempo it must be in-range.
    if bpm is not None:
        assert 60.0 <= bpm <= 180.0


def test_estimate_bpm_too_short_returns_none():
    pytest.importorskip("numpy")
    assert rhythm.estimate_bpm([0.1, 0.2, 0.3], 16000) is None


def test_analyze_bpm_and_estimate_bpm_agree():
    pytest.importorskip("numpy")
    sr = 16000
    samples = [0.0] * (sr * 4)
    for beat in range(8):
        idx = int(beat * 0.5 * sr)
        for k in range(200):
            if idx + k < len(samples):
                samples[idx + k] = 0.8
    detail = rhythm.analyze_bpm(samples, sr)
    assert set(detail) == {"bpm", "candidate_bpm", "peak_ratio"}
    assert rhythm.estimate_bpm(samples, sr) == detail["bpm"]


def test_analyze_bpm_too_short_has_no_candidate():
    pytest.importorskip("numpy")
    assert rhythm.analyze_bpm([0.1, 0.2, 0.3], 16000) == {
        "bpm": None, "candidate_bpm": None, "peak_ratio": None,
    }


def test_near_miss_note_silent_when_tempo_accepted():
    assert rhythm.near_miss_note(
        {"bpm": 120.0, "candidate_bpm": 120.0, "peak_ratio": 0.4}
    ) is None


def test_near_miss_note_silent_without_a_candidate():
    assert rhythm.near_miss_note(
        {"bpm": None, "candidate_bpm": None, "peak_ratio": None}
    ) is None


def test_near_miss_note_silent_when_far_below_gate():
    # Genuinely beatless audio: naming a "candidate" here would be noise.
    assert rhythm.near_miss_note(
        {"bpm": None, "candidate_bpm": 91.2, "peak_ratio": 0.004}
    ) is None


def test_near_miss_note_reports_candidate_and_ratio():
    # The real case this exists for: a squashed master rejected at 0.091
    # against a 0.10 gate (measured on ASIAN STATE OF MIND, 2026-09-01).
    note = rhythm.near_miss_note(
        {"bpm": None, "candidate_bpm": 64.7, "peak_ratio": 0.091}
    )
    assert note is not None
    assert "64.7" in note
    assert "0.091" in note
    assert "0.10" in note


def test_near_miss_floor_sits_below_the_gate():
    assert rhythm.BPM_NEAR_MISS_FLOOR < rhythm.BPM_PEAK_RATIO_MIN


def _click_track(sr=16000, seconds=4, interval=0.5):
    samples = [0.0] * (sr * seconds)
    for beat in range(int(seconds / interval)):
        idx = int(beat * interval * sr)
        for k in range(200):
            if idx + k < len(samples):
                samples[idx + k] = 0.8
    return samples


def test_analyze_bpm_keeps_the_candidate_when_the_gate_rejects_it(monkeypatch):
    # The whole point of the change: a rejected peak must survive as evidence.
    # Raising the gate above any real ratio forces the rejection deterministically.
    pytest.importorskip("numpy")
    sr = 16000
    samples = _click_track(sr)
    accepted = rhythm.analyze_bpm(samples, sr)
    assert accepted["bpm"] is not None, "click track should normally pass the gate"

    monkeypatch.setattr(rhythm, "BPM_PEAK_RATIO_MIN", 0.99)
    gated = rhythm.analyze_bpm(samples, sr)
    assert gated["bpm"] is None
    # Compare against the *accepted tempo*, not against the gated run's own
    # candidate: a mutation that blanks candidate_bpm blanks it on both runs and
    # would slip through a candidate-to-candidate comparison.
    assert gated["candidate_bpm"] == accepted["bpm"]
    assert gated["peak_ratio"] == accepted["peak_ratio"]
    assert gated["peak_ratio"] is not None


def test_near_miss_note_reports_at_exactly_the_floor():
    # The floor is inclusive: `ratio < FLOOR` is the silent case, so a ratio
    # sitting exactly on it still gets reported.
    note = rhythm.near_miss_note({
        "bpm": None,
        "candidate_bpm": 88.0,
        "peak_ratio": rhythm.BPM_NEAR_MISS_FLOOR,
    })
    assert note is not None
