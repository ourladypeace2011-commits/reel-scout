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

## 當前任務：Task A1 — MCP Server

**完整執行計畫**: `docs/task-a1-mcp-handover.md`

### 改動範圍
| 檔案 | 改動 |
|------|------|
| `reel_scout/mcp/__init__.py` | 新建（空 init） |
| `reel_scout/mcp/server.py` | 新建：stdio JSON-RPC transport (~120 行) |
| `reel_scout/mcp/tools.py` | 新建：5 個 MCP tool handlers (~200 行) |
| `tests/test_mcp.py` | 新建：8 個測試 |
| `pyproject.toml` | 加 `reel-scout-mcp` entry point |

### 不改的檔案
- `db.py`, `config.py`, `cli.py`, `crawl/`, `transcribe/`, `vision/`, `analyze/`, `export/` — MCP layer 只調用這些模組，不修改

### Commit Strategy
單一 commit: `feat(mcp): add MCP server with stdio transport and 5 tools`

### 自審 Checklist（完成後必填）
見 `docs/task-a1-mcp-handover.md` 底部的完整 checklist。
