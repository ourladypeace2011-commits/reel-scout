from __future__ import annotations

import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import List

from .. import config


@dataclass
class KeyframeInfo:
    frame_index: int
    timestamp_sec: float
    file_path: str
    strategy: str
    score: float = 0.0


def auto_frame_budget(duration_sec: float, focused: bool = False) -> int:
    """Duration-aware keyframe budget (招②, from claude-video, MIT).

    Returns how many keyframes to extract for a clip of the given duration.
    Longer clips earn a larger budget; in ``focused`` mode (a [start,end] window,
    招③) the same wall-clock seconds earn a *denser* budget because the window is
    where the interesting stuff is.

    IMPORTANT — cost discipline: in reel-scout each keyframe is one *local VLM
    call* (compute, not tokens). The raw claude-video budget can reach 100 frames,
    which would blow up VLM cost. The caller is responsible for clamping the return
    value to ``frame_cap(duration)`` (see ``extract_keyframes``); this function only
    encodes the duration→frame curve, capped at the claude-video hard ceiling of 100.
    """
    if duration_sec <= 0:
        return 30  # unknown duration -> claude-video's short-clip default

    if focused:
        # Denser curve for an explicit focus window (claude-video focused table).
        if duration_sec <= 5:
            budget = 10
        elif duration_sec <= 15:
            budget = 30
        elif duration_sec <= 30:
            budget = 60
        elif duration_sec <= 60:
            budget = 80
        else:
            budget = 100
    else:
        # Full-scan curve (claude-video default table).
        if duration_sec <= 30:
            budget = 30
        elif duration_sec <= 60:
            budget = 40
        elif duration_sec <= 180:
            budget = 60
        elif duration_sec <= 600:
            budget = 80
        else:
            budget = 100

    # claude-video also enforces <=2 fps so we never extract more frames than the
    # clip can supply at that rate. (e.g. a 5s clip -> at most 10 frames.)
    fps_cap = max(1, int(duration_sec * 2))
    budget = min(budget, fps_cap)

    # Hard ceiling from claude-video.
    return min(budget, 100)


def frame_cap(duration_sec: float) -> int:
    """The cost ceiling for one clip, in keyframes.

    Each keyframe is one local VLM call, so this is a compute budget, not a
    formatting preference — which is why a single flat number governed it for a
    long time. The flat number is wrong in one direction only: a 9-second reel
    and an 82-minute interview both got `KEYFRAME_MAX` frames, so the reel was
    sampled roughly once a second while the interview was sampled once every
    seven minutes. Downstream that shows up as a `timeline` whose single segment
    covers 96% of the clip — technically produced, practically useless.

    So the cap stays flat up to `KEYFRAME_LONG_SEC` (short-form keeps its exact
    previous behaviour, bit for bit) and above it earns frames at
    `KEYFRAME_PER_MIN` a minute, never exceeding `KEYFRAME_MAX_LONG`. The
    ceiling is the point: an 82-minute clip asks for 164 frames at 2/min and is
    told 40. Long-form gets a usable sampling rate, not an unbounded bill.
    """
    if duration_sec <= 0 or duration_sec <= config.KEYFRAME_LONG_SEC:
        return config.KEYFRAME_MAX
    earned = int(math.ceil(duration_sec / 60.0 * config.KEYFRAME_PER_MIN))
    return max(config.KEYFRAME_MAX, min(earned, config.KEYFRAME_MAX_LONG))


def _ensure_first_last(
    video_path: str,
    output_dir: str,
    video_id: str,
    frames: List[KeyframeInfo],
    max_frames: int,
    duration: float,
    resolution: int = 0,
    start_sec: float = 0.0,
    end_sec: float = 0.0,
) -> List[KeyframeInfo]:
    """Guarantee first and last frames are represented.

    When a [start,end] focus window is given (招③), "first"/"last" track the
    window edges instead of the whole-clip edges.
    """
    scale = _scale_vf(resolution)
    win_start = start_sec if (start_sec and start_sec > 0) else 0.0
    win_end = end_sec if (end_sec and end_sec > 0) else duration

    # Check first frame zone (< window start + 0.5s)
    has_first = any(f.timestamp_sec < (win_start + 0.5) for f in frames)
    if not has_first:
        ts = win_start + 0.1
        fpath = os.path.join(output_dir, f"{video_id}_first.jpg")
        cmd = [config.FFMPEG_BIN, "-ss", str(ts), "-i", video_path]
        if scale:
            cmd += ["-vf", scale]
        cmd += ["-frames:v", "1", "-q:v", "2", "-y", fpath]
        subprocess.run(cmd, capture_output=True, timeout=30)
        if os.path.exists(fpath):
            frames.insert(0, KeyframeInfo(
                frame_index=0,
                timestamp_sec=ts,
                file_path=fpath,
                strategy="first",
                score=0.0,
            ))

    # Check last frame zone (> window end - 1.0)
    has_last = any(f.timestamp_sec > (win_end - 1.0) for f in frames)
    if not has_last and win_end > (win_start + 1.0):
        ts = win_end - 0.5
        fpath = os.path.join(output_dir, f"{video_id}_last.jpg")
        cmd = [config.FFMPEG_BIN, "-ss", str(ts), "-i", video_path]
        if scale:
            cmd += ["-vf", scale]
        cmd += ["-frames:v", "1", "-q:v", "2", "-y", fpath]
        subprocess.run(cmd, capture_output=True, timeout=30)
        if os.path.exists(fpath):
            frames.append(KeyframeInfo(
                frame_index=len(frames),
                timestamp_sec=ts,
                file_path=fpath,
                strategy="last",
                score=0.0,
            ))

    # Trim middle frames if exceeding max_frames (remove lowest score)
    while len(frames) > max_frames:
        # Find the frame with lowest score among middle frames (not first/last)
        middle = frames[1:-1]
        if not middle:
            break
        worst = min(middle, key=lambda f: f.score)
        frames.remove(worst)

    return frames


def _scale_vf(resolution: int) -> str:
    """ffmpeg scale filter fragment for 招④, or '' when no scaling requested.

    ``-2`` keeps the aspect ratio and forces the other edge to an even number
    (required by most encoders). resolution sets the WIDTH; tall portrait reels
    keep their height-driven detail because aspect ratio is preserved.
    """
    if resolution and resolution > 0:
        return "scale=%d:-2" % resolution
    return ""


def _seek_args(start_sec: float, end_sec: float) -> List[str]:
    """ffmpeg -ss/-to fragment for the 招③ focus window (empty when [0,0])."""
    args: List[str] = []
    if start_sec and start_sec > 0:
        args += ["-ss", str(start_sec)]
    if end_sec and end_sec > 0:
        args += ["-to", str(end_sec)]
    return args


def extract_keyframes(
    video_path: str,
    output_dir: str,
    video_id: str,
    strategy: str = "",
    max_frames: int = 0,
    resolution: int = 0,
    start_sec: float = 0.0,
    end_sec: float = 0.0,
) -> List[KeyframeInfo]:
    strategy = strategy or config.KEYFRAME_STRATEGY
    if resolution == 0:
        resolution = config.KEYFRAME_RESOLUTION

    duration = _get_duration(video_path)
    focused = bool((start_sec and start_sec > 0) or (end_sec and end_sec > 0))

    if max_frames == 0:
        # 招② auto budget. Use the focus-window span when focused so a tight window
        # gets the denser claude-video table.
        if focused:
            window = (end_sec or duration) - (start_sec or 0.0)
            budget = auto_frame_budget(window, focused=True)
        else:
            budget = auto_frame_budget(duration, focused=False)
        # COST RED LINE: never spend more local VLM calls than the cap allows.
        max_frames = min(budget, frame_cap(duration if not focused else
                                           (end_sec or duration) - (start_sec or 0.0)))
    # else: caller forced an explicit count (e.g. --keyframe-max); respect it as-is.

    os.makedirs(output_dir, exist_ok=True)

    if strategy == "scene":
        frames = _extract_scene(
            video_path, output_dir, video_id, max_frames,
            resolution, start_sec, end_sec,
        )
        # A clip can legitimately have no cuts to find — a lecture, a livestream,
        # one person talking for four hours. Scene detection then returns nothing
        # (or overruns and returns nothing), and leaving it there produced a video
        # with zero keyframes, therefore no visual layer and no score, sitting in
        # the library at status "transcribed" looking merely unfinished. Interval
        # sampling seeks per frame instead of decoding through, so it costs the
        # same whatever the duration; topping up here is what makes "some frames"
        # the guaranteed floor.
        if len(frames) < max_frames:
            frames = _top_up_with_interval(
                frames, video_path, output_dir, video_id, max_frames,
                resolution, start_sec, end_sec,
            )
    elif strategy == "interval":
        frames = _extract_interval(
            video_path, output_dir, video_id, max_frames,
            resolution, start_sec, end_sec,
        )
    elif strategy == "motion":
        frames = _extract_motion(
            video_path, output_dir, video_id, max_frames,
            resolution, start_sec, end_sec,
        )
    elif strategy == "hybrid":
        frames = _extract_scene(
            video_path, output_dir, video_id, max_frames,
            resolution, start_sec, end_sec,
        )
        if len(frames) < max_frames:
            frames = _top_up_with_interval(
                frames, video_path, output_dir, video_id, max_frames,
                resolution, start_sec, end_sec,
            )
    else:
        raise ValueError(f"Unknown keyframe strategy: {strategy}")

    frames.sort(key=lambda f: f.timestamp_sec)
    # Re-index
    for i, f in enumerate(frames):
        f.frame_index = i

    # Ensure first/last frame coverage. When focused, "first/last" mean the edges
    # of the [start,end] window, not the whole clip.
    frames = _ensure_first_last(
        video_path, output_dir, video_id, frames, max_frames, duration,
        resolution, start_sec, end_sec,
    )

    return frames[:max_frames]


def _get_duration(video_path: str) -> float:
    """Deliberately NOT `reel_scout.ffprobe.probe_duration`: a keyframe budget
    needs *some* number to divide by, so this variant falls back to a nominal
    60s rather than returning None and skipping extraction. The None-on-failure
    policy is for values that get persisted; this one never leaves this module."""
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


def _top_up_with_interval(
    frames: List[KeyframeInfo], video_path: str, output_dir: str, video_id: str,
    max_frames: int, resolution: int, start_sec: float, end_sec: float,
) -> List[KeyframeInfo]:
    """Fill a short frame set out to `max_frames` with evenly spaced samples.

    Lifted verbatim out of the `hybrid` branch so `scene` can use it too. Scene
    detection was the only strategy that could come back empty and stay empty,
    which is the one case where having no frames at all is the outcome.
    """
    interval_frames = _extract_interval(
        video_path, output_dir, video_id, max_frames - len(frames),
        resolution, start_sec, end_sec,
    )
    # Deduplicate by checking timestamp proximity (within 1s)
    existing_ts = {f.timestamp_sec for f in frames}
    for f in interval_frames:
        if not any(abs(f.timestamp_sec - t) < 1.0 for t in existing_ts):
            frames.append(f)
            existing_ts.add(f.timestamp_sec)
        if len(frames) >= max_frames:
            break
    return frames


def scene_timeout(duration_sec: float) -> int:
    """How long scene detection may run, as a function of the clip.

    Scene detection has to decode until it finds cuts; `-frames:v` only exits
    early once enough of them exist. A fixed 120s was fine while every clip was
    a reel, and impossible for a 4-hour livestream of one person talking to
    camera — almost no cuts, so the early exit never fires and ffmpeg decodes
    the whole file against a budget it cannot meet.

    Capped at 300s, and the cap is chosen from evidence rather than taste: a
    52-minute edited talk finished scene detection inside the OLD 120s budget
    and returned 38 cuts. `-frames:v` exits as soon as there are enough, so a
    clip that has cuts finds them early; only a clip that has none decodes on
    and on. 300s is therefore 2.5x what the one long clip we measured needed,
    while halving what a cut-less clip wastes before giving up.

    Capping at all is safe only because a timeout is no longer fatal:
    extraction falls through to interval sampling, which seeks per frame and
    costs the same at any duration.
    """
    return int(min(max(120.0, duration_sec * 0.2), 300.0))


def _extract_scene(
    video_path: str, output_dir: str, video_id: str, max_frames: int,
    resolution: int = 0, start_sec: float = 0.0, end_sec: float = 0.0,
) -> List[KeyframeInfo]:
    """Extract keyframes using ffmpeg scene change detection.

    Returns [] rather than raising when ffmpeg overruns: a clip with no usable
    cuts is a normal thing to meet, not an error, and the caller has a cheaper
    strategy to fall back on.
    """
    pattern = os.path.join(output_dir, f"{video_id}_scene_%03d.jpg")
    vf = "select='gt(scene,0.3)',showinfo"
    scale = _scale_vf(resolution)
    if scale:
        vf += "," + scale
    cmd = [config.FFMPEG_BIN]
    cmd += _seek_args(start_sec, end_sec)
    cmd += [
        "-i", video_path,
        "-vf", vf,
        "-vsync", "vfr",
        "-frames:v", str(max_frames),
        "-y",
        pattern,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=scene_timeout(_get_duration(video_path)),
        )
    except subprocess.TimeoutExpired:
        print("  Scene detection timed out — falling back to interval sampling",
              file=sys.stderr)
        return []

    # Parse timestamps from showinfo output (in stderr). With input seeking (-ss
    # before -i) ffmpeg resets pts to 0 at the seek point, so add start_sec back to
    # recover absolute video time.
    offset = start_sec if (start_sec and start_sec > 0) else 0.0
    frames = []
    ts_pattern = re.compile(r"pts_time:(\d+\.?\d*)")
    matches = ts_pattern.findall(result.stderr)

    for i, ts_str in enumerate(matches[:max_frames]):
        fpath = os.path.join(output_dir, f"{video_id}_scene_{i+1:03d}.jpg")
        if os.path.exists(fpath):
            frames.append(KeyframeInfo(
                frame_index=i,
                timestamp_sec=float(ts_str) + offset,
                file_path=fpath,
                strategy="scene",
            ))

    return frames


def _extract_motion(
    video_path: str, output_dir: str, video_id: str, max_frames: int,
    resolution: int = 0, start_sec: float = 0.0, end_sec: float = 0.0,
) -> List[KeyframeInfo]:
    """Extract keyframes using ffmpeg mpdecimate (high-motion frames)."""
    pattern = os.path.join(output_dir, f"{video_id}_motion_%03d.jpg")
    vf = "mpdecimate=hi=200:lo=100:frac=0.5,setpts=N/FRAME_RATE/TB,showinfo"
    scale = _scale_vf(resolution)
    if scale:
        vf += "," + scale
    cmd = [config.FFMPEG_BIN]
    cmd += _seek_args(start_sec, end_sec)
    cmd += [
        "-i", video_path,
        "-vf", vf,
        "-vsync", "vfr",
        "-frames:v", str(max_frames),
        "-y",
        pattern,
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120,
    )

    # Parse timestamps from showinfo output (in stderr). setpts already rebases pts
    # to start at 0; add start_sec back to recover absolute video time when focused.
    offset = start_sec if (start_sec and start_sec > 0) else 0.0
    frames = []
    ts_pattern = re.compile(r"pts_time:(\d+\.?\d*)")
    matches = ts_pattern.findall(result.stderr)

    for i, ts_str in enumerate(matches[:max_frames]):
        fpath = os.path.join(output_dir, f"{video_id}_motion_{i+1:03d}.jpg")
        if os.path.exists(fpath):
            frames.append(KeyframeInfo(
                frame_index=i,
                timestamp_sec=float(ts_str) + offset,
                file_path=fpath,
                strategy="motion",
                score=1.0,
            ))

    return frames


def _extract_interval(
    video_path: str, output_dir: str, video_id: str, max_frames: int,
    resolution: int = 0, start_sec: float = 0.0, end_sec: float = 0.0,
) -> List[KeyframeInfo]:
    """Extract keyframes at regular intervals (within the focus window if given)."""
    duration = _get_duration(video_path)
    win_start = start_sec if (start_sec and start_sec > 0) else 0.0
    win_end = end_sec if (end_sec and end_sec > 0) else duration
    span = max(win_end - win_start, 0.0)
    interval = max(span / (max_frames + 1), 1.0)
    scale = _scale_vf(resolution)

    frames = []
    for i in range(max_frames):
        ts = win_start + interval * (i + 1)
        if ts >= win_end:
            break
        fpath = os.path.join(output_dir, f"{video_id}_int_{i:03d}.jpg")
        cmd = [
            config.FFMPEG_BIN,
            "-ss", str(ts),
            "-i", video_path,
        ]
        if scale:
            cmd += ["-vf", scale]
        cmd += [
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
