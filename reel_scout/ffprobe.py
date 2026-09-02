"""Shared ffprobe helpers.

Duration probing was copy-pasted into three modules (`analyze.pipeline`,
`shots`, `vision.keyframe`) with two different failure policies. The
None-on-failure policy lives here so callers that must not fabricate a
duration share one implementation; `vision.keyframe` deliberately keeps its
own variant because a keyframe budget needs *some* number and falls back to a
nominal 60s rather than skipping extraction.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional, Tuple

from . import config
from .utils.stderr import warn


def probe_duration(path: str) -> Optional[float]:
    """Best-effort duration probe in seconds.

    Returns ``None`` (never a fabricated fallback) on failure so a bad probe
    doesn't get written to the DB as if it were real — callers keep duration
    unset until something actually measures it.
    """
    cmd = [
        config.FFMPEG_BIN.replace("ffmpeg", "ffprobe"),
        "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except (ValueError, TypeError, OSError, subprocess.SubprocessError):
        return None


#: Video codecs Apple's MP4 pipeline will actually render. Measured on a real
#: iPad 2026-08-25: h264 plays, vp9 does not, av1 does not. Audio codec is not
#: part of this -- `h264 + opus` plays with sound.
APPLE_PLAYABLE_VIDEO_CODECS = frozenset(("h264", "hevc"))


def probe_video_codec(path: str) -> Optional[str]:
    """Codec name of the first video stream, or ``None`` if it can't be read.

    ``None`` means "could not measure", never "fine" -- callers must not read a
    failed probe as a pass.
    """
    cmd = [
        config.FFMPEG_BIN.replace("ffmpeg", "ffprobe"),
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "csv=p=0",
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # csv=p=0 can emit a trailing empty field when the stream carries side data
    # (seen on two files in the real library: "vp9," not "vp9"). Splitting on
    # the comma is the difference between reading the codec and inventing a
    # second video stream that was never there.
    name = result.stdout.strip().split(",")[0].strip()
    return name or None


def probe_dimensions(path: str) -> Optional[Tuple[int, int]]:
    """(width, height) of the first video stream, or ``None`` if unreadable.

    ``None`` means "could not measure", never a default. A caller that treats a
    failed probe as landscape will silently mis-shape every vertical clip.
    """
    cmd = [
        config.FFMPEG_BIN.replace("ffmpeg", "ffprobe"),
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0:s=x",
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split("\n")[0].split("x")
    if len(parts) < 2:
        return None
    try:
        w, h = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return (w, h) if w > 0 and h > 0 else None


def warn_if_not_apple_playable(path: str, label: str = "") -> Optional[str]:
    """Probe the file that actually landed and say so loudly if it will not play.

    This is the first check that asserts the property a gate promises -- namely
    playability -- about the file that actually landed. It is NOT the first thing
    in the pipeline to look at a landed file: ``probe_duration`` above has done
    that since PR #50 (2026-07-27), and ``analyze.pipeline`` stats the file for
    its size. The earlier wording here claimed "the only place in the pipeline
    that measures the artifact instead of the request", which was a universal
    that the code right above it contradicts; narrowed 2026-08-25.

    The distinction still matters: the format selector states a preference, and
    yt-dlp is free to fall down the chain to an unconstrained rung. Until this
    check existed the difference was invisible -- the clip downloaded, analyzed
    and scored perfectly, and failed only when a human opened it on an iPhone or
    iPad.

    Returns the codec it measured (``None`` if it could not measure). It catches
    the failures it knows about, but callers must not treat that as "never
    raises" -- the crawlers wrap the call, because a probe failure must not cost
    a download that already succeeded.
    """
    codec = probe_video_codec(path)
    if codec is None:
        warn("  codec check: could not probe %s -- playability unverified"
             % (label or os.path.basename(path)))
        return None
    if codec not in APPLE_PLAYABLE_VIDEO_CODECS:
        warn("  codec check: %s landed as %s -- will NOT play on iOS/iPadOS "
             "Safari (Apple's MP4 path takes h264/hevc only). The clip is still "
             "usable for transcript/keyframes/score."
             % (label or os.path.basename(path), codec))
    return codec
