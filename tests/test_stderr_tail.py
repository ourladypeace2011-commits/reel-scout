"""Failures must report the end of stderr, not the beginning.

``crawl/ytdlp.py`` already fixed this for yt-dlp; the ffmpeg and whisper.cpp
paths kept a blind ``stderr[:300]`` / ``[:500]``. Both tools print banners and
progress first and the cause last, so the head was reliably the useless part.
"""

from __future__ import annotations

from reel_scout.utils.stderr import tail_stderr


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
