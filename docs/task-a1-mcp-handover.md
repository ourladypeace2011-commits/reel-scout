# Task A1: MCP Server — Codex Handover 執行計畫

## Context
Reel Scout 是短影音分析 CLI 工具，目前所有操作都透過 `python -m reel_scout.cli` 執行。為了讓 Claude Code 直接操作 reel-scout（不用手動打 CLI 指令），需要建立 MCP (Model Context Protocol) server，將核心功能暴露為 MCP tools。

觸發原因：MCP 整合是 Phase 2 最高優先級，解鎖後所有後續開發都能透過 Claude Code 直接驗證。

預期成果：`python -m reel_scout.mcp.server` 啟動 stdio transport MCP server，提供 5 個 tools。

## Repo / Constraints
- Repo: `C:\Users\user\reel-scout\`（也適用 Unix 路徑）
- Python: 3.9（禁止 match/case、walrus in complex expr、3.10+ 語法）
- 所有檔案必須有 `from __future__ import annotations`
- HTTP 用 urllib，不用 requests
- **不使用 MCP Python SDK**（需要 3.10+），自寫 stdio JSON-RPC handler
- 現有測試: 4 個（tests/test_db.py），全部通過

## 執行順序與依賴

```
[Step 1] mcp/__init__.py + mcp/server.py (stdio JSON-RPC transport)
    ↓
[Step 2] mcp/tools.py (5 個 tool handlers)
    ↓
[Step 3] tests/test_mcp.py
    ↓
[Step 4] pyproject.toml 更新
```

## 逐步驟實作細節

### Step 1: mcp/server.py — stdio JSON-RPC transport (~100 行)

- 檔案: `reel_scout/mcp/__init__.py` (空 init)
- 檔案: `reel_scout/mcp/server.py` (~120 行)

MCP 協議基於 JSON-RPC 2.0 over stdio。實作需求：

```python
# server.py 核心邏輯：
# 1. 從 stdin 讀 JSON-RPC request（每行一個 JSON）
# 2. 根據 method dispatch 到 handler
# 3. 將 result 寫回 stdout（JSON-RPC response）
#
# 必須支援的 MCP methods:
# - "initialize" → 回傳 server info + capabilities
# - "tools/list" → 回傳 5 個 tool 定義（name, description, inputSchema）
# - "tools/call" → dispatch 到 tools.py 的 handler
# - "notifications/initialized" → 忽略（notification 不需回應）
#
# 注意事項：
# - stdout 是 MCP transport，絕對不能 print() 任何非 JSON-RPC 的東西
# - stderr 可以用來 debug log
# - 每個 response 結尾必須有 \n
# - 讀取格式：Content-Length header + \r\n\r\n + JSON body（HTTP-like framing）
#   或者用 line-delimited JSON（每行一個 JSON），看 MCP spec 決定
```

MCP stdio transport 使用 **Content-Length header framing**（類似 LSP）：
```
Content-Length: 123\r\n
\r\n
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}
```

回應也用同樣格式：
```
Content-Length: 456\r\n
\r\n
{"jsonrpc":"2.0","id":1,"result":{...}}
```

server.py 結構：

```python
from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional

from . import tools

SERVER_INFO = {
    "name": "reel-scout",
    "version": "0.1.0",
}

CAPABILITIES = {
    "tools": {},
}


def read_message() -> Optional[Dict[str, Any]]:
    """Read a JSON-RPC message from stdin using Content-Length framing."""
    # 讀 headers 直到空行
    # 解析 Content-Length
    # 讀 body
    ...


def write_message(msg: Dict[str, Any]) -> None:
    """Write a JSON-RPC message to stdout using Content-Length framing."""
    body = json.dumps(msg)
    header = f"Content-Length: {len(body)}\r\n\r\n"
    sys.stdout.write(header + body)
    sys.stdout.flush()


def handle_request(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Route JSON-RPC request to handler. Returns response or None for notifications."""
    method = req.get("method", "")
    req_id = req.get("id")  # None for notifications
    params = req.get("params", {})
    
    if method == "initialize":
        return _rpc_result(req_id, {
            "protocolVersion": "2024-11-05",
            "serverInfo": SERVER_INFO,
            "capabilities": CAPABILITIES,
        })
    elif method == "notifications/initialized":
        return None  # notification, no response
    elif method == "tools/list":
        return _rpc_result(req_id, {"tools": tools.list_tools()})
    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = tools.call_tool(tool_name, arguments)
        return _rpc_result(req_id, result)
    else:
        return _rpc_error(req_id, -32601, f"Method not found: {method}")


def _rpc_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}

def _rpc_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def main() -> None:
    """Main loop: read requests, handle, respond."""
    while True:
        msg = read_message()
        if msg is None:
            break
        response = handle_request(msg)
        if response is not None:
            write_message(response)


if __name__ == "__main__":
    main()
```

### Step 2: mcp/tools.py — 5 個 tool handlers (~200 行)

- 檔案: `reel_scout/mcp/tools.py`

每個 tool 直接用 `db.py` 的函數取資料，回傳 MCP content format：

```python
from __future__ import annotations

import json
from typing import Any, Dict, List

from .. import config, db


def list_tools() -> List[Dict[str, Any]]:
    """Return MCP tool definitions."""
    return [
        {
            "name": "crawl",
            "description": "Download short-form videos from YouTube, Instagram, or TikTok",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "urls": {"type": "array", "items": {"type": "string"}, "description": "Video URLs to download"},
                    "cookies": {"type": "string", "description": "Path to cookies file (for Instagram)"},
                },
                "required": ["urls"],
            },
        },
        {
            "name": "analyze",
            "description": "Full pipeline: download + transcribe + vision analysis + structured merge",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "urls": {"type": "array", "items": {"type": "string"}, "description": "Video URLs"},
                    "skip_vision": {"type": "boolean", "default": False},
                    "skip_transcribe": {"type": "boolean", "default": False},
                    "wait": {"type": "boolean", "default": True, "description": "If false, return batch_id immediately"},
                },
                "required": ["urls"],
            },
        },
        {
            "name": "list_videos",
            "description": "List analyzed videos with optional filters",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status (downloaded, transcribed, analyzed)"},
                    "platform": {"type": "string", "description": "Filter by platform (youtube, instagram, tiktok)"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        },
        {
            "name": "show_video",
            "description": "Show full analysis for a specific video",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string", "description": "Video ID to show"},
                },
                "required": ["video_id"],
            },
        },
        {
            "name": "export",
            "description": "Export analyses to JSON or CSV",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": ["json", "csv"], "default": "json"},
                    "output": {"type": "string", "default": "./export"},
                },
            },
        },
    ]


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch tool call to handler. Returns MCP tool result."""
    handlers = {
        "crawl": _tool_crawl,
        "analyze": _tool_analyze,
        "list_videos": _tool_list_videos,
        "show_video": _tool_show_video,
        "export": _tool_export,
    }
    handler = handlers.get(name)
    if handler is None:
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
    try:
        return handler(arguments)
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}


def _tool_crawl(args: Dict[str, Any]) -> Dict[str, Any]:
    """Download videos and return metadata."""
    # 用 crawl module 下載
    # 回傳每支影片的 metadata (video_id, title, platform, duration)
    ...

def _tool_analyze(args: Dict[str, Any]) -> Dict[str, Any]:
    """Run full pipeline."""
    # wait=True: 同步跑完回傳結果
    # wait=False: 建 batch 回傳 batch_id
    ...

def _tool_list_videos(args: Dict[str, Any]) -> Dict[str, Any]:
    """List videos from DB."""
    config.ensure_dirs()
    conn = db.init_db()
    videos = db.list_videos(
        conn,
        status=args.get("status"),
        platform=args.get("platform"),
        limit=args.get("limit", 50),
    )
    result = []
    for v in videos:
        result.append({
            "video_id": v["id"],
            "platform": v["platform"],
            "title": v["title"],
            "status": v["status"],
            "duration_sec": v["duration_sec"],
            "url": v["url"],
        })
    conn.close()
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

def _tool_show_video(args: Dict[str, Any]) -> Dict[str, Any]:
    """Show full video analysis."""
    # 回傳 video metadata + transcript + analysis JSON
    ...

def _tool_export(args: Dict[str, Any]) -> Dict[str, Any]:
    """Export analyses."""
    # 呼叫 export module，回傳匯出檔案數
    ...
```

**注意事項：**
- `_tool_crawl` 和 `_tool_analyze` 內部呼叫的下載/pipeline 可能耗時長，stdout 不能有任何非 JSON-RPC 輸出
- 所有 print() 必須導向 stderr：`print(..., file=sys.stderr)`
- pipeline.py 現在直接 print()，在 MCP 模式下需要 redirect 或 suppress

### Step 3: tests/test_mcp.py (~80 行)

- 檔案: `tests/test_mcp.py`

測試重點（不需要網路、不需要真影片）：

```python
# 測試 1: list_tools() 回傳 5 個 tool 定義
# 測試 2: call_tool("list_videos", {}) 回傳空 list（空 DB）
# 測試 3: call_tool("show_video", {"video_id": "nonexist"}) 回傳 error
# 測試 4: call_tool("unknown", {}) 回傳 isError=True
# 測試 5: handle_request initialize 回傳 serverInfo
# 測試 6: handle_request tools/list 回傳 5 tools
# 測試 7: read_message / write_message round-trip（用 StringIO mock stdin/stdout）
```

用 `tempfile.mkstemp` 建暫時 DB 測試，不碰真實 data/。

### Step 4: pyproject.toml 更新

- 檔案: `pyproject.toml`

加入：
```toml
[project.scripts]
reel-scout = "reel_scout.cli:main"
reel-scout-mcp = "reel_scout.mcp.server:main"
```

## 不改的檔案

| 檔案 | 原因 |
|------|------|
| `db.py` | MCP tools 直接用現有 DB 函數，不需改 |
| `config.py` | MCP server 用相同 env-based config |
| `crawl/*.py` | MCP tools 呼叫現有 crawler |
| `transcribe/*.py` | 不改 |
| `vision/*.py` | 不改 |
| `analyze/merger.py` | 不改 |
| `analyze/pipeline.py` | MCP 直接呼叫 `pipeline.run()`，但需注意 stdout redirect — 如果 pipeline.py 的 print() 導致問題，可以在 mcp/tools.py 裡用 contextlib.redirect_stdout(sys.stderr) 包裹呼叫，**不改 pipeline.py 本身** |

## 測試計畫

| # | 測試名 | 驗證什麼 |
|---|--------|---------|
| 1 | test_list_tools_count | list_tools() 回傳 5 個 tool |
| 2 | test_list_tools_schema | 每個 tool 有 name, description, inputSchema |
| 3 | test_call_list_videos_empty | 空 DB 回傳空 list |
| 4 | test_call_show_video_not_found | 不存在的 video_id 回傳適當訊息 |
| 5 | test_call_unknown_tool | 未知 tool 回傳 isError=True |
| 6 | test_handle_initialize | initialize method 回傳 protocolVersion + serverInfo |
| 7 | test_handle_tools_list | tools/list method 回傳 tools array |
| 8 | test_message_framing | read/write Content-Length framing round-trip |

## 自審 Checklist

```
── 基礎 ──
[ ] pytest 通過（原有 4 + 新增 8 = 12）
[ ] 所有 .py 檔有 `from __future__ import annotations`
[ ] 無 match/case 語法
[ ] 無 3.10+ 語法（dict | None, type alias 等）
[ ] typing 用 Optional, List, Dict（不用 list[], dict[]）

── 功能 ──
[ ] `python -m reel_scout.mcp.server` 可啟動不報錯
[ ] initialize request 回傳正確 serverInfo
[ ] tools/list 回傳 5 個 tool 定義
[ ] list_videos tool 可查詢 DB
[ ] show_video tool 回傳結構化 JSON
[ ] export tool 可匯出
[ ] 未知 tool 回傳 isError
[ ] stdout 只有 JSON-RPC 輸出（無 print pollution）

── 整合 ──
[ ] 無硬編碼敏感資訊（IP、API key、password）
[ ] 無未使用 import
[ ] pyproject.toml 的 entry point 可用
[ ] .gitignore 正確（不 commit data/）
```

## 風險與緩解

| 風險 | 緩解 |
|------|------|
| MCP Content-Length framing 實作錯誤 | 寫 round-trip test 驗證 read/write 對稱 |
| pipeline.py 的 print() 污染 stdout | 用 `contextlib.redirect_stdout(sys.stderr)` 包裹 |
| analyze tool 長時間阻塞 | wait=False 模式立即回傳 batch_id |
| stdin EOF 處理 | read_message 回傳 None → main loop break |

## 交付格式
Codex 完成後提交：
1. Git commit(s)：`feat(mcp): add MCP server with stdio transport and 5 tools`
2. 自審報告：逐項填寫上方 checklist
3. 測試輸出：完整 `pytest -v` 輸出
