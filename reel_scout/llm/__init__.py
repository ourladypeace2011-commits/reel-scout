from __future__ import annotations

from typing import Optional

from .. import config
from .base import BaseLLM


def get_llm(backend: Optional[str] = None) -> BaseLLM:
    backend_name = backend or config.LLM_BACKEND
    if backend_name == "omlx":
        from .omlx import OmlxLLM

        return OmlxLLM(base_url=config.OMLX_BASE_URL, model=config.LLM_MODEL)
    if backend_name == "ollama":
        from .ollama import OllamaLLM

        return OllamaLLM(base_url=config.OLLAMA_BASE_URL, model=config.LLM_MODEL)
    if backend_name == "openclaw":
        from .openclaw import OpenClawLLM

        return OpenClawLLM(
            base_url=config.OPENCLAW_BASE_URL,
            model=config.OPENCLAW_MODEL,
        )
    raise ValueError("Unknown LLM backend: %s" % backend_name)
