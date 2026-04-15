from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from reel_scout.diarize.base import (
    BaseDiarizer,
    DiarizationResult,
    SpeakerSegment,
)
from reel_scout.diarize.align import align_speakers_to_transcript
from reel_scout.diarize.pyannote import PyannoteDiarizer


# --- Dataclass tests ---


def test_speaker_segment_dataclass():
    seg = SpeakerSegment(speaker="SPEAKER_00", start_sec=1.5, end_sec=3.2)
    assert seg.speaker == "SPEAKER_00"
    assert seg.start_sec == 1.5
    assert seg.end_sec == 3.2


def test_diarization_result_dataclass():
    segs = [
        SpeakerSegment("SPEAKER_00", 0.0, 5.0),
        SpeakerSegment("SPEAKER_01", 5.0, 10.0),
    ]
    result = DiarizationResult(segments=segs, num_speakers=2)
    assert result.num_speakers == 2
    assert len(result.segments) == 2
    # Default values
    empty = DiarizationResult()
    assert empty.segments == []
    assert empty.num_speakers == 0


# --- Alignment tests ---


def test_align_single_speaker():
    speaker_segs = [SpeakerSegment("SPEAKER_00", 0.0, 20.0)]
    transcript = json.dumps([
        {"start": 0.0, "end": 3.0, "text": "Hello"},
        {"start": 3.0, "end": 6.0, "text": "world"},
        {"start": 6.0, "end": 9.0, "text": "foo"},
    ])
    result = json.loads(align_speakers_to_transcript(speaker_segs, transcript))
    for seg in result:
        assert seg["speaker"] == "SPEAKER_00"


def test_align_two_speakers():
    """Speaker A at 0-5s, Speaker B at 5-10s.
    Transcript segment 3-7s overlaps A by 2s and B by 2s.
    But A: overlap = 5-3 = 2s, B: overlap = 7-5 = 2s — tied.
    We pick first found (A), since loop checks > not >=.
    Actually let's make it clearer: segment 2-6s => A overlap=3s, B overlap=1s => A wins.
    """
    speaker_segs = [
        SpeakerSegment("SPEAKER_00", 0.0, 5.0),
        SpeakerSegment("SPEAKER_01", 5.0, 10.0),
    ]
    transcript = json.dumps([
        {"start": 2.0, "end": 6.0, "text": "mostly in A"},
    ])
    result = json.loads(align_speakers_to_transcript(speaker_segs, transcript))
    assert result[0]["speaker"] == "SPEAKER_00"


def test_align_no_overlap():
    speaker_segs = [SpeakerSegment("SPEAKER_00", 20.0, 30.0)]
    transcript = json.dumps([
        {"start": 0.0, "end": 5.0, "text": "no overlap"},
    ])
    result = json.loads(align_speakers_to_transcript(speaker_segs, transcript))
    assert result[0]["speaker"] == ""


def test_align_empty_segments():
    transcript = json.dumps([
        {"start": 0.0, "end": 3.0, "text": "Hello"},
        {"start": 3.0, "end": 6.0, "text": "world"},
    ])
    result = json.loads(align_speakers_to_transcript([], transcript))
    for seg in result:
        assert seg["speaker"] == ""


# --- PyannoteDiarizer tests ---


def test_pyannote_no_token_raises():
    """PyannoteDiarizer with empty token raises ValueError on diarize()."""
    diarizer = PyannoteDiarizer(auth_token="")
    # Mock the import so we don't need pyannote installed
    mock_pipeline_cls = MagicMock()
    with patch.dict("sys.modules", {"pyannote": MagicMock(), "pyannote.audio": MagicMock(Pipeline=mock_pipeline_cls)}):
        with pytest.raises(ValueError, match="PYANNOTE_AUTH_TOKEN"):
            diarizer.diarize("fake.wav")


def test_get_diarizer_unknown():
    from reel_scout.diarize import get_diarizer

    with pytest.raises(ValueError, match="Unknown diarization backend"):
        get_diarizer("nonexistent")
