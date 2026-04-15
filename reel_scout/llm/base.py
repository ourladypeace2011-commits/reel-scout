from __future__ import annotations

import abc


class BaseLLM(abc.ABC):
    @abc.abstractmethod
    def complete(
        self,
        prompt: str,
        max_tokens: int = 800,
        temperature: float = 0.1,
    ) -> str:
        raise NotImplementedError
