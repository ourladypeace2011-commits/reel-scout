# Reel Scout

> English ｜ [繁體中文](README.zh.md)

Short-form video analysis CLI tool.

Crawl, transcribe, and visually analyze YouTube Shorts, Instagram Reels, and TikTok videos into structured data.

![The reel-scout inspector — craft scores, a live re-weight panel, and a bilingual interface. Chinese labels sit beside the model's own English output, which is left exactly as produced.](docs/assets/inspector.jpg)

## How it works

```mermaid
flowchart LR
  U([URL]) --> C[crawl<br/>yt-dlp]
  C --> T[transcribe<br/>Whisper]
  C --> K[keyframes<br/>ffmpeg]
  K --> V[vision<br/>VLM / agent]
  T --> A[analyze<br/>merge]
  V --> A
  A --> S[score<br/>craft rubric]
  S --> I[inspect / view<br/>interactive]
  S --> E[export<br/>json · csv · bundle]
```

Keyframes are ffmpeg, not a model, so the frames exist before any model runs — which
is why an **agent** can stand in for the VLM/score stage when no local model is
present (the **L1** tier). Craft scores are a *reference*, not a verdict.

## Install

```bash
pip install reel-scout
reel-scout skill install       # only if you drive it from Claude — see below
```

`ffmpeg` must be on PATH (macOS: `brew install ffmpeg`). `yt-dlp` comes with the
package. Extras: `whisper` (faster-whisper transcription), `audio` (audio events
+ BPM), `ocr`, `diarize`, `instagram` — e.g. `pip install "reel-scout[whisper]"`.

### Using it from Claude

`pip install` gives you the CLI. The **skill** is the half an agent reads:
`SKILL.md` (the pipeline procedure and the capability/surface matrix), the `/scout` slash
command, and the reverse-decode `prompts/`. `reel-scout skill install` copies
them to `~/.claude/skills/reel-scout` (`--dest` to put them elsewhere, `--force`
to overwrite). Restart Claude Code, then:

```
/scout https://www.instagram.com/reel/XXXXXXXX/
```

`reel-scout skill path` shows where the assets are being read from.

**No local model?** You do not need one. Keyframe extraction is ffmpeg, so the
frames exist before any model runs — the skill's **L1** tier has the agent
describe them and apply the craft rubric itself, then writes that back with
`reel-scout ingest`. No API key, no cloud, no GPU. See `SKILL.md`.

From a clone, for development:

```bash
pip install -e ".[dev]"
```

## Usage

```bash
reel-scout crawl "https://youtube.com/shorts/xxxxx"
reel-scout analyze "https://youtube.com/shorts/xxxxx"
reel-scout analyze --file urls.txt --skip-vision
reel-scout list
reel-scout show <video_id>
reel-scout inspect <video_id>          # interactive single-clip inspector web app
reel-scout export --format json -o ./export
reel-scout config check
```

`inspect` starts a small local web app for one clip and opens it in the browser.
The **video player is the single source of truth**: a **waveform** (ffmpeg peaks,
cached) with a click-to-seek playhead, a **keyframe filmstrip**, and the
**transcript** all seek the player and highlight as it plays. Set **IN/OUT**
markers on the waveform and export the trimmed window as SRT. Needs the downloaded
video file on disk. (Design ported from arkiv's live inspector.) `export --format
html` remains the offline multi-clip bundle; `view` is the library browsing
server.

**Craft scores are a reference, not a verdict — and the page shows why.** The four
dimensions come from a model, and the same clip scores differently across models,
so the number is never the point. A collapsed **re-weight** panel lets you drag the
weighting of the four dimensions and watch `overall` recompute live, your blend
beside the stored default. The dimensions themselves never move — only how they are
combined — so you can see exactly how much the verdict depends on what *you* value.

**Interface language toggle (EN / 中文).** The inspector and the `view` server
carry both English and Traditional Chinese in the page: an instant client-side
switch that follows your browser's language on first load and remembers your
choice. Only interface labels translate — the model's own output (transcript,
descriptions, decoded values) is left exactly as produced. (This is the *interface*
language; for bilingual *audio* transcription, see the section below.)

## Instagram links

**Paste the link however Instagram gave it to you.** Both forms work — the
canonical one and the account-scoped one the share button produces:

```
https://www.instagram.com/reel/Da3UcDsudRN/                    # canonical
https://www.instagram.com/zacharywinterton/reel/Da3UcDsudRN/   # share button
```

**Most public reels need no login.** In a 24-clip batch run, 20 of 21 Instagram
reels downloaded fine with no cookies at all; **one** hit the login wall and
failed in about three seconds with `Instagram sent an empty media response`.

So treat cookies as a **retry step, not a prerequisite** — run the batch first,
then re-run the handful that failed:

```bash
# 1. export cookies for the reels that failed (Chrome or Brave; Safari's
#    binarycookies file is not readable)
yt-dlp --cookies-from-browser chrome --cookies /tmp/ig_cookies.txt \
       --skip-download "https://www.instagram.com/reel/<code>/"

# 2. retry just those
IG_COOKIES_FILE=/tmp/ig_cookies.txt reel-scout analyze "<url>" --score
```

> **The cookie file contains a live Instagram session.** Keep it out of the repo
> and out of any synced folder, and delete it when the retry is done.

**Timing.** An Instagram reel costs roughly the same regardless of length —
there is no subtitle track to read, so every clip is transcribed locally. Budget
around **3 minutes per reel** whether it runs 10 seconds or 70. (YouTube is the
opposite: a video that ships subtitles skips transcription entirely and finishes
far faster than its runtime suggests.)

## Bilingual / code-switching audio (中英對照)

Whisper `large-v3` locks onto the language it detects in the opening window and, on
long files, "translates" later speech of the *other* language back into the locked
one — a code-switching interview (Chinese host + English guest) comes out with the
guest's English mangled into garbled Chinese. It is a long-form drift, not a bad
audio issue: the same passage transcribes perfectly when sliced out on its own.

Fix — force per-chunk language re-detection:

```bash
WHISPER_MULTILINGUAL=1 WHISPER_CHUNK_LENGTH=15 reel-scout analyze "<url>"
```

`multilingual` alone is not enough — it needs a short `chunk_length` (~15s) so each
chunk re-detects. Verified on a 40-min ZH-host/EN-guest interview: latin-char
recovery 56% → 90%. Leave OFF for single-language short-form (per-chunk detection
adds cost). Other levers: `WHISPER_LANGUAGE=en` (force one language),
`WHISPER_TASK=translate` (force English output).

## MCP Server

An agent can drive the whole pipeline over MCP — no shell needed.

```bash
reel-scout mcp install    # register the server in the client's config (no hand-editing JSON)
reel-scout mcp path       # show where it's registered
reel-scout-mcp            # or run it directly (stdio transport)
```

Tools cover both sides: **read** — `list_videos`, `show_video`, `patterns`,
`inspire`, `research`, and a `keyframes` tool so an agent with no filesystem can
still see the extracted frames; **write** — `ingest_vision` / `ingest_score` /
`ingest_analysis`, `mark` and `annotate`, a background `batch_start` /
`batch_status` / `batch_cancel`, and `inspect`. This is how the **L1** tier works: an agent that can see images supplies
the visual layer and craft score itself, so results land in `show` / `view` /
`inspect` / `export` instead of a chat log.

## Prompt Pack (analysis layer)

Reel Scout's pipeline gets clean input into a model. The **reverse-decode prompt
pack** in [`prompts/`](./prompts/) is the analysis brain you point at that input —
to reverse-engineer *why* a short-form video works and extract a transferable
structure, with anti-hallucination guardrails (observation vs. inference, cite the
timestamp). Open (MIT). See [`prompts/README.md`](./prompts/README.md).

## Requirements

- Python 3.9+
- ffmpeg
- yt-dlp

## Attribution

Video extraction techniques (captions-first transcription, duration-aware frame
budgeting, time-range focus, on-screen-text resolution bump) are adapted from
[claude-video](https://github.com/bradautomates/claude-video) (MIT). See [`NOTICE`](./NOTICE).
