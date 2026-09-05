"""Camera movement from motion vectors (roadmap §6C, the restart)."""
import os
import sys
import sqlite3
import tempfile
import types
from unittest.mock import patch

import numpy as np
import pytest

from reel_scout import db, motion


def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn, path


def _clip(conn, vid="v1", shots=((0, 0.0, 5.0), (1, 5.0, 10.0))):
    conn.execute("INSERT INTO videos (id, platform, platform_id, url, file_path) "
                 "VALUES (?, 'p', ?, 'u', 'f.mp4')", (vid, vid))
    for idx, a, b in shots:
        conn.execute("INSERT INTO shots (video_id, idx, start_sec, end_sec) "
                     "VALUES (?, ?, ?, ?)", (vid, idx, a, b))
    conn.commit()
    return vid


# --- the four states -------------------------------------------------------

def test_the_four_states_are_the_two_numbers_crossed():
    assert motion.classify(0.0, 0.9) == motion.STATIC
    assert motion.classify(0.0, 0.3) == motion.STILL_SUBJECT_MOVES
    assert motion.classify(5.0, 0.9) == motion.CAMERA_MOVES
    assert motion.classify(5.0, 0.3) == motion.UNSTEADY


def test_a_still_camera_with_a_moving_element_is_not_called_static():
    # The state the 2026-09-02 spike had no way to name, and the one it kept
    # reading as camera work. Measured on the composited transition at 74-78s
    # of `Wax On Wax Off`: median motion stays 0.00 while agreement falls to
    # 0.46 because 60-73% of blocks move.
    assert motion.classify(0.0, 0.46) == motion.STILL_SUBJECT_MOVES


def test_the_control_clip_numbers_land_on_static():
    # A locked-off talking head, 50 shots: speed 0.00, agreement 0.76.
    assert motion.classify(0.0, 0.76) == motion.STATIC


def test_the_hand_checked_camera_move_lands_on_camera_moves():
    # yt_Zh32NmKpktI 67.4-70.5s: framing visibly drifts across one scene.
    assert motion.classify(3.34, 0.72) == motion.CAMERA_MOVES


# --- the tolerance bug that manufactured its own signal ---------------------

def test_the_tolerance_grows_with_the_motion():
    # Fixed at ±1px, a 12 px/frame pan cannot hold its blocks inside one pixel
    # of the median, so every fast camera move came back "incoherent" -- the
    # metric producing the finding it then reported.
    assert motion._tolerance(0.0, 0.0) == 1.0
    assert motion._tolerance(12.0, 0.0) == pytest.approx(3.0)
    assert motion._tolerance(0.0, 12.0) == pytest.approx(3.0)
    assert motion._tolerance(2.0, 0.0) == 1.0   # floor still applies


# --- extraction, without requiring the decoder -----------------------------

_MV_DTYPE = np.dtype({
    "names": ["source", "w", "h", "src_x", "src_y", "dst_x", "dst_y",
              "flags", "motion_x", "motion_y", "motion_scale"],
    "formats": ["<i4", "u1", "u1", "<i2", "<i2", "<i2", "<i2", "<u8",
                "<i4", "<i4", "<u2"],
    "offsets": [0, 4, 5, 6, 8, 10, 12, 16, 24, 28, 32],
    "itemsize": 40, "aligned": True})


#: The fixture frame. Blocks are placed inside it because the fit reads
#: positions -- a fixture leaving every block at (0, 0) cannot tell a
#: translation from a scale, and would have let the zoom bug through again.
_W, _H = 640, 360


def _grid(n):
    """`n` block centres spread over the fixture frame."""
    cols = int(np.ceil(np.sqrt(n)))
    rows = int(np.ceil(n / cols))
    return [(int((i % cols + 0.5) * _W / cols),
             int((i // cols + 0.5) * _H / rows)) for i in range(n)]


def _mvs(motions, scale=4, positions=None):
    a = np.zeros(len(motions), dtype=_MV_DTYPE)
    a["source"] = -1
    a["motion_scale"] = scale
    pos = positions if positions is not None else _grid(len(motions))
    a["dst_x"] = [p[0] for p in pos]
    a["dst_y"] = [p[1] for p in pos]
    a["motion_x"] = [int(round(mx * scale)) for mx, _ in motions]
    a["motion_y"] = [int(round(my * scale)) for _, my in motions]
    return a


def _radial(n, rate):
    """Blocks on the grid with a radial field, whose median is exactly (0, 0).

    A **positive** `rate` points the vectors outward. That is the codec coding
    a frame that got *wider*: its vectors run from the current block back to
    its reference, so content the frame is pushing in on has its reference
    nearer the centre, not further from it. A push-in is a negative rate.
    """
    pos = _grid(n)
    return pos, [((x - _W / 2.0) * rate, (y - _H / 2.0) * rate) for x, y in pos]


class _Side:
    def __init__(self, arr):
        self._arr = arr

    def to_ndarray(self):
        return self._arr


class _Frame:
    def __init__(self, pts, arr, width=_W, height=_H):
        self.pts = pts
        self.width = width
        self.height = height
        self.side_data = {"MOTION_VECTORS": _Side(arr)} if arr is not None else {}


def _fake_av(frames):
    """A stand-in for PyAV good enough to drive `frame_motion`.

    Scripted rather than skipped: `av` is not in the dev extra, and a test that
    skips itself on CI is decoration, not a gate -- the same call this package
    already made about onnxruntime.
    """
    av = types.ModuleType("av")
    ctx = types.ModuleType("av.codec.context")

    class Flags2:
        export_mvs = 1

    ctx.Flags2 = Flags2
    codec = types.ModuleType("av.codec")
    codec.context = ctx
    av.codec = codec

    class _CC:
        flags2 = 0

    class _Stream:
        codec_context = _CC()
        time_base = 0.5

    class _Container:
        streams = types.SimpleNamespace(video=[_Stream()])

        def decode(self, stream):
            return iter(frames)

        def close(self):
            self.closed = True

    av.open = lambda path: _Container()
    return {"av": av, "av.codec": codec, "av.codec.context": ctx}


def test_a_pan_reads_as_one_motion_every_block_agrees_with():
    frames = [_Frame(2, _mvs([(4.0, 0.0)] * 50))]
    with patch.dict(sys.modules, _fake_av(frames)):
        out = list(motion.frame_motion("f.mp4"))
    (t, dx, dy, zoom, rot, agree), = out
    # approx, not equality: since 2026-09-05 these come out of a least squares
    # solve rather than a median, so 4.0 arrives as 3.999999999999997. The
    # tolerance is for the solver, not for the measurement.
    assert t == 1.0
    assert (dx, dy) == pytest.approx((4.0, 0.0), abs=1e-9)
    assert agree == 1.0
    # And the fit does not invent a zoom out of a pure translation -- one
    # least squares put a real pan at -11.8%, which is why it refits.
    assert zoom == pytest.approx(0.0, abs=1e-9)
    assert rot == pytest.approx(0.0, abs=1e-9)


def test_one_moving_element_leaves_the_median_still_and_drops_agreement():
    # 30 background blocks hold; 20 blocks travel. This is the shape the spike
    # could not distinguish from a pan.
    frames = [_Frame(0, _mvs([(0.0, 0.0)] * 30 + [(6.0, 0.0)] * 20))]
    with patch.dict(sys.modules, _fake_av(frames)):
        (_, dx, dy, zoom, rot, agree), = list(motion.frame_motion("f.mp4"))
    assert (dx, dy) == pytest.approx((0.0, 0.0), abs=0.5)
    assert agree == pytest.approx(0.6, abs=0.05)
    assert motion.classify(0.0, agree, zoom, rot) != motion.CAMERA_MOVES


def test_an_intra_frame_is_skipped_not_counted_as_still():
    # A keyframe predicts nothing. Counting it as zero motion would drag every
    # median toward "static" once every couple of seconds.
    frames = [_Frame(0, None), _Frame(2, _mvs([(4.0, 0.0)] * 10))]
    with patch.dict(sys.modules, _fake_av(frames)):
        out = list(motion.frame_motion("f.mp4"))
    assert len(out) == 1 and out[0][1] == pytest.approx(4.0)


def test_motion_is_read_in_pixels_not_quarter_pixels():
    # motion_scale is 4 on this corpus; forgetting it reports every clip as
    # moving four times as fast as it does.
    frames = [_Frame(0, _mvs([(3.0, 0.0)] * 10, scale=4))]
    with patch.dict(sys.modules, _fake_av(frames)):
        (_, dx, _, _, _, _), = list(motion.frame_motion("f.mp4"))
    assert dx == pytest.approx(3.0)


# --- the field a median cannot see (2026-09-05) ------------------------------
#
# A push-in's vectors point outward from centre and a rotation's point around
# it. Both fields have a median of exactly (0, 0), so `speed 0.00` was read as
# "the camera is locked off" -- confidently, and wrongly. Measured on synthetic
# clips before the fix: a 45% push-in and a 51.6 deg rotation both classified
# `static`, the 45% one at agreement 0.93.

@pytest.mark.parametrize("rate, sign, label", [
    (-0.01, +1, "push-in: inward vectors, frame gets tighter"),
    (+0.01, -1, "pull-out: outward vectors, frame gets wider"),
])
def test_a_zoom_is_not_a_still_camera(rate, sign, label):
    pos, motions = _radial(64, rate)
    frames = [_Frame(0, _mvs(motions, positions=pos))]
    with patch.dict(sys.modules, _fake_av(frames)):
        (_, dx, dy, zoom, rot, agree), = list(motion.frame_motion("f.mp4"))
    # The old pair still says "not moving" -- that is the bug, preserved here
    # so the next reader can see what the new numbers are for.
    assert (dx, dy) == pytest.approx((0.0, 0.0), abs=0.05), label
    assert motion.classify(0.0, agree) == motion.STATIC, label
    # The fit sees it, and reports it in the reader's direction rather than
    # the codec's: positive means the frame got tighter.
    assert zoom * sign > 0, label
    assert abs(zoom) == pytest.approx(0.01, rel=0.05), label
    assert rot == pytest.approx(0.0, abs=1e-6), label
    assert motion.classify(0.0, agree, zoom * 20, rot) == motion.CAMERA_MOVES


def test_a_rotation_is_not_a_still_camera():
    pos = _grid(64)
    motions = [(-(y - _H / 2.0) * 0.01, (x - _W / 2.0) * 0.01) for x, y in pos]
    frames = [_Frame(0, _mvs(motions, positions=pos))]
    with patch.dict(sys.modules, _fake_av(frames)):
        (_, dx, dy, zoom, rot, agree), = list(motion.frame_motion("f.mp4"))
    assert (dx, dy) == pytest.approx((0.0, 0.0), abs=0.05)
    assert motion.classify(0.0, agree) == motion.STATIC
    assert abs(rot) == pytest.approx(0.01, rel=0.05)
    assert zoom == pytest.approx(0.0, abs=1e-3)
    assert motion.classify(0.0, agree, zoom, rot * 20) == motion.CAMERA_MOVES


def test_a_moving_subject_does_not_pull_the_fit_into_a_camera_move():
    # The regression the median-seeding exists to prevent. Unseeded, this same
    # field fits a translation of 2.50 -- and 2.50 with high agreement is a
    # camera move, so a still camera with a subject walking through it would
    # have been reported as one.
    pos = _grid(50)
    motions = [(0.0, 0.0)] * 30 + [(6.0, 0.0)] * 20
    frames = [_Frame(0, _mvs(motions, positions=pos))]
    with patch.dict(sys.modules, _fake_av(frames)):
        (_, dx, dy, zoom, rot, agree), = list(motion.frame_motion("f.mp4"))
    assert (dx, dy) == pytest.approx((0.0, 0.0), abs=1e-6)
    assert zoom == pytest.approx(0.0, abs=1e-6)
    assert motion.classify(0.0, agree, zoom, rot) != motion.CAMERA_MOVES


def test_a_pan_is_not_given_a_zoom_it_does_not_have():
    # The false positive that decided the fit had to reject outliers: a single
    # least squares put a pure pan at -11.8% zoom and +9.6 deg rotation, which
    # is *larger* than a real 45% push-in measures (+7.1%). A threshold set
    # above that noise would have been above the signal too.
    pos = _grid(64)
    frames = [_Frame(0, _mvs([(4.0, 0.0)] * 64, positions=pos))]
    with patch.dict(sys.modules, _fake_av(frames)):
        (_, dx, _, zoom, rot, _), = list(motion.frame_motion("f.mp4"))
    assert dx == pytest.approx(4.0)
    assert zoom == pytest.approx(0.0, abs=1e-9)
    assert rot == pytest.approx(0.0, abs=1e-9)


def test_the_floors_admit_a_zoom_with_no_translation_at_all():
    assert motion.classify(0.0, 0.9, zoom=0.10) == motion.CAMERA_MOVES
    assert motion.classify(0.0, 0.9, rotation=0.25) == motion.CAMERA_MOVES
    # And do not admit noise: a shot that neither travels nor transforms is
    # still locked off, which is the reading the floors have to keep earning.
    assert motion.classify(0.0, 0.9, zoom=0.01, rotation=0.01) == motion.STATIC


def test_a_shot_compounds_its_zoom_across_the_frames_it_holds():
    # Across the shot, not per frame: half a second at the same rate changes
    # nothing a viewer would call a camera move, so the threshold has to sit
    # on the total.
    conn, path = _temp_db()
    try:
        vid = _clip(conn, shots=((0, 0.0, 5.0),))
        frames = [(0.1 * i, 0.0, 0.0, 0.01, 0.0, 0.9) for i in range(20)]
        with patch.object(motion, "frame_motion", return_value=iter(frames)):
            rows = motion.shot_motion(conn, vid, "f.mp4")
        assert rows[0]["zoom"] == pytest.approx(1.01 ** 20 - 1)
        assert rows[0]["movement"] == motion.CAMERA_MOVES
    finally:
        conn.close(); os.unlink(path)


def test_a_missing_decoder_says_which_extra_to_install():
    with patch.dict(sys.modules, {"av": None}):
        with pytest.raises(ImportError) as exc:
            list(motion.frame_motion("f.mp4"))
    assert "motion" in str(exc.value)


# --- per-shot aggregation ---------------------------------------------------

def test_a_shot_with_too_few_coded_frames_is_unknown_not_guessed():
    conn, path = _temp_db()
    try:
        vid = _clip(conn, shots=((0, 0.0, 5.0),))
        few = [(0.1 * i, 5.0, 0.0, 0.0, 0.0, 0.9) for i in range(motion.MIN_FRAMES - 1)]
        with patch.object(motion, "frame_motion", return_value=iter(few)):
            rows = motion.shot_motion(conn, vid, "f.mp4")
        assert rows[0]["movement"] == motion.UNKNOWN
        assert rows[0]["speed"] is None
    finally:
        conn.close(); os.unlink(path)


def test_a_few_large_jumps_do_not_carry_the_shot():
    # The spike's own measurement: one shot had a median of 0.00 and a mean of
    # 4.16 because a handful of frames jumped. A mean here would report a
    # locked-off camera as moving.
    conn, path = _temp_db()
    try:
        vid = _clip(conn, shots=((0, 0.0, 5.0),))
        frames = [(0.1 * i, 0.0, 0.0, 0.0, 0.0, 0.9) for i in range(17)]
        frames += [(0.1 * (17 + i), 40.0, 0.0, 0.0, 0.0, 0.9) for i in range(3)]
        with patch.object(motion, "frame_motion", return_value=iter(frames)):
            rows = motion.shot_motion(conn, vid, "f.mp4")
        assert rows[0]["speed"] == 0.0
        assert rows[0]["movement"] == motion.STATIC
    finally:
        conn.close(); os.unlink(path)


def test_the_frame_type_rhythm_is_tolerated_not_filtered():
    # Adjacent frames of one shot measured 2% and 55% moving blocks. A median
    # over a strictly alternating series is its mean, so this is documented as
    # a known limit rather than claimed as handled.
    conn, path = _temp_db()
    try:
        vid = _clip(conn, shots=((0, 0.0, 5.0),))
        swing = [(0.1 * i, 0.0, 0.0, 0.0, 0.0, 0.95 if i % 2 else 0.10)
                 for i in range(20)]
        with patch.object(motion, "frame_motion", return_value=iter(swing)):
            rows = motion.shot_motion(conn, vid, "f.mp4")
        assert rows[0]["agreement"] == pytest.approx(0.525)
    finally:
        conn.close(); os.unlink(path)


def test_frames_are_bound_to_the_shot_whose_span_contains_them():
    conn, path = _temp_db()
    try:
        vid = _clip(conn)   # 0-5 and 5-10
        frames = ([(0.1 * i, 0.0, 0.0, 0.0, 0.0, 0.9) for i in range(20)] +
                  [(5.0 + 0.1 * i, 6.0, 0.0, 0.0, 0.0, 0.9) for i in range(20)])
        with patch.object(motion, "frame_motion", return_value=iter(frames)):
            rows = motion.shot_motion(conn, vid, "f.mp4")
        assert [r["movement"] for r in rows] == [motion.STATIC,
                                                 motion.CAMERA_MOVES]
    finally:
        conn.close(); os.unlink(path)


# --- "no vectors" is not "too few frames" (2026-09-05) -----------------------

def test_a_file_whose_codec_exports_no_vectors_says_so():
    # HEVC, VP9, AV1 and an all-intra H.264 all decode fine and yield nothing.
    # Reported as `unknown` they read as "this shot was too short", which is a
    # claim about the clip; the truth is a claim about the container, and no
    # re-run changes it. Verified on real files 2026-09-05: pan_hevc, pan_vp9
    # and an all-intra H.264 all returned frames=0, while a genuinely short
    # H.264 shot returned frames=5.
    conn, path = _temp_db()
    try:
        vid = _clip(conn, shots=((0, 0.0, 5.0),))
        frames = [_Frame(i, None) for i in range(30)]   # decoded, no side data
        with patch.dict(sys.modules, _fake_av(frames)):
            rows = motion.shot_motion(conn, vid, "f.mp4")
        assert rows[0]["movement"] == motion.UNSUPPORTED
        assert rows[0]["frames"] == 0
    finally:
        conn.close(); os.unlink(path)


def test_a_short_shot_in_a_readable_file_is_still_unknown():
    # The other side of the split: vectors exist, this shot just has too few.
    conn, path = _temp_db()
    try:
        vid = _clip(conn, shots=((0, 0.0, 5.0),))
        few = [_Frame(2 * i, _mvs([(1.0, 0.0)] * 16))
               for i in range(motion.MIN_FRAMES - 1)]
        with patch.dict(sys.modules, _fake_av(few)):
            rows = motion.shot_motion(conn, vid, "f.mp4")
        assert rows[0]["movement"] == motion.UNKNOWN
        assert 0 < rows[0]["frames"] < motion.MIN_FRAMES
    finally:
        conn.close(); os.unlink(path)


def test_a_file_that_decodes_nothing_at_all_is_unknown_not_unsupported():
    # An empty or unreadable stream is not evidence about the codec.
    conn, path = _temp_db()
    try:
        vid = _clip(conn, shots=((0, 0.0, 5.0),))
        with patch.dict(sys.modules, _fake_av([])):
            rows = motion.shot_motion(conn, vid, "f.mp4")
        assert rows[0]["movement"] == motion.UNKNOWN
    finally:
        conn.close(); os.unlink(path)


def test_the_stored_speed_is_not_the_length_of_the_stored_dx_dy():
    # R10: they are different statistics on purpose, and a reader who assumes
    # otherwise cannot reproduce the classification from the row. A shot that
    # drifts left as often as right has dx ~ 0 and a speed well above it.
    conn, path = _temp_db()
    try:
        vid = _clip(conn, shots=((0, 0.0, 5.0),))
        swing = [(0.1 * i, 6.0 if i % 2 else -6.0, 0.0, 0.0, 0.0, 0.9)
                 for i in range(20)]
        with patch.object(motion, "frame_motion", return_value=iter(swing)):
            rows = motion.shot_motion(conn, vid, "f.mp4")
        r = rows[0]
        assert abs(r["dx"]) < 1e-9 and r["speed"] == pytest.approx(6.0)
        assert r["movement"] == motion.CAMERA_MOVES
    finally:
        conn.close(); os.unlink(path)


def test_a_clip_with_no_shots_measures_nothing():
    conn, path = _temp_db()
    try:
        vid = _clip(conn, shots=())
        assert motion.shot_motion(conn, vid, "f.mp4") == []
    finally:
        conn.close(); os.unlink(path)


# --- storage ---------------------------------------------------------------

def test_saving_replaces_rather_than_appends():
    # Motion is derived wholly from the current spans, so a re-run with
    # different spans must not leave the old rows beside the new ones.
    conn, path = _temp_db()
    try:
        vid = _clip(conn)
        row = {"t_sec": 0.0, "dx": 0.0, "dy": 0.0, "speed": 0.0,
               "agreement": 0.9, "frames": 20, "movement": motion.STATIC}
        db.save_shot_motion(conn, vid, [row, dict(row, t_sec=5.0)])
        db.save_shot_motion(conn, vid, [dict(row, t_sec=2.5)])
        stored = db.get_shot_motion(conn, vid)
        assert [r["t_sec"] for r in stored] == [2.5]
    finally:
        conn.close(); os.unlink(path)


def test_the_numbers_the_class_came_from_are_stored_with_it():
    # A class with no measurement under it is the shape §4E warned about.
    conn, path = _temp_db()
    try:
        vid = _clip(conn)
        db.save_shot_motion(conn, vid, [{
            "t_sec": 0.0, "dx": -3.5, "dy": 1.25, "speed": 3.7,
            "agreement": 0.71, "frames": 42, "movement": motion.CAMERA_MOVES}])
        r = db.get_shot_motion(conn, vid)[0]
        assert (r["dx"], r["dy"], r["speed"], r["agreement"], r["frames"]) == \
               (-3.5, 1.25, 3.7, 0.71, 42)
    finally:
        conn.close(); os.unlink(path)
