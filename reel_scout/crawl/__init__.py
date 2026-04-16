from __future__ import annotations

import re
from typing import Optional

from .youtube import YouTubeCrawler
from .instagram import InstagramCrawler
from .tiktok import TikTokCrawler
from .base import BaseCrawler


_PLATFORM_PATTERNS = [
    (re.compile(r"(youtube\.com|youtu\.be)"), "youtube"),
    (re.compile(r"instagram\.com"), "instagram"),
    (re.compile(r"tiktok\.com"), "tiktok"),
    (re.compile(r"twitter\.com|x\.com"), "twitter"),
]

_CRAWLERS = {
    "youtube": YouTubeCrawler,
    "instagram": InstagramCrawler,
    "tiktok": TikTokCrawler,
}


def detect_platform(url: str) -> Optional[str]:
    for pattern, name in _PLATFORM_PATTERNS:
        if pattern.search(url):
            return name
    return None


def get_crawler(url: str) -> BaseCrawler:
    platform = detect_platform(url)
    if platform is None or platform not in _CRAWLERS:
        raise ValueError(f"Unsupported platform for URL: {url}")
    return _CRAWLERS[platform]()


def is_profile_url(url: str) -> bool:
    """Check if a URL is a profile/channel page rather than a single video."""
    platform = detect_platform(url)
    if platform == "instagram":
        return InstagramCrawler().is_profile_url(url)
    if platform == "youtube":
        # Channel, @handle, or /shorts tab
        return bool(re.search(
            r"youtube\.com/(?:@[^/]+|channel/|c/|user/|[^/]+/shorts)", url
        ))
    return False
