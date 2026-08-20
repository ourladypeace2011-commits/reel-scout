from __future__ import annotations

import glob
import os
from typing import List, Optional

from .base import BaseTranscriber, TranscriptResult
from .. import config

# Preferred subtitle languages, in priority order, for 招① (subtitle-first).
# Only consulted as a tie-break between *original* tracks; a video normally has
# exactly one, so this rarely decides anything.
_SUB_LANG_PREFIXES = ("en", "zh")


def _subtag_is_language(subtag: str) -> bool:
    """True when a BCP-47 subtag is a *language* rather than a script or region.

    Language subtags are 2-3 lowercase letters (``zh``, ``bho``); script subtags are
    four Titlecase letters (``Hant``); region subtags are two uppercase letters or
    three digits (``TW``, ``419``). yt-dlp preserves that casing in the filenames it
    writes, and that is what lets ``zh-TW`` (a locale) be told apart from
    ``en-zh-TW`` (a translation).
    """
    return 2 <= len(subtag) <= 3 and subtag.isalpha() and subtag.islower()


def is_translated_track(lang: str) -> bool:
    """True for YouTube's auto-*translated* caption tracks.

    YouTube names them ``<target>-<source>``. `yt-dlp --list-subs` on a Taiwanese
    video shows the original as a bare ``zh-TW`` with no display name, followed by
    ~100 translations of it: ``en-zh-TW`` is listed as "English from Chinese
    (Taiwan)", ``zh-Hant-zh-TW`` as "Chinese (Traditional) from Chinese (Taiwan)".

    Every translated code therefore carries a *second* language subtag, and a plain
    locale never does::

        zh-TW          -> TW   is a region    -> original
        zh-Hant        -> Hant is a script    -> original
        pt-BR          -> BR   is a region    -> original
        en-orig        -> orig is 4 chars     -> original (yt-dlp's own marker)
        en-zh-TW       -> zh   is a language  -> translation
        zh-Hant-zh-TW  -> zh   is a language  -> translation
    """
    parts = lang.split("-")
    return any(_subtag_is_language(p) for p in parts[1:])


def find_subtitle(video_path: str) -> Optional[str]:
    """Find a usable native subtitle (.vtt) sitting next to a downloaded video.

    yt-dlp writes subs as ``<stem>.<lang>.vtt`` alongside the media file.

    Auto-translated tracks are skipped outright. They used to win: the download
    step asks for ``--sub-langs en.*,zh.*``, which happily matches ``en-zh-TW``,
    and the old preference order tried ``en`` first — so a Mandarin video came back
    with an English machine translation and nothing said so. The stored transcript
    for one 壹加壹 video opened "I'm so nervous!" for a sentence nobody spoke in
    English. Everything downstream (VLM prompts, search, swipe extraction, quoting)
    then inherits a paraphrase as if it were what the speaker said.

    Local Whisper on the original audio beats somebody else's machine translation
    for all of those, so when only translations are on disk this returns None and
    lets the caller fall back — loudly, rather than silently preferring the wrong
    thing.
    """
    if not video_path:
        return None
    stem = os.path.splitext(video_path)[0]
    candidates = sorted(glob.glob(stem + ".*.vtt"))
    if not candidates:
        return None

    base = os.path.basename(stem)
    originals: List[tuple] = []
    for c in candidates:
        lang = os.path.basename(c)[len(base) + 1:]
        if lang.lower().endswith(".vtt"):
            lang = lang[:-4]
        if is_translated_track(lang):
            continue
        originals.append((lang, c))

    if not originals:
        print(
            "  Only auto-translated subtitles found ("
            + ", ".join(sorted(lang_c for lang_c in
                               (os.path.basename(c) for c in candidates)))
            + "); using Whisper on the original audio instead."
        )
        return None

    for prefix in _SUB_LANG_PREFIXES:
        for lang, c in originals:
            if lang.lower().startswith(prefix):
                return c
    return originals[0][1]


def get_transcriber(backend: Optional[str] = None) -> BaseTranscriber:
    backend = backend or config.WHISPER_BACKEND
    if backend == "faster-whisper":
        from .faster_whisper import FasterWhisperTranscriber
        return FasterWhisperTranscriber(model=config.WHISPER_MODEL)
    elif backend == "whisper-cpp":
        from .whisper_cpp import WhisperCppTranscriber
        return WhisperCppTranscriber(model=config.WHISPER_MODEL)
    else:
        raise ValueError(f"Unknown whisper backend: {backend}")
