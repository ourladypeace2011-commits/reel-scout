from __future__ import annotations

import os
import shutil

import pytest

from reel_scout.transcribe import find_subtitle, is_translated_track
from reel_scout.transcribe.vtt import _lang_from_path, _parse_ts, parse_vtt


@pytest.fixture
def workdir():
    # The global pytest tmp_path is unwritable on this PC (sandbox ACL), so use a
    # self-managed dir next to the test file instead.
    d = os.path.join(os.path.dirname(__file__), "_vtt_tmp")
    if os.path.exists(d):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)

SAMPLE = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000 align:start position:0%
hello everyone

00:00:02.000 --> 00:00:04.000 align:start position:0%
hello everyone<00:00:02.500><c> and</c><00:00:03.000><c> welcome</c>

00:00:04.000 --> 00:00:06.000
and welcome

00:00:06.000 --> 00:00:08.000
to the show today

00:00:08.000 --> 00:00:10.000
to the show today
"""


class TestParseTs:
    def test_hms(self) -> None:
        assert abs(_parse_ts("00:00:02.500") - 2.5) < 1e-6
        assert abs(_parse_ts("01:02:03.000") - 3723.0) < 1e-6

    def test_ms(self) -> None:
        assert abs(_parse_ts("01:02.250") - 62.25) < 1e-6

    def test_comma_decimal(self) -> None:
        assert abs(_parse_ts("00:00:01,500") - 1.5) < 1e-6


class TestLangFromPath:
    def test_lang_extraction(self) -> None:
        assert _lang_from_path("/x/yt_abc.en.vtt") == "en"
        assert _lang_from_path("/x/yt_abc.zh-Hant.vtt") == "zh-Hant"
        assert _lang_from_path("/x/yt_abc.vtt") == ""


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class TestParseVtt:
    def test_parse_and_dedupe(self, workdir) -> None:
        p = os.path.join(workdir, "sample.en.vtt")
        _write(p, SAMPLE)
        r = parse_vtt(p)
        assert r.language == "en"
        assert r.model == "native-subtitles"
        assert r.duration_sec == 8.0
        # rolling duplicates collapsed
        assert r.text_full.count("hello everyone") == 1
        assert r.text_full.count("to the show today") == 1
        assert "welcome" in r.text_full
        # inline <...> tags stripped
        assert "<" not in r.text_full

    def test_empty_file(self, workdir) -> None:
        p = os.path.join(workdir, "empty.en.vtt")
        _write(p, "WEBVTT\n\n")
        r = parse_vtt(p)
        assert r.segments == []
        assert r.text_full == ""


class TestFindSubtitle:
    def test_prefers_english(self, workdir) -> None:
        video = os.path.join(workdir, "yt_abc.mp4")
        _write(video, "")
        _write(os.path.join(workdir, "yt_abc.zh.vtt"), "WEBVTT\n")
        _write(os.path.join(workdir, "yt_abc.en.vtt"), "WEBVTT\n")
        found = find_subtitle(video)
        assert found is not None
        assert found.endswith(".en.vtt")

    def test_none_when_absent(self, workdir) -> None:
        video = os.path.join(workdir, "yt_abc.mp4")
        _write(video, "")
        assert find_subtitle(video) is None

    def test_none_for_empty_path(self) -> None:
        assert find_subtitle("") is None

    def test_falls_back_to_any_vtt(self, workdir) -> None:
        video = os.path.join(workdir, "yt_abc.mp4")
        _write(video, "")
        _write(os.path.join(workdir, "yt_abc.fr.vtt"), "WEBVTT\n")
        found = find_subtitle(video)
        assert found is not None
        assert found.endswith(".fr.vtt")


class TestTranslatedTrackDetection:
    """YouTube names auto-translated tracks ``<target>-<source>``.

    `yt-dlp --list-subs` on a Taiwanese video lists the original as a bare
    ``zh-TW`` with no display name, then ~100 translations of it — ``en-zh-TW``
    shows as "English from Chinese (Taiwan)". The distinguishing feature is that a
    translated code carries a *second language* subtag, which a plain locale never
    does.
    """

    @pytest.mark.parametrize(
        "lang",
        ["zh-TW", "zh-Hant", "zh", "en", "en-US", "pt-BR", "en-orig", "es-419"],
    )
    def test_originals_are_not_translations(self, lang: str) -> None:
        assert not is_translated_track(lang), f"{lang} is an original track"

    @pytest.mark.parametrize(
        "lang",
        ["en-zh-TW", "zh-Hant-zh-TW", "zh-Hans-zh-TW", "bho-zh-TW", "ja-en"],
    )
    def test_translations_are_detected(self, lang: str) -> None:
        assert is_translated_track(lang), f"{lang} is a machine translation"


class TestFindSubtitleSkipsTranslations:
    """Regression: a Mandarin video came back transcribed into English.

    The download step asks for ``--sub-langs en.*,zh.*``, which matches the
    translated ``en-zh-TW`` track, and the old preference order tried ``en``
    first — so the English machine translation beat the ``zh-TW`` original and
    nothing in the output said so. The stored transcript for one 壹加壹 video
    opened "I'm so nervous!" for a sentence nobody spoke in English.
    """

    def test_original_beats_translation_even_when_translation_is_preferred_lang(
        self, workdir
    ) -> None:
        video = os.path.join(workdir, "yt_abc.mp4")
        _write(video, "")
        # Exactly the pair that shipped the bug: preferred language, but only as a
        # translation, against the video's real language.
        _write(os.path.join(workdir, "yt_abc.en-zh-TW.vtt"), "WEBVTT\n")
        _write(os.path.join(workdir, "yt_abc.zh-TW.vtt"), "WEBVTT\n")
        found = find_subtitle(video)
        assert found is not None
        assert found.endswith(".zh-TW.vtt"), (
            "picked the machine translation over the original track"
        )

    def test_returns_none_when_only_translations_exist(self, workdir) -> None:
        """Whisper on the real audio beats somebody else's translation."""
        video = os.path.join(workdir, "yt_abc.mp4")
        _write(video, "")
        _write(os.path.join(workdir, "yt_abc.en-zh-TW.vtt"), "WEBVTT\n")
        _write(os.path.join(workdir, "yt_abc.ja-zh-TW.vtt"), "WEBVTT\n")
        assert find_subtitle(video) is None

    def test_says_so_when_it_declines(self, workdir, capsys) -> None:
        """Silently falling back is how this went unnoticed for 110 videos."""
        video = os.path.join(workdir, "yt_abc.mp4")
        _write(video, "")
        _write(os.path.join(workdir, "yt_abc.en-zh-TW.vtt"), "WEBVTT\n")
        find_subtitle(video)
        out = capsys.readouterr().out
        assert "auto-translated" in out and "en-zh-TW" in out

    def test_en_orig_is_still_usable(self, workdir) -> None:
        """yt-dlp's own ``-orig`` marker is an original, not a translation."""
        video = os.path.join(workdir, "yt_abc.mp4")
        _write(video, "")
        _write(os.path.join(workdir, "yt_abc.en-orig.vtt"), "WEBVTT\n")
        found = find_subtitle(video)
        assert found is not None
        assert found.endswith(".en-orig.vtt")
