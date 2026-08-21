from __future__ import annotations

import json
import os
import re
import subprocess
from typing import List, Optional

from .base import BaseCrawler, VideoMeta
from .rate_limiter import get_limiter
from . import ytdlp
from .. import config
from ..utils.stderr import warn


def _landed(result, expected: str) -> bool:
    """Did this yt-dlp run actually produce the file we asked for?

    Instagram and TikTok check ``returncode`` and raise on non-zero
    (``instagram.py``, ``tiktok.py``); YouTube was the odd one out, treating
    file existence as the sole success signal and never reading the status at
    all. That made every "a file is sitting there" case -- stale leftovers, a
    partial from a killed run -- indistinguishable from a completed download.

    Both conditions, because either alone lies: a non-zero exit with a file
    present is a partial, and a zero exit with no file is yt-dlp declining to do
    anything at all.
    """
    return result.returncode == 0 and os.path.exists(expected)


class YouTubeCrawler(BaseCrawler):
    platform = "youtube"

    def extract_id(self, url: str) -> str:
        # Handle youtu.be/ID, youtube.com/watch?v=ID, youtube.com/shorts/ID
        patterns = [
            re.compile(r"youtu\.be/([a-zA-Z0-9_-]{11})"),
            re.compile(r"youtube\.com/watch\?.*v=([a-zA-Z0-9_-]{11})"),
            re.compile(r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})"),
        ]
        for p in patterns:
            m = p.search(url)
            if m:
                return m.group(1)
        raise ValueError(f"Cannot extract YouTube video ID from: {url}")

    def _fetch_subtitles(self, url: str, output_template: str) -> None:
        """Fetch native + auto-generated subs alongside the media. Best effort.

        When subs exist the transcribe step can skip local Whisper entirely (招①);
        they are converted to vtt so the stdlib parser can read them. Cloud ASR is
        intentionally NOT used.

        Every failure here is swallowed — no captions, or HTTP 429 from the caption
        endpoint, just means we fall back to local Whisper. 429 is routine and gets
        likelier the more videos you pull, which is exactly what channel crawling
        does, so it must never cost us media we already downloaded.
        """
        cmd = ytdlp.cmd(
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", "en.*,zh.*",
            "--convert-subs", "vtt",
            "-o", output_template,
            "--no-playlist",
            "--remote-components", "ejs:github",
            url,
        )
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except Exception:
            pass

    def download(self, url: str, output_dir: Optional[str] = None) -> VideoMeta:
        if output_dir is None:
            output_dir = config.VIDEOS_DIR

        limiter = get_limiter(self.platform)
        limiter.wait()

        vid = self.extract_id(url)
        output_template = os.path.join(output_dir, f"yt_{vid}.%(ext)s")

        # First get metadata
        meta_cmd = ytdlp.cmd(
            "--dump-json",
            "--no-download",
            "--remote-components", "ejs:github",
            url,
        )
        result = subprocess.run(
            meta_cmd, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp metadata failed: {ytdlp.format_error(result.stderr)}")

        info = json.loads(result.stdout)

        # Media only. Subtitles are fetched by a separate invocation below —
        # asking for both at once means a caption-endpoint failure takes the media
        # down with it. --no-abort-on-error does not prevent that: it governs
        # whether yt-dlp continues to the *next* playlist entry, not whether one
        # entry survives a partial failure.
        # Ask for what Apple's media stack can actually decode: H.264 video
        # with AAC audio. Anything else downloads, analyzes and scores
        # perfectly while the player shows a blank rectangle — with nothing
        # saying why. Chrome decodes it, so the same library plays for one
        # person and not the next: the shape of bug nobody reports correctly.
        #
        # AV1 was the first codec caught doing this (hardware decode only from
        # M3 on; Safari will not fall back to software). VP9 was left alone at
        # the time, with the note "it is the common case here and it plays" —
        # that was measured on desktop Chrome and it is wrong. Safari on
        # iOS/iPadOS decodes VP9 only inside WebM; inside the MP4 container
        # this crawler asks for (--merge-output-format mp4) it plays nothing.
        # Opus audio in MP4 fails on Apple platforms for the same reason.
        #
        # Measured on the real library 2026-08-21, 109 files: 86 vp9+aac,
        # 13 vp9+opus, 2 av1+opus, 2 h264+opus — only 4 were h264+aac. The
        # library was unplayable on iPad while every one of those files played
        # in desktop Chrome, and the av01 filter reported no problem the whole
        # time because AV1 was never the codec doing the damage.
        #
        # The chain degrades rather than fails, and the av01-free rungs stay in
        # the middle: a video published only in VP9 or AV1 still downloads on
        # the tail — a blank player is worse than nothing, but refusing to
        # ingest at all is worse than both.
        expected = os.path.join(output_dir, f"yt_{vid}.mp4")
        # A leftover file at the destination makes yt-dlp exit 0 without
        # transferring anything, and the checks below read that as success.
        ytdlp.clear_unusable_output(expected)

        def _download(selector: str, *extra: str):
            return subprocess.run(
                ytdlp.cmd(
                    "-f", selector,
                    "--merge-output-format", "mp4",
                    "-o", output_template,
                    "--no-playlist",
                    "--remote-components", "ejs:github",
                    *extra,
                    url,
                ),
                capture_output=True, text=True, timeout=300,
            )

        result = _download(
            "bestvideo[height<=720][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
            "best[height<=720][vcodec^=avc1][acodec^=mp4a]/"
            "bestvideo[height<=720][vcodec^=avc1]+bestaudio/"
            "bestvideo[height<=720][vcodec!*=av01]+bestaudio/"
            "best[height<=720][vcodec!*=av01]/"
            "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
        )

        if not _landed(result, expected):
            # The `/` chain above only degrades while *choosing* a format. Once a
            # format is chosen and the download itself dies — 403 on the separate
            # video stream is the one seen in the wild (E8Bx9OlpmdM, 2026-08-13)
            # — yt-dlp does not walk to the next selector. It just fails, and the
            # clip never enters the library.
            #
            # So the fallback has to live here, at the download layer. Progressive
            # (pre-muxed) formats come from a different URL than the separate
            # streams, so they are frequently reachable when the split ones are
            # not: that same 403'd video downloaded fine at `-f 18`.
            #
            # Quality-first stays first. This is the "something beats nothing"
            # floor, not a new default — the previous chain's own comment makes
            # the same trade for AV1.
            #
            # `--no-continue` is load-bearing, not tidiness. yt-dlp resumes a
            # partial by default, and both attempts write to the same
            # `yt_<id>.mp4.part`: if the first selector landed on a progressive
            # format and died mid-transfer, the second would issue a Range
            # request for a *different* format's stream and append it to those
            # bytes. Nothing validates that the two halves are the same video.
            # The result is a corrupt file that exists, so the check below reads
            # it as success — the exact failure this whole change is about.
            #
            # Say so out loud. The fallback usually lands on 360p, and silently
            # handing back a lower-quality clip is its own quiet wrongness.
            warn(
                "  yt-dlp: preferred formats produced no file; retrying with a"
                " progressive format (expect lower quality)"
            )
            result = _download(
                "18/best[ext=mp4][protocol^=http]/best[ext=mp4]/best",
                "--no-continue",
            )

        if not _landed(result, expected):
            # No media produced -> genuine download failure (not a subtitle hiccup).
            raise RuntimeError(f"yt-dlp download failed: {ytdlp.format_error(result.stderr)}")
        file_path = expected
        file_size = os.path.getsize(file_path) if file_path else 0

        self._fetch_subtitles(url, output_template)

        meta = VideoMeta(
            platform=self.platform,
            platform_id=vid,
            url=url,
            title=info.get("title", ""),
            uploader=info.get("uploader", info.get("channel", "")),
            duration_sec=float(info.get("duration", 0)),
            upload_date=info.get("upload_date", ""),
            file_path=file_path,
            file_size_bytes=file_size,
        )
        # Record any subtitle yt-dlp wrote next to the media (en.* / zh.* .vtt).
        if file_path:
            from ..transcribe import find_subtitle
            sub = find_subtitle(file_path)
            if sub:
                meta.extra["subtitle_path"] = sub
        return meta

    def browse(self, url: str, limit: int = 30) -> List[VideoMeta]:
        """List videos from a YouTube channel/playlist page."""
        cmd = ytdlp.cmd(
            "--flat-playlist",
            "--dump-json",
            "--no-download",
            "--playlist-end", str(limit),
            "--remote-components", "ejs:github",
            url,
        )

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp browse failed: {ytdlp.format_error(result.stderr)}")

        entries = []
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            try:
                info = json.loads(line)
            except json.JSONDecodeError:
                continue

            vid = info.get("id", "")
            entry_url = info.get("url") or info.get("webpage_url", "")
            if not entry_url and vid:
                entry_url = f"https://www.youtube.com/watch?v={vid}"

            entries.append(VideoMeta(
                platform=self.platform,
                platform_id=vid,
                url=entry_url,
                title=info.get("title", ""),
                uploader=info.get("uploader", info.get("channel", "")),
                duration_sec=float(info.get("duration") or 0),
                upload_date=info.get("upload_date", ""),
            ))

        return entries
