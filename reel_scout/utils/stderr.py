"""Reporting the part of stderr that explains a failure.

``crawl/ytdlp.py`` already does this for yt-dlp. The same head-truncation bug
survived in the ffmpeg and whisper.cpp paths, which print progress chatter first
and the cause last — so a blind ``stderr[:300]`` showed encoder banners while
the real error sat past the cut.
"""

from __future__ import annotations

from typing import Optional


def tail_stderr(stderr: Optional[str], limit: int = 500) -> str:
    """The last ``limit`` characters of stderr, preferring error-looking lines.

    Tools put the cause at the end, so the tail carries the signal the head
    never did.
    """
    if not stderr:
        return ""
    lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    errors = [
        ln for ln in lines
        if ln.lstrip().lower().startswith(("error", "fatal"))
        or "error:" in ln.lower()
    ]
    picked = "\n".join(errors) if errors else "\n".join(lines)
    return picked[-limit:]
