## Self-Review Checklist

- [x] 已 clone `video-analyzer` 並完整掃描
  - Cloned to `C:\Users\user\.codex\memories\video-analyzer-research-src` and inspected repo structure, entrypoint, config, frame extraction, prompts, clients, sample output.
- [x] Frame sampling 分析含具體程式碼引用（檔案路徑+行數）
  - Report cites `video_analyzer/frame.py:17`, `video_analyzer/frame.py:50`, `video_analyzer/frame.py:95`, plus Reel Scout comparison at `reel_scout/vision/keyframe.py:19`.
- [x] VLM prompt 分析含完整 prompt 文字
  - Report embeds full text from `video_analyzer/prompts/frame_analysis/frame_analysis.txt` and `video_analyzer/prompts/frame_analysis/describe.txt`.
- [x] Output schema 分析含完整 JSON 結構
  - Report includes normalized JSON skeleton derived from `video_analyzer/cli.py:176` and `docs/sample_analysis.json`.
- [x] 每個分析都有與 Reel Scout 的對比
  - Frame sampling, prompt design, and output schema sections each include comparison tables.
- [x] 每個分析都有具體可行動的改進建議
  - Each main section ends with prioritized recommendations.
- [x] 改進建議標明要改的 Reel Scout 檔案
  - Recommendations name concrete target files such as `reel_scout/vision/keyframe.py`, `reel_scout/vision/omlx.py`, `reel_scout/analyze/merger.py`, `reel_scout/export/json_export.py`.
- [x] 研究報告結尾有優先級排序表
  - Present in section `## 6. 改進優先級`.
- [x] 報告寫在 `docs/video-analyzer-research.md`
  - File created in repo docs folder.
- [x] 無其他檔案被修改
  - Research content change is limited to `docs/video-analyzer-research.md`; `CODEX_RESULT.md` is updated only because your standard delivery format explicitly requires it.

## Pytest Output

```text
N/A — read-only research task. The handover explicitly states "不適用（唯讀任務，無 code 變更）" and "無需 pytest（無 code 變更）".
```

## Git Diff Stat

```text
 CODEX_RESULT.md                 |  94 +++-------
 docs/video-analyzer-research.md | 393 ++++++++++++++++++++++++++++++++++++++++
 2 files changed, 417 insertions(+), 70 deletions(-)
```

## REVIEW Flags

- `REVIEW`: `video-analyzer` prompts claim the reconstruction step can directly view the first frame, but `video_analyzer/analyzer.py:113`-`video_analyzer/analyzer.py:119` sends text only, not an image. I treated this as an implementation mismatch and called it out in the report rather than inferring hidden behavior.
- `REVIEW`: The report quotes the full prompt text because the handover explicitly requires complete prompt analysis. If you want a shorter audit artifact later, this section is the first place I would compress.
