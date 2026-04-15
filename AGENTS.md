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

## 當前任務：Task E1 — LLM Backend 抽象化

**完整執行計畫**: `docs/task-e1-llm-backend-handover.md`

### 改動範圍
| 檔案 | 改動 |
|------|------|
| `reel_scout/llm/__init__.py` | 新建：get_llm() factory |
| `reel_scout/llm/base.py` | 新建：BaseLLM ABC |
| `reel_scout/llm/omlx.py` | 新建：oMLX backend（從 merger.py 提取） |
| `reel_scout/llm/ollama.py` | 新建：Ollama backend |
| `reel_scout/llm/openclaw.py` | 新建：OpenClaw/Claude backend |
| `reel_scout/analyze/merger.py` | 修改：刪 _call_llm()，改用 get_llm() |
| `reel_scout/config.py` | 修改：新增 LLM_BACKEND, OPENCLAW_* 設定 |
| `tests/test_llm.py` | 新建：8 個測試 |
| `.env.example` | 修改：新增 LLM/OpenClaw 設定 |

### 不改的檔案
- `cli.py`, `mcp/`, `crawl/`, `transcribe/`, `vision/`, `db.py`, `pipeline.py`, `export/`

### Commit Strategy
單一 commit: `feat(llm): extract LLM backend abstraction with omlx/ollama/openclaw support`

### 自審 Checklist（完成後必填）
見 `docs/task-e1-llm-backend-handover.md` 底部的完整 checklist。
