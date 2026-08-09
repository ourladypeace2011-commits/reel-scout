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
            min_conf=config.AUDIO_MIN_CONF,
            event_min_conf=config.AUDIO_EVENT_MIN_CONF,
            top_k=config.AUDIO_TOP_K,
        )
    raise ValueError("Unknown audio backend: %s" % backend)
