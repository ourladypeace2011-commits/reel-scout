from __future__ import annotations

import os
import subprocess

from ..utils.stderr import tail_stderr

from .. import config


def extract_wav(video_path: str, output_path: str, sample_rate: int = 16000) -> str:
    """Extract audio from video as mono WAV."""
    cmd = [
        config.FFMPEG_BIN,
        "-i",
        video_path,
        "-ar",
        str(sample_rate),
        "-ac",
        "1",
        "-y",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError("Audio extraction failed: %s" % tail_stderr(result.stderr))
    if not os.path.exists(output_path):
        raise RuntimeError("Audio extraction produced no output file")
    return output_path
