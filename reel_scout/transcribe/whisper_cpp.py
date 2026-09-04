from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import List, Optional

from ..utils.stderr import tail_stderr

from .base import BaseTranscriber, Segment, TranscriptResult

#: Upstream renamed the CLI to `whisper-cli`; the Homebrew formula ships only
#: that name. `whisper` and `main` are kept for older builds still in the wild.
_BINARY_CANDIDATES = ("whisper-cli", "whisper", "main")

#: ffmpeg only has to decode and downsample, but a 3.6-hour livestream is a real
#: input here -- the old flat 120s was chosen when the corpus was short reels.
_FFMPEG_TIMEOUT_FLOOR = 900.0

#: whisper.cpp on Metal runs comfortably faster than realtime, but "comfortably"
#: is not a contract. Budget 2x the audio and never less than 10 minutes, so a
#: slow first run (model load, cold cache) does not get killed and reported as
#: a transcription failure.
_TRANSCRIBE_TIMEOUT_FLOOR = 600.0
_TRANSCRIBE_TIMEOUT_FACTOR = 2.0


def resolve_binary(explicit: Optional[str] = None) -> Optional[str]:
    """Absolute path to a usable whisper.cpp CLI, or None.

    An explicit name is honoured as given (it may be a path); otherwise the
    known upstream names are tried in order.
    """
    if explicit:
        return shutil.which(explicit) or (explicit if os.path.isfile(explicit) else None)
    for name in _BINARY_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


class WhisperCppTranscriber(BaseTranscriber):
    """whisper.cpp backend — **experimental, and it had never actually run**.

    Three independent defects, all found 2026-09-04 by reading
    ``whisper-cli --help`` against this file. None of them was reachable by the
    test suite, and none of them would surface until someone selected this
    backend on a long file and waited:

    1. ``--output-json`` writes ``<wav>.json``; it does **not** print to stdout.
       The previous code did ``json.loads(result.stdout)`` and could only ever
       have raised ``JSONDecodeError``.
    2. ``-m`` takes a **model file path** (``models/ggml-large-v3.bin``), but
       :func:`get_transcriber` hands it ``config.WHISPER_MODEL`` — ``"large-v3"``,
       a bare name whisper.cpp cannot resolve.
    3. The default binary name was ``"whisper"``; upstream renamed it to
       ``whisper-cli``.

    Two capability gaps against the faster-whisper backend. These are **not
    bugs**, but they change the output, so callers must know:

    * **No Traditional-Chinese decoder seed** → Chinese comes back Simplified.
    * **No whisper-guard** → hallucination filtering does not happen.

    Why this class refuses to start rather than trying harder: there is no ggml
    model on the machine this was written on, so a speculative repair could not
    be verified end to end — and shipping an unverified repair of something that
    never worked just moves the trap. :meth:`preflight` names every missing
    precondition at once; a loud stop beats a backend that looks selectable and
    is not.
    """

    def __init__(self, model: str = "", binary: str = "") -> None:
        from .. import config

        self._model = model or getattr(config, "WHISPER_CPP_MODEL", "") or ""
        self._binary_name = binary or getattr(config, "WHISPER_CPP_BIN", "") or ""
        self._binary = resolve_binary(self._binary_name or None)

    # -- preconditions -----------------------------------------------------

    def preflight(self) -> List[str]:
        """Every unmet precondition, as human sentences. Empty list = ready."""
        problems: List[str] = []
        if not self._binary:
            tried = self._binary_name or "/".join(_BINARY_CANDIDATES)
            problems.append(
                "whisper.cpp binary not found (tried %s). "
                "Install it (`brew install whisper-cpp`) or set WHISPER_CPP_BIN "
                "to the executable." % tried)
        if not self._model:
            problems.append(
                "no model file configured. whisper.cpp takes a **path** to a "
                "ggml file, not a model name -- set WHISPER_CPP_MODEL="
                "/path/to/ggml-large-v3.bin. (WHISPER_MODEL, e.g. 'large-v3', "
                "is the faster-whisper name and cannot be used here.)")
        elif not os.path.isfile(self._model):
            problems.append(
                "model file does not exist: %s. Download one from "
                "huggingface.co/ggerganov/whisper.cpp." % self._model)
        return problems

    def _require_ready(self) -> None:
        problems = self.preflight()
        if problems:
            raise RuntimeError(
                "whisper-cpp backend is not usable:\n" +
                "\n".join("  - " + p for p in problems) +
                "\n  Note: this backend also has no Traditional-Chinese seed and "
                "does not run whisper-guard; faster-whisper does both.")

    # -- work --------------------------------------------------------------

    def transcribe(self, audio_path: str) -> TranscriptResult:
        from .. import config
        from ..ffprobe import probe_duration

        self._require_ready()

        duration = probe_duration(audio_path) or 0.0
        tmp_wav = None
        wav_path = audio_path

        if not audio_path.endswith(".wav"):
            handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            handle.close()
            tmp_wav = handle.name
            wav_path = tmp_wav
            # rc was previously ignored: a failed convert left an empty file and
            # whisper.cpp was handed silence, which reads as "the audio had no
            # speech" rather than "the conversion broke".
            conv = subprocess.run(
                [config.FFMPEG_BIN, "-i", audio_path, "-ar", "16000", "-ac", "1",
                 "-y", wav_path],
                capture_output=True, text=True,
                timeout=max(_FFMPEG_TIMEOUT_FLOOR, duration),
            )
            if conv.returncode != 0:
                raise RuntimeError(
                    "ffmpeg could not convert %s to 16k mono wav: %s"
                    % (audio_path, tail_stderr(conv.stderr)))

        json_path = wav_path + ".json"
        try:
            timeout = max(_TRANSCRIBE_TIMEOUT_FLOOR,
                          duration * _TRANSCRIBE_TIMEOUT_FACTOR)
            result = subprocess.run(
                [self._binary, "-m", self._model, "-f", wav_path,
                 "--output-json", "-l", "auto"],
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "whisper.cpp failed: %s" % tail_stderr(result.stderr))

            # `--output-json` writes a sidecar file. Reading stdout here is the
            # bug that made this backend dead on arrival.
            if not os.path.isfile(json_path):
                raise RuntimeError(
                    "whisper.cpp reported success but wrote no JSON at %s -- "
                    "check that this build supports --output-json." % json_path)
            with open(json_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)

            segments = []
            texts = []
            for seg in data.get("transcription", []):
                text = (seg.get("text") or "").strip()
                if not text:
                    continue
                offsets = seg.get("offsets", {}) or {}
                segments.append(Segment(
                    start=offsets.get("from", 0) / 1000.0,
                    end=offsets.get("to", 0) / 1000.0,
                    text=text,
                ))
                texts.append(text)

            lang = (data.get("result", {}) or {}).get("language", "")
            return TranscriptResult(
                language=lang,
                text_full=" ".join(texts),
                segments=segments,
                # 0.0 used to be hardcoded; downstream overrun detection compares
                # transcript end against this, so a zero here disables that check.
                duration_sec=duration,
                model=os.path.basename(self._model) or self._model,
            )
        finally:
            if os.path.exists(json_path):
                os.unlink(json_path)
            if tmp_wav and os.path.exists(tmp_wav):
                os.unlink(tmp_wav)
