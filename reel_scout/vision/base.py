from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class FrameDescription:
    keyframe_index: int = 0
    timestamp_sec: float = 0.0
    description: str = ""
    objects: List[str] = field(default_factory=list)
    text_in_frame: str = ""


class BaseVLM(abc.ABC):
    @abc.abstractmethod
    def describe_frame(self, image_path: str) -> FrameDescription:
        """Describe a single keyframe image."""
        ...
