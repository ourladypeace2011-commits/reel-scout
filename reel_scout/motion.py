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

**What the measurement supports, and nothing more.** Each frame is fitted with
a similarity transform — translation, scale, rotation — over the block
displacements, and two numbers come out of it: how far the fitted transform
moves the picture, and the share of blocks that the fit explains. Those give
four states:

    moving ≈ 0, agreement high   the camera is locked off
    moving ≈ 0, agreement low    the camera is still; something in frame moves
    moving > 0, agreement high   the camera is moving, coherently
    moving > 0, agreement low    moving and incoherent: handheld, or compositing

🔴 **The fit replaced a median, and that is the 2026-09-05 fix.** Reading only
`motion_x`/`motion_y` and taking their median measures *translation* and
nothing else. The vector field of a push-in is radial and the field of a
rotation is tangential, and **both have a median of exactly (0, 0)** — so a
zoom came back as `speed 0.00, agreement 0.93` and was reported as "the camera
is locked off", confidently and wrongly. Measured on synthetic clips:

    clip              before                    after
    45% push-in       static                    camera_moves (zoom +7.1%)
    200% push-in      still_subject_moves       camera_moves (zoom +112.9%)
    51.6 deg rotate   static                    camera_moves (rot +25.3 deg)
    80 px/s pan       camera_moves (unchanged, and the fit reports zoom +0.0%)
    locked off        static (unchanged)

⚠️ **The fitted magnitudes are a floor, not a reading.** A 45% push-in measures
+7.1% and a 200% one measures +113%: the encoder codes a sub-pixel displacement
as a zero vector, so the slow end of the range is quantised away. The number
answers "did the frame transform beyond translation, and by roughly how much",
not "by exactly how much".

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

#: The codec exported no motion vectors for this file at all.
#:
#: 🔴 **Split from `UNKNOWN` because they have different remedies and only one
#: of them is about the clip.** FFmpeg exports vectors for H.264; HEVC, VP9 and
#: AV1 give none, and an all-intra file has none to give. All three used to
#: produce `frames=0` and land in `UNKNOWN`, whose explanation — in the CLI and
#: in the Chinese UI — says "too few coded frames". That reads as a fact about
#: a short shot. It is a fact about the container, and no amount of re-running
#: changes it.
#:
#: Measured 2026-09-05: `pan_hevc`, `pan_vp9` and an all-intra H.264 all
#: returned `frames=0`; a genuinely short H.264 shot returned `frames=5`. The
#: library today is 118/118 H.264 so nothing was mislabelled yet — but
#: `analyze <local path>` takes any file, and an iPhone shoots HEVC by default.
UNSUPPORTED = "unsupported"
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
#:
#: ⚠️ **Unlike `SPEED_FLOOR`, this number has no measurement behind it.** It is
#: an assertion that eight is enough and seven is not, and nothing was run to
#: separate them — 4, 8 and 12 would each have sounded equally reasonable when
#: it was written. Recorded rather than quietly kept, because a threshold whose
#: provenance is "it seemed right" is the kind that survives for years by never
#: being questioned. At 30fps this is roughly a quarter-second of coded frames.
MIN_FRAMES = 8

#: Scale and rotation accumulated **across the whole shot** — 3% and 3 degrees
#: — above which the camera counts as moving even with no translation.
#:
#: Across the shot, not per frame, because that is the unit a viewer sees: a
#: half-second at the same rate barely changes the framing. And the separation
#: is the reason a robust fit was needed at all -- a plain least squares put a
#: pure pan at a spurious -11.8% zoom and +9.6 deg rotation, which is *larger*
#: than the 45% push-in's real +7.1%. After dropping outliers and refitting,
#: the pan reads +0.0% / +0.0 deg and the separation is total.
#:
#: ⚠️ Set from four synthetic clips (two true, two false). That is a thinner
#: base than `SPEED_FLOOR`, which sits on a measured hard zero, and the same
#: order as `AGREEMENT_FLOOR`, which was set from n=2. Stated so the next
#: person can widen it rather than trust it.
ZOOM_FLOOR = 0.03
ROTATION_FLOOR = 0.052


def _tolerance(dx, dy):
    """How far a block may sit from the modelled motion and still agree.

    Scalars or arrays; the fit calls it per block, so the rule has one
    definition rather than a vectorised copy that can drift from it.

    🔴 **Scales with the motion, and that is not a nicety.** The first version
    fixed it at ±1 px, which made agreement collapse on every fast move — a
    12 px/frame pan cannot hold every block inside one pixel of the median. The
    metric was manufacturing the signal it then reported: fast camera moves came
    back as "incoherent" by construction.
    """
    mag = (dx * dx + dy * dy) ** 0.5
    try:
        return max(1.0, 0.25 * mag)
    except (TypeError, ValueError):   # numpy array
        import numpy as np
        return np.maximum(1.0, 0.25 * mag)


def _fit_similarity(np, ux, uy, dxs, dys):
    """Least squares `(zoom, rotation, tx, ty, agreement)` over one frame.

    Solves `d = M·u + t` with `M = s·R - I`, four unknowns over every block:
    `dx = a·ux - b·uy + tx`, `dy = b·ux + a·uy + ty`. `u` is the block's
    position relative to frame centre, which is why this needs the positions
    the old median never read.

    🔴 **Seeded from the median, then grown by the fit's own tolerance.** Both
    halves are load-bearing, and each was chosen against a measurement:

    - **Seeded**, because a plain least squares has no majority rule. Thirty
      still blocks and twenty moving 6 px — the definition of "the camera is
      still, something in frame moves" — fit a translation of **2.50**, and
      that reads out as a camera move. The median is 0.00 by majority, so
      starting from the blocks that agree with it keeps the property the whole
      restart exists to protect. Measured: seeded, the same field fits 0.00.
    - **Grown**, because a zoom's blocks *disagree* with the median by design —
      the field is radial and its median is (0, 0). Seeding alone would throw
      away exactly the signal being looked for, so each pass re-admits every
      block the current fit explains, and the inlier set expands outward until
      it stops changing.

    ⚠️ **Seeding costs magnitude, and that is the trade taken.** A 200%
    push-in fits +113% unseeded and **+73%** seeded; a 51.6° rotation fits
    +25.3° and **+14.2°**. Both stay an order of magnitude above the floors,
    and a subject that pulls the fit is the worse failure: it turns a still
    camera into a camera move, which is a claim about the filmmaker.

    ⚠️ **It does not lock onto a moving subject.** Measured with a synthetic
    subject covering 70% of the frame: zoom +0.0%, translation 0.00, and the
    subject shows up where it belongs — agreement falling to 0.80.
    """
    import math

    n = len(ux)
    A = np.zeros((2 * n, 4))
    rhs = np.empty(2 * n)
    A[0::2, 0] = ux; A[0::2, 1] = -uy; A[0::2, 2] = 1.0
    A[1::2, 0] = uy; A[1::2, 1] = ux;  A[1::2, 3] = 1.0
    rhs[0::2] = dxs; rhs[1::2] = dys
    mdx, mdy = float(np.median(dxs)), float(np.median(dys))
    seed_tol = _tolerance(mdx, mdy)
    keep = (np.abs(dxs - mdx) <= seed_tol) & (np.abs(dys - mdy) <= seed_tol)
    if int(keep.sum()) < MIN_FRAMES:
        keep = np.ones(n, bool)
    sol = np.zeros(4)
    for _ in range(3):
        if int(keep.sum()) < MIN_FRAMES:
            break
        idx = np.repeat(keep, 2)
        sol = np.linalg.lstsq(A[idx], rhs[idx], rcond=None)[0]
        pred = A.dot(sol)
        res = np.hypot(rhs[0::2] - pred[0::2], rhs[1::2] - pred[1::2])
        grown = res <= _tolerance(pred[0::2], pred[1::2])
        if int(grown.sum()) < MIN_FRAMES:
            break
        keep = grown
    pred = A.dot(sol)
    res = np.hypot(rhs[0::2] - pred[0::2], rhs[1::2] - pred[1::2])
    # Agreement is now "the share of blocks the fit explains", not "the share
    # matching one translation". That is why a fast push-in stops reading as
    # handheld: 0.57 before, 0.63 after, on the same clip -- the zoom moved
    # from being unexplained variance to being part of the model.
    agree = float(np.mean(res <= _tolerance(pred[0::2], pred[1::2])))
    a, b, tx, ty = (float(x) for x in sol)
    # The codec's vectors point from the current block *back* to its reference,
    # so the fitted transform is current -> reference and a push-in comes out
    # as a shrink. Inverting it (`1/s`, `-theta`) makes a positive `zoom` mean
    # the frame got tighter, which is what a reader will assume. `tx`/`ty` are
    # deliberately **not** inverted: `dx`/`dy` have meant the codec's direction
    # since v18 and rows from both versions have to stay comparable.
    scale_c = math.hypot(1.0 + a, b)
    zoom = (1.0 / scale_c - 1.0) if scale_c > 1e-9 else 0.0
    return zoom, -math.atan2(b, 1.0 + a), tx, ty, agree


def read_motion(path: str):
    """`(rows, saw_frames)` — the per-frame readings, and whether any frame was
    decoded at all.

    Exists so the caller can tell "this shot is too short" from "this file has
    no vectors to read": both arrive as an empty list, and only one of them is
    about the clip. A file that decoded frames and yielded no readings has a
    codec that does not export motion vectors, or is coded entirely intra.
    """
    saw = [False]
    rows = list(frame_motion(path, _decoded=saw))
    return rows, saw[0]


def frame_motion(path: str, _decoded=None
                 ) -> Iterator[Tuple[float, float, float, float, float, float]]:
    """`(t_sec, dx, dy, zoom, rotation, agreement)` per inter-coded frame.

    `zoom` and `rotation` are per frame; the caller accumulates them across the
    shot. A positive `zoom` means the frame got tighter — the codec's own
    convention is the inverse of that, and :func:`_fit_similarity` inverts it.
    `dx`/`dy` keep the codec's direction, unchanged since v18.

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
            if _decoded is not None:
                _decoded[0] = True
            side = frame.side_data.get("MOTION_VECTORS")
            if side is None:
                # Intra-coded: nothing was predicted, so nothing moved *here*.
                # Skipped rather than counted as zero motion -- a keyframe every
                # two seconds would otherwise drag every median toward still.
                continue
            vectors = side.to_ndarray()
            forward = vectors[vectors["source"] < 0]
            if len(forward) < MIN_FRAMES:
                # Four unknowns need more than four blocks before the fit means
                # anything. Below that the frame says nothing, same as an
                # intra one.
                continue
            scale = forward["motion_scale"].astype(np.float64)
            scale[scale == 0] = 1
            dxs = forward["motion_x"] / scale
            dys = forward["motion_y"] / scale
            zoom, rot, dx, dy, agree = _fit_similarity(
                np,
                forward["dst_x"].astype(np.float64) - frame.width / 2.0,
                forward["dst_y"].astype(np.float64) - frame.height / 2.0,
                dxs, dys)
            yield (frame.pts or 0) * time_base, dx, dy, zoom, rot, agree
    finally:
        container.close()


def classify(speed: float, agreement: float,
             zoom: float = 0.0, rotation: float = 0.0) -> str:
    """One of :data:`MOVEMENTS` from the measured numbers.

    `zoom` and `rotation` are the totals across the shot, and they default to
    zero so a caller with only the old pair still gets the old answer — but a
    caller that passes zeros is asserting the shot was checked and does not
    zoom, which is a different claim from not having looked.
    """
    moving = (speed >= SPEED_FLOOR
              or abs(zoom) >= ZOOM_FLOOR
              or abs(rotation) >= ROTATION_FLOOR)
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
    frames, saw_frames = read_motion(path)
    # Decoded something, read nothing: the container has no vectors to give.
    # Every shot gets the same answer because it is the same fact about the
    # file, not a property of any shot in it.
    blank = UNSUPPORTED if (saw_frames and not frames) else UNKNOWN
    out: List[Dict[str, Any]] = []
    for shot in shots:
        inside = [f for f in frames if shot["start_sec"] <= f[0] < shot["end_sec"]]
        if len(inside) < MIN_FRAMES:
            out.append({"idx": shot["idx"], "t_sec": shot["start_sec"],
                        "dx": None, "dy": None, "speed": None,
                        "agreement": None, "zoom": None, "rotation": None,
                        "frames": len(inside), "movement": blank})
            continue
        # ⚠️ `speed` is NOT `hypot(dx, dy)`, and a reader who assumes it is
        # will not be able to reproduce the classification from the stored
        # row. They are two different statistics on purpose: `dx`/`dy` are the
        # medians of each component ("which way, typically") and `speed` is the
        # median of the magnitudes ("how fast, typically"). A shot that drifts
        # left as often as right has dx ≈ 0 and a speed well above zero, and
        # only the second of those is the answer to "is this moving".
        dx = float(np.median([f[1] for f in inside]))
        dy = float(np.median([f[2] for f in inside]))
        speed = float(np.median([(f[1] ** 2 + f[2] ** 2) ** 0.5 for f in inside]))
        agreement = float(np.median([f[5] for f in inside]))
        # Compounded, not summed: scale multiplies. The typical per-frame rate
        # sustained across the shot is what a viewer sees as "it ended tighter
        # than it started", and it is the only form of this number worth a
        # threshold -- half a second of the same rate changes nothing.
        n = len(inside)
        zoom = (1.0 + float(np.median([f[3] for f in inside]))) ** n - 1.0
        rotation = float(np.median([f[4] for f in inside])) * n
        out.append({"idx": shot["idx"], "t_sec": shot["start_sec"],
                    "dx": dx, "dy": dy, "speed": speed,
                    "agreement": agreement, "zoom": zoom, "rotation": rotation,
                    "frames": n,
                    "movement": classify(speed, agreement, zoom, rotation)})
    return out
