from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Optional

from .base import BaseCrawler, VideoMeta
from .rate_limiter import get_limiter
from .. import config


class InstagramCrawler(BaseCrawler):
    platform = "instagram"

    def extract_id(self, url: str) -> str:
        # Handle /p/CODE/, /reel/CODE/, /reels/CODE/
        m = re.search(r"instagram\.com/(?:p|reel|reels)/([a-zA-Z0-9_-]+)", url)
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
