"""The merge prompt's audio block: summarised, not dumped.

`merger` joined every audio row into the prompt verbatim. At one row per second
of runtime that is fine for a 20-second reel and ruinous for an 82-minute
interview — 5,230 rows rendered as ~200 KB, which does not error, it pushes the
transcript and the on-screen text out of the model's window.
"""
from __future__ import annotations

from reel_scout.analyze.audio_summary import _collapse, _merge_intervals, summarize


def _ev(kind, label, start, end, conf=0.5):
    return {"event_type": kind, "label": label, "start_sec": start,
            "end_sec": end, "confidence": conf}


def test_no_events_keeps_the_existing_sentinel():
    assert summarize([], 30.0) == "(no audio analysis)"


def test_overlapping_windows_are_counted_once():
    """2s windows on a 1s hop overlap by design. Summing end-start would report
    a 10-second clip as 20 seconds of speech — a number that cannot be true."""
    events = [_ev("speech", "Speech", float(i), float(i + 2)) for i in range(9)]
    out = summarize(events, 10.0)
    assert "speech 100%" in out


def test_coverage_is_a_share_of_the_clip_not_of_the_events():
    events = [_ev("music", "Music", 0.0, 2.0), _ev("music", "Music", 1.0, 3.0)]
    out = summarize(events, 12.0)
    assert "music 25%" in out


def test_a_held_effect_is_one_event_not_one_per_window():
    """A one-second hop reports a held sound many times over. Listing each
    window says it happened N times when it happened once."""
    events = [_ev("sound_effect", "Whoosh", float(i), float(i + 2)) for i in range(5)]
    out = summarize(events, 20.0)
    assert "Discrete sound events: 1 across 1 kinds" in out
    assert out.count("Whoosh") == 1
    assert "[0.0-6.0s] Whoosh" in out


def test_beds_never_appear_as_discrete_events():
    events = [_ev("speech", "Speech", float(i), float(i + 2)) for i in range(30)]
    out = summarize(events, 32.0)
    assert "Discrete sound events: none detected" in out
    assert "[0.0-" not in out


def test_effects_are_listed_with_time_and_confidence():
    events = [
        _ev("sound_effect", "Coin (dropping)", 9.0, 11.0, 0.20),
        _ev("sound_effect", "Camera", 11.0, 13.0, 0.16),
    ]
    out = summarize(events, 20.0)
    assert "[9.0-11.0s] Coin (dropping) (20%)" in out
    assert "[11.0-13.0s] Camera (16%)" in out


def test_truncation_keeps_the_inventory():
    """Cutting the timeline must not lose which effects a video uses — that is
    the answer to 'what is its sound design', and it survives when the
    individual timings cannot."""
    events = []
    for i in range(60):
        events.append(_ev("sound_effect", "Whoosh" if i % 2 else "Thud",
                          float(i * 10), float(i * 10 + 2)))
    out = summarize(events, 700.0, max_events=10)
    assert "... 50 more" in out
    assert "all kinds by count:" in out
    assert "Whoosh x30" in out
    assert "Thud x30" in out


def test_long_form_collapses_from_kilobytes_to_a_readable_block():
    """The defect in one assertion: an hour of talk must not become 100 KB of
    prompt."""
    events = [_ev("speech", "Speech", float(i), float(i + 2)) for i in range(3600)]
    old = "\n".join(
        "[%.1fs-%.1fs] %s: %s (%.0f%%)"
        % (e["start_sec"], e["end_sec"], e["event_type"], e["label"],
           e["confidence"] * 100) for e in events)
    new = summarize(events, 3602.0)
    assert len(old) > 100_000
    assert len(new) < 200


def test_the_detector_caveat_travels_with_the_numbers():
    """The layer mishears speech as animal noise and neither confidence nor
    speech coverage separates the two, so the reader is told what the numbers
    are worth rather than handed a filter that cannot be validated."""
    out = summarize([_ev("sound_effect", "Quack", 1.0, 3.0, 0.41)], 30.0)
    assert "mislabels" in out
    assert "Quack" in out


def test_merge_intervals_handles_gaps_and_containment():
    assert _merge_intervals([(0, 2), (1, 3)]) == 3.0
    assert _merge_intervals([(0, 2), (5, 7)]) == 4.0
    assert _merge_intervals([(0, 10), (2, 4)]) == 10.0
    assert _merge_intervals([]) == 0.0
    assert _merge_intervals([(5, 5)]) == 0.0


def test_collapse_does_not_merge_different_labels():
    rows = [_ev("sound_effect", "Thud", 0.0, 2.0),
            _ev("sound_effect", "Whoosh", 1.0, 3.0)]
    assert len(_collapse(rows)) == 2


def test_collapse_does_not_bridge_a_real_gap():
    """Same label seventeen seconds apart is two events, not one long one."""
    rows = [_ev("sound_effect", "Thud", 0.0, 2.0),
            _ev("sound_effect", "Thud", 19.0, 21.0)]
    spans = _collapse(rows)
    assert len(spans) == 2
