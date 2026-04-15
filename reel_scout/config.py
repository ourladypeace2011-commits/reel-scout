from __future__ import annotations

import os
from pathlib import Path


def _load_env(env_path: str = ".env") -> None:
    """Load .env file into os.environ (no external dependency)."""
    p = Path(env_path)
    if not p.exists():
        return
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            os.environ.setdefault(key, value)


# Load .env on import
_load_env()

# --- Paths ---
DATA_DIR = os.getenv("REEL_SCOUT_DATA", "./data")
DB_PATH = os.path.join(DATA_DIR, "reel_scout.db")
VIDEOS_DIR = os.path.join(DATA_DIR, "videos")
KEYFRAMES_DIR = os.path.join(DATA_DIR, "keyframes")
ANALYSIS_DIR = os.path.join(DATA_DIR, "analysis")

# --- VLM ---
VLM_BACKEND = os.getenv("VLM_BACKEND", "omlx")
VLM_MODEL = os.getenv("VLM_MODEL", "")
OMLX_BASE_URL = os.getenv("OMLX_BASE_URL", "http://localhost:8000/v1")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# --- LLM (for merger/scorer) ---
LLM_BACKEND = os.getenv("LLM_BACKEND", "omlx")
LLM_MODEL = os.getenv("LLM_MODEL", "")
OPENCLAW_BASE_URL = os.getenv("OPENCLAW_BASE_URL", "http://localhost:18789/v1")
OPENCLAW_MODEL = os.getenv("OPENCLAW_MODEL", "")

# --- Whisper ---
WHISPER_BACKEND = os.getenv("WHISPER_BACKEND", "faster-whisper")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3")

# --- Crawl ---
IG_COOKIES_FILE = os.getenv("IG_COOKIES_FILE", "")
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "10"))

# --- Vision ---
KEYFRAME_STRATEGY = os.getenv("KEYFRAME_STRATEGY", "scene")
KEYFRAME_MAX = int(os.getenv("KEYFRAME_MAX", "8"))

# --- Audio ---
PANNS_MODEL_PATH = os.getenv("PANNS_MODEL_PATH", "")
AUDIO_WINDOW_SEC = float(os.getenv("AUDIO_WINDOW_SEC", "2.0"))
AUDIO_HOP_SEC = float(os.getenv("AUDIO_HOP_SEC", "1.0"))

# --- External tools ---
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")

# --- Diarization ---
DIARIZE_ENABLED = os.getenv("DIARIZE_ENABLED", "false").lower() in ("true", "1", "yes")
PYANNOTE_AUTH_TOKEN = os.getenv("PYANNOTE_AUTH_TOKEN", "")

# --- Optional ---
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")


def ensure_dirs() -> None:
    """Create data directories if they don't exist."""
    for d in [DATA_DIR, VIDEOS_DIR, KEYFRAMES_DIR, ANALYSIS_DIR]:
        os.makedirs(d, exist_ok=True)


def show() -> str:
    """Return resolved config as a formatted string."""
    lines = [
        "Reel Scout Configuration",
        "=" * 40,
        f"DATA_DIR:             {DATA_DIR}",
        f"DB_PATH:              {DB_PATH}",
        f"VLM_BACKEND:          {VLM_BACKEND}",
        f"VLM_MODEL:            {VLM_MODEL or '(auto)'}",
        f"OMLX_BASE_URL:        {OMLX_BASE_URL}",
        f"OLLAMA_BASE_URL:      {OLLAMA_BASE_URL}",
        f"LLM_BACKEND:          {LLM_BACKEND}",
        f"LLM_MODEL:            {LLM_MODEL or '(auto)'}",
        f"OPENCLAW_BASE_URL:    {OPENCLAW_BASE_URL}",
        f"OPENCLAW_MODEL:       {OPENCLAW_MODEL or '(auto)'}",
        f"WHISPER_BACKEND:      {WHISPER_BACKEND}",
        f"WHISPER_MODEL:        {WHISPER_MODEL}",
        f"IG_COOKIES_FILE:      {IG_COOKIES_FILE or '(not set)'}",
        f"RATE_LIMIT_PER_MINUTE:{RATE_LIMIT_PER_MINUTE}",
        f"KEYFRAME_STRATEGY:    {KEYFRAME_STRATEGY}",
        f"KEYFRAME_MAX:         {KEYFRAME_MAX}",
        f"PANNS_MODEL_PATH:     {PANNS_MODEL_PATH or '(not set)'}",
        f"AUDIO_WINDOW_SEC:     {AUDIO_WINDOW_SEC}",
        f"AUDIO_HOP_SEC:        {AUDIO_HOP_SEC}",
        f"FFMPEG_BIN:           {FFMPEG_BIN}",
        f"DIARIZE_ENABLED:      {DIARIZE_ENABLED}",
        f"PYANNOTE_AUTH_TOKEN:  {'***' if PYANNOTE_AUTH_TOKEN else '(not set)'}",
        f"WEBHOOK_URL:          {WEBHOOK_URL or '(not set)'}",
    ]
    return "\n".join(lines)
