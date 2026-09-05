from __future__ import annotations

import abc


class BaseLLM(abc.ABC):
    @property
    def model(self) -> str:
        """The model this client actually asks for, after its own defaults.

        Exists because the caller could not previously find out. `scores.model_used`
        recorded the *backend* -- 115 of 115 rows in the library read `ollama`
        -- while `stats` grouped on it under a docstring promising "the exact
        model string, the finest grain that is actually correct". Two models
        under one backend were pooled into one yardstick, which is the exact
        thing that column was added to prevent.
        """
        return getattr(self, "_model", "") or ""

    @abc.abstractmethod
    def complete(
        self,
        prompt: str,
        max_tokens: int = 800,
        temperature: float = 0.1,
    ) -> str:
        raise NotImplementedError
