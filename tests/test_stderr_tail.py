"""Failures must report the end of stderr, not the beginning.

``crawl/ytdlp.py`` already fixed this for yt-dlp; the ffmpeg and whisper.cpp
paths kept a blind ``stderr[:300]`` / ``[:500]``. Both tools print banners and
progress first and the cause last, so the head was reliably the useless part.
"""

from __future__ import annotations

import sys

from reel_scout.utils.stderr import tail_stderr, warn


class _AsciiOnlyConsole:
    """cp950 / cp1252 / cp437 in one class: raises on anything it cannot encode.

    ``cli._force_utf8_stdio`` fixes this for the CLI, but it is the CLI entry
    point — the MCP server and any direct importer never call it, and the MCP
    server is exactly how the Windows students reach this package.
    """

    def __init__(self) -> None:
        self.written = []

    def write(self, s: str) -> int:
        s.encode("ascii")  # UnicodeEncodeError on an emoji or an em dash
        self.written.append(s)
        return len(s)

    def flush(self) -> None:
        pass


class _DeadConsole:
    """Detached, closed, or nobody reading the other end."""

    def write(self, s: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        pass


def test_warn_degrades_characters_the_console_cannot_encode(monkeypatch):
    console = _AsciiOnlyConsole()
    monkeypatch.setattr(sys, "stderr", console)

    warn("  ⚠️  mixes traditional and simplified — search misses half")

    printed = "".join(console.written)
    assert "mixes traditional and simplified" in printed
    assert "⚠" not in printed and "—" not in printed, "unencodable chars must not survive"


def test_warn_never_raises_at_the_caller(monkeypatch):
    # A report that can kill its caller is not a report. Whether the operator
    # sees the line is never worth more than the work the caller was doing.
    monkeypatch.setattr(sys, "stderr", _DeadConsole())
    warn("anything at all")  # must not raise


def test_warn_prints_normally_when_the_console_can_take_it(capsys):
    warn("  ⚠️  plain path — still emitted verbatim")
    assert "⚠️" in capsys.readouterr().err, "the degrade path must not fire when unneeded"


FFMPEG_NOISE = """\
ffmpeg version 8.1.1 Copyright (c) 2000-2026 the FFmpeg developers
  built with Apple clang version 17.0.0
  configuration: --prefix=/opt/homebrew --enable-gpl --enable-libx264
  libavutil      60.  2.100 / 60.  2.100
  libavcodec     62.  3.100 / 62.  3.100
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'clip.mp4':
  Duration: 00:00:31.45, start: 0.000000, bitrate: 1843 kb/s
Error opening output file out.wav: Permission denied
"""


def test_tail_finds_the_cause_the_head_would_have_hidden():
    assert "Permission denied" not in FFMPEG_NOISE[:300]   # the old behaviour
    assert "Permission denied" in tail_stderr(FFMPEG_NOISE)


def test_error_lines_win_over_banner_lines():
    out = tail_stderr(FFMPEG_NOISE)
    assert "ffmpeg version" not in out
    assert "Error opening output file" in out


def test_falls_back_to_tail_when_nothing_looks_like_an_error():
    stderr = "\n".join("line %d" % i for i in range(200))
    out = tail_stderr(stderr, limit=40)
    assert "line 199" in out
    assert "line 0" not in out


def test_respects_the_character_limit():
    assert len(tail_stderr("Error: " + "x" * 4000, limit=200)) == 200


def test_empty_input_is_safe():
    assert tail_stderr("") == ""
    assert tail_stderr(None) == ""
    assert tail_stderr("   \n  \n") == ""
