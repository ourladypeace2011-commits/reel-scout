from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Optional

from .base import BaseCrawler, VideoMeta
from .rate_limiter import get_limiter
from .. import config


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
        meta_cmd = ["yt-dlp", "--dump-json", "--no-download", url]
        result = subprocess.run(
            meta_cmd, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp TikTok metadata failed: {result.stderr[:500]}")

        info = json.loads(result.stdout)

        # Update vid if we got the real ID from metadata
        real_id = info.get("id", vid)
        if real_id != vid:
            vid = real_id
            output_template = os.path.join(output_dir, f"tt_{vid}.%(ext)s")

        # Download
        dl_cmd = [
            "yt-dlp",
            "-f", "bestvideo+bestaudio/best",
            "--merge-output-format", "mp4",
            "-o", output_template,
            url,
        ]
        result = subprocess.run(
            dl_cmd, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp TikTok download failed: {result.stderr[:500]}")

        expected = os.path.join(output_dir, f"tt_{vid}.mp4")
        file_path = expected if os.path.exists(expected) else ""
        file_size = os.path.getsize(file_path) if file_path else 0

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
