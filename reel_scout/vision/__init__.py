from __future__ import annotations

from typing import Optional

from .base import BaseVLM
from .. import config


def get_vlm(backend: Optional[str] = None) -> BaseVLM:
    backend = backend or config.VLM_BACKEND
    if backend == "omlx":
        from .omlx import OmlxVLM
        return OmlxVLM(
            base_url=config.OMLX_BASE_URL,
            model=config.VLM_MODEL,
        )
    elif backend == "ollama":
        from .ollama import OllamaVLM
        return OllamaVLM(
            base_url=config.OLLAMA_BASE_URL,
            model=config.VLM_MODEL,
        )
    else:
        raise ValueError(f"Unknown VLM backend: {backend}")
