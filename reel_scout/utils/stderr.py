"""Reporting the part of stderr that explains a failure.

``crawl/ytdlp.py`` already does this for yt-dlp. The same head-truncation bug
survived in the ffmpeg and whisper.cpp paths, which print progress chatter first
and the cause last — so a blind ``stderr[:300]`` showed encoder banners while
the real error sat past the cut.
"""

from __future__ import annotations

import sys
from typing import Optional


def force_utf8_stdio() -> None:
    """Print UTF-8 regardless of the console's codepage.

    Python encodes stdout with the locale codepage, which on Windows is cp950
    (zh-TW), cp1252 (US/EU) or cp437 (legacy cmd). None of them can encode the
    emoji that short-form titles are full of, and cp437 cannot even encode the
    em dash this package prints throughout -- so `show` died on a traceback
    before it could list the keyframe paths that Step 2b needs. Replacement
    characters are a bad look; a UnicodeEncodeError is a broken tool.

    This lived in ``cli.py`` and ran only from the CLI entry point, which left
    every other way into this package unprotected -- including the MCP server,
    which is how the Windows students reach it with no terminal at all.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # pytest's capture and anything else that swaps in a plain buffer.
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Detached or already-closed stream; printing is not our job to fix.
            pass


def warn(message: str) -> None:
    """Print to stderr without letting the console's codepage kill the caller.

    ``cli.py`` reconfigures both streams to UTF-8, but it is the *CLI* entry
    point — the MCP server (``mcp/server.py``) and anything importing this
    package directly never run it. Library code that prints an emoji or an em
    dash therefore raises ``UnicodeEncodeError`` on a cp950/cp1252/cp437 console,
    which is how the Windows students reach this package.

    That turns a report into a failure. ``save_transcript`` warns about mixed
    traditional/simplified text and then writes the transcript; if the warning
    raises, the transcript is never stored — a detector that deletes the data it
    was watching. Degrade the characters instead: a `?` is ugly, losing the row
    is not recoverable.
    """
    try:
        print(message, file=sys.stderr)
        return
    except UnicodeEncodeError:
        pass
    except (ValueError, OSError):
        # Detached, closed, or nobody reading the other end of the pipe.
        # ``cli._force_utf8_stdio`` swallows the same pair for the same reason:
        # whether the operator can see this line is not worth the caller's work.
        return
    try:
        print(message.encode("ascii", "replace").decode("ascii"), file=sys.stderr)
    except (ValueError, OSError):
        return


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
