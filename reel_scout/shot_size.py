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

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

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


#: What a label's `source` column says when a VLM produced it under the
#: constrained prompt below, vs. when it was pulled out of an old free-text
#: description. They are different claims and must not overwrite each other.
SOURCE_VLM = "vlm"
SOURCE_DESCRIPTION = "description"

#: The model may answer this when a frame has no subject to frame against — a
#: title card, a graphic, black. Storing it is the point: "asked, no answer" and
#: "never asked" are different states, and only one of them is worth re-running.
UNKNOWN = "UNKNOWN"


#: What each code means, in the only terms that decide it: how much of the frame
#: the subject fills. Without these the model collapses the taxonomy onto the two
#: labels it knows best — measured, ten ECUs where one belonged.
_DEFINITIONS = {
    "ECU": "part of a face or object fills the frame (eyes, hands, a detail)",
    "CU": "head and a little shoulder",
    "MCU": "head to chest",
    "MS": "waist up",
    "MLS": "knees up, some setting visible",
    "LS": "the whole body, setting clearly visible",
    "ELS": "the subject is small in a wide landscape or space",
}


def classification_prompt() -> str:
    """The constrained prompt, built from the vocabulary rather than repeating it.

    🔴 **Each code carries its definition.** The first version listed the seven
    codes with their English names and nothing else — and "MLS = medium long
    shot" is not a definition, it is the same words in a longer form. Measured
    on 24 frames from 18 clips, same model, same images:

        codes + names only      ECU=10  CU=3  MCU=4   LS=5  UNKNOWN=2
        codes + definitions     ECU=1   CU=3  MCU=13  LS=6  UNKNOWN=1

    Nine of the ten "extreme close-ups" were head-and-chest shots. The model was
    not failing to see; it was being asked to apply a taxonomy it had not been
    given. Wall-clock was the same (259s vs 235s), so this was never a question
    of spending more.

    ⚠️ **What definitions did not fix**: `MS` / `MLS` / `ELS` stay near zero, so
    roughly half the vocabulary is still unused. And on a stacked split-screen
    frame the answer swung from `ECU` to `ELS` — wrong both times, in opposite
    directions. That frame has no single subject-to-frame ratio, and no prompt
    recovers one. Treat the labels as signal, not ground truth.
    """
    lines = "\n".join("%-4s %s — %s" % (c, SHOT_SIZE_NAMES[c], _DEFINITIONS[c])
                      for c in SHOT_SIZES)
    return (
        "Classify the SHOT SIZE of this film frame by how much of the frame the "
        "main subject fills.\n\n%s\n\n"
        "Judge by the SUBJECT-TO-FRAME RATIO, not by how close the camera feels.\n"
        "If there is no identifiable subject (a title card, a graphic, black), "
        "answer %s.\n"
        "Answer with exactly one code and nothing else." % (lines, UNKNOWN)
    )


def subject_gate_prompt() -> str:
    """The binary question asked before the seven-way one.

    🔴 **Why a separate pass rather than a longer prompt.** The obvious move is
    to spell the exceptions out inside `classification_prompt`. It was tried and
    measured on 2026-09-03, same 12 frames, same model:

        current prompt (short UNKNOWN clause)   2/12   12.8 s/frame
        + the five exception categories         0/12   35.6 s/frame

    It got *worse* — it broke the two frames the short prompt got right — and
    cost three times the wall-clock. The identical rules asked as a **binary**
    question score 10/12, rejecting 9 of 9 ill-posed frames with no false
    accepts. So the problem was never the rules; it is a long exception list
    against a seven-way output in a 7B model.

    ⚠️ **What this costs**: the gate over-rejects. Of three well-posed frames it
    accepted one. n=3, so the number is soft, but the direction is not — this
    trades recall for precision deliberately, because these labels become `REF:`
    hints a person reads, and a confident wrong hint anchors them worse than a
    missing one does.
    """
    return (
        "Is this frame a single photographic shot whose main subject is a "
        "person (or human figure) framed clearly enough to judge how much of "
        "the frame the body fills?\n"
        "Answer NO if the frame is a title card, a graphic, black, a screen "
        "recording or interface, a split-screen or side-by-side comparison "
        "layout, or if there is no person as the main subject.\n"
        "Answer with exactly YES or NO and nothing else."
    )


def parse_gate_answer(text) -> Optional[bool]:
    """True/False for a usable answer, None for anything else.

    Prefix-matched rather than equality-matched: the model reliably leads with
    the word and sometimes trails punctuation. Anything that is neither is a
    refusal, and a refusal is not a NO — see `label_shots`, which falls through
    to the classifier rather than silently marking the frame unusable.
    """
    if not text:
        return None
    t = text.strip().upper()
    if t.startswith("YES"):
        return True
    if t.startswith("NO"):
        return False
    return None


def prompt_fingerprint() -> str:
    """Short hash of the current prompt, stored beside every label it produced.

    A version number somebody has to remember to bump goes stale silently; a
    fingerprint of the actual text cannot. Same reasoning as
    `translations.source_hash`.
    """
    # Both prompts, because both decide what a label says. A gate change that
    # left the fingerprint alone would leave the column holding two different
    # measurements again -- the exact thing the column was added to prevent.
    joined = subject_gate_prompt() + "\n\n" + classification_prompt()
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def parse_answer(raw: Optional[str]) -> Optional[str]:
    """A code, `UNKNOWN`, or None when the reply was neither.

    Strict on purpose. A model that answers "probably a medium shot" has not
    followed the instruction, and quietly salvaging a code out of that sentence
    would hide how often the constraint is being ignored — which is the number
    worth watching when the backend or model changes.
    """
    if not raw:
        return None
    first = str(raw).strip().split()
    if not first:
        return None
    token = first[0].strip(".,:;\"'").upper()
    if token in SHOT_SIZES:
        return token
    if token == UNKNOWN:
        return UNKNOWN
    return None
