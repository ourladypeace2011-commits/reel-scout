"""Failures must report the end of stderr, not the beginning.

``crawl/ytdlp.py`` already fixed this for yt-dlp; the ffmpeg and whisper.cpp
paths kept a blind ``stderr[:300]`` / ``[:500]``. Both tools print banners and
progress first and the cause last, so the head was reliably the useless part.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from reel_scout.utils import stderr as stderr_mod
from reel_scout.utils.stderr import tail_stderr, warn

REPO_ROOT = Path(__file__).resolve().parents[1]


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


# --- the entry points that were not protected ---------------------------------


def test_the_mcp_server_fixes_its_console_before_serving():
    """`force_utf8_stdio` lived in cli.py and ran only from the CLI.

    The MCP server is how the Windows students reach this package -- no terminal,
    Claude Desktop, whatever codepage the host hands it -- and it never called
    the fix. Worse, `tools.py` wraps the pipeline in `redirect_stdout(sys.stderr)`
    to keep pipeline chatter off the JSON-RPC channel, which is precisely what
    lands that chatter on the stream nobody had fixed.
    """
    import inspect

    from reel_scout.mcp import server

    src = inspect.getsource(server.main)
    assert "force_utf8_stdio()" in src, (
        "the MCP entry point must fix its console like the CLI one does")


# --- the direction that was still unprotected: what comes IN -------------------


def test_force_utf8_stdio_reconfigures_stdin_too():
    """The name says stdio; only the "o" was ever done.

    Everything reaching this package as text on stdin is UTF-8 — the MCP
    transport, `ingest --from-json -`, `mark --import -`, `crawl --file -`.
    Python decodes stdin with the locale codepage regardless, so on cp950/cp1252
    every CJK label arrived as surrogates.
    """
    class FakeStream:
        def __init__(self):
            self.calls = []

        def reconfigure(self, **kw):
            self.calls.append(kw)

    stdin, stdout, stderr = FakeStream(), FakeStream(), FakeStream()
    with mock.patch.object(sys, "stdin", stdin), \
            mock.patch.object(sys, "stdout", stdout), \
            mock.patch.object(sys, "stderr", stderr):
        stderr_mod.force_utf8_stdio()

    assert stdin.calls, "stdin was never reconfigured"
    assert stdin.calls[0]["encoding"] == "utf-8"
    # Strict on the way in: a replacement character here is corrupt DATA, and
    # silently storing 中文 as `???` is worse than refusing the input. The
    # streams going out keep errors="replace" for the opposite reason — losing a
    # glyph beats losing the row.
    assert stdin.calls[0].get("errors") in (None, "strict"), (
        "stdin must not silently replace undecodable bytes")
    assert stdout.calls[0]["errors"] == "replace"


def test_a_stdin_that_cannot_be_reconfigured_is_not_fatal():
    """pytest's capture, a pipe wrapper, a detached stream — printing is not
    this function's job to fix, and neither is reading."""
    class NoReconfigure:
        pass

    class Raises:
        def reconfigure(self, **kw):
            raise ValueError("detached")

    for fake in (NoReconfigure(), Raises()):
        with mock.patch.object(sys, "stdin", fake):
            stderr_mod.force_utf8_stdio()          # must not raise


def test_cjk_on_stdin_survives_a_non_utf8_locale(tmp_path):
    """End-to-end, in a child process whose locale cannot decode what we send.

    This is the shape the bug actually had: `mark --import -` fed UTF-8 JSON with
    a Chinese label, read back through cp1252/ASCII, stored as surrogates, and
    blown up at the sqlite write with a message about encoding rather than about
    the pipe. The label must come back out identical.
    """
    data = tmp_path / "lib"
    data.mkdir()
    env = dict(os.environ, REEL_SCOUT_DATA=str(data), PYTHONPATH=str(REPO_ROOT),
               LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
    env.pop("PYTHONIOENCODING", None)              # no rescue from outside

    seed = subprocess.run(
        [sys.executable, "-c",
         "from reel_scout import db\n"
         "c = db.init_db()\n"
         "v = db.upsert_video(c, platform='instagram', platform_id='CJK00001',\n"
         "                    url='https://www.instagram.com/reel/CJK00001/',\n"
         "                    title='t', duration_sec=30.0)\n"
         "db.update_video_status(c, v, 'analyzed'); c.commit(); print(v)\n"],
        capture_output=True, text=True, encoding="utf-8", env=env)
    assert seed.returncode == 0, seed.stdout + seed.stderr
    vid = seed.stdout.strip().splitlines()[-1]

    payload = '{"marks":[{"t":1.0,"label":"語意轉場","note":"人眼特寫"}]}'
    imported = subprocess.run(
        [sys.executable, "-m", "reel_scout.cli", "mark", vid,
         "--import", "-", "--source", "cjk"],
        input=payload.encode("utf-8"), capture_output=True, env=env)
    out = imported.stdout.decode("utf-8", "replace") + \
        imported.stderr.decode("utf-8", "replace")
    assert imported.returncode == 0, out
    assert "UnicodeEncodeError" not in out and "surrogates" not in out

    # Read back through pure-ASCII JSON. Printing 中文 from a `-c` snippet under
    # this locale would raise in the READER — and a reader that dies looks exactly
    # like a row that was never written. The check has to survive the environment
    # it is checking. (Same trap, fourth time this batch: the tool, the harness
    # running it, and now the probe reading the result each need saying.)
    read_back = subprocess.run(
        [sys.executable, "-c",
         "import json, sys\n"
         "from reel_scout import db, marks\n"
         "c = db.init_db()\n"
         "rows = [(m['label'], m['note']) for m in marks.list_for(c, %r)]\n"
         "sys.stdout.write(json.dumps(rows, ensure_ascii=True))\n" % vid],
        capture_output=True, text=True, env=env)
    assert read_back.returncode == 0, read_back.stdout + read_back.stderr
    stored = json.loads(read_back.stdout)
    assert stored == [["語意轉場", "人眼特寫"]], stored
