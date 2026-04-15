from __future__ import annotations

import json
import os
import subprocess
import tempfile

from .base import BaseTranscriber, Segment, TranscriptResult


class WhisperCppTranscriber(BaseTranscriber):
    def __init__(self, model: str = "large-v3", binary: str = "whisper") -> None:
        self._model = model
        self._binary = binary

    def transcribe(self, audio_path: str) -> TranscriptResult:
        # whisper.cpp expects WAV input — convert if needed
        wav_path = audio_path
        tmp_wav = None
        if not audio_path.endswith(".wav"):
            tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp_wav.close()
            wav_path = tmp_wav.name
            subprocess.run(
                ["ffmpeg", "-i", audio_path, "-ar", "16000", "-ac", "1",
                 "-y", wav_path],
                capture_output=True, timeout=120,
            )

        try:
            # Run whisper.cpp with JSON output
            result = subprocess.run(
                [self._binary, "-m", self._model, "-f", wav_path,
                 "--output-json", "-l", "auto"],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                raise RuntimeError(f"whisper.cpp failed: {result.stderr[:500]}")

            # Parse JSON output
            data = json.loads(result.stdout)
            segments = []
            texts = []
            for seg in data.get("transcription", []):
                text = seg.get("text", "").strip()
                if text:
                    segments.append(Segment(
                        start=seg.get("offsets", {}).get("from", 0) / 1000.0,
                        end=seg.get("offsets", {}).get("to", 0) / 1000.0,
                        text=text,
                    ))
                    texts.append(text)

            lang = data.get("result", {}).get("language", "")
            return TranscriptResult(
                language=lang,
                text_full=" ".join(texts),
                segments=segments,
                duration_sec=0.0,
                model=self._model,
            )
        finally:
            if tmp_wav and os.path.exists(wav_path):
                os.unlink(wav_path)
