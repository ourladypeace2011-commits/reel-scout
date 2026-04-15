from __future__ import annotations

import io
import struct
import tempfile
import wave
from unittest.mock import MagicMock, patch

import pytest

from reel_scout.audio.base import AudioEvent, AudioTimeline
from reel_scout.audio.panns import (
    PannsAnalyzer,
    _build_timeline,
    _classify_label,
    _merge_adjacent,
    _read_wav_samples,
)


class TestExtractWav:
    def test_extract_wav_command(self) -> None:
        """Mock subprocess.run and verify ffmpeg command format."""
        with patch("reel_scout.audio.extract.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with patch("reel_scout.audio.extract.os.path.exists", return_value=True):
                from reel_scout.audio.extract import extract_wav

                result = extract_wav("/tmp/video.mp4", "/tmp/audio.wav", 16000)

            assert result == "/tmp/audio.wav"
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "ffmpeg"  # default FFMPEG_BIN
            assert "-i" in cmd
            assert "/tmp/video.mp4" in cmd
            assert "-ar" in cmd
            assert "16000" in cmd
            assert "-ac" in cmd
            assert "1" in cmd
            assert "-y" in cmd
            assert "/tmp/audio.wav" in cmd


class TestReadWavSamples:
    def test_read_wav_samples(self) -> None:
        """Create a small WAV in memory and verify float output."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name

        # Write a small mono 16-bit WAV: 100 samples at 16000 Hz
        sample_rate = 16000
        n_samples = 100
        # Half-amplitude sine-ish: just use a fixed value for simplicity
        raw_samples = [16384] * n_samples  # half of 32768

        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            packed = struct.pack("<%dh" % n_samples, *raw_samples)
            wf.writeframes(packed)

        samples, sr = _read_wav_samples(tmp_path)
        assert sr == sample_rate
        assert len(samples) == n_samples
        # 16384 / 32768 = 0.5
        assert abs(samples[0] - 0.5) < 0.001

        import os

        os.unlink(tmp_path)


class TestMergeAdjacent:
    def test_merge_adjacent_same_type(self) -> None:
        """Two adjacent music events should be merged."""
        events = [
            AudioEvent("music", "Music", 0.0, 2.0, 0.8),
            AudioEvent("music", "Music", 2.0, 4.0, 0.9),
        ]
        merged = _merge_adjacent(events)
        assert len(merged) == 1
        assert merged[0].start_sec == 0.0
        assert merged[0].end_sec == 4.0
        assert merged[0].confidence == 0.9

    def test_merge_adjacent_different_type(self) -> None:
        """Music then speech should not be merged."""
        events = [
            AudioEvent("music", "Music", 0.0, 2.0, 0.8),
            AudioEvent("speech", "Speech", 2.0, 4.0, 0.7),
        ]
        merged = _merge_adjacent(events)
        assert len(merged) == 2

    def test_merge_empty(self) -> None:
        """Empty list returns empty."""
        assert _merge_adjacent([]) == []


class TestBuildTimeline:
    def test_build_timeline_stats(self) -> None:
        """Verify music_ratio, silence_ratio, dominant_type."""
        events = [
            AudioEvent("music", "Music", 0.0, 6.0, 0.9),
            AudioEvent("silence", "Silence", 6.0, 8.0, 0.5),
            AudioEvent("speech", "Speech", 8.0, 10.0, 0.7),
        ]
        tl = _build_timeline(events, 10.0)
        assert tl.has_music is True
        assert tl.music_ratio == 0.6
        assert tl.silence_ratio == 0.2
        assert tl.dominant_audio_type == "music"
        assert tl.duration_sec == 10.0

    def test_build_timeline_zero_duration(self) -> None:
        """Zero duration returns empty timeline."""
        tl = _build_timeline([], 0.0)
        assert tl.events == []
        assert tl.dominant_audio_type == ""


class TestClassifyLabel:
    def test_classify_label_music(self) -> None:
        assert _classify_label("Music") == "music"
        assert _classify_label("Guitar") == "music"

    def test_classify_label_speech(self) -> None:
        assert _classify_label("Speech") == "speech"

    def test_classify_label_silence(self) -> None:
        assert _classify_label("Silence") == "silence"

    def test_classify_label_applause(self) -> None:
        assert _classify_label("Applause") == "applause"

    def test_classify_label_unknown(self) -> None:
        assert _classify_label("Random") == "sound_effect"
        assert _classify_label("Dog bark") == "sound_effect"


class TestAudioEventDataclass:
    def test_audio_event_dataclass(self) -> None:
        """Verify AudioEvent fields."""
        ev = AudioEvent(
            event_type="music",
            label="Guitar",
            start_sec=1.0,
            end_sec=3.0,
            confidence=0.85,
        )
        assert ev.event_type == "music"
        assert ev.label == "Guitar"
        assert ev.start_sec == 1.0
        assert ev.end_sec == 3.0
        assert ev.confidence == 0.85

    def test_audio_event_default_confidence(self) -> None:
        ev = AudioEvent("speech", "Speech", 0.0, 1.0)
        assert ev.confidence == 0.0


class TestPannsNoModel:
    def test_panns_no_model_raises(self) -> None:
        """PannsAnalyzer with empty path raises FileNotFoundError."""
        analyzer = PannsAnalyzer(model_path="", window_sec=2.0, hop_sec=1.0)
        with pytest.raises(FileNotFoundError):
            analyzer._ensure_model()

    def test_panns_missing_model_file_raises(self) -> None:
        """PannsAnalyzer with non-existent path raises FileNotFoundError."""
        analyzer = PannsAnalyzer(
            model_path="/tmp/nonexistent_model.onnx",
            window_sec=2.0,
            hop_sec=1.0,
        )
        with pytest.raises(FileNotFoundError):
            analyzer._ensure_model()
