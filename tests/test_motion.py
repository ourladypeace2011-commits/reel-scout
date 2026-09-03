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


def _mvs(motions, scale=4):
    a = np.zeros(len(motions), dtype=_MV_DTYPE)
    a["source"] = -1
    a["motion_scale"] = scale
    a["motion_x"] = [int(round(mx * scale)) for mx, _ in motions]
    a["motion_y"] = [int(round(my * scale)) for _, my in motions]
    return a


class _Side:
    def __init__(self, arr):
        self._arr = arr

    def to_ndarray(self):
        return self._arr


class _Frame:
    def __init__(self, pts, arr):
        self.pts = pts
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
    (t, dx, dy, agree), = out
    assert (t, dx, dy) == (1.0, 4.0, 0.0)
    assert agree == 1.0


def test_one_moving_element_leaves_the_median_still_and_drops_agreement():
    # 30 background blocks hold; 20 blocks travel. This is the shape the spike
    # could not distinguish from a pan.
    frames = [_Frame(0, _mvs([(0.0, 0.0)] * 30 + [(6.0, 0.0)] * 20))]
    with patch.dict(sys.modules, _fake_av(frames)):
        (_, dx, dy, agree), = list(motion.frame_motion("f.mp4"))
    assert (dx, dy) == (0.0, 0.0)
    assert agree == pytest.approx(0.6)
    assert motion.classify(0.0, agree) != motion.CAMERA_MOVES


def test_an_intra_frame_is_skipped_not_counted_as_still():
    # A keyframe predicts nothing. Counting it as zero motion would drag every
    # median toward "static" once every couple of seconds.
    frames = [_Frame(0, None), _Frame(2, _mvs([(4.0, 0.0)] * 10))]
    with patch.dict(sys.modules, _fake_av(frames)):
        out = list(motion.frame_motion("f.mp4"))
    assert len(out) == 1 and out[0][1] == 4.0


def test_motion_is_read_in_pixels_not_quarter_pixels():
    # motion_scale is 4 on this corpus; forgetting it reports every clip as
    # moving four times as fast as it does.
    frames = [_Frame(0, _mvs([(3.0, 0.0)] * 10, scale=4))]
    with patch.dict(sys.modules, _fake_av(frames)):
        (_, dx, _, _), = list(motion.frame_motion("f.mp4"))
    assert dx == 3.0


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
        few = [(0.1 * i, 5.0, 0.0, 0.9) for i in range(motion.MIN_FRAMES - 1)]
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
        frames = [(0.1 * i, 0.0, 0.0, 0.9) for i in range(17)]
        frames += [(0.1 * (17 + i), 40.0, 0.0, 0.9) for i in range(3)]
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
        swing = [(0.1 * i, 0.0, 0.0, 0.95 if i % 2 else 0.10) for i in range(20)]
        with patch.object(motion, "frame_motion", return_value=iter(swing)):
            rows = motion.shot_motion(conn, vid, "f.mp4")
        assert rows[0]["agreement"] == pytest.approx(0.525)
    finally:
        conn.close(); os.unlink(path)


def test_frames_are_bound_to_the_shot_whose_span_contains_them():
    conn, path = _temp_db()
    try:
        vid = _clip(conn)   # 0-5 and 5-10
        frames = ([(0.1 * i, 0.0, 0.0, 0.9) for i in range(20)] +
                  [(5.0 + 0.1 * i, 6.0, 0.0, 0.9) for i in range(20)])
        with patch.object(motion, "frame_motion", return_value=iter(frames)):
            rows = motion.shot_motion(conn, vid, "f.mp4")
        assert [r["movement"] for r in rows] == [motion.STATIC,
                                                 motion.CAMERA_MOVES]
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
