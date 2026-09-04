"""Produce shot-size labels for a clip's shots (roadmap §6B, the producer half).

`shot_size.py` owns the vocabulary and refuses to guess; this owns the pass that
actually asks something. Two ways in, because the answer to "which model" is not
this module's to make:

* **`--from-json`** — labels supplied from outside (an agent, a person, another
  tool), the same L1 shape `ingest` already uses. No local model, no GPU.
* **a local VLM** — the constrained prompt against ollama.

Every row is stamped with `source` and `model`. That is not bookkeeping: the
same clip scores 7.43 under `qwen3-vl:8b` and 5.5 under `qwen2.5vl:7b`, and a
shot size is no less model-dependent than a craft score. Two passes with
different models coexist rather than overwrite, and a reader choosing between
them can see which is which.

**Measured cost** (2026-09-02, M2 Max, `qwen2.5vl:7b`, one representative frame
per shot): 39 frames in 2.4 minutes ≈ 3.7 s/frame. The whole 2,872-frame library
is therefore ≈ 3 hours, not the 9.5 estimated before measuring — the constrained
answer caps output at a dozen tokens, where a full description does not.

**Known weaknesses, both measured rather than suspected:**

* A title card or a graphic gets a subject-based code instead of `UNKNOWN`. One
  of three spot-checked frames was a title card labelled `LS`. Treat `UNKNOWN`
  as **under-reported**.
* `ECU` is **over-reported**. Across the whole library (825 labels, 2026-09-02)
  it came back as the single most common size at 35% — which no mixed corpus of
  tutorials, montages and vlogs actually looks like. Two randomly sampled `ECU`
  frames were opened: one was a genuine extreme close-up (two faces cropped
  forehead-to-chin), the other was an astronaut framed from the waist up, which
  is a medium shot. **1 of 2 wrong.**
  Part of the cause is the material rather than the model: this library is
  mostly 9:16 short-form, and a stacked split-screen frame has no single answer
  to "how much of the frame does the subject fill". `ELS` came back zero times
  and `MLS` once, so in practice the model uses about half the vocabulary.

* The model sometimes emits a code that does not exist. Measured 2026-09-03 on
  `560dfe9c082f7e87`: `EUC`, a transposition of `ECU`, returned identically on
  every run because temperature is 0. `parse_answer` refuses it, and
  **deliberately does not repair it** — a vocabulary that quietly accepts near
  misses is how a confident wrong value gets into a column meant to be trusted.

⇒ **Use these labels as a signal, never as ground truth.** A storyboard cut
that carries one is a starting point for the person editing it, which is what
the export's `REF:` marking already assumes.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from . import config, db
from .shot_size import (SOURCE_GATE, SOURCE_VLM, UNKNOWN, classification_prompt,
                         parse_answer, parse_gate_answer, prompt_fingerprint,
                         subject_gate_prompt)
from .shots import Shot, bind_frames_to_shots
from .utils import paths as media_paths

#: Enough for a code. A budget that allows a sentence invites one, and a
#: sentence is exactly what `parse_answer` refuses.
_NUM_PREDICT = 12

#: Formats where every shot is the same framing, so labelling each one costs
#: real time and returns almost no information. An interview is the clear case:
#: a locked-off MCU on a talking subject, held for the whole piece. The values
#: are the normalized `analyses.style_format` tags (schema v5/v6), not a new
#: vocabulary — the corpus already carries 20 clips tagged `talking_head`, and
#: at ~3.7 s per frame that is half an hour spent confirming one answer.
#:
#: This is a default, not a rule about what is knowable: `--force` labels them
#: anyway, because "mostly uniform" is not "uniform" and somebody may want the
#: exceptions.
SKIP_FORMATS = ("talking_head",)


#: The single statement of "this label did not come from the current prompt".
#: Callers must alias `shot_labels` as `l`.
#:
#: 🔴 **One judge, not three.** The first version of this had the rule written
#: as a Python predicate in this module, as SQL in `health.py`, and again in the
#: re-run script outside the repo — and the Python one had no production caller
#: at all, so it was tested code that judged nothing. Three copies of a rule
#: agree right up until the day one of them is edited, and the disagreement
#: would surface as health saying "0 stale" while the re-run kept finding work.
STALE_PROMPT_SQL = ("l.kind = 'shot_size' AND l.source IN (?, ?) "
                    "AND COALESCE(l.prompt_hash, '') != ?")


def stale_prompt_params() -> tuple:
    """The bindings for :data:`STALE_PROMPT_SQL`, in order.

    NULL counts as stale: rows written before v17 carry no fingerprint, and
    "unknown provenance" is not the same claim as "current". The distribution
    moved ten-to-one when the prompt changed, so a label whose prompt cannot be
    established is not comparable to one whose can.
    """
    return (SOURCE_VLM, SOURCE_GATE, prompt_fingerprint())


def drop_superseded_label(conn, video_id: str, t_sec: float) -> int:
    """Delete this frame's label if it came from a prompt we no longer use.

    Narrow on purpose, and every part of the narrowness is load-bearing: only
    this one frame, only `vlm` rows, and only when the fingerprint is not the
    current one. A label written by the current prompt is never touched, so a
    run that finds nothing superseded deletes nothing.
    """
    cur = conn.execute(
        "DELETE FROM shot_labels WHERE id IN ("
        "  SELECT l.id FROM shot_labels l"
        "  WHERE l.video_id = ? AND l.t_sec = ? AND " + STALE_PROMPT_SQL + ")",
        (video_id, t_sec) + stale_prompt_params())
    conn.commit()
    return cur.rowcount


def analysable(conn, video_id: str) -> Tuple[int, int]:
    """`(frames that yielded a scale, frames labelled)` for one clip.

    The number Hevin asked for on 2026-09-03, and it is reported rather than
    enforced: a clip that is 90% graphics still gets its labels written, with
    the ratio next to them, and the person reading decides whether to trust it.
    Blocking would be the wrong shape here — the same detect-and-surface rule
    `db health` already follows.

    🔴 **Keyed on the value, not the source.** Anything stored as `UNKNOWN`
    yielded no scale, whether the gate rejected the frame or the classifier
    looked and found no subject. Keying on `source` instead would silently
    report 100% for every clip labelled before `SOURCE_GATE` existed — a wrong
    number that looks like a healthy one.

    **Why the denominator is labelled frames, not shots.** A clip whose frames
    are unreadable has no labels at all, and dividing by shots would report it
    as 0% analysable — which reads as "this is a graphics piece" when the truth
    is "the files are missing". Those are different problems with different
    remedies, and `db health` already counts the second one.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN value != ? THEN 1 ELSE 0 END) AS scaled "
        "FROM shot_labels WHERE video_id = ? AND kind = 'shot_size' "
        "AND source != 'supplied'", (UNKNOWN, video_id)).fetchone()
    return int(row["scaled"] or 0), int(row["total"] or 0)


def stale_videos(conn) -> List[str]:
    """Clips holding at least one label from a superseded prompt, id order.

    Re-derived on every call rather than collected once: a caller that snapshots
    the list up front cannot tell a clip that finished from a clip that failed —
    both simply stop appearing in its loop.
    """
    rows = conn.execute(
        "SELECT DISTINCT l.video_id FROM shot_labels l WHERE " +
        STALE_PROMPT_SQL + " ORDER BY l.video_id", stale_prompt_params())
    return [r["video_id"] for r in rows]


def skip_reason(conn, video_id: str) -> Optional[str]:
    """Why this clip should not be labelled, or None to go ahead.

    Reads the format the analysis already decided. A clip with no analysis is
    not skipped — "we never classified it" is not evidence that it is an
    interview, and refusing on missing data would quietly shrink the run.
    """
    row = conn.execute(
        "SELECT style_format FROM analyses WHERE video_id = ?", (video_id,)
    ).fetchone()
    fmt = row["style_format"] if row else None
    if fmt and fmt in SKIP_FORMATS:
        return fmt
    return None


def representative_frames(conn, video_id: str) -> List[Tuple[Shot, Any]]:
    """(shot, its representative keyframe) for shots that have one."""
    rows = db.get_shots(conn, video_id)
    if not rows:
        return []
    spans = [Shot(r["idx"], r["start_sec"], r["end_sec"], r["dur_sec"]) for r in rows]
    per, _ = bind_frames_to_shots(spans, db.get_keyframes_with_descriptions(conn, video_id) or [])
    return [(s.shot, s.representative) for s in per if s.representative is not None]


#: Why a frame produced no code. The distinction is not cosmetic: only
#: `UNPARSEABLE` is deterministic. A model that answers outside the vocabulary
#: answers the same way on a retry (temperature is 0); ollama being down does
#: not.
#:
#: 🔴 **These were one value once, and that was a real bug.** The first version
#: returned bare `None` for all four causes and the caller counted them all as
#: "refused" — so an ollama outage and a model that cannot spell were the same
#: line of output, and any convergence logic built on top would have treated an
#: outage as a permanent verdict. Found 2026-09-03 while working out why one
#: clip would not converge: the answer was `EUC`, a transposition of `ECU`.
REFUSAL_UNPARSEABLE = "unparseable"
REFUSAL_UNREACHABLE = "unreachable"
REFUSAL_UNREADABLE = "unreadable"
REFUSAL_INCONCLUSIVE = "inconclusive"

# Refusals that say nothing about the frame: the model either never answered
# or never had the room to. Only REFUSAL_UNPARSEABLE is a verdict -- the model
# answered, at temperature 0, with something outside the vocabulary -- and a
# verdict is the only thing that may retire a stored row. Everything else
# leaves what is already there alone.
INCONCLUSIVE_REFUSALS = (REFUSAL_UNREACHABLE, REFUSAL_UNREADABLE,
                         REFUSAL_INCONCLUSIVE)

# Each lands in the bucket that already means that thing, so the tally keeps
# naming causes rather than growing a bucket per code path.
_REFUSAL_BUCKET = {REFUSAL_UNREACHABLE: "unreachable",
                   REFUSAL_UNREADABLE: "missing",
                   REFUSAL_INCONCLUSIVE: "inconclusive"}


def ask_subject_gate(image_path: str, model: str,
                     base_url: Optional[str] = None) -> Tuple[Optional[bool], Optional[str]]:
    """Does the shot-size question apply to this frame? `(answer, refusal)`.

    Measured 2026-09-03 on 12 hand-judged frames: 9 of 9 ill-posed frames
    rejected, no false accepts, 10.2 s/frame. Since it skips the classifier on
    everything it rejects, the gate adds roughly 13% to the pass rather than
    doubling it.
    """
    raw, why, truncated = _ollama(image_path, model, subject_gate_prompt(), 4,
                                  base_url)
    if why is not None:
        return None, why
    answer = parse_gate_answer(raw)
    if answer is not None:
        return answer, None
    return None, REFUSAL_INCONCLUSIVE if truncated else REFUSAL_UNPARSEABLE


def _ollama(image_path: str, model: str, prompt: str, num_predict: int,
            base_url: Optional[str]
            ) -> Tuple[Optional[str], Optional[str], bool]:
    """One image, one short answer. `(text, refusal, truncated)`.

    Exactly one of text/refusal is None. `truncated` says the reply stopped at
    the token budget rather than because the model was finished. On its own
    that is not a failure -- a four-token budget holds `MCU` comfortably --
    but it changes what an unparseable reply means: we cut the model off, so
    what came back is not its answer and must not be scored as one.
    """
    url = (base_url or getattr(config, "OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
    try:
        with open(image_path, "rb") as f:
            img = base64.b64encode(f.read()).decode("utf-8")
    except OSError:
        return None, REFUSAL_UNREADABLE, False
    body = json.dumps({
        "model": model, "prompt": prompt, "images": [img], "stream": False,
        # Reasoning models otherwise spend the whole budget thinking and answer
        # nothing; the same guard the vision path already needs.
        "think": False,
        "options": {"num_predict": num_predict, "temperature": 0},
    }).encode("utf-8")
    req = urllib.request.Request(url + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None, REFUSAL_UNREACHABLE, False
    # A 200 is not by itself an answer. ollama returns 200 carrying its own
    # error envelope, 200 with an empty string, and 200 with nothing but
    # reasoning the budget cut off before any verdict -- and the vision path
    # already had to learn exactly this (CHANGELOG 1.4.0: "a 200 response with
    # no `response` field ... is not a generate response at all"). Handing any
    # of them back as text makes the caller parse a non-answer, and the caller
    # reports that as the model breaking the vocabulary. That verdict deletes
    # rows, which is how a model that said nothing came to empty a library.
    if payload.get("error"):
        return None, REFUSAL_INCONCLUSIVE, False
    text = payload.get("response")
    truncated = payload.get("done_reason") == "length"
    if not isinstance(text, str) or not text.strip():
        return None, REFUSAL_INCONCLUSIVE, truncated
    return text, None, truncated


def classify_with_ollama(image_path: str, model: str,
                         base_url: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """One frame, one code. Returns `(code, refusal)`; exactly one is None.

    The refusal says whether asking again could ever produce a different
    answer. `REFUSAL_UNPARSEABLE` means no — the constraint was stated and not
    followed, at temperature 0. The others are the environment or our own
    token budget, and both of those can be fixed.
    """
    raw, why, truncated = _ollama(image_path, model, classification_prompt(),
                                  _NUM_PREDICT, base_url)
    if why is not None:
        return None, why
    code = parse_answer(raw)
    if code:
        return code, None
    return None, REFUSAL_INCONCLUSIVE if truncated else REFUSAL_UNPARSEABLE


def label_video(conn, video_id: str, model: str,
                base_url: Optional[str] = None,
                supplied: Optional[Dict[str, str]] = None,
                force: bool = False, gate: bool = True) -> Dict[str, Any]:
    """Label every shot that has a representative frame. Returns a tally.

    `supplied` maps a keyframe id (as a string, since it arrives from JSON) to a
    code, and skips the model entirely. Anything not in it is asked, unless the
    caller passed `supplied` for every frame.
    """
    # `missing` is its own bucket on purpose. The first run of this command
    # reported "39 refused" when every single frame file was simply unreadable
    # -- blaming the model for a path problem is the exact conflation this
    # library keeps paying for, and it took one run to build a fresh one.
    tally: Dict[str, Any] = {"labelled": 0, "unknown": 0, "refused": 0,
                             "unreachable": 0, "inconclusive": 0, "dropped": 0,
                             "gated": 0, "skipped": 0, "missing": 0,
                             "skipped_format": None}
    if not force:
        fmt = skip_reason(conn, video_id)
        if fmt:
            # Reported, never silent: a run that quietly does nothing looks
            # exactly like a run that found nothing to do.
            tally["skipped_format"] = fmt
            return tally
    for shot, frame in representative_frames(conn, video_id):
        code = None
        source = SOURCE_VLM
        used_model: Optional[str] = model
        if supplied is not None:
            code = parse_answer(supplied.get(str(frame["id"])))
            source, used_model = "supplied", None
            if code is None:
                tally["skipped"] += 1
                continue
        else:
            if not media_paths.exists(frame["file_path"]):
                tally["missing"] += 1
                continue
            path = media_paths.resolve_media_path(frame["file_path"])
            refusal = None
            if gate:
                applies, refusal = ask_subject_gate(path, model, base_url)
                if refusal in INCONCLUSIVE_REFUSALS:
                    tally[_REFUSAL_BUCKET[refusal]] += 1
                    continue
                if applies is False:
                    source = SOURCE_GATE
                    # Not a demotion -- this is what UNKNOWN was defined to
                    # mean here: "no subject to frame against". Stored with the
                    # current fingerprint, so the frame converges instead of
                    # being asked again every run.
                    code = UNKNOWN
                    tally["gated"] += 1
                # A gate that could not answer falls through to the classifier.
                # Failing closed would let one unparseable reply mark a frame
                # unusable, which is a bigger claim than the gate has earned.
            if code is None:
                code, refusal = classify_with_ollama(path, model, base_url)
            if code is None:
                if refusal in INCONCLUSIVE_REFUSALS:
                    # Environment or our own token budget, not a verdict.
                    # Leave whatever is already stored alone and let the next
                    # run decide. A frame whose file vanished mid-run lands
                    # here too: before, it fell through to `refused` and took
                    # the stored label with it.
                    tally[_REFUSAL_BUCKET[refusal]] += 1
                    continue
                tally["refused"] += 1
                # A frame the current prompt cannot answer, still holding an
                # answer from a prompt we have retired. Keeping it would leave
                # the clip permanently on the re-run list with a remedy that
                # cannot work -- exactly the "cannot" that `db health` exists
                # to separate from "not yet". Saying nothing about this frame
                # is more honest than keeping a reading from a retired
                # instrument, so the superseded row goes.
                tally["dropped"] += drop_superseded_label(
                    conn, video_id, frame["timestamp_sec"])
                continue
        # The label hangs off the frame's timestamp, not the shot id: spans are
        # replaced on every re-analyze, timestamps are not.
        db.save_shot_label(conn, video_id, frame["timestamp_sec"],
                           "shot_size", code, source, used_model,
                           # Supplied labels did not come from our prompt, so
                           # they carry no fingerprint — stamping one would
                           # claim a provenance they do not have.
                           prompt_fingerprint()
                           if source in (SOURCE_VLM, SOURCE_GATE) else None)
        tally["unknown" if code == UNKNOWN else "labelled"] += 1
    return tally
