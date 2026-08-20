from __future__ import annotations

from .base import BaseTranscriber, Segment, TranscriptResult


class FasterWhisperTranscriber(BaseTranscriber):
    def __init__(self, model: str = "large-v3") -> None:
        self._model_name = model
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                raise ImportError(
                    "faster-whisper not installed. "
                    "Install with: pip install faster-whisper"
                )
            self._model = WhisperModel(
                self._model_name,
                device="auto",
                compute_type="auto",
            )

    #: 語言探測只看開頭這麼多秒。
    _ZH_PROBE_SECONDS = 30
    #: Whisper 會吐的中文標籤。`yue` 是粵語，在使用它的地方同樣寫繁體，所以吃同一個種子。
    _ZH_TAGS = ("zh", "yue")

    def _zh_prompt_for(self, audio_path, forced_language):
        """這支片要用的繁體種子提示，或 None。

        功能關閉、音訊不是中文、或**查不出來**時一律回 None —— 那三種情況下呼叫端
        的行為跟改動前一字不差。

        **為什麼要先確定語言**：`WHISPER_LANGUAGE` 預設是 ""（自動偵測），而把中文
        提示餵給日文音訊會把解碼器往中文偏。這個語料裡就有一支 `lang=ja`，所以那不是
        假想的風險。

        **為什麼探測只取開頭 30 秒**：`detect_language` 要吃音訊陣列，而 `decode_audio`
        沒有長度參數 —— 為了判斷語言就把整支解開，會讓語料裡那支 4 小時的片變成
        ~960MB 的 float32。ffmpeg 本來就是硬依賴，所以切開頭那一段只要 1.8MB、
        約 6 秒，而且**與片長無關**。

        失敗會出聲，不會被吞掉：靜默跳過等於交回一份簡體逐字稿，卻不說明這個設定
        為什麼沒作用。
        """
        from .. import config

        if (config.WHISPER_ZH_SCRIPT or "").lower() != "traditional":
            return None
        if not config.WHISPER_ZH_PROMPT:
            return None
        if forced_language:
            # 已經指定語言就不必探測 —— 也不該探測，那是使用者的宣告。
            return (config.WHISPER_ZH_PROMPT
                    if forced_language.lower() in self._ZH_TAGS else None)
        lang = self._probe_language(audio_path)
        if lang is None:
            return None
        return config.WHISPER_ZH_PROMPT if lang.lower() in self._ZH_TAGS else None

    def _probe_language(self, audio_path):
        """開頭那一段的 Whisper 語言標籤，讀不到回 None。"""
        import subprocess

        from .. import config as cfg
        from ..utils.stderr import warn


        try:
            import numpy as np
        except ImportError:                                    # pragma: no cover
            warn("  note: 沒有 numpy — 略過繁簡種子的語言探測")
            return None
        try:
            proc = subprocess.run(
                [cfg.FFMPEG_BIN, "-v", "error", "-i", audio_path,
                 "-t", str(self._ZH_PROBE_SECONDS), "-ac", "1", "-ar", "16000",
                 "-f", "f32le", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            warn("  note: 語言探測失敗（%s）— 不加繁體種子直接轉錄" % exc)
            return None
        if proc.returncode != 0 or not proc.stdout:
            warn("  note: 語言探測取不到音訊 — 不加繁體種子直接轉錄")
            return None
        audio = np.frombuffer(proc.stdout, dtype=np.float32)
        if audio.size == 0:
            return None
        try:
            lang, _prob, _all = self._model.detect_language(audio)
        except Exception as exc:                               # noqa: BLE001
            warn("  note: 語言判定失敗（%s）— 不加繁體種子直接轉錄" % exc)
            return None
        return lang

    def transcribe(self, audio_path: str) -> TranscriptResult:
        self._ensure_model()

        from .. import config
        kwargs = {"beam_size": 5, "vad_filter": True}
        # language="" -> None so faster-whisper auto-detects
        if config.WHISPER_LANGUAGE:
            kwargs["language"] = config.WHISPER_LANGUAGE
        if config.WHISPER_TASK and config.WHISPER_TASK != "transcribe":
            kwargs["task"] = config.WHISPER_TASK
        # Per-chunk language detection for code-switching (中英對照) audio.
        # Only meaningful when language is NOT forced.
        if config.WHISPER_MULTILINGUAL and not config.WHISPER_LANGUAGE:
            kwargs["multilingual"] = True
            if config.WHISPER_CHUNK_LENGTH > 0:
                kwargs["chunk_length"] = config.WHISPER_CHUNK_LENGTH

        zh_prompt = self._zh_prompt_for(audio_path, kwargs.get("language"))
        if zh_prompt:
            kwargs["initial_prompt"] = zh_prompt
            # Forcing the language is what makes the prompt land reliably -- but not
            # in multilingual mode, where per-chunk detection is the whole point and
            # a forced language switches it off.
            if not config.WHISPER_MULTILINGUAL:
                kwargs["language"] = "zh"

        segments_iter, info = self._model.transcribe(audio_path, **kwargs)

        # Collect raw segments WITH the per-segment probabilities the guard needs
        # (no_speech_prob / avg_logprob / compression_ratio). Reducing to Segment
        # first would drop these fields and neuter whisper-guard's probability layers.
        raw = [
            {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text.strip(),
                "no_speech_prob": getattr(seg, "no_speech_prob", 0.0),
                "avg_logprob": getattr(seg, "avg_logprob", 0.0),
                "compression_ratio": getattr(seg, "compression_ratio", 1.0),
            }
            for seg in segments_iter
        ]

        kept = _apply_guard(raw)

        segments = [
            Segment(start=r["start"], end=r["end"], text=r["text"],
                    confidence=r["avg_logprob"])
            for r in kept
        ]

        return TranscriptResult(
            language=info.language,
            text_full=" ".join(r["text"] for r in kept),
            segments=segments,
            duration_sec=info.duration,
            model=self._model_name,
        )


def _apply_guard(segments):
    """Run whisper-guard's anti-hallucination filter over raw Whisper segments.

    Returns the kept segments (dicts with start/end/text + probabilities), dropping
    silence / low-logprob / high-compression / repetition / char-loop hallucinations
    — the same 4-layer guard arkiv and media-manager already run. No-op (returns the
    input) when WHISPER_GUARD_ENABLED is off, and degrades gracefully if whisper-guard
    isn't installed so transcription never hard-fails on a missing optional dep.
    """
    from .. import config
    if not config.WHISPER_GUARD_ENABLED:
        return segments
    try:
        from whisper_guard import filter_hallucinations
    except ImportError:
        # whisper-guard ships in the `whisper` extra; a partial install shouldn't
        # break transcription — just skip guarding.
        print("  [guard] whisper-guard not installed; skipping (pip install whisper-guard)")
        return segments
    return filter_hallucinations(segments)
