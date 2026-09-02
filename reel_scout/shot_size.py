"""Shot size as a constrained value, not prose (roadmap §Phase 6B).

The VLM already says "a close-up of a person" in its description. What has never
existed is a *field* — a value from a fixed set that something downstream can
group by, count, or write into a storyboard cut. Prose cannot be grouped by.

**The vocabulary is not invented here.** `prompts/storyboard-visualize.md` has
carried `ECU / CU / MCU / MS / MLS / LS / ELS` since the prompt pack shipped;
that file serves the forward direction (a shot list becoming an image prompt)
and this module serves the reverse. Two lists would drift, and the drift would
be silent, so there is one list and this module owns the machine-readable copy
with the prompt pack as its cited source.

## What this module refuses to do

`normalize` returns None rather than guessing, in two cases that matter:

* **Nothing recognisable.** Most VLM descriptions never mention framing at all —
  they describe content. A tool that returned a shot size for every frame would
  be inventing one for most of them.
* **More than one size named.** "a close-up of a hand against a wide city
  backdrop" contains two. There is a real answer, but it is not recoverable from
  the sentence, and picking the first match would look identical to knowing.

Lens language is deliberately *not* a shot size. "wide-angle lens" describes
optics; a wide-angle lens shoots close-ups all the time. Mapping it would put a
confident wrong value in a field meant to be trustworthy.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

#: Canonical codes, widest-subject-first (ECU is the tightest framing).
#: Source: `prompts/storyboard-visualize.md` — "景別用標準縮寫
#: (ECU / CU / MCU / MS / MLS / LS / ELS)".
SHOT_SIZES: Tuple[str, ...] = ("ECU", "CU", "MCU", "MS", "MLS", "LS", "ELS")

#: Full English name per code, as the prompt pack spells them out.
SHOT_SIZE_NAMES: Dict[str, str] = {
    "ECU": "extreme close-up",
    "CU": "close-up",
    "MCU": "medium close-up",
    "MS": "medium shot",
    "MLS": "medium long shot",
    "LS": "long shot",
    "ELS": "extreme long shot",
}

#: Phrases that identify a size, per code. Matched longest-first so "extreme
#: close-up" cannot be swallowed by "close-up". Every entry has to name a
#: *framing*: bare "wide" is absent on purpose, because "wide-angle" is a lens.
_PHRASES: Dict[str, Tuple[str, ...]] = {
    "ECU": ("extreme close up", "extreme closeup", "extreme close-up", "ecu"),
    "MCU": ("medium close up", "medium closeup", "medium close-up", "mcu"),
    "CU": ("close up shot", "close-up shot", "close up", "closeup", "close-up", "cu"),
    "MLS": ("medium long shot", "medium full shot", "mls"),
    "MS": ("medium shot", "mid shot", "ms"),
    "ELS": ("extreme long shot", "extreme wide shot", "extreme wide angle shot",
            "establishing shot", "els"),
    "LS": ("long shot", "wide shot", "full shot", "full body shot", "ls"),
}

# Longest phrase first so a longer name always wins over a shorter substring.
_ORDERED: List[Tuple[str, str]] = sorted(
    ((phrase, code) for code, phrases in _PHRASES.items() for phrase in phrases),
    key=lambda pair: len(pair[0]),
    reverse=True,
)

#: Two-letter codes are matched only as standalone words. Without this, "cu"
#: fires inside "curtain", "document", "focus" — and a shot size that appears
#: because a wall had curtains is worse than no shot size.
_WORD_RE = {phrase: re.compile(r"(?<![a-z])%s(?![a-z])" % re.escape(phrase))
            for phrase, _ in _ORDERED}


def normalize(text: Optional[str]) -> Optional[str]:
    """The one shot size named in `text`, or None.

    None means "not recoverable from this sentence", which covers both saying
    nothing about framing and naming two different framings. Callers must treat
    None as absent evidence, never as a default size.
    """
    if not text:
        return None
    hay = " " + " ".join(str(text).lower().replace("_", " ").split()) + " "
    found: List[str] = []
    for phrase, code in _ORDERED:
        if code in found:
            continue
        if _WORD_RE[phrase].search(hay):
            # Blank the matched span so a longer phrase's leftovers ("close-up"
            # inside "extreme close-up") cannot register as a second, different
            # size and make an unambiguous sentence look ambiguous.
            hay = _WORD_RE[phrase].sub(" ", hay)
            found.append(code)
    if len(found) == 1:
        return found[0]
    return None


def describe(code: Optional[str]) -> str:
    """Human label for a code; the code itself if it is not one of ours."""
    if not code:
        return "—"
    return "%s (%s)" % (code, SHOT_SIZE_NAMES.get(code, "unknown"))
