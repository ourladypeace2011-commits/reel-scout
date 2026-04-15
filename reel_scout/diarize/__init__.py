from __future__ import annotations

from typing import Optional

from .base import BaseDiarizer
from .. import config


def get_diarizer(backend: Optional[str] = None) -> BaseDiarizer:
    backend = backend or "pyannote"
    if backend == "pyannote":
        from .pyannote import PyannoteDiarizer

        return PyannoteDiarizer(auth_token=config.PYANNOTE_AUTH_TOKEN)
    raise ValueError("Unknown diarization backend: %s" % backend)
