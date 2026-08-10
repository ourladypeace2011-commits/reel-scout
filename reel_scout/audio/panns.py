from __future__ import annotations

import os
import struct
import wave
from typing import Dict, List, Set, Tuple

from .base import AudioEvent, AudioTimeline, BaseAudioAnalyzer

# AudioSet label categories (simplified mapping)
_MUSIC_LABELS = {
    "Music",
    "Musical instrument",
    "Singing",
    "Song",
    "Guitar",
    "Acoustic guitar",
    "Electric guitar",
    "Piano",
    "Keyboard (musical)",
    "Drum",
    "Drum kit",
    "Bass guitar",
    "Violin",
    "Trumpet",
    "Flute",
    "Saxophone",
    "Hip hop music",
    "Pop music",
    "Rock music",
    "Electronic music",
    "Jazz",
    # Instruments the local corpus actually surfaced while being filed as
    # "sound_effect". Each one was a scored reel's backing track, not its sound
    # design, and left in place they drown the layer they were polluting.
    "Percussion",
    "Marimba",
    "Xylophone",
    "Vibraphone",
    "Glockenspiel",
    "Sitar",
    "Banjo",
    "Mandolin",
    "Ukulele",
    "Harp",
    "Cello",
    "Double bass",
    "Viola",
    "Fiddle",
    "Organ",
    "Electric piano",
    "Synthesizer",
    "Sampler",
    "Harmonica",
    "Accordion",
    "Bass drum",
    "Snare drum",
    "Hi-hat",
    "Cymbal",
    "Tambourine",
    "Drum machine",
    "Drum and bass",
    "Bass (instrument role)",
    "Plucked string instrument",
    "Brass instrument",
    "Wind instrument, woodwind instrument",
    "String section",
    "Orchestra",
    "Choir",
    "Chant",
    "Melody",
    "Theme music",
    "Soundtrack music",
    "Background music",
    "Ambient music",
    "Techno",
    "House music",
    "Dance music",
    "Trance music",
    "Drum roll",
}  # type: Set[str]

_SPEECH_LABELS = {
    "Speech",
    "Narration, monologue",
    "Conversation",
    "Male speech, man speaking",
    "Female speech, woman speaking",
    "Child speech, kid speaking",
    "Whispering",
}  # type: Set[str]

_SILENCE_LABELS = {
    "Silence",
}  # type: Set[str]

_APPLAUSE_LABELS = {
    "Applause",
    "Clapping",
    "Cheering",
    "Crowd",
    "Laughter",
}  # type: Set[str]


def _classify_label(label: str) -> str:
    """Map AudioSet label to simplified event_type.

    Matching is on the full label *and* on its first comma-separated segment.
    AudioSet names many classes as compounds — "Violin, fiddle", "Marimba,
    xylophone", "Bass drum, kick drum" — and exact-match alone sent every one of
    them to "sound_effect", so a string quartet came back looking like sound
    design. The sets below carry the head terms; the segment split covers the
    rest of each compound.
    """
    for candidate in (label, label.split(",")[0].strip()):
        if candidate in _MUSIC_LABELS:
            return "music"
        if candidate in _SPEECH_LABELS:
            return "speech"
        if candidate in _SILENCE_LABELS:
            return "silence"
        if candidate in _APPLAUSE_LABELS:
            return "applause"
    # AudioSet's music branch is wide — every genre and region gets its own
    # class ("Traditional music", "Christian music", "Music of Africa"). Naming
    # them individually is a losing game; they all say "music" on the tin.
    if "music" in label.lower():
        return "music"
    return "sound_effect"


#: Event types that sustain across a clip rather than happening at a moment.
#: They win argmax almost every window, which is why a discrete effect needs its
#: own, lower floor to survive at all.
_SUSTAINED_TYPES = frozenset(("music", "speech", "silence"))


def _read_wav_samples(wav_path: str) -> Tuple[List[float], int]:
    """Read WAV file and return normalized float samples + sample rate."""
    with wave.open(wav_path, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    # Convert to float -1.0 to 1.0
    if sampwidth == 2:
        fmt = "<%dh" % (len(raw) // 2)
        samples = struct.unpack(fmt, raw)
        max_val = 32768.0
    elif sampwidth == 4:
        fmt = "<%di" % (len(raw) // 4)
        samples = struct.unpack(fmt, raw)
        max_val = 2147483648.0
    else:
        raise ValueError("Unsupported sample width: %d" % sampwidth)

    # If stereo, take first channel
    if n_channels == 2:
        samples = samples[::2]

    return [s / max_val for s in samples], framerate


class PannsAnalyzer(BaseAudioAnalyzer):
    def __init__(
        self,
        model_path: str = "",
        window_sec: float = 2.0,
        hop_sec: float = 1.0,
        min_conf: float = 0.3,
        event_min_conf: float = 0.12,
        top_k: int = 3,
    ):
        self._model_path = model_path
        self._window_sec = window_sec
        self._hop_sec = hop_sec
        self._min_conf = min_conf
        self._event_min_conf = event_min_conf
        self._top_k = top_k
        self._session = None  # type: object
        self._labels = None  # type: List[str] | None

    def _label_at(self, idx: int) -> str:
        if self._labels and idx < len(self._labels):
            return self._labels[idx]
        return "unknown"

    @staticmethod
    def _event(label: str, start_sec: float, end_sec: float, conf: float) -> AudioEvent:
        return AudioEvent(
            event_type=_classify_label(label),
            label=label,
            start_sec=round(start_sec, 1),
            end_sec=round(end_sec, 1),
            confidence=round(conf, 3),
        )

    def _ensure_model(self) -> None:
        if self._session is not None:
            return
        try:
            import onnxruntime
        except ImportError:
            raise ImportError(
                "onnxruntime not installed. Install with: pip install onnxruntime"
            )
        if not self._model_path or not os.path.exists(self._model_path):
            raise FileNotFoundError(
                "PANNs model not found at: %s. "
                "Download from https://zenodo.org/record/3987831"
                % self._model_path
            )
        self._session = onnxruntime.InferenceSession(self._model_path)
        # Load labels (527 AudioSet classes)
        self._labels = self._load_labels()

    def _load_labels(self) -> List[str]:
        """Load AudioSet class labels. Bundled as a simple list."""
        labels_path = os.path.join(
            os.path.dirname(self._model_path), "class_labels.txt"
        )
        if os.path.exists(labels_path):
            with open(labels_path, "r", encoding="utf-8") as f:
                return [line.strip() for line in f if line.strip()]
        # Fallback: return indexed labels
        return ["class_%d" % i for i in range(527)]

    def analyze(self, audio_path: str) -> AudioTimeline:
        self._ensure_model()
        samples, sr = _read_wav_samples(audio_path)
        duration = len(samples) / sr

        window_samples = int(self._window_sec * sr)
        hop_samples = int(self._hop_sec * sr)

        events = []  # type: List[AudioEvent]
        pos = 0
        while pos + window_samples <= len(samples):
            chunk = samples[pos : pos + window_samples]
            start_sec = pos / sr
            end_sec = (pos + window_samples) / sr

            # Run inference — numpy only needed here
            import numpy as np

            input_array = np.array([chunk], dtype=np.float32)
            input_name = self._session.get_inputs()[0].name  # type: ignore[union-attr]
            output = self._session.run(None, {input_name: input_array})  # type: ignore[union-attr]

            # output[0] shape: (1, 527) — probabilities for each class
            probs = output[0][0]
            ranked = np.argsort(probs)[::-1][: max(1, self._top_k)]

            # The dominant class carries the coverage timeline (is this stretch
            # music, speech, or silence). Unchanged behaviour.
            top_idx = int(ranked[0])
            top_conf = float(probs[top_idx])
            emitted = None
            if top_conf > self._min_conf:
                emitted = self._label_at(top_idx)
                events.append(self._event(emitted, start_sec, end_sec, top_conf))

            # A discrete effect rarely wins the window it happens in — the bed
            # behind it scores higher — so scan the top-k on its own, lower
            # floor. Without this the layer reports "Music, Music, Music" for a
            # reel whose whole point is its sound design.
            #
            # This starts at rank 0, not rank 1: on a reel that is *only* sound
            # design the effect often does win its window, just at 0.2 rather
            # than 0.3, and skipping the winner dropped exactly those — the
            # clips the layer most needed to describe came back empty. `emitted`
            # keeps it from being recorded twice when it cleared both floors.
            for idx in ranked:
                conf = float(probs[int(idx)])
                if conf < self._event_min_conf:
                    continue
                label = self._label_at(int(idx))
                if label == emitted:
                    continue
                if _classify_label(label) in _SUSTAINED_TYPES:
                    continue
                events.append(self._event(label, start_sec, end_sec, conf))

            pos += hop_samples

        # Merge adjacent events of same type
        merged = _merge_adjacent(events)

        # Compute statistics
        return _build_timeline(merged, duration)


def _merge_adjacent(events: List[AudioEvent]) -> List[AudioEvent]:
    """Merge consecutive events with same event_type."""
    if not events:
        return []
    merged = [events[0]]
    for ev in events[1:]:
        prev = merged[-1]
        if (
            ev.event_type == prev.event_type
            and abs(ev.start_sec - prev.end_sec) < 0.5
        ):
            # Extend previous event
            merged[-1] = AudioEvent(
                event_type=prev.event_type,
                label=prev.label,
                start_sec=prev.start_sec,
                end_sec=ev.end_sec,
                confidence=max(prev.confidence, ev.confidence),
            )
        else:
            merged.append(ev)
    return merged


def _build_timeline(
    events: List[AudioEvent], duration: float
) -> AudioTimeline:
    """Build AudioTimeline from merged events."""
    if duration <= 0:
        return AudioTimeline(events=events, duration_sec=duration)

    music_sec = sum(
        e.end_sec - e.start_sec for e in events if e.event_type == "music"
    )
    silence_sec = sum(
        e.end_sec - e.start_sec for e in events if e.event_type == "silence"
    )

    # Count total seconds per type
    type_durations = {}  # type: Dict[str, float]
    for e in events:
        d = e.end_sec - e.start_sec
        type_durations[e.event_type] = type_durations.get(e.event_type, 0.0) + d

    dominant = (
        max(type_durations, key=type_durations.get)  # type: ignore[arg-type]
        if type_durations
        else ""
    )

    return AudioTimeline(
        events=events,
        has_music=music_sec > 0,
        music_ratio=round(music_sec / duration, 3),
        silence_ratio=round(silence_sec / duration, 3),
        dominant_audio_type=dominant,
        duration_sec=round(duration, 1),
    )
