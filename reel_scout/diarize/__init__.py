"""reel-scout diarization — thin adapter over the speaker-align package (R-mig).

The diarization types, the pyannote backend, and the transcript-alignment logic
were extracted into the standalone ``speaker-align`` package (its own repo + test
suite: https://github.com/vulture-s/speaker-align). This module now only injects
reel-scout's configured pyannote token and re-exports speaker-align's surface, so
existing ``from ..diarize import ...`` callers keep working unchanged.

Install (M2-local): ``pip install -e speaker-align[pyannote]`` — see the
``diarize`` extra in pyproject.toml. When speaker-align isn't installed, importing
this module raises ImportError; the only runtime caller (analyze/pipeline.py)
guards that import and simply skips diarization.
"""
from __future__ import annotations

from typing import Optional

from speaker_align import (
    BaseDiarizer,
    DiarizationResult,
    SpeakerSegment,
    UNKNOWN_SPEAKER,
    align_segments_json,
    align_speakers_to_transcript,
    speaker_turn_count,
)
from speaker_align import get_diarizer as _get_diarizer

from .. import config

__all__ = [
    "get_diarizer",
    "BaseDiarizer",
    "DiarizationResult",
    "SpeakerSegment",
    "UNKNOWN_SPEAKER",
    "align_segments_json",
    "align_speakers_to_transcript",
    "speaker_turn_count",
]


def get_diarizer(backend: Optional[str] = None) -> BaseDiarizer:
    """Return a diarizer, injecting reel-scout's configured pyannote token.

    Thin wrapper over ``speaker_align.get_diarizer`` that sources the token from
    reel-scout's config (``config.PYANNOTE_AUTH_TOKEN``) rather than ambient env,
    keeping the app's configuration in one place. Behaviour-preserving: an unknown
    backend still raises ``ValueError("Unknown diarization backend: ...")``.
    """
    return _get_diarizer(backend=backend, auth_token=config.PYANNOTE_AUTH_TOKEN)
