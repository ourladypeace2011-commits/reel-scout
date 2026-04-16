# Reel Scout

Short-form video analysis CLI tool.

## Stack
- Python 3.9+ (strict: no match/case, no 3.10+ syntax)
- yt-dlp for video crawling
- faster-whisper / whisper.cpp for transcription
- oMLX / Ollama for VLM visual analysis
- SQLite for state tracking
- argparse for CLI (no click/typer)
- urllib for HTTP (no requests dependency)

## IG Cookies
- Browse/crawl IG requires cookies: `cookies.txt` (Netscape format) in project root
- Export from Chrome extension "Get cookies.txt LOCALLY" on instagram.com page
- yt-dlp IG user extractor is broken as of 2026.4; `browse` falls back to instaloader for profile listing
- After analysis, ask user if downloaded videos should be kept or deleted

## Architecture
- `reel_scout/crawl/` — per-platform downloaders via yt-dlp (+ browse via --flat-playlist)
- `reel_scout/transcribe/` — whisper backends
- `reel_scout/vision/` — keyframe extraction + VLM
- `reel_scout/analyze/` — pipeline orchestrator + merger
- `reel_scout/export/` — JSON/CSV/vector DB output
- `reel_scout/db.py` — SQLite schema + CRUD
- `reel_scout/config.py` — env-based config

## Rules
- All HTTP calls use urllib.request, not requests
- Use `from __future__ import annotations` in all files
- Use `typing.Optional`, `typing.List`, `typing.Dict` (not built-in generics)
- SQLite WAL mode for safety
- Sequential processing (transcribe all, then VLM all) to avoid memory pressure
