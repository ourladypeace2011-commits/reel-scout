from __future__ import annotations

import html
import os
import re
from typing import List, Optional

from .base import Segment, TranscriptResult

# WEBVTT cue timing line, e.g. "00:00:01.000 --> 00:00:04.000 align:start position:0%"
_CUE_RE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[.,]\d{3}|\d{2}:\d{2}[.,]\d{3})\s*-->\s*"
    r"(?P<end>\d{2}:\d{2}:\d{2}[.,]\d{3}|\d{2}:\d{2}[.,]\d{3})"
)
# Inline timestamp / karaoke tags YouTube auto-subs embed, e.g. "<00:00:01.520>" and
# "<c>word</c>" colour tags. Stripped so the plain words remain.
_TAG_RE = re.compile(r"<[^>]+>")
# yt-dlp marks the video's own language track ``en-orig`` / ``zh-orig``. That is
# yt-dlp's marker, not a BCP-47 subtag -- `is_translated_track` already knows this
# (it is why ``en-orig`` counts as an original), but the value was still being
# written into the stored ``language`` column verbatim.
_ORIG_SUFFIX_RE = re.compile(r"-orig$", re.IGNORECASE)


def _parse_ts(value: str) -> float:
    """Parse an HH:MM:SS.mmm or MM:SS.mmm WebVTT timestamp into seconds."""
    value = value.replace(",", ".")
    parts = value.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    if len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return float(value)


def _clean_text(line: str) -> str:
    """Strip the tags, then unescape the entities -- in that order.

    A YouTube cue carries both: real markup (``<c>``, ``<00:00:01.520>``) and HTML
    entities (``&gt;&gt;`` for a speaker change, ``&#39;`` for an apostrophe, ``&amp;``).
    Only the tags were being removed, so three of the six natively-subtitled
    transcripts in the library open with a literal ``&gt;&gt;``.

    Unescaping first would be wrong, not merely redundant: ``&lt;c&gt;`` would become
    ``<c>`` and the tag stripper would then delete it *and* believe it had found
    markup, silently eating text the caption author had deliberately escaped.
    """
    return html.unescape(_TAG_RE.sub("", line)).strip()


def _lang_from_path(path: str) -> str:
    """Best-effort language code from a yt-dlp subtitle filename (foo.en.vtt).

    The ``-orig`` marker is dropped here, at the one place that knows the value came
    out of a filename. Leaving it in put two vocabularies in a single column: Whisper
    writes ISO-639-1 (``en``), this path wrote ``en-orig`` for the same language, and
    a ``GROUP BY language`` reads them as two different languages.
    """
    base = os.path.basename(path)
    stem = base[:-4] if base.lower().endswith(".vtt") else base
    parts = stem.rsplit(".", 1)
    # Accept codes like "en", "zh", "zh-Hant", "pt-BR" (BCP-47-ish, up to ~10 chars).
    if len(parts) == 2 and 1 <= len(parts[1]) <= 10:
        return _ORIG_SUFFIX_RE.sub("", parts[1])
    return ""


def parse_vtt(path: str, language: Optional[str] = None) -> TranscriptResult:
    """Parse a WebVTT subtitle file into a TranscriptResult (pure stdlib).

    YouTube auto-subs are noisy: each cue is repeated as it scrolls (rolling
    duplicates) and carries inline ``<00:00:01.520>`` / ``<c>`` tags. We strip tags
    and drop consecutive duplicate text so the merged transcript reads cleanly.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()

    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    segments: List[Segment] = []
    i = 0
    n = len(lines)
    last_text = ""
    while i < n:
        line = lines[i]
        m = _CUE_RE.search(line)
        if not m:
            i += 1
            continue
        start = _parse_ts(m.group("start"))
        end = _parse_ts(m.group("end"))
        # Collect the cue payload until a blank line or the next cue.
        i += 1
        payload_lines: List[str] = []
        while i < n and lines[i].strip() != "" and not _CUE_RE.search(lines[i]):
            cleaned = _clean_text(lines[i])
            if cleaned:
                payload_lines.append(cleaned)
            i += 1
        text = " ".join(payload_lines).strip()
        if not text:
            continue
        # Dedupe rolling duplicates: YouTube re-emits the previous line(s) as a cue
        # scrolls. Skip a cue whose text is identical to, or fully contained in, the
        # text we last kept (handles the common "prev\ncurr" stacked-cue pattern).
        if text == last_text:
            continue
        if last_text and text in last_text:
            continue
        if last_text and last_text in text:
            # Current cue extends the previous one — replace rather than stack.
            text = text[len(last_text):].strip() or text
        segments.append(Segment(start=start, end=end, text=text, confidence=1.0))
        last_text = " ".join(payload_lines).strip()

    text_full = " ".join(s.text for s in segments).strip()
    duration = segments[-1].end if segments else 0.0
    lang = language or _lang_from_path(path)

    return TranscriptResult(
        language=lang,
        text_full=text_full,
        segments=segments,
        duration_sec=duration,
        model="native-subtitles",
    )
