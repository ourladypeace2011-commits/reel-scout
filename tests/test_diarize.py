"""Tests for reel-scout's diarize ADAPTER over the speaker-align package (R-mig).

The alignment logic, the dataclasses, and the pyannote backend now live in the
standalone `speaker-align` package, which owns their unit tests (22 of them). So
these tests only cover reel-scout's thin adapter: that it re-exports the surface
and that `get_diarizer()` injects reel-scout's configured token.

speaker-align is an optional (M2-local, not-yet-on-PyPI) dependency, so the whole
module self-skips when it isn't installed — same pattern as the chromadb e2e test.
"""
from __future__ import annotations

import pytest

pytest.importorskip("speaker_align")

import json  # noqa: E402

from reel_scout import config  # noqa: E402
from reel_scout import diarize  # noqa: E402
from reel_scout.diarize import (  # noqa: E402
    BaseDiarizer,
    DiarizationResult,
    SpeakerSegment,
    align_segments_json,
    align_speakers_to_transcript,
    get_diarizer,
)


# --- Re-export surface: existing `from ..diarize import X` callers keep working ---

def test_reexports_are_speaker_align_types():
    import speaker_align
    assert BaseDiarizer is speaker_align.BaseDiarizer
    assert DiarizationResult is speaker_align.DiarizationResult
    assert SpeakerSegment is speaker_align.SpeakerSegment
    assert align_speakers_to_transcript is speaker_align.align_speakers_to_transcript
    assert align_segments_json is speaker_align.align_segments_json


def test_alignment_reexport_smoke():
    # Depth lives in speaker-align's suite; here we only confirm the re-exported
    # functions are callable through reel_scout.diarize and behave. Note the
    # split: the primitive takes list[dict], the *_json wrapper takes a string.
    segs = [SpeakerSegment("SPEAKER_00", 0.0, 20.0)]
    out = align_speakers_to_transcript(segs, [{"start": 0.0, "end": 3.0, "text": "Hi"}])
    assert out[0]["speaker"] == "SPEAKER_00"
    # align_segments_json is what analyze/pipeline.py uses (DB stores a JSON string).
    out_json = align_segments_json(segs, json.dumps([{"start": 0.0, "end": 3.0, "text": "Hi"}]))
    assert json.loads(out_json)[0]["speaker"] == "SPEAKER_00"


# --- Adapter behaviour: inject reel-scout's configured token ---

def test_get_diarizer_injects_config_token(monkeypatch):
    monkeypatch.setattr(config, "PYANNOTE_AUTH_TOKEN", "tok-123")
    # Building a pyannote diarizer does not import pyannote (that is lazy), so
    # this runs without the heavy dependency installed.
    d = get_diarizer()
    assert d._auth_token == "tok-123"


def test_get_diarizer_unknown_backend_raises():
    with pytest.raises(ValueError, match="Unknown diarization backend"):
        get_diarizer("nonexistent")
