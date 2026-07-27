"""Shot-boundary metrics — the measured half of §4E evidence-based pacing.

The `pacing` craft score used to be pure LLM vibes ("does the rhythm hold
attention?"), which drifts with whichever VLM/LLM backend is loaded. This module
*measures* the cut rhythm instead: a dedicated ffmpeg scene-detection pass over
the WHOLE clip counts hard cuts and derives cuts-per-minute, so the scorer can
reason on evidence rather than guess.

Why a dedicated pass (not the keyframe extractor): `vision/keyframe.py:_extract_scene`
runs the same `select='gt(scene,T)'` filter but caps it at `-frames:v max_frames`
and re-encodes JPEGs — so it only sees the first N cuts up to the keyframe budget,
never the true total. Here we run `-an -f null -` (decode + count, no output files)
with no frame cap. Concept borrowed from crv Pro's `--motion` shot table; the
implementation is our own.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Optional

from . import config, ffprobe

# showinfo prints one line per SELECTED frame; with select='gt(scene,T)' only
# scene-change frames pass, so a pts_time count == number of hard cuts.
_TS_PATTERN = re.compile(r"pts_time:(\d+\.?\d*)")


@dataclass
class ShotMetrics:
    shot_count: int
    cuts_per_minute: float
    avg_shot_sec: float
    duration_sec: Optional[float] = None


def _probe_duration(video_path: str) -> Optional[float]:
    """Strict duration probe — returns None (never a fabricated fallback) on
    failure, matching pipeline._probe_duration. A wrong denominator would make
    cuts_per_minute a lie, so we'd rather emit nothing.

    Thin wrapper kept so existing callers and test patches keep working; the
    implementation is shared in `reel_scout.ffprobe`."""
    return ffprobe.probe_duration(video_path)


def parse_cut_count(stderr: str) -> int:
    """Number of scene-change (hard cut) boundaries in an ffmpeg showinfo dump."""
    return len(_TS_PATTERN.findall(stderr))


def metrics_from_cuts(cuts: int, duration_sec: float) -> ShotMetrics:
    """Pure derivation so it can be unit-tested without ffmpeg.

    `cuts` scene boundaries partition the clip into `cuts + 1` shots. A static
    single-shot clip (cuts=0) yields shot_count=1, cuts_per_minute=0,
    avg_shot_sec=duration.
    """
    shot_count = cuts + 1
    minutes = duration_sec / 60.0
    cuts_per_minute = round(cuts / minutes, 2) if minutes > 0 else 0.0
    avg_shot_sec = round(duration_sec / shot_count, 2) if shot_count else 0.0
    return ShotMetrics(
        shot_count=shot_count,
        cuts_per_minute=cuts_per_minute,
        avg_shot_sec=avg_shot_sec,
        duration_sec=round(duration_sec, 2),
    )


def compute_shot_metrics(
    video_path: str,
    scene_threshold: Optional[float] = None,
    duration_sec: Optional[float] = None,
) -> Optional[ShotMetrics]:
    """Measure shot rhythm for a clip. Returns None when duration is unknown
    (can't compute cuts/minute) or ffmpeg is unavailable — callers treat a None
    as "no measured pacing signal" rather than a fabricated zero."""
    duration = duration_sec if (duration_sec and duration_sec > 0) else _probe_duration(video_path)
    if not duration or duration <= 0:
        return None

    threshold = config.SHOT_SCENE_THRESHOLD if scene_threshold is None else scene_threshold
    cmd = [
        config.FFMPEG_BIN,
        "-i", video_path,
        "-vf", "select='gt(scene,%g)',showinfo" % threshold,
        "-an",
        "-f", "null",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return None
    # A nonzero exit (corrupt/unreadable video) means the scene pass never ran —
    # returning it as a 0-cut clip would fabricate shot_count=1. Emit None instead.
    if result.returncode != 0:
        return None

    cuts = parse_cut_count(result.stderr)
    return metrics_from_cuts(cuts, duration)
