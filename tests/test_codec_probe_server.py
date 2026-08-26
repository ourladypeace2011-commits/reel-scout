"""Guards for the two things that made the original probe rig lie.

The rig itself is manual -- a person taps cards on an iPad. These tests cover the
two pieces that fail *silently*, where the wrong answer is indistinguishable from
a real codec failure:

* ``parse_range`` -- get this wrong and iOS Safari refuses every clip. The
  investigation this rig came from started with ``python3 -m http.server``, which
  has no Range support at all, and would have reported all five clips broken.
* ``card_label`` -- the first run showed the control and the first variant with
  no distinguishing text, so the tester tapped the control twice and reported it
  as the variant. The fix is that labels are *derived* from the file.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "codec_probe_server",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "scripts", "codec-probe-server.py"),
)
cps = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cps)


class TestParseRange:
    """206 is not optional here; a 200 for a ranged request is a broken clip."""

    def test_no_header_is_a_whole_file_200(self) -> None:
        assert cps.parse_range(None, 1000) == (0, 999, False)
        assert cps.parse_range("", 1000) == (0, 999, False)

    def test_open_ended_range(self) -> None:
        # What Safari actually sends first: "bytes=0-".
        assert cps.parse_range("bytes=0-", 1000) == (0, 999, True)
        assert cps.parse_range("bytes=500-", 1000) == (500, 999, True)

    def test_closed_range(self) -> None:
        assert cps.parse_range("bytes=100-199", 1000) == (100, 199, True)

    def test_closed_range_past_eof_is_clamped(self) -> None:
        """Clamp, don't 416: a client asking for more than exists still gets the
        bytes that exist, which is what every real player expects."""
        assert cps.parse_range("bytes=900-5000", 1000) == (900, 999, True)

    def test_suffix_range_counts_from_the_end(self) -> None:
        """``bytes=-500`` is the LAST 500 bytes, not the first 500.

        Readers reach for the moov atom this way. Getting it backwards serves
        plausible-looking bytes from the wrong place, and the player reports a
        decode error -- which reads exactly like an unsupported codec.
        """
        assert cps.parse_range("bytes=-500", 1000) == (500, 999, True)

    def test_suffix_larger_than_file_starts_at_zero(self) -> None:
        assert cps.parse_range("bytes=-5000", 1000) == (0, 999, True)

    def test_garbage_falls_back_to_the_whole_file(self) -> None:
        for junk in ("bytes=abc", "items=0-10", "bytes=", "  ", "bytes=-"):
            start, end, partial = cps.parse_range(junk, 1000)
            assert (start, end) == (0, 999), junk
            assert partial is False, junk

    def test_start_past_eof_is_left_for_the_caller_to_416(self) -> None:
        start, _, _ = cps.parse_range("bytes=2000-", 1000)
        assert start >= 1000


class TestCardLabel:
    def test_label_names_the_file_and_the_codecs(self) -> None:
        info = {"video": "vp9", "audio": "opus", "duration": "15.7s", "size": "2.7 MB"}
        label = cps.card_label("/x/y/clip.orig.mp4", info)
        assert label.startswith("clip.orig.mp4")
        assert "vp9 + opus" in label

    def test_two_clips_of_the_same_video_are_still_distinguishable(self) -> None:
        """The exact case that produced a wrong answer: same source, one
        variable changed. If the labels tie, the page cannot be read."""
        a = cps.card_label("/x/clip.mp4",
                           {"video": "h264", "audio": "aac", "duration": "15.7s", "size": "1.9 MB"})
        b = cps.card_label("/x/clip.vp9.orig.mp4",
                           {"video": "vp9", "audio": "aac", "duration": "15.7s", "size": "2.1 MB"})
        assert a != b

    def test_unprobeable_clip_says_so_instead_of_looking_normal(self) -> None:
        info = {"video": "ffprobe failed (FileNotFoundError)", "audio": "?",
                "duration": "?", "size": ""}
        assert "ffprobe failed" in cps.card_label("/x/clip.mp4", info)


class TestProbeNeverRaises:
    def test_missing_file_is_labelled_not_raised(self) -> None:
        """A rig that dies on one bad path takes the other four clips with it."""
        info = cps.probe("/definitely/not/here.mp4")
        assert isinstance(info, dict)
        assert info["video"] != "?" or info["audio"] == "?"


class _NeverServes:
    """Stands in for ``Server`` so ``serve_forever`` returns instead of blocking."""

    def serve_forever(self):
        return None


class TestMainRefusesMissingClips:
    """``Server`` is patched out in both tests, deliberately.

    Without that, a bug which lets ``main`` fall through to ``serve_forever()``
    does not fail this suite -- it *hangs* it. A hanging test is strictly worse
    than a failing one: CI reports a timeout with no line number and a local run
    just sits there. Patching the server turns "it got further than it should
    have" into an ordinary assertion.
    """

    def test_missing_clip_exits_before_binding(self, capsys, monkeypatch) -> None:
        """A 404 card is indistinguishable from an unplayable one, so the check
        happens before the socket is opened."""
        served = []

        def _fake(*a, **k):
            served.append(a)
            return _NeverServes()

        monkeypatch.setattr(cps, "Server", _fake)
        rc = cps.main(["/definitely/not/here.mp4"])
        assert rc == 2
        assert "not found" in capsys.readouterr().err
        assert served == [], "bound a socket for a clip that does not exist"

    def test_a_real_file_gets_as_far_as_serving(self, tmp_path, monkeypatch) -> None:
        """The positive half. Without it, an unconditional ``return 2`` would
        pass the test above and this whole guard would be vacuous."""
        p = tmp_path / "clip.mp4"
        p.write_bytes(b"\x00" * 16)
        served = []

        def _fake(*a, **k):
            served.append(a)
            return _NeverServes()

        monkeypatch.setattr(cps, "Server", _fake)
        rc = cps.main([str(p)])
        assert rc == 0
        assert len(served) == 1


class TestBuildPage:
    def test_every_clip_gets_a_card_and_a_distinct_source(self, tmp_path) -> None:
        a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
        a.write_bytes(b"\x00" * 8)
        b.write_bytes(b"\x00" * 8)
        html = cps.build_page([("c0", str(a)), ("c1", str(b))]).decode("utf-8")
        assert html.count("<video") == 2
        assert 'src="/v/c0"' in html and 'src="/v/c1"' in html
        assert "a.mp4" in html and "b.mp4" in html
