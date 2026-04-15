from __future__ import annotations

from typing import Optional

from .base import BaseTranscriber, TranscriptResult
from .. import config


def get_transcriber(backend: Optional[str] = None) -> BaseTranscriber:
    backend = backend or config.WHISPER_BACKEND
    if backend == "faster-whisper":
        from .faster_whisper import FasterWhisperTranscriber
        return FasterWhisperTranscriber(model=config.WHISPER_MODEL)
    elif backend == "whisper-cpp":
        from .whisper_cpp import WhisperCppTranscriber
        return WhisperCppTranscriber(model=config.WHISPER_MODEL)
    else:
        raise ValueError(f"Unknown whisper backend: {backend}")
