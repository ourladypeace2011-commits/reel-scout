from __future__ import annotations

import json
import os
import re
import subprocess
from typing import List, Optional

from .base import BaseCrawler, VideoMeta
from .rate_limiter import get_limiter
from .. import config


class InstagramCrawler(BaseCrawler):
    platform = "instagram"

    # Single post/reel URL
    _SINGLE_RE = re.compile(r"instagram\.com/(?:p|reel|reels)/([a-zA-Z0-9_-]+)")
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
        base_cmd = ["yt-dlp"]
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
                f"yt-dlp IG metadata failed (need cookies?): {result.stderr[:500]}"
            )

        info = json.loads(result.stdout)

        # Download
        dl_cmd = base_cmd + [
            "-f", "bestvideo+bestaudio/best",
            "--merge-output-format", "mp4",
            "-o", output_template,
            url,
        ]
        result = subprocess.run(
            dl_cmd, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp IG download failed: {result.stderr[:500]}")

        expected = os.path.join(output_dir, f"ig_{post_id}.mp4")
        file_path = expected if os.path.exists(expected) else ""
        file_size = os.path.getsize(file_path) if file_path else 0

        return VideoMeta(
            platform=self.platform,
            platform_id=post_id,
            url=url,
            title=info.get("title", info.get("description", "")[:100]),
            uploader=info.get("uploader", info.get("uploader_id", "")),
            duration_sec=float(info.get("duration", 0)),
            upload_date=info.get("upload_date", ""),
            file_path=file_path,
            file_size_bytes=file_size,
        )

    def browse(self, url: str, limit: int = 30) -> List[VideoMeta]:
        """List reels from an Instagram profile page using yt-dlp --flat-playlist.

        Returns VideoMeta entries with metadata only (no downloaded files).
        Requires cookies for most profiles.
        """
        base_cmd = ["yt-dlp"]
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
            raise RuntimeError(
                f"yt-dlp browse failed (need cookies?): {result.stderr[:500]}"
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
