# Task E1: LLM Backend 抽象化 — Codex Handover 執行計畫

## Context
目前 `merger.py` 的 `_call_llm()` 硬編碼呼叫 oMLX（OpenAI-compatible API on localhost:8000）。我們需要支援多個 LLM backend（oMLX、Ollama、OpenClaw/Claude），讓 merger 和未來的 scorer 可以切換。

觸發原因：Phase 2E 的基礎，解鎖 Claude 評分功能。也讓 merger 可以用更強的模型做分析。

預期成果：新增 `reel_scout/llm/` 模組，refactor `merger.py` 使用新 backend 系統，現有功能無 regression。

## Repo / Constraints
- Repo: https://github.com/ourladypeace2011-commits/reel-scout
- Python: 3.9（禁止 match/case、3.10+ 語法）
- 所有 .py 檔必須有 `from __future__ import annotations`
- typing 用 `Optional`, `List`, `Dict`（不用 `list[]`, `dict[]`）
- HTTP 用 `urllib.request`，不用 `requests`
- 現有測試: 13 個，全部通過

## 執行順序與依賴

```
[Step 1] reel_scout/llm/ 模組（base + 3 backends）
    ↓
[Step 2] config.py 新增 LLM 設定
    ↓
[Step 3] refactor merger.py → 用新 backend
    ↓
[Step 4] tests/test_llm.py
    ↓
[Step 5] .env.example 更新
```

## 逐步驟實作細節

### Step 1: reel_scout/llm/ 模組

#### `reel_scout/llm/__init__.py` (~20 行)

```python
from __future__ import annotations

from typing import Optional

from .base import BaseLLM
from .. import config


def get_llm(backend: Optional[str] = None) -> BaseLLM:
    backend = backend or config.LLM_BACKEND
    if backend == "omlx":
        from .omlx import OmlxLLM
        return OmlxLLM(base_url=config.OMLX_BASE_URL, model=config.LLM_MODEL)
    elif backend == "ollama":
        from .ollama import OllamaLLM
        return OllamaLLM(base_url=config.OLLAMA_BASE_URL, model=config.LLM_MODEL)
    elif backend == "openclaw":
        from .openclaw import OpenClawLLM
        return OpenClawLLM(
            base_url=config.OPENCLAW_BASE_URL,
            model=config.OPENCLAW_MODEL,
        )
    else:
        raise ValueError("Unknown LLM backend: %s" % backend)
```

#### `reel_scout/llm/base.py` (~20 行)

```python
from __future__ import annotations

import abc


class BaseLLM(abc.ABC):
    @abc.abstractmethod
    def complete(self, prompt: str, max_tokens: int = 800, temperature: float = 0.1) -> str:
        """Send prompt to LLM and return text response."""
        ...
```

#### `reel_scout/llm/omlx.py` (~40 行)

從 `merger.py` 的 `_call_llm()` 提取。使用 `urllib.request`，呼叫 OpenAI-compatible `/v1/chat/completions`。

```python
from __future__ import annotations

import json
import urllib.request

from .base import BaseLLM


class OmlxLLM(BaseLLM):
    def __init__(self, base_url: str, model: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model

    def complete(self, prompt: str, max_tokens: int = 800, temperature: float = 0.1) -> str:
        payload = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if self._model:
            payload["model"] = self._model

        url = "%s/chat/completions" % self._base_url
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result["choices"][0]["message"]["content"]
```

#### `reel_scout/llm/ollama.py` (~40 行)

Ollama 的 `/api/generate` endpoint，跟 oMLX 不同格式：

```python
from __future__ import annotations

import json
import urllib.request

from .base import BaseLLM


class OllamaLLM(BaseLLM):
    def __init__(self, base_url: str, model: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model or "llama3"

    def complete(self, prompt: str, max_tokens: int = 800, temperature: float = 0.1) -> str:
        payload = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }
        url = "%s/api/generate" % self._base_url
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result.get("response", "")
```

#### `reel_scout/llm/openclaw.py` (~50 行)

OpenClaw proxy 是 OpenAI-compatible，跟 omlx.py **幾乎相同**，但：
- 可能需要 `Authorization` header（如果 OpenClaw 配了 key）
- model 名稱不同（例如 `claude-sonnet-4-5`）
- 加上 `OPENCLAW_API_KEY` 支援（optional）

```python
from __future__ import annotations

import json
import os
import urllib.request

from .base import BaseLLM


class OpenClawLLM(BaseLLM):
    def __init__(self, base_url: str, model: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = os.getenv("OPENCLAW_API_KEY", "")

    def complete(self, prompt: str, max_tokens: int = 800, temperature: float = 0.1) -> str:
        payload = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if self._model:
            payload["model"] = self._model

        url = "%s/chat/completions" % self._base_url
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = "Bearer %s" % self._api_key

        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result["choices"][0]["message"]["content"]
```

### Step 2: config.py 新增 LLM 設定

在 `config.py` 的 `# --- VLM ---` 區塊**之後**新增：

```python
# --- LLM (for merger/scorer) ---
LLM_BACKEND = os.getenv("LLM_BACKEND", "omlx")  # omlx, ollama, openclaw
LLM_MODEL = os.getenv("LLM_MODEL", "")  # model name for text LLM
OPENCLAW_BASE_URL = os.getenv("OPENCLAW_BASE_URL", "http://localhost:18789/v1")
OPENCLAW_MODEL = os.getenv("OPENCLAW_MODEL", "")  # e.g. claude-sonnet-4-5
```

在 `show()` 函數中追加這些設定的顯示。

### Step 3: refactor merger.py

**刪除** `merger.py` 的 `_call_llm()` 函數（第 114-134 行）。

**刪除** `merger.py` 頂部的 `import urllib.request`（不再需要）。

**修改** `merge_analysis()` 函數，改用 `llm` 模組：

```python
# 在 merger.py 頂部 import 區新增：
from ..llm import get_llm

# 在 merge_analysis() 中，將：
#     result_json = _call_llm(prompt)
# 改為：
    llm = get_llm()
    result_json = llm.complete(prompt, max_tokens=800, temperature=0.1)
```

其餘邏輯（JSON 解析、save_analysis）完全不動。

**關鍵：** 這是一個純提取 refactor，`merge_analysis()` 的行為不應有任何改變。當 `LLM_BACKEND=omlx` 時，呼叫路徑與 refactor 前完全相同。

### Step 4: tests/test_llm.py (~80 行)

```python
# 測試重點（不需要網路）：

# test_get_llm_omlx — LLM_BACKEND=omlx 回傳 OmlxLLM instance
# test_get_llm_ollama — LLM_BACKEND=ollama 回傳 OllamaLLM instance
# test_get_llm_openclaw — LLM_BACKEND=openclaw 回傳 OpenClawLLM instance
# test_get_llm_unknown — 未知 backend raise ValueError
# test_omlx_complete — mock urllib.request.urlopen，驗證 request payload 格式正確
# test_ollama_complete — mock urllib.request.urlopen，驗證 Ollama 格式
# test_openclaw_complete — mock urllib.request.urlopen，驗證含 Authorization header
# test_openclaw_no_key — OPENCLAW_API_KEY 未設定時不帶 Authorization header
```

mock 方式：用 `unittest.mock.patch("urllib.request.urlopen")` 回傳 mock response。

### Step 5: .env.example 更新

在 `.env.example` 中 `# Ollama` 區塊之後加入：

```bash
# LLM Backend for merger/scorer: "omlx", "ollama", or "openclaw"
LLM_BACKEND=omlx
LLM_MODEL=

# OpenClaw (Claude via proxy)
OPENCLAW_BASE_URL=http://localhost:18789/v1
OPENCLAW_MODEL=
# OPENCLAW_API_KEY=
```

## 不改的檔案

| 檔案 | 原因 |
|------|------|
| `cli.py` | LLM backend 由 config 控制，CLI 不需加 flag（Task E3 才加） |
| `mcp/` | 不改 |
| `crawl/` | 不改 |
| `transcribe/` | 不改 |
| `vision/` | VLM 和 LLM 是不同系統，vision 模組不改 |
| `db.py` | 不改 |
| `pipeline.py` | pipeline 呼叫 `merge_analysis()` 的方式不變 |
| `export/` | 不改 |

## 測試計畫

| # | 測試名 | 驗證什麼 |
|---|--------|---------|
| 1 | test_get_llm_omlx | factory 回傳正確 class |
| 2 | test_get_llm_ollama | factory 回傳正確 class |
| 3 | test_get_llm_openclaw | factory 回傳正確 class |
| 4 | test_get_llm_unknown | 無效 backend raise ValueError |
| 5 | test_omlx_complete | mock HTTP，驗證 request body 格式 |
| 6 | test_ollama_complete | mock HTTP，驗證 Ollama 格式 |
| 7 | test_openclaw_complete | mock HTTP，驗證 Authorization header |
| 8 | test_openclaw_no_key | 無 key 時不帶 header |

加上原有 13 個 = 總計 21 個測試，全部必須通過。

## 自審 Checklist

```
── 基礎 ──
[ ] pytest 通過（原有 13 + 新增 8 = 21）
[ ] 所有新 .py 檔有 `from __future__ import annotations`
[ ] 無 match/case 語法
[ ] 無 3.10+ 語法
[ ] typing 用 Optional, List, Dict

── 功能 ──
[ ] `from reel_scout.llm import get_llm` 可正常 import
[ ] `get_llm("omlx")` 回傳 OmlxLLM
[ ] `get_llm("ollama")` 回傳 OllamaLLM
[ ] `get_llm("openclaw")` 回傳 OpenClawLLM
[ ] merger.py 不再有 `_call_llm` 函數
[ ] merger.py 不再 `import urllib.request`
[ ] merger.py 用 `get_llm().complete()` 呼叫
[ ] `reel-scout config show` 顯示新 LLM 設定
[ ] .env.example 包含 LLM / OpenClaw 設定

── 整合 ──
[ ] 無硬編碼敏感資訊
[ ] 無未使用 import
[ ] 原有 13 個測試無 regression
```

## 風險與緩解

| 風險 | 緩解 |
|------|------|
| merger.py refactor 破壞現有功能 | 純提取，_call_llm → OmlxLLM.complete 邏輯完全相同；跑原有 test 驗證 |
| Ollama /api/generate vs /api/chat 端點混淆 | 用 /api/generate（non-chat），與現有 vision/ollama.py 一致 |
| OpenClaw 不可達時的錯誤訊息 | urllib 會 raise URLError，讓它自然 propagate；不加額外 try/except |

## 交付格式
Codex 完成後提交：
1. Git commit(s)：`feat(llm): extract LLM backend abstraction with omlx/ollama/openclaw support`
2. 自審報告：逐項填寫 checklist
3. 測試輸出：完整 `pytest -v` 輸出
