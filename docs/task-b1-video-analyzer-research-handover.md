# Task B1: video-analyzer 研究 — Codex Handover 執行計畫

## Context
byjlw/video-analyzer (1.3K⭐) 是 GitHub 上架構最接近 Reel Scout 的開源專案（keyframe + Whisper + Ollama VLM → structured JSON）。我們需要深入研究它的設計，找出可以吸收到 Reel Scout 的具體改進。

觸發原因：Phase 2B 的第一步，研究結果會指導後續 B2-B4 的實作。

預期成果：`docs/video-analyzer-research.md` — 結構化研究報告，含具體可行動的改進建議。

## Repo / Constraints
- 目標 repo: https://github.com/byjlw/video-analyzer
- 本 repo: https://github.com/ourladypeace2011-commits/reel-scout
- **這是唯讀研究任務，不改任何 code**
- 唯一產出是 `docs/video-analyzer-research.md`

## 執行順序與依賴

```
[Step 1] Clone video-analyzer，掃描整體架構
    ↓
[Step 2] 深入分析 3 個重點：frame sampling、VLM prompt、output schema
    ↓
[Step 3] 與 Reel Scout 現有實作對比
    ↓
[Step 4] 產出研究報告 + 具體改進建議
```

## 逐步驟研究細節

### Step 1: 整體架構掃描

掃描 video-analyzer 的：
- 目錄結構
- 核心 pipeline flow（entry point → processing → output）
- 依賴（requirements.txt / pyproject.toml）
- 支援的 VLM backends
- 設定方式（config files, CLI args, env vars）

### Step 2: 三大重點深入分析

#### 2A: Frame Sampling 策略

找出 video-analyzer 如何抽取 keyframes：
- 使用什麼演算法？（scene detection? uniform sampling? motion-based? adaptive?）
- 每個影片抽幾張？有上限嗎？
- 是否保證 first/last frame？
- 用 ffmpeg 還是 OpenCV？
- 有沒有 frame quality 評分（模糊偵測、重複偵測）？

**對比 Reel Scout**：我們目前有 `scene`、`interval`、`hybrid` 三種策略（`reel_scout/vision/keyframe.py`），用 ffmpeg scene detect，max 8 frames，沒有 first/last 保證，沒有 quality filter。

#### 2B: VLM Prompt 設計

找出 video-analyzer 的 VLM prompt：
- Prompt 是 free-form text 還是要求 structured JSON output？
- 有沒有 few-shot examples？
- 有沒有傳入 frame context（第幾張、時間戳、影片總長）？
- 單張 frame prompt vs. 多張 frame 一起送？
- 有沒有不同 prompt 用於不同分析目的（描述 vs. OCR vs. 情緒）？

**對比 Reel Scout**：我們目前是 free-form prompt（`vision/omlx.py` 和 `vision/ollama.py` 各有一份重複的 `_PROMPT`），沒有 frame context，沒有 structured output 要求。

#### 2C: Output JSON Schema

找出 video-analyzer 的最終輸出格式：
- JSON schema 結構
- 有哪些欄位？（對比我們的 summary/topics/hook/style/engagement_signals/content_type）
- 有沒有 temporal narrative（影片敘事進程）？
- 有沒有 confidence scores？
- 有沒有 per-frame vs. whole-video 分層？

**對比 Reel Scout**：我們的 output 在 `analyze/merger.py` 的 `_MERGE_PROMPT_TEMPLATE` 定義。

### Step 3: 對比分析

建立對比表，涵蓋：
- 架構差異（module 劃分、config 方式、error handling）
- 功能差異（支援的平台、VLM backends、output 豐富度）
- 品質差異（frame sampling 精準度、prompt 設計成熟度）

### Step 4: 產出研究報告

`docs/video-analyzer-research.md` 必須包含以下結構：

```markdown
# video-analyzer 研究報告

## 1. 專案概覽
{架構、依賴、pipeline flow}

## 2. Frame Sampling 分析
### 2.1 video-analyzer 的做法
{具體程式碼引用，含檔案路徑和行數}
### 2.2 與 Reel Scout 對比
### 2.3 建議改進
{具體可行動的改進，標明要改哪個檔案}

## 3. VLM Prompt 分析
### 3.1 video-analyzer 的做法
{完整 prompt 文字引用}
### 3.2 與 Reel Scout 對比
### 3.3 建議改進

## 4. Output Schema 分析
### 4.1 video-analyzer 的做法
{完整 JSON schema 引用}
### 4.2 與 Reel Scout 對比
### 4.3 建議改進

## 5. 其他值得借鏡的設計
{任何意外發現的好設計}

## 6. 改進優先級
| # | 改進 | 影響 | 複雜度 | 目標檔案 |
```

## 不改的檔案

所有檔案都不改。這是純研究任務。

## 測試計畫

不適用（唯讀任務，無 code 變更）。

## 自審 Checklist

```
[ ] 已 clone video-analyzer 並完整掃描
[ ] Frame sampling 分析含具體程式碼引用（檔案路徑+行數）
[ ] VLM prompt 分析含完整 prompt 文字
[ ] Output schema 分析含完整 JSON 結構
[ ] 每個分析都有與 Reel Scout 的對比
[ ] 每個分析都有具體可行動的改進建議
[ ] 改進建議標明要改的 Reel Scout 檔案
[ ] 研究報告結尾有優先級排序表
[ ] 報告寫在 docs/video-analyzer-research.md
[ ] 無其他檔案被修改
```

## 風險與緩解

| 風險 | 緩解 |
|------|------|
| video-analyzer 架構過度不同，無法直接借鏡 | 聚焦 3 個具體面向，不做全面對比 |
| repo 太大或結構複雜 | 從 entry point 追蹤 call chain，不需讀所有檔案 |

## 交付格式
Codex 完成後提交：
1. Git commit：`docs(research): video-analyzer architecture analysis and improvement recommendations`
2. 自審報告：逐項填寫 checklist（更新到 CODEX_RESULT.md）
3. 無需 pytest（無 code 變更）
