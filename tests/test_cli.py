from __future__ import annotations

import argparse
import io
import os

import pytest

from reel_scout import cli, crawl as crawl_pkg, db
from reel_scout.utils.stderr import force_utf8_stdio
from reel_scout.crawl import is_profile_url
from reel_scout.crawl.base import BaseCrawler, VideoMeta


def _args(**kw):
    base = dict(urls=[], file=None, channel=None, playlist=None, limit=30, cookies=None)
    base.update(kw)
    return argparse.Namespace(**base)


class _FakeCrawler(BaseCrawler):
    """Records what download() was asked for; browse() returns canned entries."""

    platform = "youtube"

    def __init__(self, entries=None, browse_raises=None):
        self._entries = entries or []
        self._browse_raises = browse_raises
        self.downloaded = []

    def extract_id(self, url):
        return url.rsplit("=", 1)[-1]

    def browse(self, url, limit=30):
        if self._browse_raises:
            raise self._browse_raises
        return self._entries[:limit]

    def download(self, url, output_dir):
        self.downloaded.append(url)
        return VideoMeta(
            platform=self.platform,
            platform_id=self.extract_id(url),
            url=url,
            title="title-" + self.extract_id(url),
            uploader="chan",
            duration_sec=42.0,
            file_path=os.path.join(output_dir, self.extract_id(url) + ".mp4"),
            file_size_bytes=1234,
        )


def _entries(n):
    return [
        VideoMeta(platform="youtube", platform_id="v%d" % i,
                  url="https://www.youtube.com/watch?v=v%d" % i, title="t%d" % i)
        for i in range(n)
    ]


@pytest.fixture
def fake_crawler(monkeypatch):
    holder = {}

    def install(entries=None, browse_raises=None):
        c = _FakeCrawler(entries=entries, browse_raises=browse_raises)
        holder["c"] = c
        monkeypatch.setattr(crawl_pkg, "get_crawler", lambda url: c)
        return c

    return install


# --- _read_url_lines / --file - (stdin) ---

def test_read_url_lines_from_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("a\nb\n"))
    assert cli._read_url_lines("-") == ["a", "b"]


def test_read_url_lines_from_file(tmp_path):
    p = tmp_path / "urls.txt"
    p.write_text("x\ny\n", encoding="utf-8")
    assert cli._read_url_lines(str(p)) == ["x", "y"]


def test_collect_urls_stdin_pipe_skips_blanks_and_comments(monkeypatch):
    monkeypatch.setattr(
        "sys.stdin", io.StringIO("https://a\n\n# note\nhttps://b\n")
    )
    assert cli._collect_urls(_args(file="-")) == ["https://a", "https://b"]


# --- is_profile_url classification ---

@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/@chan", True),
    ("https://www.youtube.com/@chan/shorts", True),
    ("https://www.youtube.com/channel/UC123", True),
    ("https://www.youtube.com/watch?v=abc", False),
    # A playlist is not a profile: --playlist is a separate flag.
    ("https://www.youtube.com/playlist?list=PL123", False),
    # A TikTok profile IS a profile even though we cannot browse it.
    ("https://www.tiktok.com/@user", True),
    ("https://www.tiktok.com/@user/video/123", False),
    ("https://www.instagram.com/user/reels/", True),
    ("https://www.instagram.com/reel/ABC123/", False),
])
def test_is_profile_url(url, expected):
    assert is_profile_url(url) is expected


# --- _expand_listing ---

def test_expand_listing_returns_browse_urls(fake_crawler):
    fake_crawler(entries=_entries(3))
    urls = cli._expand_listing("https://www.youtube.com/@chan", 10, require_profile=True)
    assert urls == ["https://www.youtube.com/watch?v=v%d" % i for i in range(3)]


def test_expand_listing_honors_limit(fake_crawler):
    fake_crawler(entries=_entries(5))
    urls = cli._expand_listing("https://www.youtube.com/@chan", 2, require_profile=True)
    assert len(urls) == 2


def test_expand_listing_rejects_non_profile_when_required(fake_crawler):
    fake_crawler(entries=_entries(1))
    with pytest.raises(ValueError, match="not a channel/profile URL"):
        cli._expand_listing("https://www.youtube.com/watch?v=x", 10, require_profile=True)


def test_expand_listing_allows_playlist_when_profile_not_required(fake_crawler):
    fake_crawler(entries=_entries(2))
    urls = cli._expand_listing(
        "https://www.youtube.com/playlist?list=PL1", 10, require_profile=False
    )
    assert len(urls) == 2


def test_expand_listing_converts_not_implemented_to_value_error(fake_crawler):
    fake_crawler(browse_raises=NotImplementedError("tiktok does not support browse"))
    with pytest.raises(ValueError, match="does not support browse"):
        cli._expand_listing("https://www.tiktok.com/@u", 10, require_profile=False)


def test_expand_listing_drops_entries_without_url(fake_crawler):
    fake_crawler(entries=[VideoMeta(url=""), VideoMeta(url="https://ok")])
    assert cli._expand_listing("https://www.youtube.com/@c", 10, require_profile=True) == [
        "https://ok"
    ]


# --- _cmd_crawl end to end (fake crawler, real sqlite) ---

def test_cmd_crawl_channel_downloads_each_listed_video(temp_db, fake_crawler, capsys):
    c = fake_crawler(entries=_entries(3))
    cli._cmd_crawl(_args(channel="https://www.youtube.com/@chan", limit=10))
    assert c.downloaded == ["https://www.youtube.com/watch?v=v%d" % i for i in range(3)]
    conn = db.init_db()
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 3
    conn.close()
    assert "Found 3 videos" in capsys.readouterr().out


def test_cmd_crawl_channel_respects_limit(temp_db, fake_crawler):
    c = fake_crawler(entries=_entries(5))
    cli._cmd_crawl(_args(channel="https://www.youtube.com/@chan", limit=2))
    assert len(c.downloaded) == 2


def test_cmd_crawl_playlist_skips_profile_guard(temp_db, fake_crawler):
    c = fake_crawler(entries=_entries(2))
    cli._cmd_crawl(_args(playlist="https://www.youtube.com/playlist?list=PL1"))
    assert len(c.downloaded) == 2


def test_cmd_crawl_tiktok_channel_reports_cleanly(temp_db, fake_crawler, capsys):
    c = fake_crawler(browse_raises=NotImplementedError("tiktok does not support browse"))
    cli._cmd_crawl(_args(channel="https://www.tiktok.com/@user"))
    out = capsys.readouterr().out
    assert "does not support browse" in out
    assert "Traceback" not in out
    assert c.downloaded == []


def test_cmd_crawl_rejects_channel_and_playlist_together(temp_db, fake_crawler, capsys):
    c = fake_crawler(entries=_entries(2))
    cli._cmd_crawl(_args(channel="https://www.youtube.com/@c",
                         playlist="https://www.youtube.com/playlist?list=PL1"))
    assert "not both" in capsys.readouterr().out
    assert c.downloaded == []


def test_cmd_crawl_channel_merges_with_positional_urls(temp_db, fake_crawler):
    c = fake_crawler(entries=_entries(2))
    cli._cmd_crawl(_args(urls=["https://www.youtube.com/watch?v=extra"],
                         channel="https://www.youtube.com/@chan"))
    assert "https://www.youtube.com/watch?v=extra" in c.downloaded
    assert len(c.downloaded) == 3


def test_cmd_crawl_empty_channel_listing_says_so(temp_db, fake_crawler, capsys):
    c = fake_crawler(entries=[])
    cli._cmd_crawl(_args(channel="https://www.youtube.com/@chan"))
    assert "No videos found" in capsys.readouterr().out
    assert c.downloaded == []


def test_cmd_crawl_no_input_lists_all_options(temp_db, capsys):
    cli._cmd_crawl(_args())
    out = capsys.readouterr().out
    assert "--channel" in out and "--playlist" in out


def test_cmd_crawl_survives_one_failing_download(temp_db, fake_crawler, capsys):
    c = fake_crawler(entries=_entries(3))
    orig = c.download

    def flaky(url, output_dir):
        if url.endswith("v1"):
            raise RuntimeError("boom")
        return orig(url, output_dir)

    c.download = flaky
    cli._cmd_crawl(_args(channel="https://www.youtube.com/@chan"))
    conn = db.init_db()
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 2
    conn.close()
    assert "boom" in capsys.readouterr().out


# --- Windows console encoding ------------------------------------------------
# A short-form title is full of emoji and this module prints em dashes by the
# hundred; neither survives the locale codepage Python picks on Windows
# (cp950 zh-TW, cp1252 US/EU, cp437 legacy cmd). `show` used to die on a
# UnicodeEncodeError before it could print the keyframe paths Step 2b needs.


def _legacy_console(codepage):
    """A text stream that behaves like a Windows console on `codepage`."""
    return io.TextIOWrapper(io.BytesIO(), encoding=codepage, errors="strict")


@pytest.mark.parametrize("codepage", ["cp950", "cp1252", "cp437"])
def test_legacy_codepages_really_cannot_encode_a_short_form_title(codepage):
    # Guards the premise: if this ever stops raising, the fix below is moot.
    stream = _legacy_console(codepage)
    with pytest.raises(UnicodeEncodeError):
        stream.write("3 CapCut Tips for Viral \U0001f525 Shorts")
        stream.flush()


@pytest.mark.parametrize("codepage", ["cp950", "cp1252", "cp437"])
def test_force_utf8_stdio_makes_titles_printable(codepage, monkeypatch):
    out, err = _legacy_console(codepage), _legacy_console(codepage)
    monkeypatch.setattr(cli.sys, "stdout", out)
    monkeypatch.setattr(cli.sys, "stderr", err)

    force_utf8_stdio()

    out.write("3 CapCut Tips for Viral \U0001f525 Shorts — pick one:")
    out.flush()
    assert out.encoding == "utf-8"
    assert err.encoding == "utf-8"


def test_force_utf8_stdio_survives_a_stream_without_reconfigure(monkeypatch):
    # pytest's capture, and anything else that swaps in a plain buffer.
    monkeypatch.setattr(cli.sys, "stdout", io.StringIO())
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())
    force_utf8_stdio()  # must not raise


def test_force_utf8_stdio_survives_a_detached_stream(monkeypatch):
    class _Detached:
        encoding = "cp950"

        def reconfigure(self, **kw):
            raise ValueError("underlying buffer has been detached")

    monkeypatch.setattr(cli.sys, "stdout", _Detached())
    monkeypatch.setattr(cli.sys, "stderr", _Detached())
    force_utf8_stdio()  # must not raise


def test_the_cli_entry_point_fixes_its_console_before_parsing():
    """Nothing asserted that `main` actually calls the fix.

    The tests above call `force_utf8_stdio` directly, so they kept passing with
    the call removed from `main` -- the function was covered, its one production
    caller was not.
    """
    import inspect

    src = inspect.getsource(cli.main)
    assert "force_utf8_stdio()" in src


def test_a_path_with_undecodable_bytes_prints_instead_of_crashing(monkeypatch):
    """Why `errors="replace"` is load-bearing even though the target is UTF-8.

    Every str encodes to UTF-8 -- except one carrying lone surrogates, which is
    exactly what `os.listdir` hands back for a filename whose bytes are not valid
    UTF-8 (PEP 383 surrogateescape). `show` prints stored file paths, so this
    reaches a real console through a real code path.
    """
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp950")
    monkeypatch.setattr(cli.sys, "stdout", out)
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    force_utf8_stdio()

    out.write("data/videos/yt_\udcff\udcfe.mp4")  # lone surrogates, from the filesystem
    out.flush()
