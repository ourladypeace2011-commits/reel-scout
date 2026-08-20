# Reel Scout — Roadmap

> 最後校正：2026-07-15（對照實際 code 逐項驗證，非憑記憶）
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
```

**目前版本**：v1.2.0 ｜ **測試**：324 passing ｜ **DB schema**：v9

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
- **DB**: SQLite WAL + batch resume + schema migration（→ v9）

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

- [ ] 🔴 **對既有語料補跑 §4E/§4F 抽取**，讓 `shot_metrics` / `ocr_captions` 真的有列。要先確認的兩件事：(a) 補跑是否需要原始影片檔仍在（`data/videos/`）——若已清掉，得先決定是重抓還是只對還在的補；(b) 補跑後**既有 12 筆分數要不要重評**——重評會讓分數跨版本不可比（§4E 落地時已知的問題，這次是第二次觸發）。
- [ ] 🔴 **加一個「證據層是否為空」的可見性**：`stats` 或 `config check` 應該要能告訴你「N 支影片有分數、其中 M 支有實測證據」。這次是靠人工 `SELECT COUNT(*)` 才發現，等於這個洞可以無限期隱形——**能力存在但沒被使用，跟能力不存在，對使用者是同一件事**。
- [ ] （順帶）`pipeline` 是否該在 `shot_metrics` 寫入失敗時出聲，而不是靜默留白。目前無從得知是「沒跑」還是「跑了但失敗」。

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

## 參考案例（study cases）

### claude-real-video / crv（2026-07-17 對標）

`HUANGCHIHHUNGLeo/claude-real-video` — 「讓 LLM 看得見影片」的擷取工具（Python/MIT，2.5 週衝 1,699★），另有閉源付費 **crv Pro（$19 一次性）**加鏡頭運動/情緒/OCR/變速鑑識。**不同層**：crv（含 Pro）是感知＋測量層（把影片攤平給 LLM 看、量化「怎麼拍的」），Reel Scout 是判讀層（帶 rubric 打分＋反解會不會紅＋競品語料）。重疊只在 ingest。

**完整對照** → [`docs/crv-vs-reel-scout.md`](./crv-vs-reel-scout.md)。

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
| **v1.3（提議）** | 🔴 §4E 證據層**補跑到語料上**（不是再寫 code，是讓既有 code 真的產出資料）+ 證據為空的可見性（`stats` 要能講「N 支有分數、M 支有證據」）。<br>條件：`shot_metrics` 列數 > 0 且 `stats` 能報出證據覆蓋率。**這條沒過之前，任何評分 UI 精修都是在裝飾沒有地基的數字。** |
