# Reel Scout — Command & Integration Reference

Concise reference for the CLI, MCP tools, LLM/VLM backends, and config flags.
For the design rationale see [`roadmap.md`](./roadmap.md); for the signal model
see [`../prompts/signal-reliability-cheatsheet.md`](../prompts/signal-reliability-cheatsheet.md).

## CLI

| Command | What it does |
|---|---|
| `browse <profile_url>` | List a channel/profile's videos (metadata only). `--json` / `--urls-only`. |
| `crawl <url…>` | Download videos. `--channel <url> --limit N`, `--playlist <url>`, `--file -` (stdin). |
| `analyze <url\|path…>` | Full pipeline: download/register → transcribe → keyframes+VLM → shot/audio metrics (§4E) → on-screen text (§4F) → merge → optional score. Accepts a local file path (platform-outage insurance). `--score`. |
| `transcribe <path>` / `vision <path>` | Individual stages against a local file. |
| `list` / `show <id>` | List analyzed videos / show one analysis. |
| `export --format json\|csv\|html\|bundle\|skeleton\|storyboard` | Export analyses. `html` = one self-contained file; **`bundle`** = one self-contained file *per reel* + an index (the course take-home). |
| `view` | Local web server for the library: a table with star / group / note per row. The analysis stays read-only; only your own annotations write back. |
| `note <ref>` | Annotate a clip. `--text`, `--group <name>` (`--new-group`), `--no-group`, `--star` / `--unstar`, `--json`. |
| `group {list,add,rename,rm}` | Manage the annotation groups (also doable from the list page toolbar). `rm` clears the filing and keeps every note and star. |
| `inspect <id>` | Interactive single-clip viewer (transcript↔keyframe time-sync). |
| `score <id>` | Craft score (hook / visual / **pacing (evidence-based, §4E)** / structure). |
| `ingest {vision,analysis,score} <id> --from-json <path\|->` | Write **agent-produced** frame descriptions / structured analysis / craft scores back into the DB, for machines with no local model. `--model` required; stored as `agent:<model>`. Analysis enums are validated. |
| `compare <id…>` | Side-by-side comparison table. |
| `stats [--channel] [--csv]` | Corpus tag distributions + score aggregates. |
| `patterns --channel <c>` | Per-channel patterns: length, hook/CTA/structure mix, high-vs-low contrast, cadence. |
| `inspire --based-on <ref> [--angle]` | Generate a fresh variant (titles/hook/structure/length) from a high scorer. |
| `track --my-video <ref> --views --likes [--comments --notes]` | Record real performance + structural iteration hints vs the top corpus. |
| `research --niche <n> --channels <url…> [--depth --out --no-analyze]` | Competitor research report. |
| `batch --stdin` / `--file <path>` / `--doc <url>` | Analyze every reel in a list, one bundle each. `--dry-run`, `--limit`, `--out`, `--mode`. Prefer `--stdin`/`--file` for someone's own list — it stays on their machine. `--doc` rewrites Google `/edit` links to no-auth export endpoints, which needs sharing set to "anyone with the link", i.e. it makes that document public to anyone holding the URL. |
| `skill {install,path}` | Copy the agent-facing assets (SKILL.md, `/scout`, prompt pack, setup preflight) to `~/.claude/skills/reel-scout`. `--dest`, `--force`. `pip install` alone does **not** give an agent anything to load. |
| `mcp install` / `mcp path` | Register the MCP server with Claude Desktop / Claude Code so they can launch it, pinning an absolute `REEL_SCOUT_DATA`. `--client`, `--data`, `--path`, `--force`, `--dry-run`. `mcp path` diagnoses what is configured versus what this directory resolves to. |
| `db {stats,reset,migrate,normalize-paths,backfill-text,health,backfill-shots,check-invalid}` / `config {show,check}` | DB / config utilities. `check-invalid` lists videos whose media never really processed (0 keyframes alongside a non-empty transcript) and would otherwise average into `stats` as a genuine 0.0; it reports by default and marks `status=invalid` only with `--apply`. It never deletes. |

Refs (`<ref>`, `<id>`) accept a full 16-hex video id or a unique prefix; `track --my-video` also accepts a URL already in the DB.

## MCP tools (`reel-scout-mcp`, stdio)

19 tools: `analyze`, `annotate`, `batch_cancel`, `batch_start`, `batch_status`,
`crawl`, `export`, `ingest_analysis`, `ingest_score`, `ingest_vision`, `inspect`,
`inspire`, `keyframes`, `list_annotations`, `list_videos`, `mark`, `patterns`,
`research`, `show_video`. LLM/network tools (`analyze`, `inspire`, `research`) route
stdout to stderr so they don't corrupt the JSON-RPC stream. `research` reads the
existing DB by default; pass `analyze: true` to crawl+analyze first (slow).

## Backends

- **VLM** (`VLM_BACKEND`): `omlx` (default) / `ollama`. Model via `VLM_MODEL`
  (default `qwen2.5vl:7b`), fallback `VLM_FALLBACK_MODEL`.
- **LLM** (merger/scorer/inspire/research, `LLM_BACKEND`): `omlx` / `ollama` /
  `openclaw` (proxy to a subscription, no local GPU).
- **Whisper** (`WHISPER_BACKEND`): `faster-whisper` (default) / whisper.cpp;
  subtitle-first when a native subtitle is present. `WHISPER_MULTILINGUAL=1` for
  code-switching interviews.

All inference defaults to localhost — no cloud API keys required.

### `batch` picks nothing for you

`batch` probes the configured backends before it runs. A reachable VLM means
`--mode full` is unambiguous and it just goes. Without one it **stops and makes
you choose**, because the alternative — quietly producing transcript-only
bundles — drops the craft score without anyone noticing:

    full        local VLM does the visual layer and the score
    agent       skip the VLM; an agent reads the keyframes and writes its own
                findings back via `ingest` (see below and SKILL.md Step 2b)
    transcript  transcript and structure only, stated plainly

In `agent` mode the run ends by listing the videos still missing a visual layer,
with the exact `ingest` command for each.

### No local model at all

Keyframe extraction is ffmpeg, not a model, so the frames exist on disk before the
VLM stage runs. An agent that can see images can supply the visual layer and the
craft score itself, and `ingest` writes that back so it shows up in `show` / `view`
/ `inspect` / `export` like any other analysis:

```bash
reel-scout analyze "<url>" --skip-vision        # download + transcript + structure
# agent reads data/keyframes/<id>/*.jpg, then:
reel-scout ingest vision   <id> --from-json - --model <name>
reel-scout ingest analysis <id> --from-json - --model <name>   # merge needs an LLM too
reel-scout ingest score    <id> --from-json - --model <name>
```

This is the only path that needs no `oMLX`/`ollama`/API key. The thing to know:

- Rows are stamped `agent:<model>`, because craft scores are **model-dependent**
  (the same clip: 7.43 under `qwen3-vl:8b`, 5.5 under `qwen2.5vl:7b`).
- `stats` therefore groups score aggregates **by `model_used`**. A corpus mixing
  agent-scored and locally-scored videos gets one block per source, and the
  pooled block is labelled as pooled (`mixed_score_sources: true` in `--json`,
  `flag,score_source,mixed,1` in `--csv`) so it is not read as a single scale.
  Per-video reads are unaffected either way.

Transcription still wants a local Whisper; set `WHISPER_MODEL=small` to keep the
download modest, or rely on subtitle-first when the source has native subtitles.

## Notable config flags

| Flag | Default | Effect |
|---|---|---|
| `REEL_SCOUT_DATA` | `./data` | Data dir (DB, videos, keyframes). |
| `KEYFRAME_STRATEGY` / `KEYFRAME_MAX` / `KEYFRAME_RESOLUTION` | `scene` / `8` / `0` | Keyframe extraction. |
| `SHOT_METRICS_ENABLED` / `SHOT_SCENE_THRESHOLD` | `true` / `0.3` | §4E measured pacing (cuts/min + energy/BPM). |
| `OCR_ENABLED` / `OCR_ENGINE` | `true` / `vlm` | §4F on-screen text; `tesseract` opt-in (needs the `ocr` extra). |
| `IG_COOKIES_FILE` | — | Instagram cookies for gated content. |
| `LLM_BACKEND` / `VLM_BACKEND` / `WHISPER_BACKEND` | `omlx`/`omlx`/`faster-whisper` | Inference backends. |

`reel-scout config show` prints the resolved config; `config check` probes backend reachability.

## Optional extras (`pip install reel-scout[<extra>]`)

`whisper`, `audio` (onnxruntime+numpy — PANNs events + BPM), `diarize`,
`instagram` (instaloader browse fallback), `ocr` (pytesseract+Pillow), `vector`.

## Take-home bundle

```bash
reel-scout export --format bundle -o ./course-reels \
  --cjk-font /path/to/NotoSansTC.ttf   # optional: embeds Chinese glyphs
```

Each reel becomes ONE html file with the video, keyframes, waveform peaks, fonts
and a per-file CJK subset inlined — no server, no sibling files, nothing to lose
when it is moved or emailed. Sized for short-form: reels land around 2–25 MB, and
anything over `--max-mb` (default 25) is skipped with a reason rather than
producing a file nobody can open. Without `--cjk-font`, Chinese falls back to the
reader's system face; Latin always uses the bundled brand faces.
