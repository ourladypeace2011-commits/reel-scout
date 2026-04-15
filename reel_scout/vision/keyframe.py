from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import List

from .. import config


@dataclass
class KeyframeInfo:
    frame_index: int
    timestamp_sec: float
    file_path: str
    strategy: str


def extract_keyframes(
    video_path: str,
    output_dir: str,
    video_id: str,
    strategy: str = "",
    max_frames: int = 0,
) -> List[KeyframeInfo]:
    strategy = strategy or config.KEYFRAME_STRATEGY
    max_frames = max_frames or config.KEYFRAME_MAX

    os.makedirs(output_dir, exist_ok=True)

    if strategy == "scene":
        frames = _extract_scene(video_path, output_dir, video_id, max_frames)
    elif strategy == "interval":
        frames = _extract_interval(video_path, output_dir, video_id, max_frames)
    elif strategy == "hybrid":
        frames = _extract_scene(video_path, output_dir, video_id, max_frames)
        if len(frames) < max_frames:
            # Fill gaps with interval frames
            interval_frames = _extract_interval(
                video_path, output_dir, video_id, max_frames - len(frames),
            )
            # Deduplicate by checking timestamp proximity (within 1s)
            existing_ts = {f.timestamp_sec for f in frames}
            for f in interval_frames:
                if not any(abs(f.timestamp_sec - t) < 1.0 for t in existing_ts):
                    frames.append(f)
                    existing_ts.add(f.timestamp_sec)
                if len(frames) >= max_frames:
                    break
    else:
        raise ValueError(f"Unknown keyframe strategy: {strategy}")

    frames.sort(key=lambda f: f.timestamp_sec)
    # Re-index
    for i, f in enumerate(frames):
        f.frame_index = i

    return frames[:max_frames]


def _get_duration(video_path: str) -> float:
    cmd = [
        config.FFMPEG_BIN.replace("ffmpeg", "ffprobe"),
        "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        return float(result.stdout.strip())
    except (ValueError, TypeError):
        return 60.0  # fallback for short videos


def _extract_scene(
    video_path: str, output_dir: str, video_id: str, max_frames: int,
) -> List[KeyframeInfo]:
    """Extract keyframes using ffmpeg scene change detection."""
    pattern = os.path.join(output_dir, f"{video_id}_scene_%03d.jpg")
    cmd = [
        config.FFMPEG_BIN,
        "-i", video_path,
        "-vf", "select='gt(scene,0.3)',showinfo",
        "-vsync", "vfr",
        "-frames:v", str(max_frames),
        "-y",
        pattern,
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120,
    )

    # Parse timestamps from showinfo output (in stderr)
    import re
    frames = []
    ts_pattern = re.compile(r"pts_time:(\d+\.?\d*)")
    matches = ts_pattern.findall(result.stderr)

    for i, ts_str in enumerate(matches[:max_frames]):
        fpath = os.path.join(output_dir, f"{video_id}_scene_{i+1:03d}.jpg")
        if os.path.exists(fpath):
            frames.append(KeyframeInfo(
                frame_index=i,
                timestamp_sec=float(ts_str),
                file_path=fpath,
                strategy="scene",
            ))

    return frames


def _extract_interval(
    video_path: str, output_dir: str, video_id: str, max_frames: int,
) -> List[KeyframeInfo]:
    """Extract keyframes at regular intervals."""
    duration = _get_duration(video_path)
    interval = max(duration / (max_frames + 1), 1.0)

    frames = []
    for i in range(max_frames):
        ts = interval * (i + 1)
        if ts >= duration:
            break
        fpath = os.path.join(output_dir, f"{video_id}_int_{i:03d}.jpg")
        cmd = [
            config.FFMPEG_BIN,
            "-ss", str(ts),
            "-i", video_path,
            "-frames:v", "1",
            "-q:v", "2",
            "-y",
            fpath,
        ]
        subprocess.run(cmd, capture_output=True, timeout=30)
        if os.path.exists(fpath):
            frames.append(KeyframeInfo(
                frame_index=i,
                timestamp_sec=ts,
                file_path=fpath,
                strategy="interval",
            ))

    return frames
