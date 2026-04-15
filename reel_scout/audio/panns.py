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
    """Map AudioSet label to simplified event_type."""
    if label in _MUSIC_LABELS:
        return "music"
    if label in _SPEECH_LABELS:
        return "speech"
    if label in _SILENCE_LABELS:
        return "silence"
    if label in _APPLAUSE_LABELS:
        return "applause"
    return "sound_effect"


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
    ):
        self._model_path = model_path
        self._window_sec = window_sec
        self._hop_sec = hop_sec
        self._session = None  # type: object
        self._labels = None  # type: List[str] | None

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
            top_idx = int(np.argmax(probs))
            top_conf = float(probs[top_idx])

            if top_conf > 0.3:  # confidence threshold
                label = (
                    self._labels[top_idx]  # type: ignore[index]
                    if self._labels and top_idx < len(self._labels)
                    else "unknown"
                )
                event_type = _classify_label(label)
                events.append(
                    AudioEvent(
                        event_type=event_type,
                        label=label,
                        start_sec=round(start_sec, 1),
                        end_sec=round(end_sec, 1),
                        confidence=round(top_conf, 3),
                    )
                )

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
