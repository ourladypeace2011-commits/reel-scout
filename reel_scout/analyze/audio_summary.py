"""Turn the audio-event table into something a merge prompt can carry.

`merger` used to join every row into the prompt verbatim. That is fine for a
20-second reel with nine events and ruinous for a 82-minute interview with
4,874: the detector samples a 2-second window every second, so a talk-heavy clip
produces one `speech: Speech (82%)` line per second of runtime. The longest clip
in the local library rendered as roughly 200 KB of that, which does not fail —
it pushes the transcript and the on-screen text out of the model's window, and
those are the two layers the merge actually reasons from.

The redundancy is the point: those thousands of speech rows carry one fact
("this is talk, nearly wall to wall") repeated per second, while the handful of
sound-effect rows each carry a distinct one. So the two are summarised
differently — coverage as a percentage, effects as a list.

Windows overlap by design (2s window, 1s hop), so covered time is measured by
merging intervals. Summing `end - start` would report a 100-second clip as 200
seconds of speech and hand the model a number that cannot be true.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

#: Event types that describe a bed rather than a moment. Summarised as coverage.
_SUSTAINED = ("speech", "music", "silence")


def _merge_intervals(spans: Iterable[Tuple[float, float]]) -> float:
    """Total seconds covered by `spans`, counting overlap once."""
    ordered = sorted((s, e) for s, e in spans if e > s)
    if not ordered:
        return 0.0
    total = 0.0
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start > cur_end:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    total += cur_end - cur_start
    return total


def _collapse(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fold consecutive rows carrying the same label into one span.

    A one-second hop means a two-second effect is reported twice and a held one
    many times over. Listing each window separately says "this happened N
    times" when it happened once.
    """
    out = []  # type: List[Dict[str, Any]]
    for r in sorted(rows, key=lambda x: (float(x["start_sec"] or 0.0), x["label"] or "")):
        label = r["label"] or ""
        start = float(r["start_sec"] or 0.0)
        end = float(r["end_sec"] or start)
        conf = float(r["confidence"] or 0.0)
        if out and out[-1]["label"] == label and start <= out[-1]["end"] + 0.5:
            out[-1]["end"] = max(out[-1]["end"], end)
            out[-1]["conf"] = max(out[-1]["conf"], conf)
        else:
            out.append({"label": label, "start": start, "end": end, "conf": conf})
    return out


def summarize(events: Sequence[Any], duration_sec: float, max_events: int = 40) -> str:
    """Render the audio layer for a merge prompt.

    Returns the "(no audio analysis)" sentinel when there is nothing to say, so
    callers keep the existing contract.
    """
    rows = [dict(e) if not isinstance(e, dict) else e for e in events]
    if not rows:
        return "(no audio analysis)"

    duration = float(duration_sec or 0.0)

    # Coverage: what share of the clip each bed occupies.
    coverage = []  # type: List[str]
    for kind in _SUSTAINED:
        spans = [
            (float(r["start_sec"] or 0.0), float(r["end_sec"] or 0.0))
            for r in rows if r["event_type"] == kind
        ]
        if not spans:
            continue
        secs = _merge_intervals(spans)
        if duration > 0:
            coverage.append("%s %.0f%%" % (kind, min(100.0, 100.0 * secs / duration)))
        else:
            coverage.append("%s %.0fs" % (kind, secs))

    lines = []  # type: List[str]
    if coverage:
        lines.append("Coverage: " + ", ".join(coverage))

    # Moments: everything that is not a bed, collapsed and listed in time order.
    discrete = _collapse([r for r in rows if r["event_type"] not in _SUSTAINED])
    if not discrete:
        lines.append("Discrete sound events: none detected")
        return "\n".join(lines)

    kinds = {}  # type: Dict[str, int]
    for d in discrete:
        kinds[d["label"]] = kinds.get(d["label"], 0) + 1

    # The caveat is not hedging, it is the measured state of the detector. On a
    # Mandarin business podcast this layer reports Duck 41%, Sheep 30% and
    # Gargling 68% — it is mishearing talk. Two gates were tried against the
    # local corpus and both failed: speech coverage does not separate them (a
    # 99%-speech clip came back clean while a 48%-speech one was a third
    # nonsense), and neither does confidence (the real effects on a sound-design
    # reel sit at 0.16-0.24, the false animals at 0.26-0.76 — the fakes score
    # higher). Rather than ship a filter that cannot be validated, the reader is
    # told what the numbers are worth.
    lines.append(
        "Discrete sound events: %d across %d kinds "
        "(AudioSet/PANNs over 2s windows; it mislabels vocal sound as animal "
        "or human noise on speech-heavy audio, so weigh these against the "
        "transcript rather than on their own)"
        % (len(discrete), len(kinds)))

    shown = discrete[:max_events]
    for d in shown:
        lines.append("  [%.1f-%.1fs] %s (%.0f%%)"
                     % (d["start"], d["end"], d["label"], d["conf"] * 100))

    if len(discrete) > len(shown):
        # Truncating the timeline must not lose the inventory: which effects a
        # video uses is the answer to "what is its sound design", and that
        # survives even when the individual timings cannot.
        top = sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))
        lines.append("  ... %d more; all kinds by count: %s"
                     % (len(discrete) - len(shown),
                        ", ".join("%s x%d" % (k, n) for k, n in top)))

    return "\n".join(lines)
