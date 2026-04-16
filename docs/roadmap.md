# Reel Scout — Roadmap

## 現況（Phase 1-2 完成，2026-04-15）

```
Phase 1  ████████████████████  ✅ Core Pipeline（crawl + transcribe + vision + merger + DB + CLI + MCP）
Phase 2  ████████████████████  ✅ Advanced Analysis（audio/PANNs + diarize/pyannote + scorer + LLM backends）
Phase 3  ░░░░░░░░░░░░░░░░░░░░  ⬜ Batch Intelligence — 跨影片模式分析
Phase 4  ░░░░░░░░░░░░░░░░░░░░  ⬜ Content Strategy Engine — 從分析到行動
Phase 5  ░░░░░░░░░░░░░░░░░░░░  ⬜ Distribution — 開源 + 社群
```

**已完成功能清單：**
- Crawl: yt-dlp (YT/IG/TikTok) + rate limiter + cookies + `--remote-components`
- Transcribe: faster-whisper / whisper.cpp
- Vision: keyframe extraction (scene/interval/motion/hybrid + first/last guarantee + score) + VLM (oMLX/Ollama)
- Audio: PANNs 音訊事件偵測 (onnxruntime, optional)
- Diarize: pyannote speaker diarization (optional)
- LLM Backend: omlx / ollama / openclaw (Claude via proxy)
- Merger: 結構化分析 JSON + timeline/narrative arc
- Scorer: 4 維度 LLM 評分 (hook/info/emotion/share)
- MCP Server: stdio JSON-RPC, 5 tools
- CLI: crawl/analyze/transcribe/vision/list/show/export/score/db/config
- DB: SQLite WAL + batch resume + schema migration (v1→v3)
- Tests: 59 passing

---

## Phase 3 — Batch Intelligence（跨影片模式分析）

**目標**：從「逐支分析」進化到「跨影片批量模式識別」，回答「什麼類型的短影音表現好？」

**Effort**: 3-4 sessions | **Dependencies**: Phase 2 完成

### 3A. 批量爬取 + 頻道模式 ✅ (部分完成)

- [x] `reel-scout browse <profile_url>` — 帳號頁瀏覽，列出所有 reels（`--flat-playlist --dump-json`）(2026-04-16)
- [x] IG browse: instaloader fallback（yt-dlp IG user extractor broken as of 2026.4）(2026-04-16)
- [x] browse 三種輸出模式：human-readable / --json / --urls-only (2026-04-16)
- [x] pyproject.toml: `instagram` optional dependency group (2026-04-16)
- [ ] `reel-scout crawl --channel <URL> --limit 50` — 批量爬取頻道最新 N 支（browse → crawl 整合）
- [ ] `reel-scout crawl --trending --platform youtube --limit 30` — 平台趨勢影片
- [ ] `reel-scout crawl --playlist <URL>` — 播放清單批量
- [ ] 頻道 metadata 存 DB（subscriber count、avg views、niche tag）

### 3B. 跨影片比較分析

- `reel-scout compare <video_id_1> <video_id_2> ...` — 結構化對比表
- `reel-scout patterns --channel <channel_id>` — 頻道模式分析：
  - 平均影片長度、hook 類型分佈、CTA 模式
  - 高分 vs 低分影片的結構差異
  - 發布節奏分析

### 3C. 模式標籤系統

- Hook 類型自動分類：question / shock / controversy / tutorial / story / trend-ride
- CTA 類型：follow / comment / share / link / none
- 內容結構：hook-body-cta / problem-solution / listicle / story-arc / raw-moment
- 標籤存 DB，可供後續篩選和統計

### 3D. 統計儀表板

- `reel-scout stats` — 全局統計（影片數、平均分數、top patterns）
- `reel-scout stats --channel <id>` — 頻道維度統計
- `reel-scout stats --export csv` — 匯出為 CSV 供 Google Sheets 分析

---

## Phase 4 — Content Strategy Engine（從分析到行動）

**目標**：基於分析數據產出可執行的內容策略建議，銜接 brand-studio 工作流。

**Effort**: 3-4 sessions | **Dependencies**: Phase 3

### 4A. 競品研究報告

- `reel-scout research --niche "audio equipment" --channels 5 --depth 20` — 自動：
  1. 爬取 5 個競品頻道各 20 支影片
  2. 全部跑 analyze pipeline
  3. 跨頻道比較 + 模式分析
  4. 產出結構化研究報告（markdown）
- 報告含：niche 共通模式、差異化機會、推薦內容策略

### 4B. 內容靈感產生器

- `reel-scout inspire --based-on <video_id> --angle <twist>` — 基於高分影片變體
- `reel-scout inspire --trending --niche "tech review"` — 結合趨勢 + niche
- 輸出：標題建議 + hook 腳本 + 結構大綱 + 推薦長度

### 4C. Hevin AI OS 整合

- MCP tool 擴充：`reel_scout_research`、`reel_scout_inspire`
- brand-studio skill 連接：研究報告自動存入 `apps/brand-studio/waffle-house/intelligence/`
- Vulture-S 課程素材：學員用 reel-scout 分析自己的競品

### 4D. A/B 結構測試框架

- 記錄自己發布的影片 URL + 實際表現（views, likes, comments）
- `reel-scout track --my-video <url> --views 1500 --likes 89`
- 對比分析：自己影片 vs 競品影片的結構差異
- 迭代建議：下一支影片應該改什麼

---

## Phase 5 — Distribution（開源 + 社群）

**目標**：整理為可公開的開源工具，建立最小使用者社群。

**Effort**: 2-3 sessions | **Dependencies**: Phase 3 穩定

### 5A. 開源準備

- README 完善：安裝教學、使用範例、架構圖
- `pyproject.toml` 完善：`pip install reel-scout` 可用
- GitHub Actions CI：pytest + linting
- 範例影片 + 範例輸出（不含版權素材）
- LICENSE: MIT

### 5B. 文件 + Demo

- `docs/` 補齊：API reference、MCP 整合教學、LLM backend 設定
- Demo video / GIF 展示核心功能
- Blog post：「用 AI 批量分析短影音的工具」

### 5C. 社群策略

- Threads / Twitter 發布（搭配 Waffle House brand）
- Reddit r/artificial、r/youtube、r/contentcreation
- Product Hunt launch（Phase 4 穩定後）

---

## 設計原則

1. **CLI-first** — 所有功能先有 CLI，再有 MCP/API
2. **Offline-capable** — 核心分析用本機 LLM (oMLX/Ollama)，不強制雲端
3. **Minimal dependencies** — urllib not requests，argparse not click，SQLite not Postgres
4. **Python 3.9** — 維持 NAS/older system 相容
5. **Batch-friendly** — 大量影片分析是核心場景，不只是單支

---

## 里程碑

| Milestone | 條件 | 預估 |
|-----------|------|------|
| **v0.3** | Phase 3A + 3B 完成（批量爬取 + 比較） | 2 sessions |
| **v0.4** | Phase 3C + 3D 完成（標籤 + 統計） | 2 sessions |
| **v0.5** | Phase 4A 完成（競品研究報告） | 2 sessions |
| **v1.0** | Phase 5A 完成（PyPI 可安裝 + CI） | 1 session |
