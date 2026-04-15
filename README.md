# Reel Scout

Short-form video analysis CLI tool.

Crawl, transcribe, and visually analyze YouTube Shorts, Instagram Reels, and TikTok videos into structured data.

## Install

```bash
pip install -e .
pip install -e ".[whisper]"  # for faster-whisper transcription
```

## Usage

```bash
reel-scout crawl "https://youtube.com/shorts/xxxxx"
reel-scout analyze "https://youtube.com/shorts/xxxxx"
reel-scout analyze --file urls.txt --skip-vision
reel-scout list
reel-scout show <video_id>
reel-scout export --format json -o ./export
reel-scout config check
```

## MCP Server

```bash
reel-scout-mcp  # stdio transport for Claude Code integration
```

## Requirements

- Python 3.9+
- ffmpeg
- yt-dlp
