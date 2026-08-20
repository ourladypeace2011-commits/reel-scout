"""Transcriber kwargs building — guards the bilingual/code-switching fix.

whisper large-v3 locks language from the opening window and garbles the other
language on long code-switching files. The fix is opt-in per-chunk re-detection
(WHISPER_MULTILINGUAL=1 + WHISPER_CHUNK_LENGTH=15). These tests pin the mapping
from config -> faster-whisper transcribe() kwargs without loading a model.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from reel_scout import config
from reel_scout.transcribe.faster_whisper import FasterWhisperTranscriber, _apply_guard


class _FakeSeg:
    start = 0.0
    end = 1.0
    text = "hi"
    avg_logprob = -0.1


class _FakeInfo:
    language = "en"
    duration = 1.0


class _FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio_path, **kwargs):
        self.calls.append(kwargs)
        return iter([_FakeSeg()]), _FakeInfo()


def _run(monkeypatch, **cfg):
    defaults = dict(
        WHISPER_LANGUAGE="", WHISPER_TASK="transcribe",
        WHISPER_MULTILINGUAL=False, WHISPER_CHUNK_LENGTH=0,
    )
    defaults.update(cfg)
    for k, v in defaults.items():
        monkeypatch.setattr(config, k, v)
    t = FasterWhisperTranscriber()
    fake = _FakeModel()
    t._model = fake  # skip _ensure_model / real model load
    t.transcribe("dummy.wav")
    return fake.calls[0]


def test_defaults_reproduce_prior_behavior(monkeypatch):
    kw = _run(monkeypatch)
    assert kw == {"beam_size": 5, "vad_filter": True}
    assert "language" not in kw and "task" not in kw and "multilingual" not in kw


def test_force_language(monkeypatch):
    kw = _run(monkeypatch, WHISPER_LANGUAGE="en")
    assert kw["language"] == "en"


def test_translate_task(monkeypatch):
    kw = _run(monkeypatch, WHISPER_TASK="translate")
    assert kw["task"] == "translate"


def test_multilingual_with_chunk(monkeypatch):
    kw = _run(monkeypatch, WHISPER_MULTILINGUAL=True, WHISPER_CHUNK_LENGTH=15)
    assert kw["multilingual"] is True
    assert kw["chunk_length"] == 15


def test_multilingual_ignored_when_language_forced(monkeypatch):
    # per-chunk detection is meaningless if a single language is forced
    kw = _run(monkeypatch, WHISPER_LANGUAGE="zh", WHISPER_MULTILINGUAL=True, WHISPER_CHUNK_LENGTH=15)
    assert kw["language"] == "zh"
    assert "multilingual" not in kw


# ---------------------------------------------------------------------------
# R-guard — whisper-guard anti-hallucination filtering on the transcribe path
# ---------------------------------------------------------------------------

class _GuardSeg:
    """A faster-whisper-shaped segment carrying the per-segment probabilities the
    guard reads (the real Segment dataclass drops these)."""
    def __init__(self, start, end, text, no_speech_prob=0.0, avg_logprob=-0.1,
                 compression_ratio=1.0):
        self.start = start
        self.end = end
        self.text = text
        self.no_speech_prob = no_speech_prob
        self.avg_logprob = avg_logprob
        self.compression_ratio = compression_ratio


def _run_transcribe(monkeypatch, segs, **cfg):
    defaults = dict(
        WHISPER_LANGUAGE="", WHISPER_TASK="transcribe",
        WHISPER_MULTILINGUAL=False, WHISPER_CHUNK_LENGTH=0,
        WHISPER_GUARD_ENABLED=True,
    )
    defaults.update(cfg)
    for k, v in defaults.items():
        monkeypatch.setattr(config, k, v)

    class _Model:
        def transcribe(self, audio_path, **kwargs):
            return iter(segs), _FakeInfo()

    t = FasterWhisperTranscriber()
    t._model = _Model()
    return t.transcribe("dummy.wav")


def test_guard_drops_hallucinated_segments(monkeypatch):
    segs = [
        _GuardSeg(0.0, 2.0, "真正的語音內容在這裡。"),               # kept
        _GuardSeg(2.0, 2.8, "字幕", no_speech_prob=0.95),           # silence → dropped
        _GuardSeg(2.8, 4.5, "低信心幻覺段落", avg_logprob=-2.0),      # low logprob → dropped
    ]
    result = _run_transcribe(monkeypatch, segs)
    assert [s.text for s in result.segments] == ["真正的語音內容在這裡。"]
    # text_full is rebuilt from the kept segments, so it excludes the dropped ones
    assert result.text_full == "真正的語音內容在這裡。"


def test_guard_disabled_keeps_all_segments(monkeypatch):
    segs = [
        _GuardSeg(0.0, 2.0, "good"),
        _GuardSeg(2.0, 2.8, "noise", no_speech_prob=0.95),
    ]
    result = _run_transcribe(monkeypatch, segs, WHISPER_GUARD_ENABLED=False)
    assert [s.text for s in result.segments] == ["good", "noise"]


def test_apply_guard_degrades_when_package_missing(monkeypatch):
    # A partial install (whisper-guard absent) must not hard-fail transcription.
    monkeypatch.setitem(sys.modules, "whisper_guard", None)  # force ImportError
    monkeypatch.setattr(config, "WHISPER_GUARD_ENABLED", True)
    raw = [{"start": 0.0, "end": 1.0, "text": "hi", "no_speech_prob": 0.0,
            "avg_logprob": -0.1, "compression_ratio": 1.0}]
    assert _apply_guard(raw) == raw   # returned unchanged, no exception


def test_apply_guard_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "WHISPER_GUARD_ENABLED", False)
    raw = [{"start": 0.0, "end": 1.0, "text": "anything", "no_speech_prob": 0.99}]
    assert _apply_guard(raw) is raw   # exact same object, not even re-filtered


# --- Chinese script: Whisper normalizes zh to Simplified unless seeded -----------
#
# large-v3's Chinese training data is overwhelmingly Simplified, so it renders a
# Traditional speaker's audio in Simplified. Measured on this corpus: 9 transcripts,
# including 唐綺陽 / 貝克書 / 財鯨動向 — all Traditional creators whose *titles*
# (from yt-dlp metadata, not Whisper) are Traditional while the transcript is not.
#
# A/B through the real transcribe path on one 70s clip: 22 distinct Simplified
# characters with the seed off, 0 with it on. The seed also restored punctuation,
# which the unseeded pass omitted entirely.


def _zh_run(monkeypatch, probe_lang=None, **cfg):
    """Build kwargs with the language probe stubbed to `probe_lang`.

    Stubbing the probe (not ffmpeg) is deliberate: what needs pinning is the
    DECISION — which language gets the seed — not whether ffmpeg can be spawned.
    """
    defaults = dict(
        WHISPER_LANGUAGE="", WHISPER_TASK="transcribe",
        WHISPER_MULTILINGUAL=False, WHISPER_CHUNK_LENGTH=0,
        WHISPER_ZH_SCRIPT="traditional", WHISPER_ZH_PROMPT="以下是繁體。",
    )
    defaults.update(cfg)
    for k, v in defaults.items():
        monkeypatch.setattr(config, k, v)
    t = FasterWhisperTranscriber()
    fake = _FakeModel()
    t._model = fake
    monkeypatch.setattr(t, "_probe_language", lambda _p: probe_lang)
    t.transcribe("dummy.wav")
    return fake.calls[0]


def test_the_production_default_seeds_traditional(monkeypatch):
    """預設值本身要被釘住 —— 哪天有人改成 off，簡體就無聲回來了。"""
    assert config.WHISPER_ZH_SCRIPT == "traditional"
    assert "繁體" in config.WHISPER_ZH_PROMPT


@pytest.mark.parametrize("lang", ["zh", "ZH", "yue"])
def test_chinese_audio_gets_the_traditional_seed(monkeypatch, lang):
    """`yue`（粵語）吃同一個種子 —— 用粵語的地方同樣寫繁體。"""
    kw = _zh_run(monkeypatch, probe_lang=lang)
    assert kw["initial_prompt"] == "以下是繁體。"
    assert kw["language"] == "zh"


@pytest.mark.parametrize("lang", ["ja", "en", "ko", None])
def test_non_chinese_audio_is_left_alone(monkeypatch, lang):
    """把中文提示餵給日文音訊會把解碼器往中文偏 —— 語料裡就有一支 `lang=ja`。"""
    kw = _zh_run(monkeypatch, probe_lang=lang)
    assert "initial_prompt" not in kw
    assert "language" not in kw


def test_off_restores_the_previous_raw_behavior(monkeypatch):
    """簡體語料要能關掉 —— 對簡體創作者，簡體才是忠實的呈現。"""
    kw = _zh_run(monkeypatch, probe_lang="zh", WHISPER_ZH_SCRIPT="off")
    assert kw == {"beam_size": 5, "vad_filter": True}


def test_an_explicitly_forced_language_is_not_probed(monkeypatch):
    """使用者指定了語言就是宣告，不該再花一次探測去質疑它。"""
    probed = []

    for k, v in dict(WHISPER_LANGUAGE="zh", WHISPER_TASK="transcribe",
                     WHISPER_MULTILINGUAL=False, WHISPER_CHUNK_LENGTH=0,
                     WHISPER_ZH_SCRIPT="traditional",
                     WHISPER_ZH_PROMPT="以下是繁體。").items():
        monkeypatch.setattr(config, k, v)
    t = FasterWhisperTranscriber()
    fake = _FakeModel()
    t._model = fake
    monkeypatch.setattr(t, "_probe_language",
                        lambda p: probed.append(p) or "ja")
    t.transcribe("dummy.wav")
    assert probed == [], "指定了 zh 還去探測，而且探到 ja 會反過來取消種子"
    assert fake.calls[0]["initial_prompt"] == "以下是繁體。"


def test_a_forced_non_chinese_language_gets_no_seed(monkeypatch):
    kw = _zh_run(monkeypatch, probe_lang="zh", WHISPER_LANGUAGE="ja")
    assert "initial_prompt" not in kw
    assert kw["language"] == "ja"


def test_multilingual_keeps_per_chunk_detection(monkeypatch):
    """multilingual 的整個重點是逐段重判語言，強制語言會把它關掉。

    所以那個模式下只下種子、不鎖語言 —— 兩個功能都要活著。
    """
    kw = _zh_run(monkeypatch, probe_lang="zh",
                 WHISPER_MULTILINGUAL=True, WHISPER_CHUNK_LENGTH=15)
    assert kw["initial_prompt"] == "以下是繁體。"
    assert kw["multilingual"] is True
    assert "language" not in kw, "鎖了語言就等於關掉 multilingual"


def test_an_unreadable_probe_degrades_to_the_old_behavior(monkeypatch):
    """探不到語言就照舊轉錄 —— 但不能靜默，否則交回簡體卻不說為什麼。"""
    warned = []
    kw = _zh_run(monkeypatch, probe_lang=None)
    assert "initial_prompt" not in kw

    # 真的走 ffmpeg 那條路：路徑不存在 → 出聲 + 回 None
    import reel_scout.utils.stderr as stderr_mod
    monkeypatch.setattr(stderr_mod, "warn", lambda m: warned.append(m))
    t = FasterWhisperTranscriber()
    assert t._probe_language("/definitely/not/here.mp4") is None
    assert warned and any("繁體種子" in w for w in warned), warned


@pytest.mark.parametrize("boom, why", [
    (OSError("ffmpeg not found"), "ffmpeg 不在"),
    (subprocess.SubprocessError("timed out"), "ffmpeg 逾時／崩掉"),
])
def test_every_probe_failure_path_says_something(monkeypatch, boom, why):
    """三條失敗路徑各自要出聲，不能只有一條有。

    變異測試抓到的：只測「路徑不存在」那條，`subprocess.run` 真的拋例外那條的
    `warn` 拿掉照樣全綠。而靜默的下場是交回一份簡體逐字稿、卻不說明設定為什麼
    沒作用 —— 使用者會以為它壞了，而不是知道探測沒跑成。
    """
    warned = []
    import reel_scout.utils.stderr as stderr_mod
    monkeypatch.setattr(stderr_mod, "warn", lambda m: warned.append(m))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(boom))

    t = FasterWhisperTranscriber()
    assert t._probe_language("whatever.mp4") is None, why
    assert warned and any("繁體種子" in w for w in warned), (why, warned)


def test_a_probe_that_returns_no_audio_says_something(monkeypatch):
    """ffmpeg 成功但吐 0 bytes（例如無音軌的片）也要出聲。"""
    warned = []
    import reel_scout.utils.stderr as stderr_mod
    monkeypatch.setattr(stderr_mod, "warn", lambda m: warned.append(m))

    class _Empty:
        returncode = 0
        stdout = b""
        stderr = b""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Empty())
    t = FasterWhisperTranscriber()
    assert t._probe_language("silent.mp4") is None
    assert warned and any("繁體種子" in w for w in warned), warned


def test_a_detect_language_crash_says_something(monkeypatch):
    """第三條失敗路徑：ffmpeg 給了音訊，但 `detect_language` 自己炸了。

    三條都補完才算把「探測失敗要出聲」這件事釘住 —— 前兩條補完時這一條仍然存活，
    也就是說「有一條測到」不等於「這個性質被守住」。
    """
    warned = []
    import reel_scout.utils.stderr as stderr_mod
    monkeypatch.setattr(stderr_mod, "warn", lambda m: warned.append(m))

    class _Ok:
        returncode = 0
        stdout = b"\x00\x00\x80\x3f" * 100
        stderr = b""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Ok())

    class _Boom:
        def detect_language(self, audio):
            raise RuntimeError("ctranslate2 exploded")
    t = FasterWhisperTranscriber()
    t._model = _Boom()
    assert t._probe_language("x.mp4") is None
    assert warned and any("繁體種子" in w for w in warned), warned
