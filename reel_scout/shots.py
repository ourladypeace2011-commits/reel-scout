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

That borrowing was half done until now. crv's shot table carries **per-shot
duration**; this module kept only the aggregate, because `parse_cut_count`
resolved every `pts_time` in the dump and then returned `len()` of them. The
boundaries were measured on every run and thrown away, so "one shot" was never
an addressable thing — nothing downstream could hang a shot size, a camera move
or a line of VO off a specific span. `parse_cut_times` keeps what was already
being computed.

**One list feeds both outputs.** `shots_from_cuts` partitions on exactly the
boundaries `metrics_from_cuts` counts, so `len(shots) == shot_count` holds by
construction rather than by agreement. Filtering "degenerate" boundaries out of
one and not the other is how two tables in the same database start disagreeing
quietly, so nothing is filtered: a boundary at 0.0 yields a zero-length first
shot and says so, which is information about the detector, not noise to hide.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence, Tuple

from . import config, ffprobe

# showinfo prints one line per SELECTED frame; with select='gt(scene,T)' only
# scene-change frames pass, so a pts_time count == number of hard cuts.
_TS_PATTERN = re.compile(r"pts_time:(\d+\.?\d*)")


@dataclass
class Shot:
    """One shot: a span between two cuts. `index` is 0-based clip order."""
    index: int
    start_sec: float
    end_sec: float
    dur_sec: float


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


def parse_cut_times(stderr: str) -> List[float]:
    """Every scene-change boundary in an ffmpeg showinfo dump, in seconds.

    Sorted ascending and de-duplicated. ffmpeg emits these in decode order and
    measured runs come back already sorted with no repeats, so both steps are
    belt-and-braces — but an unsorted list would silently produce shots with
    negative durations, which is worth two cheap lines to make impossible.
    """
    seen = set()
    out = []
    for raw in _TS_PATTERN.findall(stderr):
        try:
            t = float(raw)
        except ValueError:  # pragma: no cover - regex already constrains this
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    out.sort()
    return out


def parse_cut_count(stderr: str) -> int:
    """Number of scene-change (hard cut) boundaries in an ffmpeg showinfo dump.

    Kept as the counting entry point; it now counts the same de-duplicated list
    `parse_cut_times` returns, so the count and the table can never be derived
    from different sets."""
    return len(parse_cut_times(stderr))


def shots_from_cuts(cut_times: List[float], duration_sec: float) -> List[Shot]:
    """Partition a clip into shots on `cut_times`. Pure, so it unit-tests
    without ffmpeg.

    `n` boundaries produce `n + 1` shots — the same partition
    `metrics_from_cuts` counts, which is the point: these two must never
    disagree about how many shots a clip has.

    Boundaries are neither dropped nor merged. A boundary at 0.0 gives a
    zero-length opening shot; one at or past `duration_sec` gives a zero-length
    closing shot. Both are honest reports about the detector, and both keep the
    `n + 1` invariant that dropping them would break.
    """
    if duration_sec is None or duration_sec <= 0:
        return []
    shots: List[Shot] = []
    prev = 0.0
    for i, t in enumerate(list(cut_times) + [duration_sec]):
        end = min(max(float(t), prev), duration_sec)
        # 🔴 Not rounded, and that is load-bearing. Rounding to milliseconds
        # moves a boundary by up to 0.5 ms in whichever direction the third
        # decimal happens to fall, and a keyframe extracted *at* that cut
        # carries the unrounded timestamp. Half the frames then land in the
        # shot the cut closes instead of the one it opens -- measured 19/19 on
        # a 50-shot clip, split purely by rounding direction, with nothing
        # raising. Round for display, never for storage.
        shots.append(Shot(
            index=i,
            start_sec=prev,
            end_sec=end,
            dur_sec=end - prev,
        ))
        prev = end
    return shots


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


@dataclass
class ShotFrames:
    """One shot together with the keyframes that fall inside it."""
    shot: Shot
    frames: List[Any]
    representative: Optional[Any]


def shot_index_for(t: float, shots: Sequence[Shot]) -> Optional[int]:
    """Which shot contains timestamp `t`, or None when it falls outside them all.

    Spans are half-open `[start, end)`: a frame landing exactly on a cut belongs
    to the shot that cut **opens**, not the one it closes — that is what a cut
    means. The final shot is closed at its end so a frame at exactly the clip
    duration still lands somewhere rather than falling off the back.

    A consequence worth stating rather than discovering: a zero-length shot (the
    honest record of a boundary detected at 0.0, see `shots_from_cuts`) can never
    contain a frame. That is correct — nothing was on screen for zero seconds —
    and it is why `bind_frames_to_shots` reports an empty `frames` list for it
    instead of borrowing a neighbour's frame to look tidy.
    """
    if not shots:
        return None
    last = len(shots) - 1
    for i, sh in enumerate(shots):
        if sh.start_sec <= t < sh.end_sec:
            return i
        if i == last and t == sh.end_sec:
            return i
    return None


def bind_frames_to_shots(
    shots: Sequence[Shot],
    frames: Sequence[Any],
    timestamp_of: Optional[Callable[[Any], float]] = None,
) -> Tuple[List[ShotFrames], List[Any]]:
    """Group already-extracted keyframes under the shot each one falls in.

    **Derived, never stored.** An earlier design put a `shot_id` column on
    `keyframes`, and it would have been wrong: `save_shots` replaces a clip's
    spans wholesale, so every re-analyze mints new `shots.id` values and every
    stored `shot_id` would point at a row that no longer exists — silently, with
    nothing raising. Recomputing the grouping from the current spans costs one
    pass over a few hundred rows and cannot go stale.

    Returns `(per_shot, unassigned)`. `unassigned` is not swallowed: a frame
    outside every span means the frame table and the shot table were built from
    different measurements of the clip, and that is worth seeing.
    """
    get = timestamp_of or (lambda f: float(f["timestamp_sec"]))
    buckets: List[List[Any]] = [[] for _ in shots]
    unassigned: List[Any] = []
    for f in frames:
        try:
            t = float(get(f))
        except (TypeError, ValueError, KeyError, IndexError):
            unassigned.append(f)
            continue
        i = shot_index_for(t, shots)
        if i is None:
            unassigned.append(f)
        else:
            buckets[i].append(f)

    out: List[ShotFrames] = []
    for sh, fr in zip(shots, buckets):
        fr = sorted(fr, key=get)
        out.append(ShotFrames(shot=sh, frames=fr, representative=_representative(sh, fr, get)))
    return out, unassigned


def _representative(shot: Shot, frames: List[Any], get: Callable[[Any], float]) -> Optional[Any]:
    """The frame nearest the shot's midpoint; ties go to the earlier one.

    Not the first frame: the frame immediately after a cut is the one most
    likely to be a dissolve, a whip or motion blur — exactly the frame that
    represents the shot worst. The midpoint is the cheapest defensible choice
    that avoids both edges, and it is deterministic, which matters because this
    id ends up quoted in downstream output.
    """
    if not frames:
        return None
    mid = (shot.start_sec + shot.end_sec) / 2.0
    return min(frames, key=lambda f: (abs(get(f) - mid), get(f)))


def compute_shot_metrics(
    video_path: str,
    scene_threshold: Optional[float] = None,
    duration_sec: Optional[float] = None,
) -> Optional[ShotMetrics]:
    """Measure shot rhythm for a clip. Returns None when duration is unknown
    (can't compute cuts/minute) or ffmpeg is unavailable — callers treat a None
    as "no measured pacing signal" rather than a fabricated zero.

    Thin wrapper over `compute_shot_table` for callers that only want the
    aggregate. Anything wanting the spans should call that directly rather than
    run the ffmpeg pass a second time."""
    table = compute_shot_table(video_path, scene_threshold, duration_sec)
    return table[0] if table else None


def compute_shot_table(
    video_path: str,
    scene_threshold: Optional[float] = None,
    duration_sec: Optional[float] = None,
) -> Optional[Tuple[ShotMetrics, List[Shot]]]:
    """One ffmpeg pass, both outputs: the aggregate and the per-shot spans.

    Returns None on the same conditions the aggregate alone returned None for —
    unknown duration or a failed pass — because a shot table built on a
    fabricated duration would be worse than no table."""
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

    cut_times = parse_cut_times(result.stderr)
    return metrics_from_cuts(len(cut_times), duration), shots_from_cuts(cut_times, duration)
