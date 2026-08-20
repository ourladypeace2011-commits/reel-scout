from __future__ import annotations

import os
import stat
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from reel_scout.vision.keyframe import (
    KeyframeInfo,
    _ensure_first_last,
    _scale_vf,
    _seek_args,
    auto_frame_budget,
    detect_timeout,
    extract_keyframes,
    select_spread,
)
from reel_scout import config


class TestKeyframeInfoHasScore:
    def test_keyframe_info_has_score(self) -> None:
        kf = KeyframeInfo(
            frame_index=0,
            timestamp_sec=1.0,
            file_path="/tmp/frame.jpg",
            strategy="scene",
        )
        assert hasattr(kf, "score")
        assert kf.score == 0.0

    def test_keyframe_info_custom_score(self) -> None:
        kf = KeyframeInfo(
            frame_index=0,
            timestamp_sec=1.0,
            file_path="/tmp/frame.jpg",
            strategy="scene",
            score=0.85,
        )
        assert kf.score == 0.85


class TestEnsureFirstLast:
    @patch("reel_scout.vision.keyframe.subprocess.run")
    @patch("reel_scout.vision.keyframe.os.path.exists", return_value=True)
    def test_ensure_first_last_adds_first(
        self, mock_exists: MagicMock, mock_run: MagicMock,
    ) -> None:
        frames = [
            KeyframeInfo(0, 5.0, "/tmp/f1.jpg", "scene", 0.5),
            KeyframeInfo(1, 10.0, "/tmp/f2.jpg", "scene", 0.8),
        ]
        result = _ensure_first_last(
            "/tmp/video.mp4", "/tmp/out", "vid1", frames, 10, 15.0,
        )
        # First frame should be prepended (timestamp 0.1)
        assert result[0].timestamp_sec == 0.1
        assert result[0].strategy == "first"
        # Last frame should be appended (timestamp 14.5)
        assert result[-1].timestamp_sec == 14.5
        assert result[-1].strategy == "last"
        assert len(result) == 4

    @patch("reel_scout.vision.keyframe.subprocess.run")
    def test_ensure_first_last_no_change(self, mock_run: MagicMock) -> None:
        frames = [
            KeyframeInfo(0, 0.2, "/tmp/f0.jpg", "scene", 0.5),
            KeyframeInfo(1, 5.0, "/tmp/f1.jpg", "scene", 0.8),
            KeyframeInfo(2, 9.5, "/tmp/f2.jpg", "scene", 0.6),
        ]
        result = _ensure_first_last(
            "/tmp/video.mp4", "/tmp/out", "vid1", frames, 10, 10.0,
        )
        # No change expected: 0.2 < 0.5 and 9.5 > (10.0 - 1.0)
        assert len(result) == 3
        mock_run.assert_not_called()

    @patch("reel_scout.vision.keyframe.subprocess.run")
    @patch("reel_scout.vision.keyframe.os.path.exists", return_value=True)
    def test_ensure_first_last_only_ever_adds(
        self, mock_exists: MagicMock, mock_run: MagicMock,
    ) -> None:
        """It used to trim, and it trimmed the wrong end.

        The loop removed ``min(middle, key=score)``. Every scene frame carries
        score 0.0, so ``min`` returned the *earliest* middle frame every time --
        a budget enforced by eating the clip from the front, sitting directly
        downstream of a detector that was already only reporting the front.

        Enforcing the budget is the caller's job now, via ``select_spread``,
        which thins evenly. This function guarantees the ends and nothing else,
        so it must never be the reason a frame disappears.
        """
        frames = [
            KeyframeInfo(0, 3.0, "/tmp/f0.jpg", "scene", 0.2),
            KeyframeInfo(1, 5.0, "/tmp/f1.jpg", "scene", 0.9),
            KeyframeInfo(2, 7.0, "/tmp/f2.jpg", "scene", 0.1),
        ]
        result = _ensure_first_last(
            "/tmp/video.mp4", "/tmp/out", "vid1", frames, 4, 20.0,
        )
        kept = [f.timestamp_sec for f in result]
        for ts in (3.0, 5.0, 7.0):
            assert ts in kept, "an input frame was dropped by a function that only adds"
        assert len(result) == 5, "first and last on top of the three given"


class TestAutoFrameBudget:
    def test_full_scan_curve(self) -> None:
        # claude-video full-scan table (pre-fps-cap on long clips)
        assert auto_frame_budget(20) == 30
        assert auto_frame_budget(45) == 40
        assert auto_frame_budget(120) == 60
        assert auto_frame_budget(300) == 80
        assert auto_frame_budget(900) == 100

    def test_focused_curve_is_denser(self) -> None:
        assert auto_frame_budget(45, focused=True) == 80
        assert auto_frame_budget(45, focused=False) == 40

    def test_hard_ceiling_100(self) -> None:
        assert auto_frame_budget(99999) == 100
        assert auto_frame_budget(99999, focused=True) == 100

    def test_fps_cap_short_clip(self) -> None:
        # <=2 fps: a 2s clip can supply at most 4 frames
        assert auto_frame_budget(2) == 4
        # 3s focused window: table says 10 but fps cap is 6
        assert auto_frame_budget(3, focused=True) == 6

    def test_unknown_duration(self) -> None:
        assert auto_frame_budget(0) == 30
        assert auto_frame_budget(-5) == 30

    # _extract_interval is stubbed because scene returning [] is now a fallback
    # trigger, not a dead end — without this the budget assertion below would
    # shell out to a real ffmpeg, which CI does not have.
    @patch("reel_scout.vision.keyframe._get_duration", return_value=900.0)
    @patch("reel_scout.vision.keyframe._extract_scene", return_value=[])
    @patch("reel_scout.vision.keyframe._extract_interval", return_value=[])
    @patch("reel_scout.vision.keyframe._ensure_first_last", side_effect=lambda *a, **k: a[3])
    @patch("reel_scout.vision.keyframe.os.makedirs")
    def test_auto_budget_clamped_to_frame_cap(
        self,
        mock_makedirs: MagicMock,
        mock_ensure: MagicMock,
        mock_interval: MagicMock,
        mock_scene: MagicMock,
        mock_duration: MagicMock,
    ) -> None:
        # COST RED LINE still holds: a 15-min clip's raw budget is 100 and never
        # reaches the extractor. What it clamps *to* is now duration-aware.
        from reel_scout.vision.keyframe import extract_keyframes, frame_cap

        extract_keyframes("/tmp/v.mp4", "/tmp/out", "vid1", strategy="scene", max_frames=0)
        called_max = mock_scene.call_args[0][3]
        assert called_max == frame_cap(900.0)
        assert called_max < 100


class TestFrameCap:
    """The cap that decides how densely a clip gets sampled.

    One flat number could not serve both ends of this library: a 9-second reel
    and an 82-minute interview drew the same budget, so the interview's timeline
    came out as a single segment covering most of the clip.
    """

    def test_short_form_is_unchanged(self) -> None:
        from reel_scout.vision.keyframe import frame_cap

        # Everything at or under the threshold keeps the old flat behaviour,
        # which is what makes this safe to land against an existing corpus.
        for duration in (0.5, 9.0, 34.0, 60.0, config.KEYFRAME_LONG_SEC):
            assert frame_cap(duration) == config.KEYFRAME_MAX

    def test_unknown_duration_falls_back_to_the_flat_cap(self) -> None:
        from reel_scout.vision.keyframe import frame_cap

        assert frame_cap(0) == config.KEYFRAME_MAX
        assert frame_cap(-1) == config.KEYFRAME_MAX

    def test_long_form_earns_frames_by_the_minute(self) -> None:
        from reel_scout.vision.keyframe import frame_cap

        # 10 minutes at 2/min = 20, which is more than the flat cap and under
        # the ceiling — the only region where the curve is actually visible.
        assert frame_cap(600.0) == 20
        assert frame_cap(600.0) > config.KEYFRAME_MAX

    def test_the_ceiling_binds_before_the_bill_does(self) -> None:
        from reel_scout.vision.keyframe import frame_cap

        # 82 minutes asks for 164 frames at 2/min. It is told KEYFRAME_MAX_LONG.
        assert frame_cap(4928.0) == config.KEYFRAME_MAX_LONG
        assert frame_cap(99999.0) == config.KEYFRAME_MAX_LONG

    def test_never_returns_less_than_the_flat_cap(self) -> None:
        from reel_scout.vision.keyframe import frame_cap

        # A low per-minute rate must not make a long clip *worse* off than a reel.
        with patch.object(config, "KEYFRAME_PER_MIN", 0.1):
            assert frame_cap(300.0) >= config.KEYFRAME_MAX

    def test_monotonic_in_duration(self) -> None:
        from reel_scout.vision.keyframe import frame_cap

        caps = [frame_cap(d) for d in (10, 180, 300, 600, 1200, 2400, 4800)]
        assert caps == sorted(caps)


class TestScaleAndSeekHelpers:
    def test_scale_vf(self) -> None:
        assert _scale_vf(0) == ""
        assert _scale_vf(1024) == "scale=1024:-2"

    def test_seek_args(self) -> None:
        assert _seek_args(0, 0) == []
        assert _seek_args(5, 0) == ["-ss", "5"]
        assert _seek_args(5, 10) == ["-ss", "5", "-to", "10"]
        assert _seek_args(0, 10) == ["-to", "10"]

    @patch("reel_scout.vision.keyframe._get_duration", return_value=30.0)
    @patch("reel_scout.vision.keyframe.os.path.exists", return_value=True)
    @patch("reel_scout.vision.keyframe.os.makedirs")
    @patch("reel_scout.vision.keyframe.subprocess.run")
    def test_resolution_appended_to_scene_vf(
        self,
        mock_run: MagicMock,
        mock_makedirs: MagicMock,
        mock_exists: MagicMock,
        mock_duration: MagicMock,
    ) -> None:
        from reel_scout.vision.keyframe import extract_keyframes

        mock_result = MagicMock()
        mock_result.stderr = ""
        mock_result.stdout = ""
        mock_run.return_value = mock_result

        mock_result.stderr = "\n".join(
            "n:%d pts_time:%.1f" % (i, i * 2.0) for i in range(6))

        extract_keyframes(
            "/tmp/v.mp4", "/tmp/out", "vid1",
            strategy="scene", max_frames=4, resolution=1024,
        )
        cmds = [c[0][0] for c in mock_run.call_args_list]

        # Pass 1 finds cuts and encodes nothing, so scaling there would be paid
        # for and thrown away. It must still ask showinfo for the timestamps.
        detect = cmds[0]
        vf = detect[detect.index("-vf") + 1]
        assert "showinfo" in vf
        assert "scale=" not in vf, "the detection pass writes no images to scale"
        assert "-f" in detect and detect[detect.index("-f") + 1] == "null"

        # Pass 2 is where pixels are produced, so that is where the resolution
        # has to apply.
        grabs = [c for c in cmds[1:] if "-frames:v" in c]
        assert grabs, "the chosen cuts still have to be extracted"
        for cmd in grabs:
            assert "scale=1024:-2" in cmd[cmd.index("-vf") + 1]

    @patch("reel_scout.vision.keyframe._get_duration", return_value=30.0)
    @patch("reel_scout.vision.keyframe.os.path.exists", return_value=True)
    @patch("reel_scout.vision.keyframe.os.makedirs")
    @patch("reel_scout.vision.keyframe.subprocess.run")
    def test_focus_window_adds_seek_args(
        self,
        mock_run: MagicMock,
        mock_makedirs: MagicMock,
        mock_exists: MagicMock,
        mock_duration: MagicMock,
    ) -> None:
        from reel_scout.vision.keyframe import extract_keyframes

        mock_result = MagicMock()
        mock_result.stderr = ""
        mock_result.stdout = ""
        mock_run.return_value = mock_result

        extract_keyframes(
            "/tmp/v.mp4", "/tmp/out", "vid1",
            strategy="scene", max_frames=4, start_sec=5.0, end_sec=10.0,
        )
        cmd = mock_run.call_args_list[0][0][0]
        assert "-ss" in cmd and "5.0" in cmd
        assert "-to" in cmd and "10.0" in cmd


class TestMotionStrategy:
    def test_motion_strategy_name(self) -> None:
        kf = KeyframeInfo(
            frame_index=0,
            timestamp_sec=2.0,
            file_path="/tmp/motion.jpg",
            strategy="motion",
            score=1.0,
        )
        assert kf.strategy == "motion"

    @patch("reel_scout.vision.keyframe._get_duration", return_value=30.0)
    @patch("reel_scout.vision.keyframe.os.path.exists", return_value=True)
    @patch("reel_scout.vision.keyframe.os.makedirs")
    @patch("reel_scout.vision.keyframe.subprocess.run")
    def test_extract_keyframes_motion(
        self,
        mock_run: MagicMock,
        mock_makedirs: MagicMock,
        mock_exists: MagicMock,
        mock_duration: MagicMock,
    ) -> None:
        from reel_scout.vision.keyframe import extract_keyframes

        # Simulate ffmpeg showinfo output with pts_time
        mock_result = MagicMock()
        mock_result.stderr = (
            "[Parsed_showinfo] n:0 pts:0 pts_time:1.5\n"
            "[Parsed_showinfo] n:1 pts:90000 pts_time:3.0\n"
        )
        mock_result.stdout = ""
        mock_run.return_value = mock_result

        frames = extract_keyframes(
            "/tmp/video.mp4", "/tmp/out", "vid1",
            strategy="motion", max_frames=8,
        )
        # Should have called ffmpeg with mpdecimate
        first_call_args = mock_run.call_args_list[0]
        cmd = first_call_args[0][0]
        vf_arg = cmd[cmd.index("-vf") + 1]
        assert "mpdecimate" in vf_arg


def test_detect_timeout_scales_with_duration_and_stays_capped():
    """A fixed 120s made long clips unextractable, and it failed quietly.

    Scene detection decodes until it has found enough cuts; `-frames:v` is what
    exits early. A 4h livestream of one person talking has almost no cuts, so
    the early exit never fires and ffmpeg runs the whole file against a budget
    it cannot meet. Measured: a 251-minute clip timed out at 120s, left zero
    keyframes, and sat in the library at status "transcribed" — indistinguishable
    from merely unfinished.

    The 300s cap comes from evidence, not taste: a 52-minute edited talk finished
    scene detection inside the old 120s budget and returned 38 cuts. `-frames:v`
    exits once there are enough, so a clip with cuts finds them early and only a
    cut-less one decodes on. Capping at all is safe only because a timeout is no
    longer fatal.
    """
    assert detect_timeout(8) == 120           # a reel keeps the old floor
    assert detect_timeout(1200) == 240        # 20 min earns more
    assert detect_timeout(15035) == 300       # 4 h is capped, not proportional
    assert detect_timeout(0) == 120           # unknown duration degrades to the floor


def test_scene_detect_timeout_falls_back_to_interval_instead_of_zero_frames(
        tmp_path, monkeypatch):
    """Zero keyframes is the one outcome worth engineering away.

    No frames means no visual layer, therefore no craft score — the difference
    between reel-scout and a transcript dump. Interval sampling seeks per frame
    rather than decoding through, so it costs the same at any duration; there is
    no reason for a scene-detection overrun to end the run.
    """
    from reel_scout.vision import keyframe

    def _timeout_on_scene_detect(cmd, **kw):
        if any("showinfo" in str(a) for a in cmd):
            raise subprocess.TimeoutExpired(cmd, 120)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(keyframe.subprocess, "run", _timeout_on_scene_detect)
    monkeypatch.setattr(keyframe, "_get_duration", lambda p: 15035.0)
    monkeypatch.setattr(
        keyframe, "_extract_interval",
        lambda video_path, output_dir, video_id, n, *a, **kw: [
            KeyframeInfo(frame_index=i, timestamp_sec=float(i * 100),
                         file_path="%s/f%d.jpg" % (output_dir, i),
                         strategy="interval")
            for i in range(n)])
    monkeypatch.setattr(keyframe, "_ensure_first_last", lambda *a, **kw: a[3])

    frames = keyframe.extract_keyframes(
        str(tmp_path / "x.mp4"), str(tmp_path), "vid",
        strategy="scene", max_frames=5)

    assert len(frames) == 5, "a scene-detect timeout must not end with zero frames"
    assert all(f.strategy == "interval" for f in frames)


def test_partial_scene_result_is_not_padded_with_interval(tmp_path, monkeypatch):
    """Falling back is for nothing at all, not for "fewer than budget".

    The first cut of this used `len(frames) < max_frames`, which quietly turned
    `scene` into `hybrid` for every clip whose cut count came in under budget:
    real scene frames diluted with arbitrary ones, and an extra VLM call for
    each padded frame. `hybrid` already exists for people who want that. The
    defect was zero frames, so zero is the only case that changes.
    """
    from reel_scout.vision import keyframe

    scene_frames = [KeyframeInfo(frame_index=i, timestamp_sec=float(i),
                                 file_path="%s/s%d.jpg" % (tmp_path, i),
                                 strategy="scene")
                    for i in range(2)]
    called = []

    monkeypatch.setattr(keyframe, "_get_duration", lambda p: 900.0)
    monkeypatch.setattr(keyframe, "_extract_scene", lambda *a, **kw: scene_frames)
    monkeypatch.setattr(keyframe, "_extract_interval",
                        lambda *a, **kw: called.append(1) or [])
    monkeypatch.setattr(keyframe, "_ensure_first_last", lambda *a, **kw: a[3])

    frames = keyframe.extract_keyframes(
        str(tmp_path / "x.mp4"), str(tmp_path), "vid",
        strategy="scene", max_frames=10)

    assert not called, "two real cuts under a budget of ten must not be padded"
    assert len(frames) == 2


# ── near-duplicate dedupe (2026-08-10) ────────────────────────────────────────
# The saving is frames NOT sent to the VLM, so every test here asserts both that
# the right frames go AND that the guards hold. A dedupe that quietly re-creates
# the sparse-timeline defect `frame_cap` exists to fix would be a regression
# dressed up as an optimisation.

def _fr(i, ts, path="/tmp/f.jpg"):
    from reel_scout.vision.keyframe import KeyframeInfo
    return KeyframeInfo(frame_index=i, timestamp_sec=float(ts),
                        file_path="%s.%d" % (path, i), strategy="interval")


def _hashes(monkeypatch, mapping):
    """Stub frame_dhash by file_path suffix so tests state similarity directly."""
    from reel_scout.vision import keyframe
    monkeypatch.setattr(
        keyframe, "frame_dhash",
        lambda p, size=8: mapping.get(int(p.rsplit(".", 1)[1])))


def test_dedupe_drops_identical_neighbours(monkeypatch):
    from reel_scout.vision import keyframe
    frames = [_fr(i, i) for i in range(8)]          # 1s apart
    _hashes(monkeypatch, {i: 0b0000 for i in range(8)})   # all identical
    kept, dropped = keyframe.dedupe_near_duplicates(frames, distance=4,
                                                    max_gap_sec=120, min_keep=4)
    assert dropped == 4
    assert len(kept) == 4                            # floor holds
    assert kept[0].timestamp_sec == 0.0              # first kept
    assert kept[-1].timestamp_sec == 7.0             # last kept
    assert [f.frame_index for f in kept] == [0, 1, 2, 3]  # re-indexed


def test_dedupe_keeps_visually_different_frames(monkeypatch):
    from reel_scout.vision import keyframe
    frames = [_fr(i, i) for i in range(6)]
    # (1 << 8i) - 1 → 0, 255, 65535 … consecutive values differ by 8 set bits,
    # comfortably past the distance-4 threshold. (An earlier draft used
    # 1 << 8i, which differs by only TWO bits — inside the threshold, so the
    # frames were correctly treated as duplicates and the test was wrong.)
    _hashes(monkeypatch, {i: (1 << (8 * i)) - 1 for i in range(6)})
    kept, dropped = keyframe.dedupe_near_duplicates(frames, distance=4,
                                                    max_gap_sec=120, min_keep=2)
    assert dropped == 0
    assert len(kept) == 6


def test_dedupe_never_drops_across_a_wide_time_gap(monkeypatch):
    """Two identical frames seventeen minutes apart are information, not noise —
    this is the guard that stops dedupe re-creating the sparse-timeline bug."""
    from reel_scout.vision import keyframe
    frames = [_fr(0, 0), _fr(1, 1020), _fr(2, 2040), _fr(3, 3060),
              _fr(4, 4080), _fr(5, 5100)]            # 17 min apart
    _hashes(monkeypatch, {i: 0 for i in range(6)})   # all identical
    kept, dropped = keyframe.dedupe_near_duplicates(frames, distance=4,
                                                    max_gap_sec=120, min_keep=2)
    assert dropped == 0, "wide-gap frames must survive even when identical"
    assert len(kept) == 6


def test_dedupe_unhashable_frame_is_never_dropped(monkeypatch):
    """A frame ffmpeg could not read must be kept, not silently discarded."""
    from reel_scout.vision import keyframe
    frames = [_fr(i, i) for i in range(6)]
    h = {i: 0 for i in range(6)}
    h[3] = None                                      # unreadable
    _hashes(monkeypatch, h)
    kept, dropped = keyframe.dedupe_near_duplicates(frames, distance=4,
                                                    max_gap_sec=120, min_keep=2)
    assert any(f.timestamp_sec == 3.0 for f in kept)


def test_dedupe_is_a_noop_below_the_floor(monkeypatch):
    from reel_scout.vision import keyframe
    frames = [_fr(i, i) for i in range(3)]
    _hashes(monkeypatch, {i: 0 for i in range(3)})
    kept, dropped = keyframe.dedupe_near_duplicates(frames, distance=4,
                                                    max_gap_sec=120, min_keep=4)
    assert (dropped, len(kept)) == (0, 3)


def test_extract_keyframes_does_not_top_up_after_dedupe(tmp_path, monkeypatch):
    """The whole point: removed frames are NOT replaced, so the returned count
    comes in UNDER the cap. A top-up would spend the same and hide the saving."""
    from reel_scout.vision import keyframe
    frames = [_fr(i, i) for i in range(10)]
    monkeypatch.setattr(keyframe, "_get_duration", lambda p: 10.0)
    monkeypatch.setattr(keyframe, "_extract_interval", lambda *a, **kw: list(frames))
    monkeypatch.setattr(keyframe, "_ensure_first_last", lambda *a, **kw: a[3])
    monkeypatch.setattr(keyframe.config, "KEYFRAME_DEDUPE", True)
    _hashes(monkeypatch, {i: 0 for i in range(10)})

    out = keyframe.extract_keyframes(str(tmp_path / "x.mp4"), str(tmp_path), "vid",
                                     strategy="interval", max_frames=10)
    assert len(out) < 10, "dedupe must reduce spend, not reshuffle within it"
    assert len(out) == config.KEYFRAME_DEDUPE_MIN


def test_extract_keyframes_dedupe_can_be_switched_off(tmp_path, monkeypatch):
    from reel_scout.vision import keyframe
    frames = [_fr(i, i) for i in range(10)]
    monkeypatch.setattr(keyframe, "_get_duration", lambda p: 10.0)
    monkeypatch.setattr(keyframe, "_extract_interval", lambda *a, **kw: list(frames))
    monkeypatch.setattr(keyframe, "_ensure_first_last", lambda *a, **kw: a[3])
    monkeypatch.setattr(keyframe.config, "KEYFRAME_DEDUPE", False)
    _hashes(monkeypatch, {i: 0 for i in range(10)})

    out = keyframe.extract_keyframes(str(tmp_path / "x.mp4"), str(tmp_path), "vid",
                                     strategy="interval", max_frames=10)
    assert len(out) == 10


def test_frame_dhash_returns_none_on_short_ffmpeg_output(monkeypatch):
    """Truncated ffmpeg output must yield None, not a hash built from garbage."""
    from reel_scout.vision import keyframe
    monkeypatch.setattr(keyframe.subprocess, "run",
                        lambda *a, **kw: MagicMock(stdout=b"\x00" * 9))
    assert keyframe.frame_dhash("/tmp/x.jpg") is None


def test_frame_dhash_builds_a_64_bit_hash(monkeypatch):
    """9x8 grayscale in, 64 comparison bits out — the shape the distance
    threshold is calibrated against."""
    from reel_scout.vision import keyframe
    # Each row ascends left-to-right, so every comparison is "not greater" = 0.
    row = bytes(range(9))
    monkeypatch.setattr(keyframe.subprocess, "run",
                        lambda *a, **kw: MagicMock(stdout=row * 8))
    assert keyframe.frame_dhash("/tmp/x.jpg") == 0
    # Descending rows flip every bit.
    rev = bytes(reversed(range(9)))
    monkeypatch.setattr(keyframe.subprocess, "run",
                        lambda *a, **kw: MagicMock(stdout=rev * 8))
    assert keyframe.frame_dhash("/tmp/x.jpg") == (1 << 64) - 1


# --- the sampling actually reaching the whole clip -----------------------------
#
# Measured on the library before this change: 27 of 101 clips had a single
# unsampled gap larger than half their length, the average largest gap was 35.6%
# of the clip, and the worst was a 40-minute talk of which 39 minutes were never
# looked at. The cause was not the slice at the end of `extract_keyframes` -- that
# line was dead -- but `-frames:v max_frames` on the detection pass, which made
# ffmpeg stop *detecting* after N cuts.


CLUSTERED = (
    # A real shape: 10 cuts inside the first 1.7s, then 40 across the rest.
    [round(0.1 + i * 0.16, 3) for i in range(10)]
    + [round(1.7 + i * 0.34, 3) for i in range(1, 41)]
)


def _max_gap(values):
    return max(b - a for a, b in zip(values, values[1:]))


class TestSelectSpread:
    def test_it_keeps_both_ends_and_the_right_count(self):
        for n in (2, 3, 8, 17):
            picked = select_spread(CLUSTERED, n)
            assert len(picked) == n
            assert len(set(picked)) == n, "no index may be chosen twice"
            assert picked == sorted(picked)
            assert picked[0] == 0 and picked[-1] == len(CLUSTERED) - 1

    def test_the_measured_clip_is_covered_end_to_end(self):
        """The shape that was actually measured: 15.3s, 50 cuts, 10 of them by 1.7s.

        Before this change the kept frames were the earliest 8, all inside the
        first 1.7 seconds, leaving 13.6s -- 89% of the clip -- never looked at.
        """
        n = 8
        span = CLUSTERED[-1] - CLUSTERED[0]
        bound = 1.6 * span / (n - 1)

        chosen = [CLUSTERED[i] for i in select_spread(CLUSTERED, n)]
        assert _max_gap(chosen) <= bound

        earliest = CLUSTERED[:n]  # what the code did before
        assert _max_gap(earliest + [CLUSTERED[-1]]) > bound

    def test_spacing_by_ordinal_breaks_where_spacing_by_time_does_not(self):
        """Why the index-space one-liner was not good enough.

        Worth being exact about the evidence: on the measured clip above the
        ordinal version would also have passed -- 20% of the cuts in the cluster
        is not enough to break it. It breaks when the clustering is heavier, and
        heavier clustering is ordinary in this material (a rapid-fire montage
        opening over a long static tail). This distribution is synthetic, and it
        is here because the bound has to hold for the bad case, not the mild one.
        """
        heavy = ([round(0.05 * i, 3) for i in range(40)]          # 40 cuts in 2s
                 + [round(2.0 + 1.36 * i, 3) for i in range(1, 11)])  # 10 over 13.6s
        n = 8
        span = heavy[-1] - heavy[0]
        bound = 1.6 * span / (n - 1)

        by_time = [heavy[i] for i in select_spread(heavy, n)]
        by_ordinal = [heavy[round(i * (len(heavy) - 1) / (n - 1))] for i in range(n)]

        assert _max_gap(by_time) <= bound
        assert _max_gap(by_ordinal) > bound
        assert _max_gap(by_time) < _max_gap(by_ordinal)

    def test_asking_for_more_than_there_is_returns_everything(self):
        assert select_spread([1.0, 2.0, 3.0], 10) == [0, 1, 2]


class TestSceneDetectionPass:
    @patch("reel_scout.vision.keyframe._get_duration", return_value=15.3)
    @patch("reel_scout.vision.keyframe.os.path.exists", return_value=True)
    @patch("reel_scout.vision.keyframe.os.makedirs")
    @patch("reel_scout.vision.keyframe.subprocess.run")
    def test_detection_does_not_stop_at_the_budget(
        self, mock_run, mock_makedirs, mock_exists, mock_duration,
    ):
        """The actual bug, pinned at the argv.

        A test of the selector alone passes against the broken code, because the
        selector is fed a mocked list. What was wrong is that the real list only
        ever had `max_frames` entries in it.
        """
        res = MagicMock()
        res.stderr = "\n".join("n:%d pts_time:%.2f" % (i, t)
                                for i, t in enumerate(CLUSTERED))
        res.stdout = ""
        mock_run.return_value = res

        extract_keyframes("/tmp/v.mp4", "/tmp/out", "vid1",
                          strategy="scene", max_frames=8)

        detect = mock_run.call_args_list[0][0][0]
        assert "-frames:v" not in detect, (
            "capping the detection pass is what made ffmpeg stop looking")
        assert detect[detect.index("-f") + 1] == "null"

    @patch("reel_scout.vision.keyframe._get_duration", return_value=15.3)
    @patch("reel_scout.vision.keyframe.os.path.exists", return_value=True)
    @patch("reel_scout.vision.keyframe.os.makedirs")
    @patch("reel_scout.vision.keyframe.subprocess.run")
    def test_the_frames_it_grabs_cover_the_clip_not_its_opening(
        self, mock_run, mock_makedirs, mock_exists, mock_duration,
    ):
        res = MagicMock()
        res.stderr = "\n".join("n:%d pts_time:%.2f" % (i, t)
                                for i, t in enumerate(CLUSTERED))
        res.stdout = ""
        mock_run.return_value = res

        frames = extract_keyframes("/tmp/v.mp4", "/tmp/out", "vid1",
                                   strategy="scene", max_frames=8)

        stamps = sorted(f.timestamp_sec for f in frames)
        assert stamps[-1] > 13.0, (
            "before this change the last frame grabbed sat at 1.7s on this clip")
        assert _max_gap(stamps) < 4.0


class TestSelectSpreadDegenerate:
    def test_identical_timestamps_still_return_both_ends(self):
        """Why forcing the last index is not belt-and-braces.

        With a real spread the walk lands on the final index by itself -- the
        last target *is* the last timestamp, so the distance is zero. The forcing
        only bites when the span collapses: every target is then the same value,
        the argmin takes the earliest candidate each time, and the walk returns
        the first n indices with the end nowhere in it.

        The callers rely on the promise, not on the usual case:
        ``_ensure_first_last`` and the budget thinning both assume the frame at
        the end of the clip is one of the ones that survives.
        """
        picked = select_spread([5.0] * 10, 3)
        assert picked[0] == 0
        assert picked[-1] == 9, "the end must survive even when time stands still"
        assert len(set(picked)) == 3


class TestBudgetIsThinnedNotTruncated:
    @patch("reel_scout.vision.keyframe._get_duration", return_value=60.0)
    @patch("reel_scout.vision.keyframe.os.path.exists", return_value=True)
    @patch("reel_scout.vision.keyframe.os.makedirs")
    @patch("reel_scout.vision.keyframe.subprocess.run")
    def test_first_and_last_do_not_push_the_tail_off_the_end(
        self, mock_run, mock_makedirs, mock_exists, mock_duration,
    ):
        """The truncation bug, in the one place it could still live.

        `_ensure_first_last` can hand back two more frames than the budget: one
        at each end, added because neither zone was covered. Going back to budget
        with a slice would drop the frame it had just appended at the clip's end
        -- the same failure as before, moved one function along.
        """
        # Cuts that leave both edge zones empty: nothing before 0.5s, nothing
        # after 59s, and exactly the budget in between.
        stamps = [10.0, 20.0, 30.0, 40.0]
        res = MagicMock()
        res.stderr = "\n".join("n:%d pts_time:%.2f" % (i, t)
                                for i, t in enumerate(stamps))
        res.stdout = ""
        mock_run.return_value = res

        frames = extract_keyframes("/tmp/v.mp4", "/tmp/out", "vid1",
                                   strategy="scene", max_frames=4)

        kept = sorted(f.timestamp_sec for f in frames)
        assert len(frames) == 4, "the budget still has to hold"
        assert kept[0] < 0.5, "the frame at the clip's start must survive"
        assert kept[-1] > 59.0, "the frame at the clip's end must survive"
        assert [f.frame_index for f in sorted(
            frames, key=lambda f: f.timestamp_sec)] == [0, 1, 2, 3]


class TestMotionDetectionPass:
    """`motion` had the defect `scene` was fixed for in #68, plus one of its own.

    Both are pinned at the argv, because a test of the selector alone passes
    against the broken code: the selector was always fine, it was just handed a
    list that only ever described the opening of the clip.
    """

    @patch("reel_scout.vision.keyframe._get_duration", return_value=15.3)
    @patch("reel_scout.vision.keyframe.os.path.exists", return_value=True)
    @patch("reel_scout.vision.keyframe.os.makedirs")
    @patch("reel_scout.vision.keyframe.subprocess.run")
    def test_detection_does_not_stop_at_the_budget(
        self, mock_run, mock_makedirs, mock_exists, mock_duration,
    ):
        res = MagicMock()
        res.stderr = "\n".join("n:%d pts_time:%.2f" % (i, t)
                               for i, t in enumerate(CLUSTERED))
        res.stdout = ""
        mock_run.return_value = res

        extract_keyframes("/tmp/v.mp4", "/tmp/out", "vid1",
                          strategy="motion", max_frames=8)

        detect = mock_run.call_args_list[0][0][0]
        assert "-frames:v" not in detect, (
            "mpdecimate emits across the whole file — measured, 405 of a 13.7s "
            "reel's ~411 frames survive it — so capping the pass made ffmpeg "
            "quit inside the first second every time")
        assert detect[detect.index("-f") + 1] == "null", (
            "unbounded detection must not also write unbounded JPEGs")

    @patch("reel_scout.vision.keyframe._get_duration", return_value=15.3)
    @patch("reel_scout.vision.keyframe.os.path.exists", return_value=True)
    @patch("reel_scout.vision.keyframe.os.makedirs")
    @patch("reel_scout.vision.keyframe.subprocess.run")
    def test_showinfo_reads_source_time_not_a_rebased_ordinal(
        self, mock_run, mock_makedirs, mock_exists, mock_duration,
    ):
        """The second defect, and the reason it hid for so long.

        `setpts=N/FRAME_RATE/TB` sat *before* `showinfo`, so the pts_time being
        parsed was the surviving frame's ordinal over the frame rate. Measured
        on a 10s clip built as 5s frozen + 5s moving: the old chain reported
        0.00–0.27s for frames that actually came from 0.00s and 5.00–5.20s. The
        images were real; the timestamps written next to them were not.
        """
        res = MagicMock()
        res.stderr = "\n".join("n:%d pts_time:%.2f" % (i, t)
                               for i, t in enumerate(CLUSTERED))
        res.stdout = ""
        mock_run.return_value = res

        extract_keyframes("/tmp/v.mp4", "/tmp/out", "vid1",
                          strategy="motion", max_frames=8)

        detect = mock_run.call_args_list[0][0][0]
        vf = detect[detect.index("-vf") + 1]
        assert "mpdecimate" in vf
        assert "setpts" not in vf, (
            "showinfo runs after the filter chain, so any setpts ahead of it "
            "replaces the timestamp this function exists to report")

    @patch("reel_scout.vision.keyframe._get_duration", return_value=15.3)
    @patch("reel_scout.vision.keyframe.os.path.exists", return_value=True)
    @patch("reel_scout.vision.keyframe.os.makedirs")
    @patch("reel_scout.vision.keyframe.subprocess.run")
    def test_the_frames_it_grabs_cover_the_clip_not_its_opening(
        self, mock_run, mock_makedirs, mock_exists, mock_duration,
    ):
        res = MagicMock()
        res.stderr = "\n".join("n:%d pts_time:%.2f" % (i, t)
                               for i, t in enumerate(CLUSTERED))
        res.stdout = ""
        mock_run.return_value = res

        frames = extract_keyframes("/tmp/v.mp4", "/tmp/out", "vid1",
                                   strategy="motion", max_frames=8)

        stamps = sorted(f.timestamp_sec for f in frames)
        assert stamps[-1] - stamps[0] > 15.3 * 0.5, (
            "the sampled span has to cover more than half the clip; before "
            "this change it was the first eight survivors")
        assert stamps[-1] > 13.0
        assert _max_gap(stamps) < 4.0

    @patch("reel_scout.vision.keyframe._get_duration", return_value=15.3)
    @patch("reel_scout.vision.keyframe.os.path.exists", return_value=True)
    @patch("reel_scout.vision.keyframe.os.makedirs")
    @patch("reel_scout.vision.keyframe.subprocess.run")
    def test_the_detection_pass_reads_ffmpeg_as_utf8(
        self, mock_run, mock_makedirs, mock_exists, mock_duration,
    ):
        """Companion to the behavioural test below; this one names the kwarg.
        `text=True` on its own decodes with the console codepage."""
        res = MagicMock()
        res.stderr = "n:0 pts_time:0.00"
        res.stdout = ""
        mock_run.return_value = res

        extract_keyframes("/tmp/v.mp4", "/tmp/out", "vid1",
                          strategy="motion", max_frames=8)

        assert mock_run.call_args_list[0][1].get("encoding") == "utf-8"


def test_motion_detect_timeout_falls_back_to_interval_instead_of_zero_frames(
        tmp_path, monkeypatch):
    """Removing the early exit is what made this reachable.

    While `-frames:v` was there the pass exited within the first second and
    could not overrun. Unbounded, it can — and an overrun that ended in zero
    frames would cost the whole visual layer, which is the one outcome worth
    engineering away.
    """
    from reel_scout.vision import keyframe

    def _timeout_on_motion_detect(cmd, **kw):
        if any("mpdecimate" in str(a) for a in cmd):
            raise subprocess.TimeoutExpired(cmd, 300)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(keyframe.subprocess, "run", _timeout_on_motion_detect)
    monkeypatch.setattr(keyframe, "_get_duration", lambda p: 15035.0)
    monkeypatch.setattr(
        keyframe, "_extract_interval",
        lambda video_path, output_dir, video_id, n, *a, **kw: [
            KeyframeInfo(frame_index=i, timestamp_sec=float(i * 100),
                         file_path="%s/f%d.jpg" % (output_dir, i),
                         strategy="interval")
            for i in range(n)])
    monkeypatch.setattr(keyframe, "_ensure_first_last", lambda *a, **kw: a[3])

    frames = keyframe.extract_keyframes(
        str(tmp_path / "x.mp4"), str(tmp_path), "vid",
        strategy="motion", max_frames=5)

    assert len(frames) == 5, "a motion-detect timeout must not end with zero frames"
    assert all(f.strategy == "interval" for f in frames)


def test_partial_motion_result_is_not_padded_with_interval(tmp_path, monkeypatch):
    """Falling back is for nothing at all, not for "fewer than budget".

    `len(frames) < max_frames` would quietly turn `motion` into `hybrid` for
    every clip with few moving frames, diluting them with arbitrary ones and
    buying an extra VLM call for each. `hybrid` already exists for that.
    """
    from reel_scout.vision import keyframe

    motion_frames = [
        KeyframeInfo(frame_index=0, timestamp_sec=1.0,
                     file_path=str(tmp_path / "m0.jpg"), strategy="motion"),
        KeyframeInfo(frame_index=1, timestamp_sec=9.0,
                     file_path=str(tmp_path / "m1.jpg"), strategy="motion"),
    ]
    monkeypatch.setattr(keyframe, "_extract_motion", lambda *a, **kw: motion_frames)
    monkeypatch.setattr(keyframe, "_get_duration", lambda p: 30.0)
    monkeypatch.setattr(keyframe, "_ensure_first_last", lambda *a, **kw: a[3])

    def _no_interval(*a, **kw):
        raise AssertionError("a partial motion result must not be topped up")

    monkeypatch.setattr(keyframe, "_extract_interval", _no_interval)

    frames = keyframe.extract_keyframes(
        str(tmp_path / "x.mp4"), str(tmp_path), "vid",
        strategy="motion", max_frames=8)

    assert [f.strategy for f in frames] == ["motion", "motion"]


# A stand-in for both ffmpeg and ffprobe. The filename deliberately contains no
# "ffmpeg", because `_get_duration` builds the ffprobe path by string-replacing
# it — so one script answers both calls.
FAKE_FFMPEG = '''#!/usr/bin/env python3
import sys
args = sys.argv[1:]
if "-show_entries" in args:
    sys.stdout.write("24.0\\n")
elif "null" in args:
    out = "Input #0, mov,mp4, from '/tmp/\\u5f71\\u7247\\u6a19\\u984c.mp4':\\n".encode("utf-8")
    out += b"  Metadata: comment=\\xff\\xfe\\n"
    for i, t in enumerate([0.0, 6.0, 12.0, 18.0]):
        out += ("[Parsed_showinfo] n:%d pts_time:%.2f\\n" % (i, t)).encode("utf-8")
    sys.stderr.buffer.write(out)
    sys.stderr.buffer.flush()
else:
    open(args[-1], "wb").write(b"\\xff\\xd8\\xff")
'''


def test_motion_survives_stderr_the_console_locale_cannot_read(tmp_path):
    # The test's own name must not contain the string this module rewrites
    # to find ffprobe: pytest builds tmp_path out of it, and `_get_duration`
    # would rewrite the fake binary's own directory out from under it.
    """The kwarg assertion above cannot see this, and neither can an exit code.

    ffmpeg echoes the input path and its metadata into the same stderr this
    function parses. A CJK filename is therefore ordinary, and `text=True` on
    its own decodes it with the console codepage — ASCII under this locale, so
    the whole extraction dies on a UnicodeDecodeError that names none of the
    above. The stray `\\xff\\xfe` is there for the other half: ffmpeg copies
    metadata through without promising it is UTF-8, so the decode has to be
    lenient as well as explicit.
    """
    fake = tmp_path / "fake_bin.py"
    fake.write_text(FAKE_FFMPEG, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    import reel_scout
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(
        reel_scout.__file__)))

    env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONUTF8="0",
               PYTHONCOERCECLOCALE="0", PYTHONPATH=repo_root)
    env.pop("PYTHONIOENCODING", None)              # no rescue from outside

    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys\n"
         "from reel_scout import config\n"
         "from reel_scout.vision import keyframe\n"
         "config.FFMPEG_BIN = %r\n"
         "frames = keyframe._extract_motion(%r, %r, 'vid', 4)\n"
         "sys.stdout.write(','.join('%%.2f' %% f.timestamp_sec for f in frames))\n"
         % (str(fake), str(tmp_path / "clip.mp4"), str(tmp_path))],
        capture_output=True, env=env)

    out = probe.stdout.decode("utf-8", "replace")
    err = probe.stderr.decode("utf-8", "replace")
    assert probe.returncode == 0, out + err
    assert "UnicodeDecodeError" not in err, err
    assert out.strip() == "0.00,6.00,12.00,18.00", (
        "the timestamps have to come back intact, not merely not-crash: %r" % out)
