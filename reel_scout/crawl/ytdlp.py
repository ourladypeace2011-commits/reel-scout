"""yt-dlp invocation helpers.

Two long-standing footguns live here (roadmap 5B):

1. Crawlers used to shell out to a bare ``["yt-dlp", ...]``, which resolves to
   the *first* yt-dlp on PATH — often a stale system/homebrew build, NOT the
   version pinned in reel-scout's own venv. yt-dlp breaks constantly (its
   extractors chase moving platforms), so an old binary silently produces
   baffling errors while ``pyproject.toml``'s pinned dependency sits unused.

2. Error messages printed a blind ``stderr[:500]``, which buries the real
   ``ERROR:`` line under leading warnings (e.g. yt-dlp's Python-version
   deprecation banner). Users saw the warning, not the 429/extractor failure.
"""
from __future__ import annotations

import glob
import importlib.util
import os
import sys
from functools import lru_cache
from typing import List, Tuple

from .. import config
from ..utils.stderr import warn


@lru_cache(maxsize=1)
def base_cmd() -> Tuple[str, ...]:
    """Resolve the yt-dlp invocation, preferring the copy that ships with this
    package over whatever ``yt-dlp`` happens to be first on PATH.

    Resolution order:
      1. ``config.YTDLP_BIN`` (explicit override)
      2. ``python -m yt_dlp`` via *this* interpreter, if ``yt_dlp`` is importable
         here — guarantees the version that travels with the install
      3. bare ``yt-dlp`` from PATH (last resort)
    """
    if config.YTDLP_BIN:
        return (config.YTDLP_BIN,)
    if importlib.util.find_spec("yt_dlp") is not None:
        return (sys.executable, "-m", "yt_dlp")
    return ("yt-dlp",)


# Measured on a real iPad, 2026-08-25, one variable at a time (same clip,
# same server, only the video codec swapped):
#
#     h264 + aac   plays        vp9 + aac    does NOT play
#     h264 + opus  plays        vp9 + opus   does NOT play
#                               av1 + opus   does NOT play
#
# The video codec is the whole story. Apple's MP4 path takes H.264 (and HEVC);
# VP9 and AV1 do not play. The audio codec turned out to be irrelevant --
# `h264 + opus` plays with sound, so the earlier belief that Opus-in-MP4 is
# dead on Apple was wrong. `acodec^=mp4a` is kept as a first preference anyway:
# it costs nothing when AAC is available and it is one fewer thing to be wrong
# about.
#
# On the library that produced this measurement, 103 of 109 files were
# unplayable, and 88 of those came through the Instagram path -- which had no
# codec condition at all until this change.
APPLE_SAFE_MAX_HEIGHT_DEFAULT = None


def apple_safe_format(max_height: int = None) -> str:
    """yt-dlp ``-f`` chain preferring what Apple's media stack can decode.

    Positive selection, not a denylist. The previous chain here excluded
    ``av01`` and nothing else, so VP9 walked straight through it -- and VP9 was
    what the library was actually full of. Naming the good codec fails toward
    "we passed on a clip"; naming a bad one fails toward "we ingested something
    that will not play", and nobody sees that until they open it on a phone.

    The tail stays unconstrained on purpose: a clip published only in VP9 or
    AV1 still enters the library. A transcript, keyframes and a score are worth
    having even when the player will not render it.
    """
    h = "[height<=%d]" % max_height if max_height else ""
    return (
        "bestvideo{h}[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
        "best{h}[vcodec^=avc1][acodec^=mp4a]/"
        "bestvideo{h}[vcodec^=avc1]+bestaudio/"
        "bestvideo{h}[vcodec!*=av01]+bestaudio/"
        "best{h}[vcodec!*=av01]/"
        "bestvideo{h}+bestaudio/best{h}"
    ).format(h=h) + ("/best" if h else "")


def cmd(*args: str) -> List[str]:
    """yt-dlp base invocation + the given arguments, ready for subprocess.run."""
    return list(base_cmd()) + list(args)


def format_error(stderr: str) -> str:
    """Surface the real failure from yt-dlp's stderr instead of a blind head-cut.

    Keeps the ``ERROR:`` lines when present; otherwise falls back to the tail of
    stderr (the failure is far likelier at the end than in the first 500 chars),
    and appends an update hint when the failure smells like a broken extractor.
    """
    if not stderr:
        return ""
    lines = stderr.strip().splitlines()
    errors = [ln for ln in lines if ln.lstrip().startswith("ERROR:")]
    if errors:
        msg = "\n".join(errors)[:500]
    else:
        msg = stderr.strip()[-500:]
    return msg + _extractor_hint(msg)


def _extractor_hint(msg: str) -> str:
    lowered = msg.lower()
    markers = (
        "unable to extract", "unsupported url", "not available",
        "no video formats", "unable to download webpage",
        "requested format is not available", "unable to download api page",
    )
    if any(m in lowered for m in markers):
        shown = " ".join(base_cmd())
        return (
            "\n[hint] the platform extractor may have changed or yt-dlp is "
            "outdated — update it: `%s -U` (or `pip install -U yt-dlp`)." % shown
        )
    return ""


def clear_unusable_output(expected: str) -> bool:
    """Move a leftover destination file aside when it is not playable media.

    yt-dlp skips a destination that already exists and exits 0 -- "has already
    been downloaded". That is the right call for a real cache hit and a trap for
    anything else: a zero-byte or truncated ``yt_<id>.mp4`` from a killed run
    makes the whole download report success without transferring a byte, and the
    caller's ``os.path.exists`` check agrees. Worse, the pipeline's own reuse
    gate is ``os.path.exists`` too, so the bad file is then skipped *forever* --
    the failure is sticky, not a one-run blip.

    Playability is the test, not size: ``probe_duration`` returns None for
    anything ffmpeg cannot read, which is exactly the question being asked, and
    it costs one subprocess only when a file is already sitting there.

    Nothing is deleted. The file is renamed with a ``.unusable`` suffix, because
    a file that cannot be read is still evidence about what went wrong, and
    deciding it is worthless is not this function's call to make.

    Subtitles move with it. ``find_subtitle`` globs ``<stem>.*.vtt``, so a stale
    caption file outlives the video it came from and would be re-attached to the
    next download -- a transcript from one clip presented as another's.

    Returns True when something was moved.
    """
    if not os.path.exists(expected):
        return False

    from .. import ffprobe

    if ffprobe.probe_duration(expected) is not None:
        return False

    stem, _ = os.path.splitext(expected)
    moved = []
    for path in [expected] + sorted(glob.glob(stem + ".*.vtt")):
        try:
            os.replace(path, path + ".unusable")
            moved.append(os.path.basename(path))
        except OSError:
            # Read-only directory, or something else holding it. Say so rather
            # than pretend: the download below will overwrite or fail loudly.
            pass
    if moved:
        warn("  note: %s was not playable media; moved aside as .unusable"
             % ", ".join(moved))
    return bool(moved)
