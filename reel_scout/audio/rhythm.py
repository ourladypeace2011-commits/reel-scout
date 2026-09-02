"""Audio rhythm signals — energy + BPM, the audio half of §4E.

These back the pacing score with *measured* audio evidence, independent of the
optional PANNs event detector (which needs onnxruntime + a downloaded model).
Energy (RMS loudness) is pure-stdlib and always available. BPM (tempo) uses an
onset-envelope autocorrelation and is numpy-gated + best-effort — it returns
None rather than a shaky guess when it can't find a stable tempo.

When the gate rejects a tempo it now says so with numbers: `analyze_bpm` keeps
the candidate and its peak/zero-lag ratio even after refusing them, and
`near_miss_note` turns a *close* rejection into one printable line. Without
that, a 0.09 ratio and a 0.001 ratio both surfaced as a silent `None` — the
former is a squashed master the gate barely rejected, the latter is genuinely
beatless, and the caller could not tell them apart.

No librosa/scipy: that would drag numba+scipy and break the repo's minimal-deps
+ py3.9 principle. numpy is already the `audio` optional extra.
"""
from __future__ import annotations

import math
import wave
from typing import Dict, List, Optional

from .panns import _read_wav_samples

#: Autocorrelation search window. A tempo outside this range is not reported.
BPM_SEARCH_MIN = 60.0
BPM_SEARCH_MAX = 180.0

#: The tempo peak must clear this fraction of the zero-lag autocorrelation
#: energy before we commit to a number. Flat autocorrelation — speech, noise,
#: no beat — falls below it and yields None rather than a confident artifact.
BPM_PEAK_RATIO_MIN = 0.10

#: A rejected peak is only worth reporting if it got at least this close. Below
#: it there is no tempo to speak of and the note would be noise.
BPM_NEAR_MISS_FLOOR = 0.05


def _rms(samples: List[float]) -> float:
    """Root-mean-square loudness of normalized (-1..1) samples. Pure stdlib."""
    if not samples:
        return 0.0
    total = 0.0
    for s in samples:
        total += s * s
    return math.sqrt(total / len(samples))


def _blank_bpm() -> Dict[str, Optional[float]]:
    """No tempo, and no candidate worth naming."""
    return {"bpm": None, "candidate_bpm": None, "peak_ratio": None}


def analyze_bpm(samples: List[float], sr: int) -> Dict[str, Optional[float]]:
    """Best-effort tempo via onset-envelope autocorrelation, with its evidence.

    Returns ``{"bpm", "candidate_bpm", "peak_ratio"}``. ``bpm`` is None unless
    the peak cleared :data:`BPM_PEAK_RATIO_MIN`; ``candidate_bpm`` and
    ``peak_ratio`` are filled in whenever a peak was located at all, gate or no
    gate, so a near-miss can be reported instead of vanishing.
    """
    try:
        import numpy as np
    except ImportError:
        return _blank_bpm()
    try:
        x = np.asarray(samples, dtype=np.float32)
        frame, hop = 1024, 512
        if x.size < frame * 4:
            return _blank_bpm()
        n_frames = 1 + (x.size - frame) // hop
        env = np.empty(n_frames, dtype=np.float32)
        for i in range(n_frames):
            seg = x[i * hop: i * hop + frame]
            env[i] = math.sqrt(float(np.mean(seg * seg)))
        # onset strength = positive first difference of the energy envelope
        onset = np.diff(env)
        onset[onset < 0] = 0.0
        if onset.size < 4 or float(onset.std()) == 0.0:
            return _blank_bpm()
        onset = onset - onset.mean()
        ac = np.correlate(onset, onset, mode="full")
        ac = ac[ac.size // 2:]
        fps = sr / hop  # envelope frames per second
        min_lag = int(fps * 60.0 / BPM_SEARCH_MAX)
        max_lag = int(fps * 60.0 / BPM_SEARCH_MIN)
        if min_lag < 1 or max_lag >= ac.size:
            return _blank_bpm()
        window = ac[min_lag: max_lag + 1]
        if window.size == 0:
            return _blank_bpm()
        zero_lag = float(ac[0])
        peak = float(window.max())
        if zero_lag <= 0.0:
            return _blank_bpm()
        best_lag = int(np.argmax(window)) + min_lag
        if best_lag <= 0:
            return _blank_bpm()
        ratio = peak / zero_lag
        candidate = round(fps * 60.0 / best_lag, 1)
        return {
            "bpm": candidate if ratio >= BPM_PEAK_RATIO_MIN else None,
            "candidate_bpm": candidate,
            "peak_ratio": round(ratio, 4),
        }
    except Exception:  # noqa: BLE001 — best-effort; any numeric hiccup → no BPM
        return _blank_bpm()


def estimate_bpm(samples: List[float], sr: int) -> Optional[float]:
    """Best-effort tempo (BPM), or None when no stable peak clears the gate."""
    return analyze_bpm(samples, sr)["bpm"]


def near_miss_note(rhythm: Dict[str, Optional[float]]) -> Optional[str]:
    """One line naming a tempo the gate rejected but only just, else None.

    Silence is the right answer for genuinely beatless audio; it is the wrong
    answer for a master squashed flat enough to land at 0.09 against a 0.10
    gate, which is what this exists to say out loud.
    """
    if rhythm.get("bpm") is not None:
        return None
    candidate = rhythm.get("candidate_bpm")
    ratio = rhythm.get("peak_ratio")
    if candidate is None or ratio is None:
        return None
    if ratio < BPM_NEAR_MISS_FLOOR:
        return None
    return (
        "BPM undetermined: best candidate %.1f BPM at peak/zero-lag %.3f "
        "(needs >= %.2f)" % (candidate, ratio, BPM_PEAK_RATIO_MIN)
    )


def compute_rhythm(wav_path: str) -> Dict[str, Optional[float]]:
    """Read a mono WAV and return energy plus the BPM verdict and its evidence.

    Keys: ``energy``, ``bpm``, ``candidate_bpm``, ``peak_ratio``. The shape is
    the same on every path, so callers can read the evidence keys without
    checking which failure they got.
    """
    result = _blank_bpm()
    result["energy"] = None
    try:
        samples, sr = _read_wav_samples(wav_path)
    except (OSError, ValueError, EOFError, wave.Error):
        # wave.Error covers a text/corrupt file that isn't a real RIFF/WAV.
        return result
    if not samples:
        return result
    result = analyze_bpm(samples, sr)
    result["energy"] = round(_rms(samples), 4)
    return result
