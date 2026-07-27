"""Shared ffprobe helpers.

Duration probing was copy-pasted into three modules (`analyze.pipeline`,
`shots`, `vision.keyframe`) with two different failure policies. The
None-on-failure policy lives here so callers that must not fabricate a
duration share one implementation; `vision.keyframe` deliberately keeps its
own variant because a keyframe budget needs *some* number and falls back to a
nominal 60s rather than skipping extraction.
"""

from __future__ import annotations

import subprocess
from typing import Optional

from . import config


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
