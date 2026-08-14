from __future__ import annotations

import os
from pathlib import Path


def _parse_env_file(p: Path) -> None:
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            os.environ.setdefault(key, value)


def _env_candidates(env_path: str = ".env"):
    """.env search paths: the CWD (back-compat) then the project root.

    The project-root path is resolved from __file__ (the dir above this
    package), so it is independent of the current working directory.
    """
    return [Path(env_path), Path(__file__).resolve().parent.parent / ".env"]


def _load_env(env_path: str = ".env") -> None:
    """Load .env into os.environ (no external dependency).

    Searches the CWD (back-compat) then the project root. Without the
    project-root fallback, launching the MCP server or the CLI from any other
    CWD silently missed the project's .env, so every backend fell back to the
    built-in defaults (dead omlx:8000) and all VLM/LLM calls failed with
    Connection refused — the "reel-scout MCP cwd footgun". os.environ.setdefault
    keeps real env vars (e.g. REEL_SCOUT_DATA from .mcp.json) authoritative, and
    the first file found wins per key.
    """
    seen = set()
    for p in _env_candidates(env_path):
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp in seen or not p.exists():
            continue
        seen.add(rp)
        _parse_env_file(p)


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
# Default vision model. qwen2.5vl:7b runs ~8s/frame on an M2 Max vs qwen3-vl:8b's
# ~60s/frame (Qwen3-VL offloads vision to CPU under Ollama) for comparable tag
# quality — see arkiv #83. Override with VLM_MODEL.
VLM_MODEL = os.getenv("VLM_MODEL", "qwen2.5vl:7b")
# Fallback vision model: failed frames are retried with this. Skipped gracefully
# (model_available check, logged once) when not installed — the frame is left
# empty for a later vision retry instead of erroring per frame. Aligned w/ arkiv #83.
VLM_FALLBACK_MODEL = os.getenv("VLM_FALLBACK_MODEL", "qwen3-vl:8b")
OMLX_BASE_URL = os.getenv("OMLX_BASE_URL", "http://localhost:8000/v1")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# --- LLM (for merger/scorer) ---
LLM_BACKEND = os.getenv("LLM_BACKEND", "omlx")
LLM_MODEL = os.getenv("LLM_MODEL", "")
# How long one LLM call may take, and how many times to retry a timeout.
#
# The 600s default was hardcoded and unreachable from the outside, which made a
# real failure mode invisible: on 2026-08-11 a 96-clip re-merge lost 3 clips to
# Ollama contention — the same clips re-ran fine 84/125/101s later with the
# machine idle. Contention is an external condition; a fixed constant is an
# internal choice, and the two should not be welded together.
#
# Retries are for timeouts only. A malformed request or a model that does not
# exist fails the same way on attempt three, so retrying those just delays the
# error by LLM_TIMEOUT × LLM_MAX_RETRIES.
#
# Know the worst case before starting a large unattended batch: at these
# defaults one call that keeps timing out costs 600 + 5 + 600 + 10 + 600 =
# 1815s, three times the 600s it cost before. That is the price of surviving
# transient contention, and it is only paid when the calls actually time out —
# but if the model is simply too large for the machine, every call pays it and a
# 100-clip run takes 3x as long to tell you. `LLM_MAX_RETRIES=0` restores the
# old fail-fast behaviour without touching the source.
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "600"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
# Seconds before the first retry; each further retry doubles it. Backoff matters
# because the thing being waited out is another process holding the model.
LLM_RETRY_BACKOFF = float(os.getenv("LLM_RETRY_BACKOFF", "5"))
OPENCLAW_BASE_URL = os.getenv("OPENCLAW_BASE_URL", "http://localhost:18789/v1")
OPENCLAW_MODEL = os.getenv("OPENCLAW_MODEL", "")

# --- Whisper ---
WHISPER_BACKEND = os.getenv("WHISPER_BACKEND", "faster-whisper")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3")
# Language handling. Defaults reproduce prior behavior (detect-once, transcribe).
#   WHISPER_LANGUAGE=""      -> auto-detect (whisper locks one language from the opening window)
#   WHISPER_LANGUAGE="en"    -> force a single language
#   WHISPER_TASK="translate" -> force output to English regardless of source
#   WHISPER_MULTILINGUAL=1   -> per-chunk language detection (faster-whisper >=1.1);
#                               required for code-switching / 中英對照 interviews
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "")
WHISPER_TASK = os.getenv("WHISPER_TASK", "transcribe")
WHISPER_MULTILINGUAL = os.getenv("WHISPER_MULTILINGUAL", "false").lower() in ("true", "1", "yes")
WHISPER_CHUNK_LENGTH = int(os.getenv("WHISPER_CHUNK_LENGTH", "0"))  # 0 = library default
# Anti-hallucination guard (whisper-guard package). Filters silence / low-logprob /
# high-compression / repetition / char-loop segments out of the Whisper transcript
# before it's saved — the same 4-layer guard arkiv/media-manager already run.
# Default on; set WHISPER_GUARD_ENABLED=0 to keep raw Whisper output. Only the
# faster-whisper backend carries the per-segment probabilities the guard needs.
WHISPER_GUARD_ENABLED = os.getenv("WHISPER_GUARD_ENABLED", "true").lower() in ("true", "1", "yes")

# --- Crawl ---
IG_COOKIES_FILE = os.getenv("IG_COOKIES_FILE", "")
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "10"))

# --- Vision ---
KEYFRAME_STRATEGY = os.getenv("KEYFRAME_STRATEGY", "scene")
# Hard cap on keyframes per video. Each keyframe = one local VLM call (NOT a token),
# so this is a compute-cost ceiling, not a token budget. auto_frame_budget() never
# exceeds this. Default 8 (cost-conservative); raise it to let duration-aware
# budgeting (招②) actually spread more frames across longer videos.
KEYFRAME_MAX = int(os.getenv("KEYFRAME_MAX", "8"))
# Above KEYFRAME_LONG_SEC the cap stops being flat and earns frames by the minute,
# because one number cannot serve both a 9-second reel and an 82-minute interview:
# the reel gets sampled about once a second, the interview once every seven minutes,
# and the interview's `timeline` degenerates into one segment covering most of the
# clip. Short-form is untouched — the curve only starts above the threshold, and
# KEYFRAME_MAX_LONG is the ceiling that keeps the VLM bill bounded (82 min asks for
# 164 frames at 2/min and is told 40). See keyframe.frame_cap().
KEYFRAME_LONG_SEC = float(os.getenv("KEYFRAME_LONG_SEC", "180"))
KEYFRAME_PER_MIN = float(os.getenv("KEYFRAME_PER_MIN", "2"))
KEYFRAME_MAX_LONG = int(os.getenv("KEYFRAME_MAX_LONG", "40"))

# Near-duplicate keyframe removal. `frame_cap` above decides how many frames a
# clip may spend; this decides how many it actually needs. They are different
# defects: the cap fixes "long clips sampled too sparsely", this fixes
# "consecutive samples look identical and each one still costs a VLM call".
#
# What is removed is NOT replaced. Backfilling to the cap would cost exactly as
# much as before and would also hide whether this is working, because the count
# would always equal the cap.
#
# KEYFRAME_DEDUPE_DISTANCE is a Hamming distance over a 64-bit dHash; 0 is
# pixel-identical after downscale. 4 is deliberately tight — this should only
# ever remove frames a person would call the same frame.
# KEYFRAME_DEDUPE_MAX_GAP_SEC is the timeline guard: a frame is only dropped
# when the previous KEPT frame is this close in time. Two identical-looking
# frames seventeen minutes apart are information ("nothing changed"); two two
# seconds apart are noise.
# KEYFRAME_DEDUPE_MIN is the count floor, so a static short clip cannot
# collapse to a single frame.
KEYFRAME_DEDUPE = os.getenv("KEYFRAME_DEDUPE", "1") not in ("0", "false", "False", "")
KEYFRAME_DEDUPE_DISTANCE = int(os.getenv("KEYFRAME_DEDUPE_DISTANCE", "4"))
KEYFRAME_DEDUPE_MAX_GAP_SEC = float(os.getenv("KEYFRAME_DEDUPE_MAX_GAP_SEC", "120"))
KEYFRAME_DEDUPE_MIN = int(os.getenv("KEYFRAME_DEDUPE_MIN", "4"))
# Optional upscale (long edge px) applied to extracted keyframes so the VLM can read
# small on-screen text (招④). 0 = keep native resolution (no scaling, default).
KEYFRAME_RESOLUTION = int(os.getenv("KEYFRAME_RESOLUTION", "0"))

# --- Scoring ---
# The ONLY deterministic arithmetic in the whole scoring path. The four dimension
# values themselves come straight out of an LLM call (scorer.score_video) — there
# is no numeric threshold under them to tune, the rubric is English prose in the
# prompt. So `overall` is the one number a reader can legitimately recompute
# without re-invoking the model, which is exactly what makes the inspector's
# weight sliders honest: re-weighting is real arithmetic on stored dimensions,
# not a re-run dressed up as interactivity.
#
# Single source of truth. scorer builds its prompt sentence from this dict and
# ingest.compute_overall reads it, so the two scoring paths cannot drift apart.
SCORE_WEIGHTS = {
    "hook_strength": 0.3,
    "visual_storytelling": 0.25,
    "pacing": 0.2,
    "structure": 0.25,
}
#: Canonical dimension order — display and iteration both follow it.
SCORE_DIMENSIONS = tuple(SCORE_WEIGHTS)

# --- Audio ---
PANNS_MODEL_PATH = os.getenv("PANNS_MODEL_PATH", "")
AUDIO_WINDOW_SEC = float(os.getenv("AUDIO_WINDOW_SEC", "2.0"))
AUDIO_HOP_SEC = float(os.getenv("AUDIO_HOP_SEC", "1.0"))
#: Confidence a window's dominant class must clear to be recorded. Music and
#: speech sustain across a whole clip and clear this easily.
AUDIO_MIN_CONF = float(os.getenv("AUDIO_MIN_CONF", "0.3"))
#: Separate, lower floor for discrete events (a door, a whoosh, a coin drop).
#: They are brief, so a 2-second window is mostly *other* sound and AudioSet
#: scores them far lower than a sustained bed — measured on three reels tagged
#: "SFX", every effect in them landed between 0.06 and 0.23 while the music
#: behind them sat at 0.8+. Holding effects to the dominant-class threshold
#: therefore keeps the beds and discards exactly what someone studying sound
#: design is looking for.
AUDIO_EVENT_MIN_CONF = float(os.getenv("AUDIO_EVENT_MIN_CONF", "0.12"))
#: How many classes per window to consider. argmax alone loses the case where a
#: window is "Music 0.469, Sound effect 0.400" — the second one is the finding.
AUDIO_TOP_K = int(os.getenv("AUDIO_TOP_K", "3"))
#: How many discrete sound events the merge prompt may list individually. Beds
#: (speech/music/silence) are always folded into a coverage percentage instead —
#: at one row per second of runtime they were the whole problem. Past this count
#: the timeline truncates but the per-label inventory is still printed, so
#: "which effects does this video use" survives even when the timings do not.
AUDIO_MERGE_MAX_EVENTS = int(os.getenv("AUDIO_MERGE_MAX_EVENTS", "40"))

# --- OCR / on-screen text (§4F, L3.5) ---
# Collect burned-in on-screen captions with timestamps as an extra signal layer
# (stronger than L2 caption, fills the L3 gap for low-dialogue/visual reels).
OCR_ENABLED = os.getenv("OCR_ENABLED", "true").lower() in ("true", "1", "yes")
# Which engine reads on-screen text:
#   "vlm"       -> reuse what the VLM already read into text_in_frame (zero new deps)
#   "tesseract" -> dedicated OCR of keyframe JPEGs (opt-in; needs the `ocr` extra +
#                  a tesseract binary; falls back to vlm if unavailable). Stronger
#                  CJK, but violates minimal-deps, hence off by default.
OCR_ENGINE = os.getenv("OCR_ENGINE", "vlm")

# --- Shot metrics (§4E evidence-based pacing) ---
# Measure cut rhythm (cuts/min) + audio energy/BPM so the pacing score rests on
# evidence, not LLM vibes. On by default: ffmpeg is already required; energy is
# pure-stdlib; BPM is numpy-gated best-effort (skipped cleanly without numpy).
SHOT_METRICS_ENABLED = os.getenv("SHOT_METRICS_ENABLED", "true").lower() in ("true", "1", "yes")
# Scene-change score threshold for counting a hard cut (matches keyframe scene mode).
SHOT_SCENE_THRESHOLD = float(os.getenv("SHOT_SCENE_THRESHOLD", "0.3"))

# --- External tools ---
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
# yt-dlp binary. Empty = auto-resolve, preferring the copy pinned in this venv
# (`python -m yt_dlp`) over whatever `yt-dlp` is first on PATH — a stale PATH
# build silently produces baffling extractor errors. See crawl/ytdlp.py.
YTDLP_BIN = os.getenv("YTDLP_BIN", "")

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
        f"LLM_TIMEOUT:          {LLM_TIMEOUT:g}s"
        f" (retries={LLM_MAX_RETRIES}, backoff={LLM_RETRY_BACKOFF:g}s)",
        f"OPENCLAW_BASE_URL:    {OPENCLAW_BASE_URL}",
        f"OPENCLAW_MODEL:       {OPENCLAW_MODEL or '(auto)'}",
        f"WHISPER_BACKEND:      {WHISPER_BACKEND}",
        f"WHISPER_MODEL:        {WHISPER_MODEL}",
        f"WHISPER_LANGUAGE:     {WHISPER_LANGUAGE or '(auto)'}",
        f"WHISPER_TASK:         {WHISPER_TASK}",
        f"WHISPER_MULTILINGUAL: {WHISPER_MULTILINGUAL}",
        f"WHISPER_CHUNK_LENGTH: {WHISPER_CHUNK_LENGTH or '(default)'}",
        f"WHISPER_GUARD:        {WHISPER_GUARD_ENABLED}",
        f"IG_COOKIES_FILE:      {IG_COOKIES_FILE or '(not set)'}",
        f"RATE_LIMIT_PER_MINUTE:{RATE_LIMIT_PER_MINUTE}",
        f"KEYFRAME_STRATEGY:    {KEYFRAME_STRATEGY}",
        f"KEYFRAME_MAX:         {KEYFRAME_MAX}",
        f"KEYFRAME_LONG_SEC:    {KEYFRAME_LONG_SEC}",
        f"KEYFRAME_PER_MIN:     {KEYFRAME_PER_MIN}",
        f"KEYFRAME_MAX_LONG:    {KEYFRAME_MAX_LONG}",
        f"KEYFRAME_DEDUPE:      {'on' if KEYFRAME_DEDUPE else 'off'}"
        f" (dist<={KEYFRAME_DEDUPE_DISTANCE},"
        f" gap<={KEYFRAME_DEDUPE_MAX_GAP_SEC:g}s, floor={KEYFRAME_DEDUPE_MIN})",
        f"KEYFRAME_RESOLUTION:  {KEYFRAME_RESOLUTION or '(native)'}",
        f"PANNS_MODEL_PATH:     {PANNS_MODEL_PATH or '(not set)'}",
        f"AUDIO_WINDOW_SEC:     {AUDIO_WINDOW_SEC}",
        f"AUDIO_HOP_SEC:        {AUDIO_HOP_SEC}",
        f"SHOT_METRICS_ENABLED: {SHOT_METRICS_ENABLED}",
        f"SHOT_SCENE_THRESHOLD: {SHOT_SCENE_THRESHOLD}",
        f"OCR_ENABLED:          {OCR_ENABLED}",
        f"OCR_ENGINE:           {OCR_ENGINE}",
        f"FFMPEG_BIN:           {FFMPEG_BIN}",
        f"YTDLP_BIN:            {YTDLP_BIN or '(auto)'}",
        f"DIARIZE_ENABLED:      {DIARIZE_ENABLED}",
        f"PYANNOTE_AUTH_TOKEN:  {'***' if PYANNOTE_AUTH_TOKEN else '(not set)'}",
        f"WEBHOOK_URL:          {WEBHOOK_URL or '(not set)'}",
    ]
    return "\n".join(lines)
