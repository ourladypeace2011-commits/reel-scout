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
from .. import ffprobe
from ..utils.stderr import warn


class InstagramCrawler(BaseCrawler):
    platform = "instagram"

    # Single post/reel URL. The account-scoped form
    # (instagram.com/<handle>/reel/<code>/) is what Instagram's own share
    # button produces, so it must parse as readily as the canonical form.
    _SINGLE_RE = re.compile(
        r"instagram\.com/(?:[a-zA-Z0-9_.]+/)?(?:p|reel|reels)/([a-zA-Z0-9_-]+)"
    )
    # Profile/channel page (with optional /reels/ tab)
    _PROFILE_RE = re.compile(r"instagram\.com/([a-zA-Z0-9_.]+)(?:/reels)?/?$")

    def is_profile_url(self, url: str) -> bool:
        """Return True if the URL points to a profile/reels page, not a single post."""
        return bool(self._PROFILE_RE.search(url)) and not self._SINGLE_RE.search(url)

    def extract_id(self, url: str) -> str:
        # Handle /p/CODE/, /reel/CODE/, /reels/CODE/
        m = self._SINGLE_RE.search(url)
        if m:
            return m.group(1)
        raise ValueError(f"Cannot extract Instagram post ID from: {url}")

    def download(self, url: str, output_dir: Optional[str] = None) -> VideoMeta:
        if output_dir is None:
            output_dir = config.VIDEOS_DIR

        limiter = get_limiter(self.platform)
        limiter.wait()

        post_id = self.extract_id(url)
        output_template = os.path.join(output_dir, f"ig_{post_id}.%(ext)s")

        # Build command with cookies if available
        base_cmd = list(ytdlp.base_cmd())
        cookies = config.IG_COOKIES_FILE
        if cookies and os.path.exists(cookies):
            base_cmd.extend(["--cookies", cookies])

        # Get metadata
        meta_cmd = base_cmd + ["--dump-json", "--no-download", url]
        result = subprocess.run(
            meta_cmd, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"yt-dlp IG metadata failed (need cookies?): {ytdlp.format_error(result.stderr)}"
            )

        info = json.loads(result.stdout)

        # Download
        # Was a bare "bestvideo+bestaudio/best" -- no codec condition at all,
        # not even the av01 exclusion youtube.py carried. 88 of the 103
        # unplayable files in the 2026-08-25 audit came through this line.
        dl_cmd = base_cmd + [
            "-f", ytdlp.apple_safe_format(),
            "--merge-output-format", "mp4",
            "-o", output_template,
            url,
        ]
        result = subprocess.run(
            dl_cmd, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp IG download failed: {ytdlp.format_error(result.stderr)}")

        expected = os.path.join(output_dir, f"ig_{post_id}.mp4")
        file_path = expected if os.path.exists(expected) else ""
        file_size = os.path.getsize(file_path) if file_path else 0

        if file_path:
            # The selector states a preference; yt-dlp is free to walk down to
            # an unconstrained rung. Until this line existed, nothing anywhere
            # measured the file that actually landed -- the clip downloaded,
            # analyzed and scored perfectly and failed only when a human opened
            # it on a phone. Warn, never fail: a bad codec is still worth its
            # transcript, keyframes and score.
            try:
                ffprobe.warn_if_not_apple_playable(file_path, os.path.basename(file_path))
            except Exception as exc:  # noqa: BLE001 - see below
                # The promise "a failed probe never costs a download that already
                # succeeded" has to be kept here, where the download is. ffprobe
                # catches the failures it knows about; this catches the ones it
                # does not. A missing binary must not turn a working clip into an
                # exception three frames up the stack.
                warn("  codec check skipped: %r" % (exc,))

        # yt-dlp routinely reports no duration for Instagram. Writing 0.0 is
        # worse than it looks: it is a *real* value, so the COALESCE-based
        # repair paths downstream treat duration as already known and never
        # correct it. We hold the file already, so measure it.
        duration = float(info.get("duration") or 0)
        if not duration and file_path:
            duration = ffprobe.probe_duration(file_path) or 0.0

        return VideoMeta(
            platform=self.platform,
            platform_id=post_id,
            url=url,
            title=info.get("title", info.get("description", "")[:100]),
            uploader=info.get("uploader", info.get("uploader_id", "")),
            duration_sec=duration,
            upload_date=info.get("upload_date", ""),
            file_path=file_path,
            file_size_bytes=file_size,
        )

    def browse(self, url: str, limit: int = 30) -> List[VideoMeta]:
        """List reels from an Instagram profile page using yt-dlp --flat-playlist.

        Returns VideoMeta entries with metadata only (no downloaded files).
        Requires cookies for most profiles.
        """
        base_cmd = list(ytdlp.base_cmd())
        cookies = config.IG_COOKIES_FILE
        if cookies and os.path.exists(cookies):
            base_cmd.extend(["--cookies", cookies])

        cmd = base_cmd + [
            "--flat-playlist",
            "--dump-json",
            "--no-download",
            "--playlist-end", str(limit),
            url,
        ]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            # yt-dlp's IG extractor breaks periodically (roadmap Non-goals #1). Try
            # the instaloader fallback (optional `instagram` extra) before giving up;
            # if it isn't installed, surface the original yt-dlp error unchanged.
            yt_err = ytdlp.format_error(result.stderr)
            try:
                return self._browse_instaloader(url, limit)
            except ImportError:
                raise RuntimeError(
                    "yt-dlp browse failed (need cookies?): %s "
                    "(install the `instagram` extra for an instaloader fallback)" % yt_err
                )
            except Exception as e:  # noqa: BLE001 — report both failures
                raise RuntimeError(
                    "yt-dlp browse failed (%s); instaloader fallback also failed: %s"
                    % (yt_err, e)
                )

        entries = []
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            try:
                info = json.loads(line)
            except json.JSONDecodeError:
                continue

            platform_id = info.get("id", "")
            entry_url = info.get("url") or info.get("webpage_url", "")
            if not entry_url and platform_id:
                entry_url = f"https://www.instagram.com/reel/{platform_id}/"

            entries.append(VideoMeta(
                platform=self.platform,
                platform_id=platform_id,
                url=entry_url,
                title=info.get("title", info.get("description", ""))[:100] if info.get("title") or info.get("description") else "",
                uploader=info.get("uploader", info.get("uploader_id", "")),
                duration_sec=float(info.get("duration") or 0),
                upload_date=info.get("upload_date", ""),
            ))

        return entries

    def _browse_instaloader(self, url: str, limit: int) -> List[VideoMeta]:
        """Fallback profile browse via instaloader (optional `instagram` extra),
        used when yt-dlp's IG extractor is down. Raises ImportError when the extra
        isn't installed so the caller can keep the original yt-dlp error."""
        import instaloader  # ImportError -> caller surfaces the yt-dlp error

        m = self._PROFILE_RE.search(url)
        if not m:
            raise ValueError("Not an Instagram profile URL: %s" % url)
        username = m.group(1)

        loader = instaloader.Instaloader(
            quiet=True, download_pictures=False, download_videos=False,
            download_comments=False, save_metadata=False,
        )
        profile = instaloader.Profile.from_username(loader.context, username)
        entries: List[VideoMeta] = []
        for post in profile.get_posts():
            if len(entries) >= limit:  # checked first so limit=0 yields 0, not 1
                break
            if not getattr(post, "is_video", False):
                continue
            entries.append(VideoMeta(
                platform=self.platform,
                platform_id=post.shortcode,
                url="https://www.instagram.com/reel/%s/" % post.shortcode,
                title=(post.caption or "")[:100],
                uploader=username,
                duration_sec=float(getattr(post, "video_duration", 0) or 0),
                upload_date=post.date_utc.strftime("%Y%m%d"),
            ))
        return entries
