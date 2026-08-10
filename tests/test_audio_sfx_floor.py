"""The discrete-event floor, and the label taxonomy it depends on.

Three defects are pinned here, all of which produced a plausible-looking
`audio_events` table rather than an error:

  1. one class per window (argmax) — a window reading "Music 0.469, Sound effect
     0.400" recorded only the music, discarding the finding
  2. one threshold for everything — a discrete effect occupies a fraction of a
     2-second window and scores far below a sustained bed, so 0.3 kept the beds
     and dropped the sound design
  3. exact-match label sets — AudioSet names classes as compounds ("Violin,
     fiddle"), so every compound fell through to "sound_effect" and filed a
     string section as sound design
"""
from __future__ import annotations

import os
import struct
import tempfile
import wave

import numpy as np
import pytest

from reel_scout.audio.panns import PannsAnalyzer, _classify_label


@pytest.fixture
def one_window_wav():
    """A real 2-second WAV — one window at the default 2.0s/1.0s settings.

    The scripted session below replaces inference, not I/O: the analyzer still
    reads and frames the file, so a change that breaks the read path fails here
    instead of passing on a mock.
    """
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<%dh" % 32000, *([0] * 32000)))
    yield path
    os.unlink(path)


class _FakeSession:
    """Stands in for the ONNX session, returning a scripted per-window
    distribution so the thresholds can be tested without the 327 MB model."""

    def __init__(self, per_window):
        self._per_window = per_window
        self._i = 0

    def get_inputs(self):
        class _In:
            name = "input"
        return [_In()]

    def run(self, _outputs, _feed):
        probs = self._per_window[min(self._i, len(self._per_window) - 1)]
        self._i += 1
        return [np.array([probs], dtype=np.float32)]


def _analyzer(labels, per_window, **kw):
    a = PannsAnalyzer(model_path="/fake", window_sec=2.0, hop_sec=1.0, **kw)
    a._session = _FakeSession(per_window)
    a._labels = labels
    return a


def _probs(n, **by_index):
    v = [0.001] * n
    for i, p in by_index.items():
        v[int(i)] = p
    return v


LABELS = ["Music", "Sound effect", "Coin (dropping)", "Speech", "Violin, fiddle"]


def test_second_class_in_a_window_is_not_discarded(one_window_wav):
    """The window that motivated the change: Music 0.469, Sound effect 0.400."""
    a = _analyzer(LABELS, [_probs(5, **{"0": 0.469, "1": 0.400})])
    tl = a.analyze(one_window_wav)
    labels = [e.label for e in tl.events]
    assert "Music" in labels
    assert "Sound effect" in labels


def test_effect_below_the_dominant_threshold_still_lands(one_window_wav):
    """0.20 clears the event floor but not the 0.3 dominant threshold. Before
    the split it produced an empty table for a reel that is all sound design."""
    a = _analyzer(LABELS, [_probs(5, **{"2": 0.20, "0": 0.05})])
    tl = a.analyze(one_window_wav)
    assert [e.label for e in tl.events] == ["Coin (dropping)"]
    assert tl.events[0].event_type == "sound_effect"


def test_effect_winning_its_window_is_not_skipped(one_window_wav):
    """Regression on the first draft of the fix, which scanned ranks 1..k and so
    dropped an effect that was rank 0 — exactly the all-effects clips."""
    a = _analyzer(LABELS, [_probs(5, **{"2": 0.23})])
    tl = a.analyze(one_window_wav)
    assert [e.label for e in tl.events] == ["Coin (dropping)"]


def test_dominant_class_is_recorded_once_not_twice(one_window_wav):
    """A class clearing both floors must not appear as its own echo."""
    a = _analyzer(LABELS, [_probs(5, **{"1": 0.80})])
    tl = a.analyze(one_window_wav)
    assert [e.label for e in tl.events] == ["Sound effect"]


def test_sustained_types_do_not_get_the_lower_floor(one_window_wav):
    """Music at 0.15 is a weak guess about the bed, not an event. Letting the
    event floor apply to sustained classes would double every window."""
    a = _analyzer(LABELS, [_probs(5, **{"3": 0.55, "0": 0.15})])
    tl = a.analyze(one_window_wav)
    assert [e.label for e in tl.events] == ["Speech"]


def test_below_both_floors_is_silence_not_a_guess(one_window_wav):
    a = _analyzer(LABELS, [_probs(5, **{"0": 0.08, "1": 0.05})])
    assert a.analyze(one_window_wav).events == []


def test_top_k_bounds_how_many_classes_a_window_can_emit(one_window_wav):
    a = _analyzer(
        LABELS, [_probs(5, **{"0": 0.9, "1": 0.5, "2": 0.4})], top_k=2
    )
    labels = [e.label for e in a.analyze(one_window_wav).events]
    assert labels == ["Music", "Sound effect"]
    assert "Coin (dropping)" not in labels


@pytest.mark.parametrize("label,expected", [
    ("Violin, fiddle", "music"),
    ("Marimba, xylophone", "music"),
    ("Bass drum, kick drum", "music"),
    ("Traditional music", "music"),
    ("Music of Africa", "music"),
    ("Male speech, man speaking", "speech"),
    ("Coin (dropping)", "sound_effect"),
    ("Whoosh, swoosh, swish", "sound_effect"),
    ("Silence", "silence"),
    ("Applause", "applause"),
])
def test_compound_audioset_labels_classify_correctly(label, expected):
    assert _classify_label(label) == expected


def test_a_string_section_is_not_sound_design(one_window_wav):
    """The whole point of the taxonomy fix: a reel's backing track must not show
    up in the layer someone reads to study sound effects."""
    labels = ["Violin, fiddle", "Coin (dropping)"]
    a = _analyzer(labels, [_probs(2, **{"0": 0.85, "1": 0.20})])
    by_type = {e.label: e.event_type for e in a.analyze(one_window_wav).events}
    assert by_type["Violin, fiddle"] == "music"
    assert by_type["Coin (dropping)"] == "sound_effect"
