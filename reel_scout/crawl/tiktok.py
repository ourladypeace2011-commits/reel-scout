from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Optional

from .base import BaseCrawler, VideoMeta
from .rate_limiter import get_limiter
from . import ytdlp
from .. import config
from .. import ffprobe


class TikTokCrawler(BaseCrawler):
    platform = "tiktok"

    def extract_id(self, url: str) -> str:
        # Handle tiktok.com/@user/video/ID or vm.tiktok.com/CODE
        m = re.search(r"tiktok\.com/@[^/]+/video/(\d+)", url)
        if m:
            return m.group(1)
        # Short URL — use the full URL as ID (will resolve on download)
        m = re.search(r"vm\.tiktok\.com/([a-zA-Z0-9]+)", url)
        if m:
            return m.group(1)
        # Fallback: hash the URL
        import hashlib
        return hashlib.md5(url.encode()).hexdigest()[:12]

    def download(self, url: str, output_dir: Optional[str] = None) -> VideoMeta:
        if output_dir is None:
            output_dir = config.VIDEOS_DIR

        limiter = get_limiter(self.platform)
        limiter.wait()

        vid = self.extract_id(url)
        output_template = os.path.join(output_dir, f"tt_{vid}.%(ext)s")

        # Get metadata
        meta_cmd = ytdlp.cmd("--dump-json", "--no-download", url)
        result = subprocess.run(
            meta_cmd, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp TikTok metadata failed: {ytdlp.format_error(result.stderr)}")

        info = json.loads(result.stdout)

        # Update vid if we got the real ID from metadata
        real_id = info.get("id", vid)
        if real_id != vid:
            vid = real_id
            output_template = os.path.join(output_dir, f"tt_{vid}.%(ext)s")

        # Download
        # Same bare selector instagram.py had; no clip in the audited library
        # came from here, which is exactly why it went unnoticed.
        dl_cmd = ytdlp.cmd(
            "-f", ytdlp.apple_safe_format(),
            "--merge-output-format", "mp4",
            "-o", output_template,
            url,
        )
        result = subprocess.run(
            dl_cmd, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp TikTok download failed: {ytdlp.format_error(result.stderr)}")

        expected = os.path.join(output_dir, f"tt_{vid}.mp4")
        file_path = expected if os.path.exists(expected) else ""
        file_size = os.path.getsize(file_path) if file_path else 0

        if file_path:
            # The selector states a preference; yt-dlp is free to walk down to
            # an unconstrained rung. Until this line existed, nothing anywhere
            # measured the file that actually landed -- the clip downloaded,
            # analyzed and scored perfectly and failed only when a human opened
            # it on a phone. Warn, never fail: a bad codec is still worth its
            # transcript, keyframes and score.
            ffprobe.warn_if_not_apple_playable(file_path, os.path.basename(file_path))

        return VideoMeta(
            platform=self.platform,
            platform_id=vid,
            url=url,
            title=info.get("title", info.get("description", "")[:100]),
            uploader=info.get("uploader", info.get("creator", "")),
            duration_sec=float(info.get("duration", 0)),
            upload_date=info.get("upload_date", ""),
            file_path=file_path,
            file_size_bytes=file_size,
        )
