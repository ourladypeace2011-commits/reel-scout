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


def test_media_format_prefers_apple_playable_codecs(tmp_path, monkeypatch, no_rate_limit):
    """A clip that downloads fine and then will not play reads as a broken clip.

    The player shows a blank rectangle and says nothing, while the transcript,
    score and keyframes all come out normal. Chrome decodes what Safari will
    not, so the same library works for one viewer and not the next.

    This assertion started life as "anything but AV1" and that was too narrow.
    Excluding av01 lets VP9 through, and VP9 in an MP4 container — which is
    what this crawler builds, via --merge-output-format mp4 — does not play on
    iOS/iPadOS at all (Safari decodes VP9 only inside WebM). Opus audio in MP4
    fails on Apple platforms for the same reason. Measured on the real library
    2026-08-21, 109 files: 86 vp9+aac, 13 vp9+opus, 2 av1+opus, 2 h264+opus,
    and only 4 h264+aac. The av01 filter was green the whole time.

    So the preferred branches now name H.264 and AAC positively instead of
    naming one bad codec negatively — a denylist only ever excludes the codec
    somebody already got burned by.

    Both halves are asserted because either alone is a trap. Without the codec
    preference the default "best" keeps handing back VP9. Without the
    unconstrained tail, a video published only in VP9 or AV1 stops being
    ingestable at all — and a clip that will not play is still worth its
    transcript, keyframes and score.
    """
    rec = _run(monkeypatch, _Recorder(str(tmp_path)))
    YouTubeCrawler().download(URL, str(tmp_path))

    media = [c for c in rec.calls if "--merge-output-format" in c][0]
    fmt = media[media.index("-f") + 1]
    alternatives = fmt.split("/")

    assert alternatives[0].startswith("bestvideo[")
    # the first branch pins both halves: H.264 video AND AAC audio. Video alone
    # is not enough — Opus in MP4 is just as dead on Apple as VP9 is.
    assert "vcodec^=avc1" in alternatives[0]
    assert "acodec^=mp4a" in alternatives[0]
    # every preferred branch, not just the first, or the next one reintroduces VP9
    for alt in alternatives[:3]:
        assert "vcodec^=avc1" in alt
    # the older av01-free rungs stay in the middle of the chain
    assert any("vcodec!*=av01" in alt for alt in alternatives[3:])
    # ...and a tail with no codec constraint, so VP9/AV1-only uploads still land
    assert any("vcodec" not in alt for alt in alternatives[3:])


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


def test_retry_does_not_resume_the_previous_format_partial(
    tmp_path, monkeypatch, no_rate_limit
):
    # Both attempts write to the same `yt_<id>.mp4.part`, and yt-dlp resumes a
    # partial by default. If the first selector picked a progressive format and
    # died mid-transfer, the retry would Range-request a *different* format's
    # bytes and append them to what is already on disk. Nothing checks that the
    # two halves came from the same stream, so the join produces a file that
    # exists and plays wrong — and `os.path.exists` below reads that as success.
    rec = _run(monkeypatch, _FailFirstMediaRecorder(str(tmp_path), succeed_on=2))
    YouTubeCrawler().download(URL, str(tmp_path))

    media = [c for c in rec.calls if "--merge-output-format" in c]
    assert "--no-continue" not in media[0], "the first attempt has nothing to resume from"
    assert "--no-continue" in media[1], (
        "the retry changes format, so any partial on disk belongs to a different "
        "stream — resuming onto it silently corrupts the file"
    )


# --- a leftover file at the destination ---------------------------------------
#
# yt-dlp skips a destination that already exists and exits 0. YouTube's download
# read file-existence as its only success signal and never looked at the exit
# status, so a zero-byte or truncated `yt_<id>.mp4` from a killed run made the
# whole thing report success without transferring a byte. And because the
# pipeline's own reuse gate is `os.path.exists` too, that bad file was then
# skipped forever -- the failure is sticky, not a one-run blip.


@pytest.fixture
def probe(monkeypatch):
    """Control what counts as playable media."""
    state = {"duration": None}
    monkeypatch.setattr("reel_scout.ffprobe.probe_duration",
                        lambda path: state["duration"])
    return state


class _NeverDownloads(_Recorder):
    """Stands in for yt-dlp finding the destination already occupied."""

    def __call__(self, cmd, **kw):
        if "--merge-output-format" in cmd:
            self.calls.append(cmd)
            return _Done(0)  # "has already been downloaded"
        return super().__call__(cmd, **kw)


def test_an_unplayable_leftover_is_moved_aside_so_the_download_runs(
    tmp_path, monkeypatch, no_rate_limit, probe
):
    stale = tmp_path / "yt_h1YeIE0vEIs.mp4"
    stale.write_bytes(b"")  # the zero-byte file a killed run leaves
    probe["duration"] = None  # ffmpeg cannot read it

    rec = _run(monkeypatch, _Recorder(str(tmp_path)))
    YouTubeCrawler().download(URL, str(tmp_path))

    assert (tmp_path / "yt_h1YeIE0vEIs.mp4.unusable").exists(), (
        "the bad file must be kept as evidence, not deleted")
    assert stale.read_bytes() == b"x" * 10, "the download must actually have run"
    assert [c for c in rec.calls if "--merge-output-format" in c]


def test_a_playable_file_already_there_is_left_alone(
    tmp_path, monkeypatch, no_rate_limit, probe
):
    # The other half of the rule. A real cache hit must not be thrown away just
    # because something was sitting at the destination.
    good = tmp_path / "yt_h1YeIE0vEIs.mp4"
    good.write_bytes(b"real media bytes")
    probe["duration"] = 12.5

    _run(monkeypatch, _NeverDownloads(str(tmp_path)))
    YouTubeCrawler().download(URL, str(tmp_path))

    assert good.read_bytes() == b"real media bytes"
    assert not (tmp_path / "yt_h1YeIE0vEIs.mp4.unusable").exists()


def test_stale_captions_move_with_the_file_they_came_from(
    tmp_path, monkeypatch, no_rate_limit, probe
):
    # `find_subtitle` globs `<stem>.*.vtt`, so a caption file outlives the video
    # it belongs to and gets re-attached to whatever downloads next -- one clip's
    # transcript presented as another's.
    (tmp_path / "yt_h1YeIE0vEIs.mp4").write_bytes(b"")
    (tmp_path / "yt_h1YeIE0vEIs.en.vtt").write_text("WEBVTT\n\nold captions\n")
    probe["duration"] = None

    _run(monkeypatch, _Recorder(str(tmp_path)))
    YouTubeCrawler().download(URL, str(tmp_path))

    assert (tmp_path / "yt_h1YeIE0vEIs.en.vtt.unusable").exists()
    assert not (tmp_path / "yt_h1YeIE0vEIs.en.vtt").exists(), (
        "a caption file left behind would be re-attached to the new download")


class _NonZeroButLeavesAFile(_Recorder):
    """yt-dlp fails partway and leaves a partial behind."""

    def __call__(self, cmd, **kw):
        if "--merge-output-format" in cmd:
            self.calls.append(cmd)
            open(os.path.join(self._out_dir, "yt_h1YeIE0vEIs.mp4"), "wb").write(b"half")
            return _Done(1, stderr="ERROR: unable to download video data: HTTP Error 403")
        return super().__call__(cmd, **kw)


def test_a_non_zero_exit_is_a_failure_even_when_a_file_is_present(
    tmp_path, monkeypatch, no_rate_limit, probe
):
    # Instagram and TikTok have always checked returncode; YouTube was the odd
    # one out. A partial left on disk after a 403 must not pass for a download.
    probe["duration"] = None
    rec = _run(monkeypatch, _NonZeroButLeavesAFile(str(tmp_path)))
    with pytest.raises(RuntimeError, match="download failed"):
        YouTubeCrawler().download(URL, str(tmp_path))
    assert len([c for c in rec.calls if "--merge-output-format" in c]) == 2, (
        "a partial from the preferred formats must still fall through to the retry")


class _SucceedsWithoutWriting(_Recorder):
    """Exit 0 and produce nothing -- yt-dlp declining the job quietly."""

    def __call__(self, cmd, **kw):
        if "--merge-output-format" in cmd:
            self.calls.append(cmd)
            return _Done(0)
        return super().__call__(cmd, **kw)


def test_a_zero_exit_that_produced_no_file_is_still_a_failure(
    tmp_path, monkeypatch, no_rate_limit, probe
):
    # The mirror of the returncode check, and it is reachable precisely because
    # of the fix above: once a stale destination has been moved aside, a yt-dlp
    # that exits 0 without downloading leaves nothing at all. Trusting the status
    # alone would hand the pipeline a VideoMeta pointing at a file that is not
    # there.
    probe["duration"] = None
    _run(monkeypatch, _SucceedsWithoutWriting(str(tmp_path)))
    with pytest.raises(RuntimeError, match="download failed"):
        YouTubeCrawler().download(URL, str(tmp_path))
