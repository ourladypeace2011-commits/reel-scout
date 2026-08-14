from __future__ import annotations

import json
import os

import pytest

from reel_scout.crawl import youtube
from reel_scout.crawl.youtube import YouTubeCrawler


URL = "https://www.youtube.com/shorts/h1YeIE0vEIs"


class _Recorder:
    """Stands in for subprocess.run; records argv and fakes yt-dlp's side effects."""

    def __init__(self, out_dir, subs_returncode=0, subs_raises=None, write_media=True):
        self.calls = []
        self._out_dir = out_dir
        self._subs_returncode = subs_returncode
        self._subs_raises = subs_raises
        self._write_media = write_media

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)

        if "--dump-json" in cmd:
            return _Done(0, stdout=json.dumps(
                {"title": "T", "uploader": "U", "duration": 12, "upload_date": "20260101"}
            ))

        if "--skip-download" in cmd:  # the subtitle pass
            if self._subs_raises:
                raise self._subs_raises
            return _Done(self._subs_returncode, stderr="HTTP Error 429: Too Many Requests")

        # the media pass
        if self._write_media:
            open(os.path.join(self._out_dir, "yt_h1YeIE0vEIs.mp4"), "wb").write(b"x" * 10)
            return _Done(0)
        return _Done(1, stderr="boom")


class _Done:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _NoWait:
    def wait(self):
        pass


@pytest.fixture
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(youtube, "get_limiter", lambda platform: _NoWait())


def _run(monkeypatch, rec):
    monkeypatch.setattr(youtube.subprocess, "run", rec)
    return rec


def test_media_and_subtitles_are_separate_invocations(tmp_path, monkeypatch, no_rate_limit):
    rec = _run(monkeypatch, _Recorder(str(tmp_path)))
    YouTubeCrawler().download(URL, str(tmp_path))

    media = [c for c in rec.calls if "--merge-output-format" in c]
    subs = [c for c in rec.calls if "--skip-download" in c]
    assert len(media) == 1 and len(subs) == 1
    # The media pass must not ask for subtitles at all — bundling them is what
    # let a caption 429 kill the download.
    for flag in ("--write-subs", "--write-auto-subs", "--convert-subs"):
        assert flag not in media[0]
    assert "--write-subs" in subs[0]


def test_subtitle_429_does_not_fail_the_download(tmp_path, monkeypatch, no_rate_limit):
    _run(monkeypatch, _Recorder(str(tmp_path), subs_returncode=1))
    meta = YouTubeCrawler().download(URL, str(tmp_path))
    assert meta.platform_id == "h1YeIE0vEIs"
    assert meta.file_path.endswith("yt_h1YeIE0vEIs.mp4")


def test_subtitle_crash_does_not_fail_the_download(tmp_path, monkeypatch, no_rate_limit):
    _run(monkeypatch, _Recorder(str(tmp_path), subs_raises=OSError("yt-dlp vanished")))
    meta = YouTubeCrawler().download(URL, str(tmp_path))
    assert meta.file_path.endswith("yt_h1YeIE0vEIs.mp4")


def test_subtitles_are_fetched_after_media_lands(tmp_path, monkeypatch, no_rate_limit):
    rec = _run(monkeypatch, _Recorder(str(tmp_path), write_media=False))
    with pytest.raises(RuntimeError, match="download failed"):
        YouTubeCrawler().download(URL, str(tmp_path))
    # No media -> no point spending a request on captions.
    assert not [c for c in rec.calls if "--skip-download" in c]


def test_media_format_prefers_anything_but_av1(tmp_path, monkeypatch, no_rate_limit):
    """AV1 downloads fine and then will not play, which reads as a broken clip.

    Apple silicon only gained an AV1 hardware decoder in M3 and Safari will not
    decode it in software, so an AV1 clip analyzes and scores normally while the
    player shows a blank rectangle — with no message saying why. Chrome decodes
    it in software, so the same library works for one viewer and not the next.
    Measured on a real library: 13 of 99 downloaded files were AV1.

    Two halves are asserted because either alone is a trap. Without the codec
    filter the default "best" keeps handing back AV1. Without the unconstrained
    tail, a video published only in AV1 stops being ingestable at all — and a
    clip that will not play is still worth its transcript, keyframes and score.
    """
    rec = _run(monkeypatch, _Recorder(str(tmp_path)))
    YouTubeCrawler().download(URL, str(tmp_path))

    media = [c for c in rec.calls if "--merge-output-format" in c][0]
    fmt = media[media.index("-f") + 1]
    alternatives = fmt.split("/")

    assert alternatives[0].startswith("bestvideo[")
    assert "vcodec!*=av01" in alternatives[0]
    # every preferred branch, not just the first, or the second one reintroduces it
    for alt in alternatives[:2]:
        assert "vcodec!*=av01" in alt
    # ...and a tail with no codec constraint, so AV1-only uploads still land
    assert any("vcodec" not in alt for alt in alternatives[2:])


# --- download-layer fallback (2026-08-15) --------------------------------
#
# The `/` chain only degrades while *choosing* a format. E8Bx9OlpmdM (Gary Chen,
# 11:41, seen 2026-08-13) chose the separate video+audio streams, then got HTTP
# 403 on the video stream — and yt-dlp did not walk to the next selector, it just
# failed. The same clip downloaded fine at `-f 18`. So the fallback has to sit at
# the download layer, which is what these two tests pin.


class _FailFirstMediaRecorder(_Recorder):
    """Media pass fails until the Nth attempt, then writes the file.

    Stands in for "the chosen format 403s but a progressive one is reachable".
    """

    def __init__(self, out_dir, succeed_on=2):
        super().__init__(out_dir)
        self._succeed_on = succeed_on
        self.media_attempts = 0

    def __call__(self, cmd, **kw):
        if "--merge-output-format" in cmd:
            self.calls.append(cmd)
            self.media_attempts += 1
            if self.media_attempts < self._succeed_on:
                return _Done(1, stderr="ERROR: unable to download video data: HTTP Error 403")
            open(os.path.join(self._out_dir, "yt_h1YeIE0vEIs.mp4"), "wb").write(b"x" * 10)
            return _Done(0)
        return super().__call__(cmd, **kw)


def test_download_retries_with_progressive_format_after_a_403(
    tmp_path, monkeypatch, no_rate_limit
):
    rec = _run(monkeypatch, _FailFirstMediaRecorder(str(tmp_path), succeed_on=2))
    YouTubeCrawler().download(URL, str(tmp_path))

    media = [c for c in rec.calls if "--merge-output-format" in c]
    assert len(media) == 2, "a failed download must be retried, not surrendered"

    first = media[0][media[0].index("-f") + 1]
    second = media[1][media[1].index("-f") + 1]

    # First attempt stays quality-first: separate streams, capped at 720.
    assert first.startswith("bestvideo[")
    # The retry must ask for something *different*, and specifically for a
    # pre-muxed stream — retrying the identical selector would 403 again.
    assert second != first
    assert second.startswith("18/"), second


def test_download_still_raises_when_every_format_fails(
    tmp_path, monkeypatch, no_rate_limit
):
    # succeed_on=99 -> nothing ever lands. The retry must not turn a real
    # failure into a silent success; a clip that never downloaded has to be
    # loud, or the batch reports "completed" over a hole.
    rec = _run(monkeypatch, _FailFirstMediaRecorder(str(tmp_path), succeed_on=99))
    with pytest.raises(RuntimeError, match="download failed"):
        YouTubeCrawler().download(URL, str(tmp_path))

    assert rec.media_attempts == 2, "exactly one retry, not an unbounded loop"
    # No media -> still no point spending a request on captions.
    assert not [c for c in rec.calls if "--skip-download" in c]
