from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Optional

from .base import BaseCrawler, VideoMeta
from .rate_limiter import get_limiter
from .. import config


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

    def download(self, url: str, output_dir: Optional[str] = None) -> VideoMeta:
        if output_dir is None:
            output_dir = config.VIDEOS_DIR

        limiter = get_limiter(self.platform)
        limiter.wait()

        vid = self.extract_id(url)
        output_template = os.path.join(output_dir, f"yt_{vid}.%(ext)s")

        # First get metadata
        meta_cmd = [
            "yt-dlp",
            "--dump-json",
            "--no-download",
            url,
        ]
        result = subprocess.run(
            meta_cmd, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp metadata failed: {result.stderr[:500]}")

        info = json.loads(result.stdout)

        # Download
        dl_cmd = [
            "yt-dlp",
            "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            "--merge-output-format", "mp4",
            "-o", output_template,
            "--no-playlist",
            url,
        ]
        result = subprocess.run(
            dl_cmd, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp download failed: {result.stderr[:500]}")

        # Find downloaded file
        expected = os.path.join(output_dir, f"yt_{vid}.mp4")
        file_path = expected if os.path.exists(expected) else ""
        file_size = os.path.getsize(file_path) if file_path else 0

        return VideoMeta(
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
