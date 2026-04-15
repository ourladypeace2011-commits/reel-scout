from __future__ import annotations

import time


class RateLimiter:
    """Simple token-bucket rate limiter."""

    def __init__(self, rate_per_minute: int = 10) -> None:
        self._interval = 60.0 / max(rate_per_minute, 1)
        self._last_call = 0.0

    def wait(self) -> None:
        elapsed = time.time() - self._last_call
        if elapsed < self._interval:
            time.sleep(self._interval - elapsed)
        self._last_call = time.time()


# Per-platform default rates
PLATFORM_RATES = {
    "youtube": 10,
    "instagram": 5,
    "tiktok": 8,
}

_limiters = {}  # type: dict


def get_limiter(platform: str) -> RateLimiter:
    if platform not in _limiters:
        rate = PLATFORM_RATES.get(platform, 10)
        _limiters[platform] = RateLimiter(rate)
    return _limiters[platform]
