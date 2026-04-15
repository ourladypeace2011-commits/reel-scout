from __future__ import annotations

from typing import Optional

from .base import BaseAudioAnalyzer
from .. import config


def get_audio_analyzer(backend: Optional[str] = None) -> BaseAudioAnalyzer:
    backend = backend or "panns"
    if backend == "panns":
        from .panns import PannsAnalyzer

        return PannsAnalyzer(
            model_path=config.PANNS_MODEL_PATH,
            window_sec=config.AUDIO_WINDOW_SEC,
            hop_sec=config.AUDIO_HOP_SEC,
        )
    raise ValueError("Unknown audio backend: %s" % backend)
