"""Structured fields out of a VLM's prose frame description.

Both backends ended `describe_frame` with `FrameDescription(description=text)`
and nothing else, so `text_in_frame` and `objects` kept their dataclass defaults
-- "" and []. Nothing errored on the way down: `ocr.collect_captions` reads
`text_in_frame`, finds "", and returns no captions; `db.save_ocr_captions` is
then never reached; `merger` folds an empty §4F/L3.5 block into the prompt. A
whole signal layer went silent across the corpus with every step reporting
success.

The prompt has always asked the model for on-screen text (item 2) and the model
has always complied -- the text is in the prose, in quotes. So there are two
readers here:

  * `parse_frame_reply` prefers the explicit ``ON_SCREEN_TEXT:`` / ``OBJECTS:``
    tail the prompt now requests. Exact, and the only trustworthy source for
    `objects`.
  * `text_from_prose` is the fallback, and the only option for the rows a
    pre-fix backend already wrote. Quoted spans are the marker; an indicator
    word ("overlaid", "caption", "logo", "reads"...) must also appear, which is
    what keeps in-world text out of a table that means *burned-in*.

That indicator rule was calibrated against the 1,199 described frames in the
local corpus: 894 carry a quoted span, 864 of those also carry an indicator, and
every one of the 30 that do not turned out to be text that was in the scene
rather than laid over it -- a poster, a shop front, a printed t-shirt, a menu
board. Dropping them is the point, not a miss.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

# Straight, curly and CJK quote pairs. The length cap keeps an unclosed quote
# from swallowing the rest of the description and calling it a caption.
_QUOTE = re.compile(
    r'"([^"\n]{1,160})"'
    r'|“([^”\n]{1,160})”'
    r'|「([^」\n]{1,160})」'
)

# A quoted span counts as burned-in text only when the surrounding description
# says it was put there. "sign", "signage", "poster", "printed" and "billboard"
# are deliberately absent: they mark text the camera found, not text the editor
# added, and ocr_captions means the latter.
_INDICATOR = re.compile(
    r'\b(on-?screen|overlaid|overlays?|labell?ed|labels?|logos?|text|captions?|'
    r'reads|reading|displayed|displays|written|titled?|watermarks?|subtitles?|'
    r'headlines?|banner|says|states|stating|wordmark|lettering|typography)\b',
    re.I,
)

# What the model writes when a frame genuinely has no overlay.
_NOTHING = re.compile(r'^(none|n/?a|no|nothing|-{1,2}|null)\.?$', re.I)

# The tail the prompt asks for. Tolerant of the decoration models like to add
# (leading "**", "- ", spacing, a "S" on OBJECT) but anchored to line starts so
# it can't match the same words inside the prose above it.
_TAIL_TEXT = re.compile(
    r'^[ \t>*_\-]*ON[_ \-]?SCREEN[_ \-]?TEXT[ \t*_]*:[ \t]*(.*)$', re.I | re.M
)
_TAIL_OBJECTS = re.compile(
    r'^[ \t>*_\-]*OBJECTS?[ \t*_]*:[ \t]*(.*)$', re.I | re.M
)


def _dedupe(items) -> List[str]:
    """Order-preserving, case-insensitive dedupe. A caption held across several
    frames of the same shot repeats verbatim; within one frame it is noise."""
    seen = set()
    out = []  # type: List[str]
    for raw in items:
        s = (raw or "").strip().strip(",;")
        if not s:
            continue
        k = s.lower()
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out


def quoted_spans(text: str) -> List[str]:
    """Every quoted span in `text`, deduped, in order."""
    return _dedupe(g for m in _QUOTE.finditer(text or "") for g in m.groups() if g)


def text_from_prose(description: str) -> str:
    """Burned-in text recovered from a prose description.

    Empty when the description carries no quoted span, or carries one with
    nothing marking it as laid over the frame -- see the indicator rule above.
    """
    d = description or ""
    if not _INDICATOR.search(d):
        return ""
    return " / ".join(quoted_spans(d))


def _strip_tail_value(value: str) -> str:
    # Models close their bold before the value, not after the key, so a
    # "**ON_SCREEN_TEXT:** foo" leaves the "**" on this side of the colon.
    v = (value or "").strip().strip("*_").strip().strip('"“”「」').strip()
    if not v or _NOTHING.match(v):
        return ""
    return v


def split_tail(raw: str) -> Tuple[str, Optional[str], List[str]]:
    """Split a raw reply into (prose, on_screen_text, objects).

    `on_screen_text` is None when the model never emitted the line -- distinct
    from "" which is the model saying the frame has no overlay. The caller needs
    that difference: absent means fall back to the prose reader, empty means
    trust the model and stop.
    """
    text = raw or ""
    m_text = _TAIL_TEXT.search(text)
    m_obj = _TAIL_OBJECTS.search(text)

    on_screen = _strip_tail_value(m_text.group(1)) if m_text else None

    objects = []  # type: List[str]
    if m_obj:
        objects = _dedupe(_strip_tail_value(m_obj.group(1)).split(","))

    # Drop the tail lines from the prose so the merger and the viewer keep
    # reading a clean description.
    prose = text
    for m in (m_text, m_obj):
        if m:
            prose = prose.replace(m.group(0), "")
    return prose.strip(), on_screen, objects


def parse_frame_reply(raw: str) -> Tuple[str, str, List[str]]:
    """(description, text_in_frame, objects) from a raw VLM reply.

    Tail first because it is exact; prose second because it is the only thing
    available for a model that ignored the directive. Never raises -- a reply
    neither reader can make sense of degrades to (raw, "", []), which is the
    pre-fix behaviour rather than a crash.
    """
    try:
        prose, tail_text, objects = split_tail(raw)
        text = tail_text if tail_text is not None else text_from_prose(prose)
        return prose, text, objects
    except Exception:  # noqa: BLE001 -- a frame description is never worth a crash
        return (raw or ""), "", []
