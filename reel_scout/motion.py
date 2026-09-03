"""Camera movement per shot, read from the codec's own motion vectors (§6C).

**This is a restart, not a first attempt.** The 2026-09-02 spike failed and the
roadmap records exactly why: phase correlation on a 64×64 downsample measures
*apparent global motion*, and apparent global motion cannot tell "the camera
moved" from "a large element inside the frame moved". A composited transition
scored 0.898 on block agreement and was read as a pan. The spike's own list of
what it would take put dense flow first — and ruled it out, because separating
global from local motion meant opencv or scipy, which this package will not
take on.

H.264 already carries that field. Every inter-coded macroblock has a vector, so
the decoder hands us a coarse dense flow for free: positions *and* motions,
which is what makes the two cases separable at all.

**What the measurement supports, and nothing more.** Two numbers per frame — the
median block motion (how fast the frame is moving) and the share of blocks that
agree with it (whether the motion is one thing or many) — give four states:

    speed ≈ 0, agreement high   the camera is locked off
    speed ≈ 0, agreement low    the camera is still; something in frame moves
    speed > 0, agreement high   the camera is moving, coherently
    speed > 0, agreement low    moving and incoherent: handheld, or compositing

🔴 **`STILL_SUBJECT_MOVES` is the whole point of the restart.** It is the state
the spike had no way to name, and the one it kept misreading as camera work.

⚠️ **We deliberately do not emit pan / tilt / dolly.** `dx` and `dy` are stored,
so the direction is there for a reader — but naming a move claims the camera did
a thing, and across 585 hand-scanned shots only 7 were coherent moves at all.
Seven is not enough to earn a vocabulary. The spike shipped nothing precisely
because it was about to name things it could not measure; this ships the four
states each of which has a hand-checked example behind it (2026-09-03):

    locked off            a talking-head clip, 50 shots, speed 0.00, agreement 76%
    still, subject moves  `Wax On Wax Off` 74–78s, the composited transition
                          that fooled the first attempt: median stays 0.00 while
                          60–73% of blocks move and agreement falls to 46%
    camera moves          `yt_Zh32NmKpktI` 67.4–70.5s, speed 3.34, agreement 72%
                          — framing visibly drifts across the same scene
    unsteady              `Wax On Wax Off` shot 45, speed 12.12, agreement 3%
                          — handheld, subjects jostling, no single motion
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import db

#: The four states, and `UNKNOWN` for a shot with too few coded frames to say.
STATIC = "static"
STILL_SUBJECT_MOVES = "still_subject_moves"
CAMERA_MOVES = "camera_moves"
UNSTEADY = "unsteady"
UNKNOWN = "unknown"
MOVEMENTS = (STATIC, STILL_SUBJECT_MOVES, CAMERA_MOVES, UNSTEADY)

#: Pixels per frame below which the frame is not travelling. Set from the
#: measured floor rather than taste: a locked-off camera returns a median of
#: exactly 0.00 because the codec codes an unmoved block as a zero vector, so
#: anything above half a pixel is real.
SPEED_FLOOR = 0.5

#: Share of blocks that must match the global motion for it to count as one
#: motion. The control clip sat at 0.76 and the composited transition at 0.46,
#: so 0.6 separates them with room on both sides.
AGREEMENT_FLOOR = 0.6

#: A shot shorter than this many coded frames is reported `UNKNOWN` rather than
#: guessed at. Medians over three frames are not medians.
MIN_FRAMES = 8


def _tolerance(dx: float, dy: float) -> float:
    """How far a block may sit from the global motion and still agree.

    🔴 **Scales with the motion, and that is not a nicety.** The first version
    fixed it at ±1 px, which made agreement collapse on every fast move — a
    12 px/frame pan cannot hold every block inside one pixel of the median. The
    metric was manufacturing the signal it then reported: fast camera moves came
    back as "incoherent" by construction.
    """
    return max(1.0, 0.25 * (dx * dx + dy * dy) ** 0.5)


def frame_motion(path: str) -> Iterator[Tuple[float, float, float, float]]:
    """`(t_sec, dx, dy, agreement)` per inter-coded frame.

    Raises ImportError when PyAV is absent: this is the `motion` extra, and a
    missing optional dependency must say so rather than return an empty result
    that reads like "this clip does not move".
    """
    try:
        import av
        from av.codec.context import Flags2
    except ImportError:
        raise ImportError(
            "PyAV not installed. Install with: pip install 'reel-scout[motion]'")
    import numpy as np

    container = av.open(path)
    try:
        stream = container.streams.video[0]
        stream.codec_context.flags2 |= Flags2.export_mvs
        time_base = float(stream.time_base)
        for frame in container.decode(stream):
            side = frame.side_data.get("MOTION_VECTORS")
            if side is None:
                # Intra-coded: nothing was predicted, so nothing moved *here*.
                # Skipped rather than counted as zero motion -- a keyframe every
                # two seconds would otherwise drag every median toward still.
                continue
            vectors = side.to_ndarray()
            forward = vectors[vectors["source"] < 0]
            if len(forward) == 0:
                continue
            scale = forward["motion_scale"].astype(np.float64)
            scale[scale == 0] = 1
            dxs = forward["motion_x"] / scale
            dys = forward["motion_y"] / scale
            dx = float(np.median(dxs))
            dy = float(np.median(dys))
            tol = _tolerance(dx, dy)
            agree = float(np.mean((np.abs(dxs - dx) <= tol)
                                  & (np.abs(dys - dy) <= tol)))
            yield (frame.pts or 0) * time_base, dx, dy, agree
    finally:
        container.close()


def classify(speed: float, agreement: float) -> str:
    """One of :data:`MOVEMENTS` from the two measured numbers."""
    moving = speed >= SPEED_FLOOR
    coherent = agreement >= AGREEMENT_FLOOR
    if not moving:
        return STATIC if coherent else STILL_SUBJECT_MOVES
    return CAMERA_MOVES if coherent else UNSTEADY


def shot_motion(conn, video_id: str, path: str) -> List[Dict[str, Any]]:
    """Per-shot motion for one clip, in shot order.

    Aggregates with medians, for the reason the 2026-09-02 spike measured
    rather than the one that sounds right: a shot there had a median of 0.00
    and a mean of 4.16, because a handful of large jumps carried the average
    away. The median resists those; that is its whole job here.

    ⚠️ **It does not fix the frame-type rhythm, and nothing here does.** The
    codec alternates between frames that reference near and far, so agreement
    swings frame to frame — measured at 2% vs 55% moving blocks on adjacent
    frames of the same shot. A median over a strictly alternating series *is*
    its mean, so this is not filtered, only tolerated: the thresholds below
    were set on data carrying the same rhythm, so they are consistent with it
    rather than corrected for it.
    """
    import numpy as np

    shots = conn.execute(
        "SELECT idx, start_sec, end_sec FROM shots WHERE video_id = ? "
        "ORDER BY idx", (video_id,)).fetchall()
    if not shots:
        return []
    frames = list(frame_motion(path))
    out: List[Dict[str, Any]] = []
    for shot in shots:
        inside = [f for f in frames if shot["start_sec"] <= f[0] < shot["end_sec"]]
        if len(inside) < MIN_FRAMES:
            out.append({"idx": shot["idx"], "t_sec": shot["start_sec"],
                        "dx": None, "dy": None, "speed": None,
                        "agreement": None, "frames": len(inside),
                        "movement": UNKNOWN})
            continue
        dx = float(np.median([f[1] for f in inside]))
        dy = float(np.median([f[2] for f in inside]))
        speed = float(np.median([(f[1] ** 2 + f[2] ** 2) ** 0.5 for f in inside]))
        agreement = float(np.median([f[3] for f in inside]))
        out.append({"idx": shot["idx"], "t_sec": shot["start_sec"],
                    "dx": dx, "dy": dy, "speed": speed,
                    "agreement": agreement, "frames": len(inside),
                    "movement": classify(speed, agreement)})
    return out
