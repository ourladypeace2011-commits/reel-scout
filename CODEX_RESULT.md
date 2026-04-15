## Self-Review Checklist

### 基礎
- [x] pytest 通過（原有 13 + 新增 8 = 21）
  - `pytest -v` collected **21** tests and passed all of them.
- [x] 所有新 `.py` 檔有 `from __future__ import annotations`
  - Verified with `rg --line-number --glob "*.py" "^from __future__ import annotations$" reel_scout tests`.
- [x] 無 `match/case` 語法
  - Verified with `rg --line-number "match |\| None|list\[|dict\[|set\[|tuple\[" reel_scout tests` returning no matches.
- [x] 無 3.10+ 語法
  - Same grep check returned no matches in tracked source and tests.
- [x] typing 用 `Optional`, `List`, `Dict`
  - New factory and backend modules use `Optional`, `List`, `Dict`, `Any` without builtin generic syntax.

### 功能
- [x] `from reel_scout.llm import get_llm` 可正常 import
  - Verified by `tests/test_llm.py::test_get_llm_omlx` and direct imports in the new test module.
- [x] `get_llm("omlx")` 回傳 `OmlxLLM`
  - Verified by `tests/test_llm.py::test_get_llm_omlx`.
- [x] `get_llm("ollama")` 回傳 `OllamaLLM`
  - Verified by `tests/test_llm.py::test_get_llm_ollama`.
- [x] `get_llm("openclaw")` 回傳 `OpenClawLLM`
  - Verified by `tests/test_llm.py::test_get_llm_openclaw`.
- [x] `merger.py` 不再有 `_call_llm` 函數
  - Verified with `rg --line-number "_call_llm|urllib\.request|get_llm\(|llm\.complete\(" reel_scout\analyze\merger.py`; only `get_llm()` and `llm.complete()` remain.
- [x] `merger.py` 不再 `import urllib.request`
  - Same grep check showed no `urllib.request` import in `reel_scout/analyze/merger.py`.
- [x] `merger.py` 用 `get_llm().complete()` 呼叫
  - Verified at `reel_scout/analyze/merger.py:87` and `reel_scout/analyze/merger.py:88`.
- [x] `reel-scout config show` 顯示新 LLM 設定
  - `python -m reel_scout.cli config show` printed `LLM_BACKEND`, `LLM_MODEL`, `OPENCLAW_BASE_URL`, `OPENCLAW_MODEL`.
- [x] `.env.example` 包含 LLM / OpenClaw 設定
  - Verified in `.env.example`.

### 整合
- [x] 無硬編碼敏感資訊
  - OpenClaw API key is read from `OPENCLAW_API_KEY` environment variable, not committed config.
- [x] 無未使用 import
  - `python -m py_compile reel_scout\llm\__init__.py reel_scout\llm\base.py reel_scout\llm\omlx.py reel_scout\llm\ollama.py reel_scout\llm\openclaw.py reel_scout\analyze\merger.py tests\test_llm.py` passed.
- [x] 原有 13 個測試無 regression
  - Final `pytest -v` run still includes `tests/test_db.py` and `tests/test_mcp.py`, both passing.

## Pytest Output

```text
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.0.2, pluggy-1.6.0
rootdir: C:\Users\user\reel-scout
configfile: pyproject.toml
testpaths: tests
plugins: anyio-4.12.1
collected 21 items

tests\test_db.py ....                                                    [ 19%]
tests\test_llm.py ........                                               [ 57%]
tests\test_mcp.py .........                                              [100%]

============================== warnings summary ===============================
..\AppData\Local\Programs\Python\Python312\Lib\site-packages\_pytest\cacheprovider.py:475
  C:\Users\user\AppData\Local\Programs\Python\Python312\Lib\site-packages\_pytest\cacheprovider.py:475: PytestCacheWarning: could not create cache path C:\Users\user\reel-scout\.pytest_cache\v\cache\nodeids: [WinError 5] 存取被拒。: 'C:\\Users\\user\\reel-scout\\.pytest_cache\\v\\cache'
    config.cache.set("cache/nodeids", sorted(self.cached_nodeids))

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
======================== 21 passed, 1 warning in 0.28s ========================
```

## Git Diff Stat

```text
 .env.example                 |   9 +++
 CODEX_RESULT.md              |  74 +++++++++++-------------
 reel_scout/analyze/merger.py |  29 +---------
 reel_scout/config.py         |  10 ++++
 reel_scout/llm/__init__.py   |  26 +++++++++
 reel_scout/llm/base.py       |  14 +++++
 reel_scout/llm/ollama.py     |  38 +++++++++++++
 reel_scout/llm/omlx.py       |  37 ++++++++++++
 reel_scout/llm/openclaw.py   |  39 +++++++++++++
 tests/test_llm.py            | 132 +++++++++++++++++++++++++++++++++++++++++++
 10 files changed, 342 insertions(+), 66 deletions(-)
```

## REVIEW Flags

- `REVIEW`: `OpenClawLLM` reads `OPENCLAW_API_KEY` directly from environment at instantiation time, matching the handover spec; if this repo later centralizes secrets in `config.py`, this constructor may need to switch to that source for consistency.
