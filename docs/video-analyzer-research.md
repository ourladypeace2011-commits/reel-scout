# video-analyzer 研究報告

## 1. 專案概覽

`video-analyzer` 是一個三階段 pipeline：CLI 先載入 JSON config、建立 vision client 與 prompt loader，接著做音訊轉錄與 frame extraction，然後逐張 frame 分析，最後把逐 frame notes 與 transcript 重建成整支影片的敘事摘要。入口與主流程在 `video_analyzer/cli.py:60`、`video_analyzer/cli.py:114`、`video_analyzer/cli.py:153`、`video_analyzer/cli.py:168`、`video_analyzer/cli.py:176`。

依賴上，它偏向「本地多媒體處理 + 雲端/本地 LLM 客戶端」：`requirements.txt` 直接依賴 `opencv-python`、`numpy`、`torch`、`openai-whisper`、`requests`、`Pillow`、`pydub`、`faster-whisper`，明顯比 Reel Scout 現在的最小依賴更重。設定採 JSON config 疊加 CLI args 的 cascade，定義在 `video_analyzer/config.py:10`、`video_analyzer/config.py:31`、`video_analyzer/config.py:59`，預設值在 `video_analyzer/config/default_config.json`。

支援的視覺/LLM backend 有兩種：

| 項目 | video-analyzer | Reel Scout |
|------|------|------|
| Vision backend | Ollama、OpenAI-compatible API，見 `video_analyzer/cli.py:48`-`video_analyzer/cli.py:58` | oMLX、Ollama |
| Text reconstruction backend | 與 vision 共用同一個 client/model，見 `video_analyzer/analyzer.py:63`-`video_analyzer/analyzer.py:70`、`video_analyzer/analyzer.py:113`-`video_analyzer/analyzer.py:121` | 獨立 LLM backend，見 `reel_scout/analyze/merger.py:87`-`reel_scout/analyze/merger.py:88` |
| Config style | JSON config + CLI override | env/config constants |
| Output | `analysis.json`：metadata/transcript/frame_analyses/video_description | DB 正規化儲存 + merged JSON |

## 2. Frame Sampling 分析

### 2.1 video-analyzer 的做法

核心邏輯在 `video_analyzer/frame.py:17`-`video_analyzer/frame.py:115`：

| 面向 | 做法 | 程式碼 |
|------|------|------|
| 抽樣底層 | 用 OpenCV 逐幀讀取，不用 ffmpeg scene detect | `video_analyzer/frame.py:54`-`video_analyzer/frame.py:60`、`video_analyzer/frame.py:80`-`video_analyzer/frame.py:91` |
| 差異分數 | 先轉灰階，再做絕對差分平均值 | `video_analyzer/frame.py:27`-`video_analyzer/frame.py:40` |
| 候選產生 | 不是每幀都算，而是先用 `sample_interval` 做預採樣，再留下高於 threshold 的候選 | `video_analyzer/frame.py:73`-`video_analyzer/frame.py:88` |
| 張數控制 | 先算 `target_frames = video_duration/60 * frames_per_minute`，再受 `max_frames` 限制 | `video_analyzer/frame.py:66`-`video_analyzer/frame.py:71` |
| 最終選幀 | 先依 score 由高到低挑前 `target_frames`，必要時再均勻抽稀，最後按時間排序 | `video_analyzer/frame.py:95`-`video_analyzer/frame.py:106` |
| 輸出欄位 | 每張 frame 記 `number/path/timestamp/score` | `video_analyzer/frame.py:10`-`video_analyzer/frame.py:15`、`video_analyzer/frame.py:108`-`video_analyzer/frame.py:115` |

幾個重要觀察：

1. 它不是 scene detection，而是「均勻預採樣 + 相鄰灰階差分 + 依分數回選」。
2. 它沒有 first/last frame guarantee。更嚴格地說，第一個採樣點因為 `prev_frame` 為 `None`，`_calculate_frame_difference()` 直接回 `0.0`，所以第一個採樣點通常不會成為候選，見 `video_analyzer/frame.py:29`-`video_analyzer/frame.py:30`、`video_analyzer/frame.py:85`-`video_analyzer/frame.py:89`。
3. 它沒有模糊偵測、重複內容抑制、OCR 可讀性評分，只用畫面差異當 score。
4. 它有保留 `score`，這對後續可視化/debug 很有價值。

### 2.2 與 Reel Scout 對比

| 面向 | video-analyzer | Reel Scout |
|------|------|------|
| 抽樣方式 | OpenCV 差分候選 + score 排序 | `scene` / `interval` / `hybrid` 三策略，主要用 ffmpeg，見 `reel_scout/vision/keyframe.py:31`-`reel_scout/vision/keyframe.py:58` |
| scene aware | 間接，靠畫面差異 | 直接用 ffmpeg `select='gt(scene,0.3)'`，見 `reel_scout/vision/keyframe.py:76`-`reel_scout/vision/keyframe.py:110` |
| 固定上限 | `frames_per_minute` + `max_frames` 雙控制，預設 `frames.max_count=30` | `KEYFRAME_MAX` 預設 8 |
| 時間分佈 | 先挑高分，再重新按時間排序；可保留高資訊密度 frame | `scene` 偏事件切點，`interval` 偏均勻，`hybrid` 只在不足時補 interval |
| 可觀測性 | 每張 frame 有 `score` | 只有 `strategy/timestamp`，沒有品質分數 |
| 首尾保證 | 沒有 | 也沒有 |

Reel Scout 的優勢是 scene cut 精確、依賴較輕、與 ffmpeg pipeline 一致；video-analyzer 的優勢是保留「顯著度排序」這個中介訊號，讓 frame 選擇不只是切點驅動。

### 2.3 建議改進

| 優先級 | 改進 | 原因 | 目標檔案 |
|------|------|------|------|
| P1 | 在 `hybrid` 之前新增 `ranked` 或 `diff` 策略：先做低成本 interval 預採樣，再以簡單 frame difference 分數排序 | 能補足 scene detect 抓不到的持續動作型影片 | `reel_scout/vision/keyframe.py` |
| P1 | 在 `KeyframeInfo` 加入 `score` 欄位 | 讓後續分析、debug、UI 能看到為何選中某張 frame | `reel_scout/vision/keyframe.py`、未來 DB schema |
| P2 | 為 `scene` / `hybrid` 加 first-frame guarantee，必要時也可加 near-last guarantee | 短影音 opening hook 通常很重要，現在兩邊都沒有保證 | `reel_scout/vision/keyframe.py` |
| P2 | 在 interval/diff 補上簡單 duplicate suppression 或 blur filter | 目前 Reel Scout 也沒有品質過濾，這是可以直接借鏡擴充的缺口 | `reel_scout/vision/keyframe.py` |

## 3. VLM Prompt 分析

### 3.1 video-analyzer 的做法

它不是單一 prompt，而是兩段 prompt 鏈：

1. `frame_analysis.txt` 做逐張 frame 描述。
2. `describe.txt` 做整支影片重建。

`PromptLoader` 會從 package resources 或使用者目錄載入 prompt 檔，見 `video_analyzer/prompt.py:8`-`video_analyzer/prompt.py:63`。CLI 再把 prompts 傳進 `VideoAnalyzer`，見 `video_analyzer/cli.py:104`-`video_analyzer/cli.py:106`、`video_analyzer/analyzer.py:35`-`video_analyzer/analyzer.py:38`。

#### Frame prompt（完整文字）

來源：`video_analyzer/prompts/frame_analysis/frame_analysis.txt`

```text
Frame Description Instructions
Previous Notes Section
[Previous frame descriptions will appear here in chronological order]

{PREVIOUS_FRAMES}

Your Tasks
You are viewing Frame [X] of this video sequence. Your goal is to document what you observe in a way that contributes to a coherent narrative of the entire video.
Step 1: Quick Scan

Watch for key changes from the previous descriptions
Note any new elements or developments
Identify if this is a transition moment or continuation

Step 2: Document Your Frame
Follow this structure for your notes:

Setting/Scene (if changed from previous)

Only describe if there's a notable change from previous frames
Include any new environmental details


Action/Movement

What is happening in this specific moment?
Focus on motion and changes
Note the direction of movement
Describe gestures or expressions


New Information

Document any new objects, people, or text that appears
Note any changes in audio described in previous frames
Record any new dialogue or text shown


Continuity Points

Connect your observations to previous notes
Highlight how this frame advances the narrative
Note if something mentioned in previous frames is no longer visible



Writing Guidelines

Use present tense
Be specific and concise
Avoid interpretation - stick to what you can see
Use clear transitional phrases to connect to previous descriptions
Include timestamp if available

{prompt}

Format Your Notes As:

```
Frame [X] If there are no existing frames you are Frame 0, otherwise you're the next frame
[Your observations following the structure above]
Key continuation points:
- [List 2-3 elements that the next viewer should particularly watch for]

```
```

這個 prompt 的關鍵特性：

| 特性 | 說明 | 實作位置 |
|------|------|------|
| 有 frame context | 明確插入 `This is frame {number} captured at {timestamp}` | `video_analyzer/analyzer.py:59`-`video_analyzer/analyzer.py:61` |
| 有前文記憶 | 把前面 frame 的 `response` 串回 `{PREVIOUS_FRAMES}` | `video_analyzer/analyzer.py:40`-`video_analyzer/analyzer.py:53`、`video_analyzer/analyzer.py:59` |
| 任務導向 | 強調 continuity / narrative advancement，不只是 OCR+物件描述 | prompt 本文 |
| 仍是 free-form | 有輸出格式，但不是 JSON schema | prompt 本文 |

#### Reconstruction prompt（完整文字）

來源：`video_analyzer/prompts/frame_analysis/describe.txt`

```text
Video Summary Instructions
Available Materials
Frame 1 of the video (viewable)

Available Materials
Complete set of chronological frame descriptions from all previous viewers

{FRAME_NOTES}

Video Transcript

{TRANSCRIPT}



Your Task
You are synthesizing multiple frame descriptions into a cohesive video summary. You have access to the first frame and detailed notes about all subsequent frames.
Step 1: Review Process

First Frame Analysis

Study your available frame in detail
Note opening composition, characters, and setting
Identify the initial tone and context


Notes Review

Read through all frame descriptions chronologically
Mark key transitions and major developments
Identify narrative patterns and themes
Note any inconsistencies or gaps in descriptions



Step 2: Synthesis Guidelines
Create your summary following this structure:

Opening Description

Begin with what you can directly verify from your frame
Establish the initial setting, characters, and situation


Narrative Development

Build the story chronologically
Connect scenes and transitions naturally
Maintain consistent character descriptions
Track significant object movements and changes
Include relevant audio elements mentioned in notes


Technical Elements

Note camera movements described
Include editing transitions
Reference significant visual effects
Mention notable lighting or composition changes



Writing Style Guidelines

Write in present tense
Use clear, active voice
Maintain objective descriptions
Avoid speculation beyond provided notes
Include specific details that build credibility
Connect scenes with smooth transitions
Maintain consistent tone throughout

Quality Check
Before submitting, verify:

The summary flows naturally
No contradictions exist between sections
All major elements from notes are included
The narrative is coherent for someone who hasn't seen the video
Technical terms are used correctly
The opening matches your viewed frame
Transitions between described frames feel natural

{prompt}

Format Your Summary As:

```
IDEO SUMMARY
Duration: [if provided in notes]

[Opening paragraph - based on your viewed frame]

[Main body - chronological progression from notes]

[Closing observations - final state/resolution]

Note: This summary is based on direct observation of the first frame combined with detailed notes from subsequent frames.
```
```

值得注意的問題是：prompt 說「first frame is viewable」，但 `reconstruct_video()` 呼叫 `client.generate()` 時沒有傳 `image_path`，只傳入文字 prompt，所以這是一個 prompt/implementation mismatch，見 `video_analyzer/analyzer.py:97`-`video_analyzer/analyzer.py:121`。

### 3.2 與 Reel Scout 對比

| 面向 | video-analyzer | Reel Scout |
|------|------|------|
| Frame prompt | 以 narrative continuity 為中心，有前文記憶與 frame index/timestamp | 單張 frame 描述 prompt，focus 在物件/OCR/情緒/風格，見 `reel_scout/vision/omlx.py:9`-`reel_scout/vision/omlx.py:16`、`reel_scout/vision/ollama.py:9`-`reel_scout/vision/ollama.py:16` |
| Prompt 管理 | 外部 prompt 檔，可 tune/覆寫 | prompt 硬編碼在 Python module |
| 結構化輸出 | frame 層 free-form、video 層 free-form | final merge 要求 JSON schema，見 `reel_scout/analyze/merger.py:10`-`reel_scout/analyze/merger.py:51` |
| Context 注入 | 前面 frame notes + 當前 frame index/timestamp + transcript | merge 時有 transcript + timestamped frame descriptions，但 frame VLM 階段沒有 context |
| Prompt 重用 | 同一套 prompt 可獨立調校 | `vision/omlx.py` 和 `vision/ollama.py` 各自重複 prompt |

Reel Scout 的優勢是最終輸出已經結構化，方便 DB / downstream tooling；video-analyzer 的優勢是更重視「敘事連續性」，讓單張 frame 不是孤立描述。

### 3.3 建議改進

| 優先級 | 改進 | 原因 | 目標檔案 |
|------|------|------|------|
| P1 | 把共用 VLM prompt 抽到單一 prompt 模組或文字檔，避免 `vision/omlx.py` 與 `vision/ollama.py` 重複 | 之後要做 prompt tuning 或 A/B test 會容易很多 | `reel_scout/vision/omlx.py`、`reel_scout/vision/ollama.py`，或新增 `reel_scout/vision/prompt.py` |
| P1 | 在 frame-level prompt 注入 `frame_index`、`timestamp`、`video_duration`，至少讓模型知道這張 frame 在片中的位置 | video-analyzer 的時間上下文是它最大的 prompt 優勢之一 | `reel_scout/vision/omlx.py`、`reel_scout/vision/ollama.py`、`reel_scout/vision/base.py` |
| P2 | 讓後續 frame 可選擇帶入前 1-2 張 frame summary，建立 lightweight continuity memory | 對 tutorial、story、montage 類內容會更穩 | `reel_scout/analyze/pipeline.py`、`reel_scout/vision/*` |
| P2 | 保持最終 merge JSON schema，但可在 frame-level 先要求半結構化小欄位，例如 `scene/action/visible_text/continuity` | 兼顧可解析性與 narrative signal | `reel_scout/vision/omlx.py`、`reel_scout/vision/ollama.py`、`reel_scout/analyze/merger.py` |

## 4. Output Schema 分析

### 4.1 video-analyzer 的做法

最終輸出寫到 `analysis.json`，結構由 `video_analyzer/cli.py:176`-`video_analyzer/cli.py:195` 組成：

```json
{
  "metadata": {
    "client": "...",
    "model": "...",
    "whisper_model": "...",
    "frames_per_minute": 60,
    "duration_processed": null,
    "frames_extracted": 5,
    "frames_processed": 5,
    "start_stage": 1,
    "audio_language": "en",
    "transcription_successful": true
  },
  "transcript": {
    "text": "...",
    "segments": [...]
  },
  "frame_analyses": [
    {
      "model": "llama3.2-vision",
      "created_at": "...",
      "response": "...",
      "done": true,
      "done_reason": "stop",
      "total_duration": 7952576674,
      "load_duration": 2623794964,
      "prompt_eval_count": 349,
      "prompt_eval_duration": 1787000000,
      "eval_count": 207,
      "eval_duration": 3317000000
    }
  ],
  "video_description": {
    "model": "llama3.2-vision",
    "created_at": "...",
    "response": "...",
    "done": true,
    "done_reason": "stop",
    "total_duration": 3877694027,
    "load_duration": 10604604,
    "prompt_eval_count": 1705,
    "prompt_eval_duration": 558000000,
    "eval_count": 297,
    "eval_duration": 3308000000
  }
}
```

這個 schema 的幾個特點：

| 特性 | 說明 | 證據 |
|------|------|------|
| 分層清楚 | metadata / transcript / frame_analyses / video_description 分離 | `video_analyzer/cli.py:176`-`video_analyzer/cli.py:195` |
| 保留原始模型輸出 | `response` 基本是 free-form text，不二次正規化 | `docs/sample_analysis.json` |
| 保留執行統計 | 包含 `done_reason`、duration、token eval count 等 client metadata | `docs/sample_analysis.json` |
| transcript 很完整 | 包含文字、segments、word timestamps/probabilities | `docs/sample_analysis.json` |
| 沒有結構化 video schema | `video_description.response` 仍是自然語言段落 | `docs/sample_analysis.json` |
| 沒有 confidence score | 沒有 schema-level confidence，只保留模型執行數據 | `docs/sample_analysis.json` |

### 4.2 與 Reel Scout 對比

| 面向 | video-analyzer | Reel Scout |
|------|------|------|
| 最終形態 | 單一 JSON 檔 | DB 正規化表 + merged JSON |
| frame 層 | 保留每張 frame 的原始回應與推理統計 | 目前只存 `description/objects_json/text_in_frame`，較扁平 |
| video 層 | free-form summary | 結構化 JSON：`summary/topics/hook/style/engagement_signals/content_type`，見 `reel_scout/analyze/merger.py:24`-`reel_scout/analyze/merger.py:49` |
| transcript 層 | 直接嵌入 segments / words | Reel Scout 有 transcript + segments_json，但尚未統一進 final analysis export |
| temporal narrative | 很強，因為 frame notes 是按時間推進 | Reel Scout 只在 merge prompt 中拼入 timestamped descriptions，沒有專門 narrative 欄位 |
| observability | 高，保留 per-call model metadata | 目前較低 |

Reel Scout 的結構化程度明顯較好；video-analyzer 的可審計性與逐層輸出較好。

### 4.3 建議改進

| 優先級 | 改進 | 原因 | 目標檔案 |
|------|------|------|------|
| P1 | 在 final analysis 補 `narrative_beats` 或 `timeline` 欄位 | 讓結構化 JSON 也保有 temporal progression，而不只是摘要分類 | `reel_scout/analyze/merger.py` |
| P1 | 在匯出層提供「full analysis package」：video metadata + transcript + keyframes + frame descriptions + merged analysis | 借鏡 video-analyzer 的單檔可審計性 | `reel_scout/export/json_export.py` |
| P2 | 保存每張 frame 的來源資訊與模型回應 metadata（model/backend/latency） | 方便 debug 與品質比較 | `reel_scout/db.py`、`reel_scout/vision/base.py`、`reel_scout/vision/*` |
| P2 | 明確把 transcript segments / words 納入最終 export schema | video-analyzer 這點比目前 Reel Scout 更完整 | `reel_scout/export/json_export.py` |

## 5. 其他值得借鏡的設計

| 設計 | 來源 | 可借鏡處 |
|------|------|------|
| Prompt 外部化 + package resource fallback | `video_analyzer/prompt.py:11`-`video_analyzer/prompt.py:41` | Reel Scout 未來若要做 prompt tuning，最好不要把 prompt 長字串硬編碼在 backend module |
| CLI stage restart (`--start-stage`) | `video_analyzer/cli.py:74`、`video_analyzer/cli.py:114`、`video_analyzer/cli.py:153`、`video_analyzer/cli.py:168` | 對開發/除錯很有幫助，可考慮給 Reel Scout CLI 或 MCP debug flow |
| Config cascade | `video_analyzer/config.py:31`-`video_analyzer/config.py:90` | Reel Scout 現在偏 env-only，未來若需要可重現實驗，可考慮引入 task-level config file |
| Prompt tuning 子專案 | `readme.md` Prompt Tuning 段落、`video-analyzer-tune/` | 代表 prompt 被視為一級資產，這對 Reel Scout 的 vision/merge prompt 都有啟發 |

## 6. 改進優先級

| # | 改進 | 影響 | 複雜度 | 目標檔案 |
|---|------|------|------|------|
| 1 | 抽出共用 VLM prompt 並加入 frame index/timestamp/video duration context | 高 | 低 | `reel_scout/vision/omlx.py`, `reel_scout/vision/ollama.py` |
| 2 | 在 merge schema 新增 `timeline` / `narrative_beats` | 高 | 中 | `reel_scout/analyze/merger.py` |
| 3 | 在 keyframe pipeline 引入 score-aware diff ranking 或 scene+score hybrid | 高 | 中 | `reel_scout/vision/keyframe.py` |
| 4 | 為 keyframe 選取加入 first-frame guarantee | 中 | 低 | `reel_scout/vision/keyframe.py` |
| 5 | 匯出 full analysis package，包含 transcript、frame descriptions、merged JSON | 中 | 中 | `reel_scout/export/json_export.py` |
| 6 | 保存每張 frame 的分析 metadata（backend/model/latency/score） | 中 | 中 | `reel_scout/db.py`, `reel_scout/vision/*` |

## 7. 總結

`video-analyzer` 最值得借鏡的不是它的最終 schema，而是它把「逐 frame 觀察」當成一條連續敘事鏈來處理：frame 之間有前文、重建時有 chronology、輸出時保留中介產物。Reel Scout 已經在結構化輸出上更成熟；下一步最有價值的方向，是把 `video-analyzer` 的 temporal/context 思維移植到 Reel Scout 的 frame prompt 與 merge schema，而不是照抄它的 free-form output。
