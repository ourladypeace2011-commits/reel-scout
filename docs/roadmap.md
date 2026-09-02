# Reel Scout — Roadmap

> 最後校正：**2026-09-01**（對照 code＋**實際 DB 內容**逐項驗證。這份檔上一次校正在 2026-07-21，之後漂了約六週：版號、schema、測試數全錯，而且 §4E 那條 🔴「證據層從沒在語料上跑過」**早就不成立了**——`shot_metrics` 現在 116 列，是本次逐項實查才發現的。⚠️ **這份檔自己示範了它命名過的那個病**：completion-blind drift 不只讓壞消息隱形，也讓**好消息**隱形了六週。詳見 §現況 與 §4E 回填段的 2026-09-01 更新）
> 前次校正：2026-07-15（對照實際 code 逐項驗證，非憑記憶）
> 2026-07-17 增補：crv 對標（§4E pacing/BPM 實測化 + §4F 燒錄字幕 OCR + 參考案例 crv）→ [`docs/crv-vs-reel-scout.md`](./crv-vs-reel-scout.md)
> 2026-07-18 drift 修正：測試 162→177（實跑驗證）+ 已完成清單補 `inspect`（PR #29 遺漏回寫）
> 2026-07-18 §4E 實作：evidence-based pacing（shot-table cuts/min + audio energy/BPM）落地，schema v7，測試 →200（含 codex+harness 雙審修正）
> 2026-07-18 §4F 實作：燒錄字幕 OCR / L3.5（vlm 復用 + tesseract opt-in）落地，schema v8，測試 →207
> 2026-07-20 GUI + 學生包：view/inspect 合併成單一 app、vulture.s 換膚（`theme.py`，偏離已 narrate）、品牌字型內嵌 + CJK 動態 subset、`export --format bundle` 自包含學生包、inspector 返回鍵、`view` 併發修復（ThreadingHTTPServer）。測試 →244
> 2026-07-18 Wave 3 一波：3B patterns / 3A instaloader / 4B inspire / 4D track（schema v9）/ 4C MCP(8 tools) / 5A CHANGELOG + 5C docs 全落地，測試 →228（含 codex+harness 雙審修正：inspire 非-JSON fallback、track partial-update COALESCE、MCP channels 驗證、instaloader limit=0）
> 2026-07-20 §5A L0/L1/L2 安裝階梯：新增 `ingest {vision,score}`（agent 當 backend，零本地模型／零 API key）+ `show` 補列 keyframe id 與分數；SKILL.md 三層 surface 改寫。測試 →263
> 2026-07-20 §5A L1 補完：`ingest analysis`（merger 同樣需要 LLM，無 LLM 時 `analyses` 整列不存在＝4-beat／hook／CTA 全缺；枚舉值驗證）+ 學生包 filmstrip 顯示逐格描述。另修 `--skip-vision` 連 keyframe 都不抽（L1 整條實際為空）與 batch 裸名呼叫。測試 →324
> 2026-07-20 §5A `batch`：Google Doc／Sheet 清單批次跑（免 API 金鑰，`/edit` 自動轉匯出端點）；能力偵測後**由使用者選 mode**（full／agent／transcript），無 VLM 不靜默降級。測試 →304
> 🔴 2026-07-20 §4E 回填發現：`shot_metrics` / `ocr_captions` **實際 0 列** — §4E/§4F 的 code 上了但從沒在語料上跑過，`_measured_block()` 永遠回空字串 → 現有 12 筆分數的 pacing 仍 100% 是 LLM 猜的，§4E 想解的問題原封不動。roadmap 標 ✅ 但資料為空＝completion-blind drift，見 §4E 回填段
> 2026-07-20 §4G 評分透明化：inspector 加權重滑桿（即時重算 overall + default-vs-yours），權重從三處重複收斂成 `config.SCORE_WEIGHTS` 單一定義。測試 →324（PR #35）
> 2026-07-21 §4G 真瀏覽器驗證 + bug 修：M2 Max 實拖滑桿驗證,抓到 zero-weight 顯示 bug（訊息說 no verdict 但 overall 仍顯示殘值）→ 修為顯示 `—` + 清空 bar（`fe87ff2`）。JS/Python 對拍測不到（回傳值等價 ≠ 渲染正確）→ 加迴歸測試釘住。
> 2026-07-21 §4H 介面中英切換（i18n）：inspector + viewer（列表頁 + 學生包 export）全支援 EN/中文即時切換,瀏覽器語言自動偵測,localStorage 持久化。抽 `reel_scout/i18n.py` 單一翻譯來源（51 鍵 en/zh）。**只翻介面 chrome,模型產出（reasoning／逐字稿／decoded 值）永不翻**。測試 →329（PR #35）
> 2026-07-20 §5A 分發補洞（乾淨機器實測）：wheel 原本只含 `reel_scout/`，`pip install` 後 SKILL.md／`/scout`／prompts／setup.py **全部缺席**＝agent 無物可載；改 force-include 進 wheel + 新增 `skill install`。另修 setup.py 對無 clone 使用者印 `<repo-root>` 佔位符的 bug（該檔原本零測試覆蓋）。測試 →280

## 定位與 Non-goals

**Reel Scout 是工具，不是末端產品。**

它的形態是 CLI + MCP + library：給會用終端機（或會叫 AI 幫你跑）的人，把短影音變成結構化資料。它**不會**變成雙擊即用的 GUI app。

**為什麼不產品化**（2026-07-15 決策）：

1. **平台存取隨時可能被關**。整條 pipeline 的入口靠 yt-dlp。IG 已經在 rate-limit，yt-dlp 的 IG user extractor 2026.4 就壞過。
   **實測爆炸半徑**（2026-07-15）：門一關，`crawl` / `browse` / **`analyze`（整條 pipeline，也就是核心價值）全死** —— `analyze` 只吃 URL，餵本機路徑直接 `ValueError: Unsupported platform for URL`。
   活下來的只有 `transcribe <path>` / `vision <path>`（個別階段）與 `score` / `show` / `export`（吃已存的 DB）。
   **也就是說這個風險目前零緩解措施** —— 補上 `analyze <local-path>`（見 5B）才是真正的保險。工具的存活優勢是**可以做到**，不是現在就有。
2. **追平台變動的維護成本極高**。把它做成終端使用者產品，等於簽下一份「平台每改一次、我就得修一次，而且使用者會抱怨」的長期合約。這不是這個專案想付的代價。
3. 因此 **star 數 / 使用者數不是這個 repo 的成功指標**。成功 = 自己（和會用 CLI 的人）能穩定拿到可信的分析資料。

**Non-goals**：GUI 桌面 app、一鍵安裝器、社群營運 / Product Hunt / 行銷推廣、SaaS 或託管服務。

> GUI 這條 2026-07-17 於 v1.0 收尾後重新檢驗過一次 → [`docs/gui-evaluation.md`](./gui-evaluation.md)：
> 全 GUI app **no-go**；只讀 DB 的最小本機 dashboard **conditional-go**（有日常 review 需求才做）。

---

## 現況

```
Phase 1  ████████████████████  ✅ Core Pipeline（crawl + transcribe + vision + merger + DB + CLI + MCP）
Phase 2  ████████████████████  ✅ Advanced Analysis（audio/PANNs + diarize/pyannote + scorer + LLM backends）
Phase 2.5████████████████████  ✅ 品質強化（subtitle-first / keyframe budget / prompt pack / skill / 雙語轉錄）
Phase 3  ████████████████████  ✅ Batch Intelligence — browse ✅、compare ✅、3C ✅、3D stats ✅、patterns ✅、instaloader fallback ✅（channel 表/batch-resume 為刻意延後的大重構）
Phase 4  ████████████████████  ✅ Content Strategy Engine — 4A research ✅、4B inspire ✅、4C MCP 擴充 ✅、4D track ✅、4E 評分證據化 ✅、4F L3.5 OCR ✅
Phase 5  ████████████████████  ✅ Tool Hygiene — LICENSE/README/CHANGELOG ✅、analyze-local ✅、yt-dlp 健壯性 ✅、CI ✅、config check ✅、**PyPI 上架 ✅（v1.2.0，Trusted Publishing 零 token）**
Phase 5+ ████████████████████  ✅ 靜默錯誤清剿（2026-07-22 → 08-26，v1.3.x + Unreleased；下方「2026-07-21 之後」清單）
Phase 6  ████████████████░░░░  🔨 Shot-level 反解 — **6A/6D/6E 完成**、6B 詞彙完成待來源拍板、**6C 實測不可行已封存**
```

**目前版本**：**v1.3.1** ｜ **測試**：**963 passing**（2026-09-02 於本支實跑）｜ **DB schema**：**v14**

> 基準寫清楚免得下一個人重數：`master` 948 ＋ 本支 15 ＝ **963**。schema **v14**（`shots` 表）。

> 上一版此行寫「v1.2.0／324 passing／schema v9」，三個數字全是 2026-07-20 的舊值。

### 語料現況（2026-09-01 實查 `data/reel_scout.db`）

```
videos 117 | analyses 117 | scores 115 | keyframes 2872
shot_metrics 116 | ocr_captions 113 支           ← §4E / §4F 的產物，兩張表都活著
有分數但無實測證據：1 支
```

**這推翻了 §4E 回填段那條 🔴。** 2026-07-20 記的「`shot_metrics` / `ocr_captions` 實際 0 列」在 2026-09-01 已不成立——證據層 116/117 覆蓋，`_measured_block()` 現在對幾乎每一支都吐得出東西。v1.3 里程碑的第二個條件（`stats` 要報得出證據覆蓋率）**在本次同一輪補上了** —— 這份表現在 `reel-scout stats` 自己就講得出來，不必再手動 `SELECT COUNT(*)`。

### 已完成功能清單（2026-07-15 驗證）

- **Crawl**: yt-dlp (YT/IG/TikTok) + rate limiter + cookies + IG instaloader fallback
- **Browse**: 帳號/頻道頁列出所有影片（human / `--json` / `--urls-only`）
- **Transcribe**: subtitle-first（優先吃原生字幕，`find_subtitle`）→ faster-whisper / whisper.cpp fallback
- **中英雙語轉錄** (v0.2.0): `WHISPER_MULTILINGUAL` + `WHISPER_CHUNK_LENGTH` 每段重偵測語言，解長檔語言鎖定漂移（40 分鐘中英訪談實測：latin 還原 56%→90%）；另有 `WHISPER_LANGUAGE` / `WHISPER_TASK`
- **Vision**: keyframe 抽取（scene / interval / motion / hybrid + first/last 保底 + score）+ duration-aware frame budget + `KEYFRAME_RESOLUTION` 升採樣（讀畫面小字）+ `--start/--end` focus window + VLM (oMLX/Ollama) + fallback model + per-frame resilience（單幀逾時不炸整跑）
- **Audio**: PANNs 音訊事件偵測（onnxruntime, optional）
- **Diarize**: pyannote speaker diarization（optional）
- **LLM Backend**: omlx / ollama / **openclaw**（走 proxy 吃訂閱制，無需本機 GPU）
- **Merger**: 結構化分析 JSON + timeline / narrative arc + hook `opening_type` + `cta_type`（含 `visit` 實體造訪）
- **Scorer**: craft 四維 LLM 評分 — `hook_strength` / `visual_storytelling` / `pacing` / `structure`（§4E：`pacing` 以實測 shot-table + audio energy/BPM 為證據）
- **Shot metrics** (§4E, schema v7): `reel_scout/shots.py` 全片剪點偵測 → `cuts_per_minute`/`shot_count`/`avg_shot_sec` + `audio/rhythm.py` energy/BPM，存 `shot_metrics` 表，merger 折進 `full_json.measured` 供 scorer
- **On-screen text / L3.5** (§4F, schema v8): `reel_scout/ocr.py` 收集帶時間戳的燒錄字幕（`OCR_ENGINE=vlm` 復用 `text_in_frame`／`tesseract` opt-in guarded），存 `ocr_captions` 表，merger 加「On-screen Text」區塊；cheatsheet 新增 L3.5 層
- **Prompt pack**: 6 份 reverse-decode prompt（開源，作為預設分析層）
- **Skill**: cross-surface skill 打包（SKILL.md + manifests）
- **MCP Server**: stdio NDJSON JSON-RPC, 8 tools（crawl/analyze/list_videos/show_video/export + patterns/inspire/research）
- **CLI**: browse / crawl / analyze / transcribe / vision / list / show / export（json/csv/**html**）/ score / compare / stats / **patterns** / **inspire** / **track** / research / **view** / **inspect** / db / config
- **統一 app** (2026-07-20): `view` = 清單 + 互動 inspector 同一 port（清單點列直接進 inspector）；`inspect <id>` 釘住單片。含返回鍵（依情境顯示）
- **vulture.s 外殼** (2026-07-20): `theme.py` 承品牌 SSOT token；品牌字型隨套件出貨（拉丁 78 KB／server 走 `/font/`、匯出走 base64），CJK 按內容動態 subset
- **學生包** (2026-07-20): `export --format bundle` — 一支 reel 一個自包含 HTML（影片/keyframe/波形/字型全內嵌）+ index；`--max-mb` 擋過大長片。實測整頁僅 1 個網路請求
- **Viewer** (v1.1.0): 唯讀檢視器三面——`export --format html`（自包含單檔、零安裝 take-home）+ `reel-scout view`（本機 server、live demo）+ `reel-scout inspect`（互動 single-clip：transcript↔keyframe 時間同步、可點時間軸跳播，port 自 arkiv，PR #29）；顯示拆解結構+keyframe+分數+逐字，無動作按鈕
- **DB**: SQLite WAL + batch resume + schema migration（→ **v13**）

### 2026-07-21 之後新增的能力（本次補登，上一版完全沒有這段）

v1.3.0 發布後到 2026-08-26 之間有 **35 顆 commit** 進 master，其中大半是「跑得動但結果悄悄不對」那一類的修復。**這批沒有一條回寫過 roadmap**，所以列在這裡：

- **`export --format skeleton`**（PR #46）：beat 結構（逐句時間碼）＋實測節奏（cuts/min、avg shot、BPM/energy）＋說話者輪替數，作為 **reel-scout → smart-edit 的交接資料格式**。檔頭寫明「介面是資料格式，不是函式呼叫；兩邊都不 import 對方」——**這是本專案第一個對外交接格式，也是 §Phase 6 要沿用的先例**
- **`export --format json` 保留逐句時間碼**（#47）
- **`mark`**（#70）：一支片的時間軸上可掛標記，inspector 邊看邊對照
- **keyframe run 身分化 + 批次失敗可見**（#68）：抽楨變成有 `run_id` 的一次「執行」；另修**關鍵楨取樣塌在片頭**（排序後才截斷，長片只看得到開頭）
- **`batch` 逾時**（#69）：每一步有時限、整批也有
- **無效片判定**（#77）：跑不成的片不再以 0.0 分進語料；`stats`/`patterns` 排除並報排除數；`db check-invalid [--apply]` 稽核既有庫
- **`stats` 分數依 `model_used` 分組**（#75）：兩把尺不再平均成一個數
- **逐字稿語言兩修**（#73/#74）：繁體講者不再回簡體；不再挑到 YouTube 的機器翻譯軌
- **codec 守門**（#60/#78/#79）：停止抓 AV1／IG 與 TikTok 補上編碼條件——「分析得很好，然後放不出來」
- **whisper-guard 反幻覺**（#45）、**speaker-align 抽成套件**（#49）
- **BPM near-miss 可見**（本輪）：被門檻否決但接近的 tempo 不再靜默消失，印出候選值與比值

---

## Phase 3 — Batch Intelligence（跨影片模式分析）

**目標**：從「逐支分析」進化到「跨影片批量模式識別」，回答「什麼類型的短影音表現好？」

### 3A. 批量爬取 + 頻道模式 🔨 半套（2026-07-15 補完主體）

- [x] `reel-scout browse <profile_url>` — 帳號頁瀏覽（2026-04-16）
- [x] IG browse: instaloader fallback（2026-07-18）：`InstagramCrawler.browse` yt-dlp 失敗時試 instaloader（`instagram` extra，guarded；裝不到就把原 yt-dlp 錯誤帶提示丟出）
- [x] browse 三種輸出模式：human / `--json` / `--urls-only`（2026-04-16）
- [x] pyproject: `instagram` optional dependency group（2026-04-16）
- [x] `crawl --channel <URL> --limit N` — browse → crawl 串起來（2026-07-15，PR #9）
- [x] `crawl --playlist <URL>` — 播放清單批量（2026-07-15，PR #9）
- [x] `crawl --file -` 吃 stdin — `browse --urls-only | crawl --file -` 這條 browse 自己打廣告的 pipe 過去**從沒通過**（`open("-")` → `FileNotFoundError`），2026-07-15 修好（PR #9）
- [ ] `crawl --trending --platform youtube` — 平台趨勢（⚠️ 最脆弱，最可能被平台擋，優先序最低；與本檔的平台風險判斷相衝，**刻意不做**）
- [ ] 頻道 metadata 存 DB（subscriber count、avg views、niche tag）— **需先設計 channel 表**：目前沒有 channel 表也沒有 `channel_id`，唯一把手是 `videos.uploader`（自由文字、無索引、實際值長成 `Ben Aizen | Artzen Media` / `小建`）。獨立一輪處理
- [ ] `crawl --channel` 傳 VideoMeta 而非 URL — browse 已經帶回 title/uploader/duration，但 `download()` 簽章吃 URL，所以會再打一次 `yt-dlp --dump-json`（每支多一個請求）。改簽章是真 refactor，v1 先付這個代價
- [ ] `crawl` 的 batch/resume — 目前 `batches` / `batch_items` / `--resume` 全是 `analyze` 專屬（`pipeline.py`）。要給 crawl 用得把 orchestration 從 `pipeline.run` 搬出來

### 3B. 跨影片比較分析 🔨 半套

- [x] `reel-scout compare <video_id_1> <video_id_2> ...` — 結構化對比表（2026-07-17）。純讀 DB
      已存的 analyses/scores（duration / format / pacing / hook type / cta type / content type + craft 四維分 + overall），
      轉置表（欄=影片、列=欄位）+ `--json`；接受 exact id 或唯一 prefix；缺分析欄位留 `—` 不捏造；平台關門也能跑。
- [x] `reel-scout patterns --channel` — 頻道模式分析（2026-07-18）：`patterns.py`，平均長度、hook/CTA/structure 分佈、高分 vs 低分半結構對比（median split）、發布節奏（upload_date gap）。純讀 DB。key on uploader substring

### 3C. 模式標籤系統 ✅

- [x] Hook 類型分類：`question|statement|visual|music|none`（merger 產出，存 `analyses.hooks_json`）
- [x] CTA 類型分類：`follow|like|comment|link|visit|none`
- [x] 內容結構分類：hook-body-cta / problem-solution / listicle / story-arc / raw-moment（2026-07-17，schema v6；merger prompt 新增 `content_structure` 欄位）
- [x] 標籤正規化進可查詢欄位（2026-07-17，schema v5）：`content_type / opening_type / cta_type / style_format / style_pacing / emotion / content_structure` 從 `full_json` 鏡射進 `analyses` 索引欄，`full_json` 仍為 SSOT；舊資料 migration 自動 backfill

### 3D. 統計 ✅（2026-07-17）

- [x] `reel-scout stats` — 全局統計：tag 分佈（content_type/content_structure/format/pacing/opening/cta/emotion）+ score 聚合（overall & 四維 avg/min/max/n），純讀正規化欄位
- [x] `reel-scout stats --channel <uploader>` — 頻道維度（key on free-text `uploader` 子字串，無 channel 表故非精確 id）
- [x] `reel-scout stats --csv <path>` — 匯出 long-format CSV（`metric,dimension,key,value`）；另有 `--json`
- [x] score 聚合**依 `model_used` 分組**（2026-08-20）— craft 分數依模型而異，agent 打的分與本地模型打的分是兩把尺；混合語料下 pooled 區塊被標記為 pooled（`mixed_score_sources`），另出每個來源各自的區塊。per-video 讀取不變

---

## Phase 4 — Content Strategy Engine（從分析到行動）

**目標**：把分析資料變成可執行的產出。全部維持 CLI/MCP 形態。

### 4A. 競品研究報告 ✅（2026-07-17）

- [x] `reel-scout research --niche "<niche>" --channels <urls...> --depth 20` — 爬取 → 全部 analyze → 跨頻道聚合 → 產出 markdown 報告（niche 共通模式、差異化機會、內容策略）。編排復用 browse + `pipeline.run(score=True)` + `compare.collect_video`；channel 歸屬記憶體映射（無 channel 表）；`aggregate()` 純函式；LLM 合成走 `get_llm().complete()`，不可達時退回 deterministic data-only 報告；`--out` 落檔、`--json` 出聚合、`--no-analyze` 只吃現有 DB

### 4B. 內容靈感產生器

- [x] `reel-scout inspire --based-on <ref> --angle <twist>` — 基於高分影片的變體（2026-07-18）：`inspire.py`，一次 LLM call 產 titles/hook script/structure outline/長度建議，non-JSON 退回 raw
- [ ] 輸出：標題建議 + hook 腳本 + 結構大綱 + 推薦長度

### 4C. MCP 擴充

- [x] MCP tool 擴充（2026-07-18）：`patterns`、`inspire`、`research` 三個新 tool（5→8），LLM/network tool redirect stdout→stderr

### 4D. 表現回填 + A/B 結構比較

- [x] `reel-scout track --my-video <ref> --views --likes --comments` — 記錄實際表現（2026-07-18）：`track.py` + `performance` 表（schema v9），接受 URL 或 id/prefix
- [x] 對比分析：自己的影片 vs 競品的結構差異 → 迭代建議（2026-07-18）：`compare_to_corpus` **確定性**（非 LLM）比對高分語料（overall≥7）的 modal structure/pacing + avg cuts/min，產具體迭代建議

### 4E. 評分證據化：pacing 從「LLM 猜」→「實測 shot-table」

**問題**：`scorer` 的 `pacing`（四維之一）目前是 LLM 憑感覺給的，且 **model-dependent**——換一顆 VLM/LLM 分數就飄（見對標 [`docs/crv-vs-reel-scout.md`](./crv-vs-reel-scout.md) §5 與 memory「Reel Scout 評分依賴模型」）。這是評分可信度的已知軟肋。

**啟發來源**：crv Pro 的 `--motion` 產出 shot table（per-shot duration / cuts per minute / 節奏變化）——客觀測量、可重現，不靠模型主觀。同樣講「節奏」，他量、你猜。

- [x] vision 階段加**確定性剪點偵測**（2026-07-18）：新 `reel_scout/shots.py` 專用 ffmpeg pass（`select='gt(scene,T)',showinfo -an -f null -`，全片不設幀上限）算 `cuts_per_minute` / `shot_count` / `avg_shot_sec`，存新 `shot_metrics` 表（schema v7）
- [x] `pacing` 分數改成「**實測 shot-table 當證據，LLM 只在證據上解讀**」（2026-07-18）：merger 折進 `full_json.measured`，scorer prompt 加「Measured Signals」區塊 + pacing 準則改為 prefer 實測 cuts/min
- [x] **音訊 BPM / energy 併進同批 evidence signal**（2026-07-18）：新 `reel_scout/audio/rhythm.py`——energy(RMS，純 stdlib) + BPM(純 numpy onset 自相關，best-effort，**不引 librosa**)，獨立於 PANNs optional（只需解碼 WAV）
- [ ] （延伸）把「分數也要能舉證」寫進 rubric——對齊 vibe-reader case study §6.1「舉證護欄機器可驗證」，從「主張舉證」推到「分數舉證」（部分達成：scorer prompt 已 prefer 實測值；rubric 文件化待補）

> ⚠️ 邊界：crv Pro 閉源、未購未跑，這裡偷的是**概念**（pacing 該用實測 shot-table / BPM 背書），不是抄它 cut 偵測的閾值或宣稱它準。

#### 🔴 4E 回填：code 上了，但從沒在語料上跑過（2026-07-20 實查 DB 發現）

上面三個 `[x]` 是**實作完成**，不是**生效**。查 `data/reel_scout.db` 實際內容：

```
videos 20 | analyses 18 | scores 12 | transcripts 19 | keyframes 213
shot_metrics 0 | ocr_captions 0        ← §4E / §4F 的產物，兩張表都是空的
```

`shot_metrics` 零列 ⇒ merger 沒有 `measured` 可折 ⇒ `scorer._measured_block()` 對**每一支**影片都回 `""` ⇒ prompt 裡那段「Measured Signals（objective — prefer these for pacing）」從來沒出現過。

也就是說：**§4E 想解的問題原封不動還在。** 現有 12 筆分數的 `pacing` 仍然百分之百是 LLM 憑感覺給的，跟 §4E 落地前沒有差別——差別只在「現在有能力算，但沒算」。roadmap 標 ✅ 而語料為空，是典型的 completion-blind drift：驗收看的是測試綠，不是資料有沒有真的長出來。

- [x] 🔴 **對既有語料補跑 §4E/§4F 抽取** ✅ **2026-09-01 實查確認已達成**（非單一補跑動作，是後續 40 顆 commit 期間語料自然長到 117 支的結果）：`shot_metrics` **116 列**、`ocr_captions` **113 支**、`scores` 115 筆，有分數卻無實測證據的只剩 **1 支**。`_measured_block()` 現在對幾乎每一支都吐得出東西——§4E 想解的問題**實質已解**。
- [x] 🔴 **加一個「證據層是否為空」的可見性** ✅ **2026-09-01 做掉**：`stats` 新增 `-- Evidence coverage --` 區塊（scored ／ with measured ／ without measured ／ on-screen text L3.5），同時進 `--json`（`evidence_coverage`）與 `--csv`（`evidence` 列），且吃 `--channel` scope。實跑：

  ```
  -- Evidence coverage --
  scored                 115
  with measured          114  (shot_metrics: cuts/min, avg shot, BPM/energy)
  without measured         1  <- pacing on these is model judgement, not measurement
  on-screen text L3.5    113  videos
  ```

  兩個刻意的設計：**報數量不報單一百分比**（100% 的兩支跟 100% 的兩百支不是同一個主張）；**「without measured」那行連後果一起印**（光給數字，讀的人不會知道那代表 pacing 退化成猜的）。CSV 即使全零也照樣輸出該列——「沒有證據」跟「這份 CSV 早於證據回報」不該長得一樣。
  > ⚠️ **這條的代價曾經是反過來付的**。原本的理由是「洞可以無限期隱形」，結果隱形的是**修好了**這件事：證據層在六週前就補齊了，而 roadmap 上那個 🔴 一直掛著，因為沒有任何介面會講這件事，只有人工 `SELECT COUNT(*)` 才看得到。原句「能力存在但沒被使用，跟能力不存在，對使用者是同一件事」補上對稱的另一半：**能力已經在用但沒有介面說，跟沒在用也是同一件事。**
- [ ] （順帶）`pipeline` 是否該在 `shot_metrics` 寫入失敗時出聲，而不是靜默留白。目前無從得知是「沒跑」還是「跑了但失敗」。
  > 2026-09-01 註：音訊那半已有部分覆蓋——`Audio rhythm skipped: %s` 會印，本輪再加上 BPM near-miss 的候選值輸出。**剪點那半仍然靜默**：`compute_shot_metrics` 回 `None` 時整段跳過、不出聲。

> 這條的來源是 JapowDB 拆解（`hevin-ai-os` case-study 2026-07-20）。該站把評分門檻交給讀者調，逼出一個對照問題：**我們的分數底下有沒有東西可以被檢驗？** 答案是有設計、沒資料。PR #35 的權重滑桿讓「怎麼合成」變透明了，但被合成的四個數字本身，目前仍無客觀依據——先補證據層，滑桿才不只是漂亮。

### 4F. 燒錄字幕 OCR → 補強 transcript / 信號可靠度

**問題**：純視覺 / 零口白 / 吵雜片，L3（transcript）給不出料或不可靠（見 `prompts/signal-reliability-cheatsheet.md` 4 層信號模型）。目前只靠 L4 VLM「讀招牌字」，沒有專門的時間戳 OCR。

**啟發來源**：crv Pro 的 `--ocr`——帶時間戳、可搜尋的螢幕文字，且**用畫面燒錄字幕反過來校正 STT**（CJK 特別強）。這等於在 L3↔L4 之間補一層**可實測的文字證據**，不是抄功能，是補你信號可靠度模型的真空。

- [x] keyframe 上跑時間戳 OCR，存進 DB（2026-07-18）：新 `ocr_captions` 表（schema v8，帶 `timestamp_sec` + `engine` 出處）；`reel_scout/ocr.py` `collect_captions`，pipeline Step 3.6 收集
- [x] **用燒錄字幕補強 transcript**（2026-07-18）：merger 新增「On-screen Text (L3.5)」區塊餵進分析——STT 空（純視覺片）時 on-screen text 仍給料。（校正走 merge context 讓 LLM 交叉判讀，非直接改 transcripts row）
- [x] 併進 signal-reliability cheatsheet（2026-07-18）：新增 L3.5 層（主表 + 專節），定位在 L3 與 L4 之間
- [x] **兩條 OCR 路都做**（Hevin「都做」）：`OCR_ENGINE=vlm`（預設，復用 VLM `text_in_frame`，零依賴）＋ `OCR_ENGINE=tesseract`（opt-in `ocr` extra，`importlib.util.find_spec` guarded，裝不到退回 vlm）

> ⚠️ 邊界：dedicated OCR 引擎（pytesseract）列為 opt-in extra、預設不啟，守 minimal-deps；PaddleOCR-CJK 更重故未納，需要再評。
> ⚠️ 契約（codex+harness 雙審確認、非 bug）：OCR captions 只透過 merge prompt 影響分析（跟 transcript/vision/audio 一樣，只在「首次 merge」折入）。重跑已分析的舊片會存 `ocr_captions` 但不會重新 merge → 分析不變，直到真正重新 merge。§4E `measured` 能 backfill 是因為 scorer 直接讀它；OCR 文字沒有下游可 append，故不對稱是刻意的。

---

## Phase 5 — Tool Hygiene（工具品質，非社群營運）

**目標**：讓這個工具**可安裝、可信賴、不會安靜爛掉**。這裡沒有推廣/社群項目——見「定位與 Non-goals」。

### 5A. 可安裝

- [x] LICENSE: MIT
- [x] README（EN + 繁中）+ 安裝/使用說明
- [x] `pyproject.toml` 完整（entry points、optional deps 分組）
- [x] **PyPI 發布** ✅ 2026-07-19 — `pip install reel-scout` 已可用（v1.2.0 上架，wheel + sdist）。走 `.github/workflows/release.yml` **Trusted Publishing / OIDC，零 token**（GitHub Release published 觸發 + 可手動指定 tag；含測試 gate 與 tag↔`__version__` 一致性檢查，build/publish 分離）。實證：PyPI API 200、乾淨 venv 從 PyPI 裝後 `reel-scout --version` → 1.2.0、entry points 與新命令皆正常。之後發版只要開 GitHub Release 即自動上傳
- [x] 版本/CHANGELOG 流程固定（2026-07-18）：CHANGELOG 加 Unreleased 段，Wave 3 每 feature 一條 + schema v6→v9 記錄
- [x] **裝得起來 ≠ 跑得動：L0/L1/L2 安裝階梯** ✅ 2026-07-20 — 原本「裝完還要自備本地 VLM」是非技術使用者的真實斷點（`pip install` 成功但 `analyze` 在 VLM 那步掛掉）。
      解法不是加雲端 backend（要 API key＝要學員付錢），而是認清 **keyframe 是 ffmpeg 抽的、不是模型抽的** —— 影格在 VLM 階段之前就已經在磁碟上。
      新增 `ingest {vision,score} --from-json`：看得見圖的 agent 自己補視覺層與 rubric 評分再寫回 DB，結果照常進 `show`/`view`/`inspect`/`export`。
      **零本地模型、零 API key、零額外費用**（用學員本來就有的 Claude）。守住兩條紅線：出處一律標 `agent:<model>`（craft 分數依模型而異，同片 7.43 vs 5.5），`overall` 一律用 `score` 的權重重算、不採信輸入。
      順帶把 `show` 補成會列 keyframe id／時間／路徑（外部定位單一影格的唯一途徑）與分數。SKILL.md 的 surface 段從二分法改寫成 L0/L1/L2 三層。測試 →263
- [x] **分發：skill 隨套件出貨** ✅ 2026-07-20 — **乾淨 venv 實測**發現上一條做的 L1 其目標使用者根本拿不到：wheel 只含 `reel_scout/`，`pip install reel-scout` 後 `SKILL.md`／`commands/scout.md`／`prompts/`／`scripts/setup.py` **四個全缺**，agent 無物可載、`/scout` 不存在，整條流程只有 clone 的人碰得到。
      而 repo 內唯一提到「怎麼裝 skill」的文字，是競品分析在講 crv 的 `npx skills add`——對標表 §103 早就誠實記著易安裝這項 **crv 贏**。
      修法＝pyproject `force-include` 把資產 vendored 進 wheel（`reel_scout/skill/`）＋新增 `skill {install,path}` 複製到 `~/.claude/skills/reel-scout`；clone 仍讀工作樹而非快照，`scripts/` 只取 setup.py。
      **學生完整安裝＝兩行**：`pip install reel-scout` → `reel-scout skill install`。順帶修掉 `setup.py` 對無 clone 使用者印字面 `<repo-root>` 佔位符的 bug（該檔原本**零測試覆蓋**，故補 6 支）。測試 →280

### 5B. 不會安靜爛掉

- [x] **GitHub Actions CI** — pytest matrix Python 3.9–3.13（2026-07-17，`.github/workflows/ci.yml`）；push master + 每個 PR 觸發，只裝 base+dev（suite 全 headless，2 個 onnxruntime 測改 `importorskip`）。實測 5 條 leg 全綠
- [x] **`analyze <local-path>` — 平台關門的唯一實際保險**（見 Non-goals #1 的實測爆炸半徑）。2026-07-17 完成：
      `analyze` 的 URL 引數現在也吃本機檔路徑 → 註冊一列 `platform="local"`、`url == file_path == abspath`、`platform_id` 用內容 hash（同內容不同路徑會 dedup 到同一 video_id）的 row，Steps 2-5 完全不用改。
      duration 用獨立 probe，失敗留 `None`（**不**寫 `60.0` 謊言）；路徑打錯給 `FileNotFoundError: Local file not found` 而非 crawler 的 opaque「Unsupported platform」。
- [x] **yt-dlp 從 PATH 解析，不是用自己 pin 的那支**（2026-07-15 實測）。2026-07-17 完成：新增 `crawl/ytdlp.py`，
      所有 crawler 改走 `ytdlp.cmd(...)` — 預設用 `python -m yt_dlp`（本 venv 那支），可用 `YTDLP_BIN` 覆寫。
      實測驗證：PATH 上是 homebrew `2026.03.17`、resolved 是 venv `2026.07.04`（先前 crawl 靜默吃到過期那支）。
- [x] yt-dlp 相依健康檢查：平台 extractor 壞掉時給明確錯誤 + fallback 指引。2026-07-17：`ytdlp.format_error` 偵測
      extractor-類失敗（Unable to extract / Unsupported URL / …）時附上 `<resolved> -U` / `pip install -U yt-dlp` 更新提示。
- [x] **錯誤訊息只印 `stderr[:500]`，真因常被前 500 字的 warning 淹掉**（2026-07-15 追字幕 429 case）。2026-07-17：
      `ytdlp.format_error` 優先留 `ERROR:` 開頭的行；無 ERROR 行才退回取 stderr 尾段（真因通常在尾不在頭）。
- [x] `config check` 涵蓋所有後端可達性（2026-07-17）：yt-dlp 改用 `ytdlp.base_cmd()`（跟 runtime 一致，非硬寫）、LLM 可達性依 `LLM_BACKEND` 查、補 audio/diarize/instagram optional 後端（已配置才查，diarize 缺 token 標紅）；`_run_config_checks()` 抽成結構化可測函式

### 5C. 文件

- [x] `docs/`：命令 + MCP + backend + config 參考（2026-07-18）→ [`docs/commands.md`](./commands.md)
- [ ] 範例輸入 → 範例輸出（不含版權素材）— 待補（需非版權素材樣本）

---

## Phase 6 — Shot-level 反解（提議中，2026-09-01 開）

**起點**：用 reel-scout 拆了三支 MV（4–5.5 分鐘的音樂錄影帶，非短影音）之後，跟一套**分鏡／PPM 製作工具**（STB，同作者的 macOS App，案子的唯一真相是一份 `project.json`）對照，發現兩邊是**同一個資料結構的兩個時間方向**：

```
分鏡工具的 cut = 計畫（要拍什麼）      ← 正向
reel-scout 的 shot = 紀錄（他拍了什麼） ← 反向
```

這正是 README/課程一直在講的「拆爆款 → 抽 SOP → 倒推自己的分鏡」閉環——**目前那個閉環只有教材，沒有工具**。

### 🔴 6A. `shots` 表：剪點時間碼現在被算出來然後丟掉

`reel_scout/shots.py` 的 `parse_cut_count()`：

```python
return len(_TS_PATTERN.findall(stderr))   # ← pts_time 全解出來了，只取 len()
```

**每一刀的時間碼每次 analyze 都算一次，然後整串扔掉，只留一個數字進 `shot_metrics`。**

⚠️ 這不是新範圍，是 **§4E 沒做完的那一半**。§4E 的啟發來源自己寫著「crv Pro 的 `--motion` 產出 shot table（**per-shot duration** / cuts per minute / 節奏變化）」——當時偷了 cuts/min，**per-shot 那半沒偷**。

- [x] ✅ **2026-09-02 落地**：保留剪點時間碼 → 新 `shots` 表（`idx` / `start_sec` / `end_sec` / `dur_sec`，schema v14）。**「一顆鏡頭」第一次成為可定址的物件。**
  - `parse_cut_times()` 保留原本就已經算出來的東西；`shots_from_cuts()` 是純函式，不用 ffmpeg 就能單元測試
  - 🔴 **一份邊界餵兩個輸出**：`compute_shot_table()` 一次 ffmpeg pass 同時回 metrics 與 spans，所以 `len(shots) == shot_count` **是結構上成立、不是靠兩邊講好**。把「退化邊界」從其中一邊濾掉而另一邊不濾，正是同一個資料庫裡兩張表開始靜默打架的方式 —— 所以**一個都不濾**：邊界落在 0.0 就產生一顆零長度的開場鏡並如實記錄，那是關於偵測器的資訊，不是該藏起來的雜訊
  - 實檔驗證（三支 MV）：spans **50 / 100 / 170**，與 `shot_count` 完全一致，各段時長總和 **精確等於片長**（243.00 / 271.00 / 334.00 秒），無縫隙無重疊
  - `save_shots` 是 replace 不是 append（`(video_id, idx)` unique）——第二次分析同一支片時 append 會留下兩份重疊的切分而沒有任何東西標示哪份是現行的
- [x] ✅ **2026-09-02**：`stats` 一併報 **shot table 覆蓋率**。
  - 🔴 **為什麼這條緊接在 6A 後面、排在 6B 前面**：`shots` 是純加法遷移，所以 v14 之前分析的片**有剪點數、沒有 span**，而工具裡沒有任何一個地方會提這件事。落地當下實測是 **3 / 116** —— 正是這份 roadmap 剛剛才寫過的那個病的形狀（§4E 空了六週沒人知道）。**新抽取層落地的同一輪就要有人在量它**，否則六週後又會有人來「發現」它是空的。
  - 輸出多兩行：`shot table (spans)` 與（有缺口時）`missing spans N <- have the cut count but not the spans; re-analyze to fill`。**補救方式跟數字一起印** —— 光給缺口數，讀的人不會知道該做什麼
- [x] ✅ **2026-09-02**：keyframe 綁到 shot、每顆鏡頭有代表幀。**改成推導、不存欄位**（原文寫的是綁 `shot_id`，實作時否決）：`save_shots` 是 replace，重跑 analyze 會換掉整份 span 並鑄出新的 `shots.id`，**存好的外鍵會全部指向不存在的列而且不會有任何東西報錯**。推導每次從當下的 span 重算，不可能陳舊。
  - `shot_index_for()` 半開區間 `[start, end)`：**落在剪點上的幀屬於它「打開」的那顆，不是它「收掉」的那顆** —— 那正是剪點的意思。最後一顆閉合，所以 `t == 片長` 的幀不會掉出去
  - 代表幀取**最接近鏡頭中點**者（同距取較早）。不取第一幀：剛過剪點的那一幀最可能是溶接／甩鏡／動態模糊，正是最不能代表該鏡頭的一幀
  - 區間外的幀**回報不吞掉**：那代表幀表與鏡頭表是對同一支片的兩次不同量測
  - `show` 新增 Shots 區塊（超過 20 顆截斷並說明截了幾顆）
  - 🔴 **同輪修掉 6A 自己埋的一個 bug**：`shots_from_cuts` 原本把邊界 `round(...,3)`，而 keyframe 帶的是全精度時間碼。同一個剪點 `7.5075` 被存成 `7.508`（進位）→ 幀掉到前一顆；`13.680333` 被存成 `13.68`（捨去）→ 正確。**四捨五入的方向決定歸屬，所以剛好一半錯**（實測 19/19），而且不會有任何東西報錯。改成**只在顯示時 round、儲存不 round**。實測效果：Wax On Wax Off 有幀的鏡頭 **30 → 39**
- [x] ✅ **2026-09-02**：逐字稿 segment 與 `ocr_captions` 投影到 shot ——**旁白與字卡自動分流到各自的鏡頭**。至此 **6A 完成**。
  - 字卡是**時間點**，跟 keyframe 同形，直接複用 `bind_frames_to_shots`
  - 🔴 **旁白是時間區間，不是點** —— 一句 VO 可以橫跨數顆鏡頭，所以這裡有一個要拍板的岔路，選了**按重疊全掛**（Hevin 2026-09-02 拍板）：一句話出現在它涵蓋的**每一顆**鏡頭底下。理由是這條線的終點是 6D 分鏡交付格式，而分鏡表的 `vo` 欄問的是「這顆鏡頭當下在講什麼」——**把一句十二秒的旁白只歸給它開始的那顆，後兩顆就會讀成沒有台詞，那是錯的資訊不是缺的資訊**。重複由 span 邊界天然界定，是結構不是雜訊
  - 重疊判定兩端都半開（`a < end and b > start`）：收尾正好在剪點上的句子不會外溢到下一顆；零寬 span（VTT 字幕 cue 真的會 `start == end`）退化成點語意；零長度鏡頭一樣什麼都不收
  - `show` 的 Shots 區塊補上 `vo:` / `sup:` 兩行（截斷並標示），表頭加上「幾顆有 VO／幾顆有字卡」
  - ⚠️ **方法學修正**：mutation 測試改為 `PYTHONDONTWRITEBYTECODE=1`。Python 的原始碼 mtime 檢查是**秒級**的，而 mutate→restore 在同一秒內完成時，還原後的檔案會沿用突變版的 bytecode —— 那會同時製造假的 CAUGHT 與假的 MISSED。發現後把 #88 的三個關鍵守衛也在無 bytecode 下重驗過，結論不變

### 6B. 受限詞彙：景別

VLM 現在用散文說「close-up shot」，**沒有正規化欄位**。而詞彙表已經寫在自己 repo 裡了——`prompts/storyboard-visualize.md` 的 ECU / CU / MCU / MS / MLS / LS / ELS，只是它現在服務的是**正向**（鏡頭表 → 生圖 prompt）。反向要的是同一套詞當**受限輸出**。

- [x] ✅ **2026-09-02**：詞彙與正規化落地（`reel_scout/shot_size.py`）。值域 `ECU / CU / MCU / MS / MLS / LS / ELS` **直接引用 `prompts/storyboard-visualize.md:100`**，並有一支測試去讀那個檔比對——**兩份清單會漂，而且會靜默地漂**，所以只有一份、另一份是被驗的副本。
  - `normalize()` **兩種情況一律回 None**：句子沒提到取景（多數如此）、或一句裡出現兩種不同景別（「a close-up of a hand against a wide shot backdrop」有真正的答案，但那個答案不在這句話裡，取第一個命中會長得跟「知道」一模一樣）
  - **鏡頭語言不是景別**：`wide-angle lens` 講的是光學，廣角鏡天天在拍特寫。硬映射會把一個有信心的錯值放進一個本來要拿來信任的欄位
  - 兩字碼只以整詞比對 —— 否則 `cu` 會在 `curtain` / `document` / `focus` 裡命中
- [ ] 🔴 **標籤的來源還沒決定，而離線抽取這條路已經被資料否掉了**（2026-09-02 實測）：拿 `normalize()` 掃既有 **2,828 筆** VLM 描述，只有 **17.3% 抽得出景別**，而且 **488 筆裡有 477 筆是 CU**。那不是真實的景別分布，是 VLM 把 close-up 當通用形容詞的用詞習慣。→ **離線抽取只能當 fallback，不能當主要來源。** 主要來源要嘛讓 VLM 從固定清單裡選（重跑 2,872 楨，約 9.5 GPU 小時，且是 model-dependent）、要嘛走既有的 `ingest` agent 路徑由外部供給。**兩條都要把來源與模型戳記進去**（比照 `scores.model_used` 的教訓），未拍板

### 6C. 運鏡（固定／手持／搖／跟／推拉）

**目前完全沒有。** `vision/keyframe.py` 的 `motion` strategy 是 mpdecimate **選幀**，不是運鏡判讀。

> 📌 **這一項在 §參考案例 的「刻意不偷」清單上並不存在**。那張清單列的是：情緒／mood／ai-report、變速鑑識、$19 漏斗。**「鏡頭運動」從未被納入、也從未被否決**，它是懸空的——而它通得過本專案自己那條篩子（「只偷讓判讀更可實測的部分」），因為幀間位移是可量的，不是 model-dependent 的主觀輸出。

- [ ] 在每顆 shot 內解降尺寸灰階幀，純 numpy 算相鄰幀位移與尺度變化 → 近零＝固定／高頻低振幅＝手持／單向持續位移＝搖或跟／尺度單調變化＝推拉
  > ⚠️ 依賴邊界（2026-09-01 實查本機 ffmpeg）：**沒有 `vidstabdetect`**（libvidstab 未編入），有 `mestimate` 但輸出是視覺疊層不是資料檔。所以走 numpy，形狀比照 `audio/rhythm.py`：純 numpy、best-effort、測不準回 None

#### 🔴 6C spike 結果（2026-09-02，兩輪）：**不出貨**，而且第一輪的診斷是錯的

寫在這裡是為了讓下一次不要重跑這三條死路。

**① 位移訊號本身是實的、而且便宜。** 全片一次 ffmpeg pass 解成 64×64 灰階 @8fps
（4 分鐘＝1,942 楨、約 8 MB），純 numpy FFT 相位相關。實測 `Wax On Wax Off`：
**61% 相鄰幀零位移**、p90 = 2.24px。統計量必須用中位數 —— 有顆鏡頭中位數 0.00
但平均 4.16，少數大跳動把平均帶走。

**② 一顆鏡頭被判成「方向一致度 0.98 的搖鏡」。抽首尾兩幀肉眼看，是兩組完全不同的素材。**

**③ 🔴 第一輪我把原因寫成「scene 偵測漏了一個剪點」—— 那是錯的**（本段即為更正）。
第二輪沿著 74–78s 逐幀抽出來看：

    75.5s  人物臉部大特寫在右，五個小人影在左
    76.0s  兩者同框，一大一小
    76.3s  五個人放大填滿畫面，臉不見了

**那裡沒有剪點，是一個設計過的合成轉場**（畫面內一個大元素在縮放位移）。
scene 偵測沒有漏。錯的是我把「畫面裡的大元素在動」讀成「攝影機在動」。

⇒ **真正的難點：相位相關量的是表觀全域運動，它分不出「攝影機動了」與
「畫面裡一個大元素動了」。** 而修剪點偵測**救不了這件事**，因為根本沒有剪點要修。

**④ 三個候選判別訊號，全部實測推翻：**

| 訊號 | 直覺 | 實測 |
|---|---|---|
| 相位相關**峰值高度** | 兩幀不相干時峰值低 | **反了**：跨剪點 p50=0.53、鏡頭內 p50=0.36（黑底畫面互相關性強） |
| **幀差**（相鄰幀平均絕對差） | 剪點處幀差大 | **反了**：已知剪點 p50=0.025、鏡頭內 p90=0.093 |
| **分格一致度**（4×4 各格位移是否同向） | 攝影機動＝全格一起動；元素動＝只有局部 | 那顆合成轉場拿 **0.898**，排在高段。且 64×64 切 4×4 後每格僅 16×16px，暗畫面上相位相關本身不可靠 |

**⑤ 結論：這條路在「便宜 ＋ 純 numpy ＋ 這種素材」的交集下不成立。**
出一個分類器等於把「元素運動」包裝成「運鏡判讀」—— 一個看起來有信心、實際沒依據的值，
正是 §4E 整條在解的病。

**要真的做起來需要什麼**（都不便宜，故未做）：(a) 更高解析度的取樣；
(b) 能區分整體與局部運動的稠密光流 —— 要引入 opencv/scipy 級依賴，違反 minimal-deps；
(c) 或先在**素材較單純**的語料上驗證 —— 音樂錄影帶（大量合成、黑底、快剪）
幾乎是最差的測試素材。

⚠️ **本項維持未勾。要重啟先讀上表，別再測一次同樣的三個訊號。**

### 6D. 交付格式：`export --format <storyboard>`

沿用 `export --format skeleton` 已經立好的先例（資料格式交接，兩邊都不 import 對方）。目標工具讀到外部改寫的 `project.json` 會在 2 秒內自動重載——等於匯出即刷新。

- [x] ✅ **2026-09-02**：`export --format storyboard` 落地。一支已分析的片 → 一份 `<video_id>.project.json`，每顆 shot 一格 cut，帶景別（可判定時）、秒數、畫面描述、VO、字卡。
  - **沿用 `--format skeleton` 的契約**：資料格式交接、兩邊都不 import 對方
  - `sec` **不足一秒就省略該欄**而不是寫 0 —— 那個欄位會被頁尾合計，一列寫著「0 秒」比沒有時長更糟（30 cuts/min 的 MV 有的是這種格）
  - **`aspect` 量不到就省略，並印警告** —— 省略時分鏡工具會套自己的預設 16:9，那對直式素材是錯的，所以不能讓「省略」默默變成一個決定
  - ⬛ **`gear` / `scoutRef` / `scoutMeta`（感光元件、焦段）一律不寫**：那些欄位的填寫者是現場的攝影師，從成片回推焦距是猜測，而**一個猜出來的數字進了 PPM，對下游所有人看起來都跟量出來的一樣**
  - `imageRef` 留 `null`（schema 明確警告該欄是圖檔資料），代表幀的 id 放進 `note` 供查
  - **每顆 cut 各自成組**：連續鏡群組是這個匯出沒有依據做的判斷，猜了會靜默合併，之後要人手動拆開
  - 沒有 `shots` 的片**跳過不匯出**：一格橫跨四分鐘不是分鏡表，吐出來會看起來像成功了

**⬛ 刻意不做**：器材、焦段、感光元件格式（`ff`/`s35`）這些欄位**留空**。從成片回推焦距是估計問題，而那些欄位的填寫者是現場的攝影師。猜一個數字填進去，它會混進客戶看的 PPM 而沒有人知道那是猜的。`props` 可以用 `objects_json` 產**候選清單**，但要標成建議。

### 🔴 6E. 紅線：來源標記

reel-scout 的 shot 是**別人的片**；分鏡工具的 `project.json` 是**要送到客戶面前的 PPM**。這座橋一通，「參考」與「模板」之間就只剩一次存檔。

- [x] ✅ **2026-09-02**：三個地方同時標記，人不可能三個都沒看到：`meta.title` 帶 `REF:` 前綴、`meta.client` 寫明是拆解不是原創、**每一顆 cut 的 `note`** 帶來源 URL 與進出時間碼（＋代表幀 id）。
  - 選 `note` 是因為分鏡工具會把它印出來，編那一格的人就會看到。要拔掉得**逐列**動手 —— 那正是重點：這道守衛防的不是惡意，是六週後有人打開檔案忘了它從哪來
  - 四個變異驗過：拿掉 note 的來源、拿掉標題前綴、清空 client、以及景別猜預設值，全部 CAUGHT

### 排序（刻意的）

**6A 不是第一步。** 這個 repo 有紀錄的頭號失效模式是 completion-blind drift（§4E 的 0 列一次、0.0 分那列一次），而 Phase 6 會新增第三個可以安靜空掉的抽取層。所以順序是：

1. ~~`stats` 報證據覆蓋率（v1.3 欠的 (b)）~~ ✅ 2026-09-01
2. ~~`db check-invalid --apply` 跑過既有語料~~ ✅ 2026-09-02 —— 命中 **1 列**（4h11m 直播，0 keyframes / 55,521 字逐字稿 / 存 0.0 分）。標掉之後 `stats` 地板從 **0.0 回到 3.05**，而且**證據覆蓋率的「without measured」那行整個消失**：那支孤兒跟那筆假的 0.0 是同一列
3. ~~開 `shots` 表~~ ✅ 2026-09-02 —— 排在 1、2 之後是刻意的，落地當下就有 `stats` 在量證據層有沒有真的長出資料
4. **下一步：6B → 6C → 6D/6E**

---

## 參考案例（study cases）

### claude-real-video / crv（2026-07-17 對標）

`HUANGCHIHHUNGLeo/claude-real-video` — 「讓 LLM 看得見影片」的擷取工具（Python/MIT，2.5 週衝 1,699★），另有閉源付費 **crv Pro（$19 一次性）**加鏡頭運動/情緒/OCR/變速鑑識。**不同層**：crv（含 Pro）是感知＋測量層（把影片攤平給 LLM 看、量化「怎麼拍的」），Reel Scout 是判讀層（帶 rubric 打分＋反解會不會紅＋競品語料）。重疊只在 ingest。

**完整對照** → [`docs/crv-vs-reel-scout.md`](./crv-vs-reel-scout.md)。

> 📌 **2026-09-01 修正這段的「不同層」**：§Phase 6C（運鏡）一旦做下去，reel-scout 就**跨進了 crv 那個感知／測量層**，「重疊只在 ingest」不再成立。這不是改變立場——原則仍是「只偷可實測的部分」，運鏡通得過那條篩子——但**這句話該停止當成兩邊互不相干的證據**。真正沒被偷的仍然是：情緒／mood／ai-report、變速鑑識、$19 漏斗。

**對本專案的關聯**：可偷的實測強化已開兩條 → §4E（pacing/BPM 實測化）、§4F（燒錄字幕 OCR 補 transcript）。原則是**只偷讓判讀更可實測的部分**，crv 另一半 model-dependent 主觀輸出（情緒/mood/ai-report）、變速鑑識、病毒開源→$19 漏斗**刻意不偷**（前者放大既有軟肋、後者違反「工具不是產品」定位，只當課程 case study 存檔）。

### lapian-notes（2026-07 觀察）

`bkingfilm/lapian-notes` — AI 輔助電影拉片工具（TS/React/Vite，MIT）。**不同類別**（末端產品，非工具），但架構有一點值得存檔備用：

**AI-agnostic package handoff**：它自己完全不跑推論。本機抽幀 + 抓字幕 + metadata → 打包 ZIP → 使用者自己丟給任何 AI → 把回傳的 JSON 匯入 → 工具只負責視覺化。零 API key、零 GPU、零後端。

**對本專案的關聯**：目前 **不需要** — reel-scout 已有 `--llm-backend openclaw`（走 proxy 吃訂閱制，同樣不需要 GPU），是比手動貼 ZIP 更好的機制。

**未來可能用得上的情境**：若要讓完全不想配置任何後端的人也能跑 merge/score，可加 `package` / `import` 一對指令（reel-scout 已有 keyframe + transcript，工程量不大）。**等真的有這個需求再做。**

---

## 設計原則

1. **CLI-first** — 所有功能先有 CLI，再有 MCP/API
2. **Offline-capable** — 核心分析可用本機 LLM（oMLX/Ollama），不強制雲端；但雲端/訂閱後端（openclaw）也是一等公民
3. **Minimal dependencies** — urllib not requests，argparse not click，SQLite not Postgres
4. **Python 3.9** — 維持較舊系統相容
5. **Batch-friendly** — 大量影片分析是核心場景，不只是單支
6. **工具不是產品** — 見「定位與 Non-goals」

---

## 里程碑

> ⚠️ 版號與 phase 已脫鉤：v0.2.0（雙語轉錄）是計畫外的品質修復，不在原 Phase 規劃內。以下里程碑改為**以能力為準、不綁 phase 編號**。

| Milestone | 條件 |
|-----------|------|
| **v0.3** | ~~3A 補完（`crawl --channel/--playlist`）~~ ✅ 2026-07-15 + ~~3B（`compare`）~~ ✅ 2026-07-17 |
| **v0.4** | ~~3C 標籤正規化~~ ✅ + ~~3D（`stats`）~~ ✅ 2026-07-17（達成，隨 v1.0 一次發布） |
| **v0.5** | ~~4A（競品研究報告）~~ ✅ 2026-07-17（達成，隨 v1.0 一次發布） |
| **v1.0** | ~~5A + 5B 完成（PyPI build 就緒 + CI 綠 + yt-dlp 健康檢查 + config check）~~ ✅ 2026-07-17（PyPI 上架待人工 token） |
| **v1.2** | ~~§4E 評分證據化 + §4F L3.5 OCR + Wave 3（3B patterns / 3A instaloader / 4B inspire / 4D track / 4C MCP 8-tools / 5A+5C docs）~~ ✅ 2026-07-19（PR #31/#32/#33，schema v9，228 tests；⚠️ pacing 評分行為改變，跨版本分數不可比）<br>🔴 **2026-07-20 更正：§4E「達成」僅指 code，語料上 `shot_metrics` 為 0 列、從沒真的跑過** → 證據層實質未生效，見 §4E 回填段 |
| **v1.3** | ~~權重即時重算 + 中英介面切換~~ ✅ 2026-07-21（PR #35，v1.3.0 發布）|
| **v1.3（原提議的證據層條件）** | 條件有兩個，**2026-09-01 實查：一過一沒過**。<br>✅ (a) `shot_metrics` 列數 > 0 —— **116 列**，遠超條件。<br>✅ (b) `stats` 能報出證據覆蓋率 —— **2026-09-01 補上**（`-- Evidence coverage --` ＋ `--json` ＋ `--csv` ＋ `--channel` scope）。<br>⚠️ 記一下這條怎麼欠下的：**版號當時已經走過去了（v1.3.1）而 (b) 沒跟上**——里程碑當條件用、版號當進度用，兩者脫鉤就會這樣。原句「這條沒過之前，任何評分 UI 精修都是在裝飾沒有地基的數字」現在兩半都有了：地基 116 列，儀表板也在了。 |
| **v1.4（提議）** | **證據可見性 + shot-level 地基**，順序是刻意的（見 §Phase 6）：<br>✅ ① `stats` 報證據覆蓋率（補完 v1.3 的 (b)）—— 2026-09-01 done<br>② `db check-invalid --apply` 跑過既有語料（Unreleased 已有 code，庫裡那列 0.0 還在）<br>✅ ③ `shots` 表（schema v14）—— 2026-09-02 done<br>**條件已達成**：`stats` 講得出證據覆蓋率（114/114）**且** `shots` 有列（三支 MV 共 320 段實測）。**①② 排在 ③ 前面不是禮貌，是因為 ③ 新增了第三個可以安靜空掉的抽取層，而這個 repo 已經在同一個坑裡跌過兩次。** |
