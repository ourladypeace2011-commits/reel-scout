from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class Segment:
    start: float
    end: float
    text: str
    confidence: float = 0.0


@dataclass
class TranscriptResult:
    language: str = ""
    text_full: str = ""
    segments: List[Segment] = field(default_factory=list)
    duration_sec: float = 0.0
    model: str = ""


class BaseTranscriber(abc.ABC):
    @abc.abstractmethod
    def transcribe(self, audio_path: str) -> TranscriptResult:
        ...
