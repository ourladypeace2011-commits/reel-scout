from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class VideoMeta:
    platform: str = ""
    platform_id: str = ""
    url: str = ""
    title: str = ""
    uploader: str = ""
    duration_sec: float = 0.0
    upload_date: str = ""
    file_path: str = ""
    file_size_bytes: int = 0
    extra: Dict[str, str] = field(default_factory=dict)


class BaseCrawler(abc.ABC):
    """Abstract base for platform-specific video crawlers."""

    platform: str = ""

    @abc.abstractmethod
    def download(self, url: str, output_dir: str) -> VideoMeta:
        """Download a video and return its metadata."""
        ...

    @abc.abstractmethod
    def extract_id(self, url: str) -> str:
        """Extract platform-specific video ID from URL."""
        ...
