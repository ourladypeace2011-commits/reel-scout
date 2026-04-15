from __future__ import annotations

from typing import Optional

from .base import BaseDiarizer, DiarizationResult, SpeakerSegment


class PyannoteDiarizer(BaseDiarizer):
    def __init__(self, auth_token: str = "") -> None:
        self._auth_token = auth_token
        self._pipeline = None  # type: Optional[object]

    def _ensure_pipeline(self) -> None:
        if self._pipeline is not None:
            return
        try:
            from pyannote.audio import Pipeline  # type: ignore[import-untyped]
        except ImportError:
            raise ImportError(
                "pyannote.audio not installed. Install with: pip install pyannote.audio"
            )
        if not self._auth_token:
            raise ValueError(
                "PYANNOTE_AUTH_TOKEN not set. Get a token from "
                "https://huggingface.co/pyannote/speaker-diarization-3.1"
            )
        self._pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=self._auth_token,
        )

    def diarize(self, audio_path: str) -> DiarizationResult:
        self._ensure_pipeline()
        diarization = self._pipeline(audio_path)

        segments = []
        speakers = set()
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            segments.append(SpeakerSegment(
                speaker=speaker,
                start_sec=round(turn.start, 2),
                end_sec=round(turn.end, 2),
            ))
            speakers.add(speaker)

        return DiarizationResult(
            segments=segments,
            num_speakers=len(speakers),
        )
