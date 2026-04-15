# Codex 發包 Prompt 標準模板

每次發 Codex task 時，複製以下內容，只改 `{...}` 的部分。

---

```
你是 Codex agent，負責執行 Reel Scout 專案的開發任務。

## Repo
https://github.com/ourladypeace2011-commits/reel-scout

## 任務
請閱讀以下兩個檔案，按照指示執行：
1. `AGENTS.md` — 專案約束 + 當前任務概覽
2. `docs/{task-name}-handover.md` — 完整執行計畫

## 執行流程
1. 讀完 handover doc，確認理解每個 step
2. 按照「執行順序與依賴」逐步實作
3. 每個 step 完成後跑 `pytest -v` 確認不壞
4. 全部完成後，執行自審 checklist（逐項驗證，不能跳過）
5. 產出 `CODEX_RESULT.md` 在 repo 根目錄，內容包含：
   - 自審 checklist（每項標 [x] 或 [ ] + 說明）
   - 完整 `pytest -v` 輸出
   - `git diff --stat` 輸出
   - 任何 REVIEW flag（你不確定的決策，標記讓審計者注意）

## 交付
- Git commit，message 格式：`feat({scope}): {描述}`
- 每個 commit 尾部加：`Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>`
- 不要 push 到 main 以外的 branch

## 硬規則
- Python 3.9 嚴格相容（禁 match/case、禁 3.10+ 語法）
- 所有 .py 檔必須有 `from __future__ import annotations`
- typing 用 Optional, List, Dict（不用 list[], dict[]）
- HTTP 用 urllib，不用 requests
- 不硬編碼 IP / API key / password
- 不改 handover doc 未列出的檔案
- 不確定的地方標 REVIEW flag，不要自己猜
```

---

## 使用步驟

1. CC (Opus) 更新 `AGENTS.md` 的「當前任務」區塊
2. CC 寫新的 `docs/{task}-handover.md`
3. Push 到 GitHub
4. 複製上方 prompt，改 `{task-name}` 為實際檔名，貼給 Codex
5. Codex 完成後交回 CC 審計
