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


def frame_dhash(path: str, size: int = 8) -> "int | None":
    """64-bit difference hash of one extracted frame, or None if unreadable.

    Deliberately computed through ffmpeg rather than Pillow: ffmpeg is already a
    hard requirement of this module, Pillow is only in the `ocr` extra, and a
    dedupe pass is not worth promoting an optional dependency to a core one. The
    downscale happens in ffmpeg, the bit arithmetic on stdlib bytes.

    One extra ffmpeg call per frame — milliseconds each, against the seconds a
    local VLM call costs. Removing a single frame pays for the whole pass.

    Returns None rather than raising: a frame we cannot hash must never be
    deduped away, and a broken hash must not take extraction down.
    """
    cmd = [
        config.FFMPEG_BIN, "-v", "error", "-i", path,
        # size+1 columns so each row yields `size` left-to-right comparisons.
        "-vf", "scale=%d:%d,format=gray" % (size + 1, size),
        "-f", "rawvideo", "-",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=15).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    if len(out) < (size + 1) * size:
        return None
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | (1 if out[base + col] > out[base + col + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def dedupe_near_duplicates(
    frames: List["KeyframeInfo"],
    distance: int = -1,
    max_gap_sec: float = -1.0,
    min_keep: int = -1,
) -> "tuple[List[KeyframeInfo], int]":
    """Drop frames that look like the previous kept frame. Returns (kept, dropped).

    **Nothing is topped up.** Each keyframe is one local VLM call, so the point
    is to spend fewer of them; a version that backfilled to `frame_cap` would
    cost exactly what it cost before and would also make the saving invisible,
    because the count would always equal the cap.

    Two guards keep this from re-creating the sparse-timeline defect that
    `frame_cap` exists to fix:

    * ``max_gap_sec`` — a frame is only ever dropped while the previous KEPT
      frame is within this many seconds. Two identical-looking frames seventeen
      minutes apart are information ("nothing changed for seventeen minutes");
      two seconds apart is noise. Consequence: the kept set never opens a gap
      wider than the sampler had already chosen.
    * ``min_keep`` — a floor on the count, so a static short clip cannot
      collapse to one frame.

    First and last are never dropped: `_ensure_first_last` just guaranteed them
    and this pass must not undo that.
    """
    if distance < 0:
        distance = config.KEYFRAME_DEDUPE_DISTANCE
    if max_gap_sec < 0:
        max_gap_sec = config.KEYFRAME_DEDUPE_MAX_GAP_SEC
    if min_keep < 0:
        min_keep = config.KEYFRAME_DEDUPE_MIN
    if len(frames) <= max(2, min_keep):
        return frames, 0

    kept = [frames[0]]
    last_hash = frame_dhash(frames[0].file_path)
    dropped = 0
    # frames[-1] is excluded from the scan and appended at the end. Indexed by
    # position, not by value: KeyframeInfo is a dataclass, so `.index(f)` would
    # match on field equality and could return the wrong slot.
    last_i = len(frames) - 1
    for i in range(1, last_i):
        f = frames[i]
        # Floor first: once dropping another frame would breach min_keep, the
        # rest are kept regardless of how similar they look. `remaining` counts
        # this frame plus everything after it that is still unvisited, including
        # the final frame appended below.
        remaining = last_i - i + 1
        if len(kept) + remaining <= min_keep:
            kept.append(f)
            last_hash = None
            continue
        gap = f.timestamp_sec - kept[-1].timestamp_sec
        if gap > max_gap_sec:
            kept.append(f)
            last_hash = frame_dhash(f.file_path)
            continue
        h = frame_dhash(f.file_path)
        if h is None or last_hash is None or hamming(h, last_hash) > distance:
            kept.append(f)
            last_hash = h
        else:
            dropped += 1
    kept.append(frames[-1])
    for i, f in enumerate(kept):
        f.frame_index = i
    return kept, dropped


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

    # No trimming here any more. This loop removed `min(middle, key=score)`, and
    # every scene frame carries score 0.0 -- so `min` returned the *earliest*
    # middle frame and the budget was balanced by eating the clip from the front,
    # compounding the truncation it was sitting downstream of. Staying within
    # budget is now `select_spread`'s job in the caller, which does it by
    # thinning evenly instead.
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
        # Only when there is nothing at all. A clip can legitimately have no cuts
        # to find — a lecture, a livestream, one person talking for four hours —
        # and leaving that at zero keyframes cost the whole visual layer and the
        # score, while the video sat at status "transcribed" looking merely
        # unfinished. Interval sampling seeks per frame instead of decoding
        # through, so it costs the same whatever the duration.
        #
        # Deliberately NOT `len(frames) < max_frames`: that was the first cut and
        # it padded every clip whose cut count came in under budget, diluting real
        # scene frames with arbitrary ones and buying extra VLM calls for all of
        # them. `hybrid` exists for people who want that; `scene` should not
        # silently become it. The defect here is zero, so zero is what is fixed.
        if not frames:
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

    # Back to budget by thinning evenly, not by cutting off the tail.
    # `_ensure_first_last` may have pushed one or two over; `select_spread` keeps
    # both ends by construction, so the frames it just guaranteed survive.
    if len(frames) > max_frames:
        frames.sort(key=lambda f: f.timestamp_sec)
        keep = select_spread([f.timestamp_sec for f in frames], max_frames)
        frames = [frames[i] for i in keep]

    frames.sort(key=lambda f: f.timestamp_sec)
    for i, f in enumerate(frames):
        f.frame_index = i

    # Last, and deliberately after the cap: `max_frames` says how many this clip
    # is ALLOWED to spend, dedupe says how many it actually needs. Running it
    # here means the saving is real spend avoided, not a reshuffle inside the
    # same budget — nothing is topped up to replace what goes.
    if config.KEYFRAME_DEDUPE:
        frames, dropped = dedupe_near_duplicates(frames)
        if dropped:
            print(
                "  keyframes: %d near-duplicates dropped, %d kept "
                "(no top-up — that is the saving)" % (dropped, len(frames)),
                file=sys.stderr,
            )

    return frames


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


def select_spread(timestamps: List[float], n: int) -> List[int]:
    """Indices of ``n`` timestamps spread across the *time* axis. Ends included.

    The obvious version -- ``[round(i * (m - 1) / (n - 1))]`` -- spreads over cut
    *ordinal*, and cuts are precisely what is non-uniform in time. A reel with a
    rapid-fire opening (40 cuts in 4 seconds) and a static outro (2 cuts in 6)
    would spend three quarters of its budget on the opening and still leave the
    hole this exists to close. The quantity that has to be bounded is "how much
    of the clip did the model never see", so the spacing has to be measured in
    seconds.

    Both ends always survive: the first target is the earliest timestamp and the
    last is forced, so the extremes of the clip are never the thing that gets
    dropped to make room.
    """
    m = len(timestamps)
    if n >= m:
        return list(range(m))
    if n <= 1:
        return [0]
    if n == 2:
        return [0, m - 1]

    first, last = timestamps[0], timestamps[-1]
    span = last - first
    out: List[int] = []
    lo = 0
    for j in range(n):
        target = first + span * j / (n - 1)
        # Leave room for the picks still to come, so the walk cannot run out of
        # list and duplicate an index.
        hi = m - n + j
        best, best_d = lo, abs(timestamps[lo] - target)
        for i in range(lo, hi + 1):
            d = abs(timestamps[i] - target)
            if d < best_d:
                best, best_d = i, d
        out.append(best)
        lo = best + 1
    out[-1] = m - 1
    return out


def _extract_scene(
    video_path: str, output_dir: str, video_id: str, max_frames: int,
    resolution: int = 0, start_sec: float = 0.0, end_sec: float = 0.0,
) -> List[KeyframeInfo]:
    """Extract keyframes using ffmpeg scene change detection.

    Returns [] rather than raising when ffmpeg overruns: a clip with no usable
    cuts is a normal thing to meet, not an error, and the caller has a cheaper
    strategy to fall back on.
    """
    # Pass 1 -- find every cut. No `-frames:v`, and no image encoding at all.
    #
    # That cap used to be here, and it is where the sampling actually broke:
    # ffmpeg exits as soon as N output frames exist, so on a clip with 50 cuts
    # and a budget of 8 it stopped detecting at cut 8 and never looked at the
    # rest of the video. Everything downstream then divided up a list that only
    # ever described the opening seconds -- measured on the library, 27 clips
    # had a single unsampled gap larger than half their length, the worst being
    # a 40-minute talk seen only for its first minute.
    #
    # Dropping the encoding makes this pass strictly cheaper per decoded frame
    # than the old one, and `-f null -` writes nothing.
    vf = "select='gt(scene,0.3)',showinfo"
    cmd = [config.FFMPEG_BIN]
    cmd += _seek_args(start_sec, end_sec)
    cmd += ["-i", video_path, "-vf", vf, "-an", "-f", "null", "-"]
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
    ts_pattern = re.compile(r"pts_time:(\d+\.?\d*)")
    stamps = [float(m) + offset for m in ts_pattern.findall(result.stderr)]
    if not stamps:
        return []

    # Pass 2 -- extract only the cuts we are keeping, seeking to each one. Same
    # per-frame grab `_extract_interval` uses, which costs the same whatever the
    # clip's duration.
    scale = _scale_vf(resolution)
    frames = []
    for i, idx in enumerate(select_spread(stamps, max_frames)):
        ts = stamps[idx]
        fpath = os.path.join(output_dir, f"{video_id}_scene_{i+1:03d}.jpg")
        cmd = [config.FFMPEG_BIN, "-ss", str(ts), "-i", video_path]
        if scale:
            cmd += ["-vf", scale]
        cmd += ["-frames:v", "1", "-q:v", "2", "-y", fpath]
        subprocess.run(cmd, capture_output=True, timeout=30)
        if os.path.exists(fpath):
            frames.append(KeyframeInfo(
                frame_index=i,
                timestamp_sec=ts,
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
