# Reel Scout — Agent Instructions

## Project Overview
Short-form video analysis CLI tool. Crawls YT Shorts / IG Reels / TikTok via yt-dlp, transcribes with Whisper, analyzes visuals with VLM, and outputs structured JSON.

## Constraints
- **Python 3.9 strict** — no match/case, no walrus in complex expr, no 3.10+ syntax
- All files must have `from __future__ import annotations`
- Use `typing.Optional`, `typing.List`, `typing.Dict` (not `list[str]`, `dict[str, X]`)
- HTTP calls use `urllib.request`, not `requests`
- No hardcoded IPs, API keys, or passwords
- Tests use `pytest`; run `pytest -v` to verify

## 已完成任務
- **Task A1 — MCP Server**: `docs/task-a1-mcp-handover.md` — PASS with notes ✓
- **Task E1 — LLM Backend**: `docs/task-e1-llm-backend-handover.md` — PASS ✓

## 當前任務：Task B1 — video-analyzer 研究

**完整執行計畫**: `docs/task-b1-video-analyzer-research-handover.md`

### 任務性質
唯讀研究任務。Clone byjlw/video-analyzer，分析 frame sampling / VLM prompt / output schema，與 Reel Scout 對比後提出改進建議。

### 產出
- `docs/video-analyzer-research.md` — 結構化研究報告

### 不改的檔案
所有現有檔案都不改。

### Commit Strategy
單一 commit: `docs(research): video-analyzer architecture analysis and improvement recommendations`

### 自審 Checklist（完成後必填）
見 `docs/task-b1-video-analyzer-research-handover.md` 底部的完整 checklist。
