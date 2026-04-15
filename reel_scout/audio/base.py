from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import List


@dataclass
class AudioEvent:
    event_type: str  # "music", "speech", "applause", "silence", "sound_effect"
    label: str  # PANNs class label (e.g., "Acoustic guitar", "Crowd")
    start_sec: float
    end_sec: float
    confidence: float = 0.0


@dataclass
class AudioTimeline:
    events: List[AudioEvent] = field(default_factory=list)
    has_music: bool = False
    music_ratio: float = 0.0
    silence_ratio: float = 0.0
    dominant_audio_type: str = ""
    duration_sec: float = 0.0


class BaseAudioAnalyzer(abc.ABC):
    @abc.abstractmethod
    def analyze(self, audio_path: str) -> AudioTimeline:
        ...
