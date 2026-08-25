"""TikTokCrawler download contract.

This file did not exist until 2026-08-25. That is the whole reason the crawler
sat on a bare ``-f "bestvideo+bestaudio/best"`` -- no codec condition, not even
the ``av01`` exclusion ``youtube.py`` carried -- through the entire audit that
found 103 of 109 library files unplayable on an iPad. It went unnoticed because
zero clips in that library came from TikTok: the untested path and the unused
path were the same path, so nothing ever pushed back.

An untested crawler is not a lower risk than a tested one. It is the same risk
with nobody watching.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from reel_scout.crawl.tiktok import TikTokCrawler

CANONICAL = "https://www.tiktok.com/@someone/video/1234567890123456789"


def _patch_basecmd():
    return patch("reel_scout.crawl.tiktok.ytdlp.base_cmd", return_value=["yt-dlp"])


def _download(recorder=None):
    c = TikTokCrawler()
    calls = []

    def _rec(cmd, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout='{"id": "1234567890123456789", '
                                              '"title": "t", "duration": 9}')

    with _patch_basecmd(), \
         patch("reel_scout.crawl.tiktok.get_limiter"), \
         patch("reel_scout.crawl.tiktok.subprocess.run", side_effect=_rec), \
         patch("reel_scout.crawl.tiktok.os.path.exists", return_value=True), \
         patch("reel_scout.crawl.tiktok.os.path.getsize", return_value=4321), \
         patch("reel_scout.crawl.tiktok.ffprobe.warn_if_not_apple_playable") as chk:
        meta = c.download(CANONICAL, output_dir="/tmp")
    return meta, calls, chk


def test_download_asks_for_a_codec_apple_can_decode():
    """Assert on the argv this crawler actually sends.

    A test that only exercised ``ytdlp.apple_safe_format()`` would stay green
    while this module drifted back to a bare selector -- which is exactly what
    happened here, for months, in the absence of any test at all.
    """
    _, calls, _ = _download()

    dl = [x for x in calls if "--merge-output-format" in x][0]
    fmt = dl[dl.index("-f") + 1]
    alts = fmt.split("/")

    assert "vcodec^=avc1" in alts[0]
    assert "acodec^=mp4a" in alts[0]
    for alt in alts[:3]:
        assert "vcodec^=avc1" in alt
    # ...and an unconstrained tail, so a VP9-only clip still enters the library.
    # A clip that will not render is still worth its transcript and its score.
    assert any("vcodec" not in a for a in alts[3:])


def test_download_measures_the_file_that_actually_landed():
    """The selector states a preference; yt-dlp is free to walk down to the
    unconstrained tail. Something has to look at what arrived, or the difference
    between the two is invisible until a human opens the clip on a phone."""
    _, _, chk = _download()
    chk.assert_called_once()


def test_the_landed_file_check_never_costs_a_successful_download():
    """A probe that cannot run must not turn a download that worked into a
    failure. Warn, never raise."""
    c = TikTokCrawler()

    with _patch_basecmd(), \
         patch("reel_scout.crawl.tiktok.get_limiter"), \
         patch("reel_scout.crawl.tiktok.subprocess.run",
               return_value=MagicMock(returncode=0,
                                      stdout='{"id": "1234567890123456789", "duration": 9}')), \
         patch("reel_scout.crawl.tiktok.os.path.exists", return_value=True), \
         patch("reel_scout.crawl.tiktok.os.path.getsize", return_value=4321), \
         patch("reel_scout.crawl.tiktok.ffprobe.warn_if_not_apple_playable",
               side_effect=OSError("ffprobe missing")):
        try:
            c.download(CANONICAL, output_dir="/tmp")
        except OSError:
            raise AssertionError(
                "a failed codec probe must not sink a download that succeeded"
            )
