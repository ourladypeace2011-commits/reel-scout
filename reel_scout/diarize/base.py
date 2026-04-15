from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import List


@dataclass
class SpeakerSegment:
    speaker: str         # "SPEAKER_00", "SPEAKER_01", etc.
    start_sec: float
    end_sec: float


@dataclass
class DiarizationResult:
    segments: List[SpeakerSegment] = field(default_factory=list)
    num_speakers: int = 0


class BaseDiarizer(abc.ABC):
    @abc.abstractmethod
    def diarize(self, audio_path: str) -> DiarizationResult:
        ...
