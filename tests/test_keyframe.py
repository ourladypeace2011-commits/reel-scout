from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from reel_scout.vision.keyframe import (
    KeyframeInfo,
    _ensure_first_last,
    _scale_vf,
    _seek_args,
    auto_frame_budget,
    scene_timeout,
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
    def test_ensure_first_last_trims_middle(
        self, mock_exists: MagicMock, mock_run: MagicMock,
    ) -> None:
        frames = [
            KeyframeInfo(0, 3.0, "/tmp/f0.jpg", "scene", 0.2),
            KeyframeInfo(1, 5.0, "/tmp/f1.jpg", "scene", 0.9),
            KeyframeInfo(2, 7.0, "/tmp/f2.jpg", "scene", 0.1),
        ]
        # max_frames=4: after adding first+last we get 5, trim to 4
        result = _ensure_first_last(
            "/tmp/video.mp4", "/tmp/out", "vid1", frames, 4, 20.0,
        )
        assert len(result) == 4
        # The frame with lowest score (0.1 at 7.0) should be removed
        timestamps = [f.timestamp_sec for f in result]
        assert 7.0 not in timestamps


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

    @patch("reel_scout.vision.keyframe._get_duration", return_value=900.0)
    @patch("reel_scout.vision.keyframe._extract_scene", return_value=[])
    @patch("reel_scout.vision.keyframe._ensure_first_last", side_effect=lambda *a, **k: a[3])
    @patch("reel_scout.vision.keyframe.os.makedirs")
    def test_auto_budget_clamped_to_frame_cap(
        self,
        mock_makedirs: MagicMock,
        mock_ensure: MagicMock,
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

        extract_keyframes(
            "/tmp/v.mp4", "/tmp/out", "vid1",
            strategy="scene", max_frames=4, resolution=1024,
        )
        cmd = mock_run.call_args_list[0][0][0]
        vf = cmd[cmd.index("-vf") + 1]
        assert "scale=1024:-2" in vf
        assert "showinfo" in vf

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


def test_scene_timeout_scales_with_duration_and_stays_capped():
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
    assert scene_timeout(8) == 120           # a reel keeps the old floor
    assert scene_timeout(1200) == 240        # 20 min earns more
    assert scene_timeout(15035) == 300       # 4 h is capped, not proportional
    assert scene_timeout(0) == 120           # unknown duration degrades to the floor


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
