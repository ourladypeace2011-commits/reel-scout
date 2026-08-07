# Changelog

## Unreleased

### Changed
- **`show_video` no longer hands an agent a whole transcript timeline it did not
  ask for.** Measured across the 101-video library with the same serializer both
  sides, one call used to return up to **288,359 tokens** — a four-hour clip whose
  5,808 timed segments dwarfed everything else in the payload. Three things
  changed, and each one keeps what it trims reachable rather than dropping it:
  - **Timed `segments` are opt-in.** They are gone from the default response;
    `has_segments` and `segment_count` take their place, so an agent still knows
    they exist and can pass `include_segments: true` when it actually needs to cut
    on a timecode. The flat `text_full` still comes free. When segments *are*
    returned, `confidence` is rounded to 3 dp — whisper emits the full float repr
    (`-0.1858760386370541`), which is roughly twenty characters of noise per
    segment for a number nobody reads past the second decimal.
  - **`analysis.full` is de-duplicated against its own projections.** It used to
    ship whole while `summary` / `topics` / `hooks` / `style` /
    `engagement_signals` sat beside it as separate keys — the same content twice.
    Only those five keys are stripped: `timeline`, `content_type`,
    `content_structure` and `measured` live nowhere else and still ship. (`hook`
    was verified byte-equal to `hooks_json` on all 99 analysed rows before it went
    on the strip list; it is not there because the name looked similar.)
  - **`keyframes` is capped at 12 by default**, with `keyframes_total` and
    `keyframes_truncated` reporting what happened and `max_keyframes: 0` lifting
    the cap entirely.

  Result on the same library: total **1,098,903 → 428,460** tokens, p90
  **8,467 → 4,459**, worst case **288,359 → 50,596**.

  ⚠️ **This changes a default over MCP.** Anything that read
  `show_video(...)["transcript"]["segments"]` without passing `include_segments`
  will now find the key absent — check `has_segments` and ask for them.

### Added
- **Long clips get a sampling rate, not the same twelve frames a reel gets.** The
  keyframe cap was one flat number, so a 9-second reel and an 82-minute interview
  drew the same budget — the reel sampled about once a second, the interview once
  every seven minutes. Downstream that produced a `timeline` whose single segment
  covered 96% of the clip: technically produced, practically useless. The cap now
  stays flat up to `KEYFRAME_LONG_SEC` (180s — **short-form behaviour is unchanged,
  bit for bit**) and above it earns `KEYFRAME_PER_MIN` frames a minute up to
  `KEYFRAME_MAX_LONG` (40). The ceiling is the point: 82 minutes asks for 164
  frames at 2/min and is told 40. Each frame is still one local VLM call, so the
  cost red line moved deliberately, not accidentally.
- **Delete a group from the page.** A picker plus a button in the toolbar, rather
  than an X inside each row's dropdown — a group is a library-wide object, and its
  destructor does not belong inside a per-video control. Each option carries its
  row count as the warning, and deleting still keeps every note and star.

- **Your own layer on the library: a note, a group, a star.** The served list
  (`reel-scout view`) is now a table, because it carries per-row controls a list
  of links had nowhere to put: a **star** to mark what is worth coming back to,
  a **group** dropdown you define yourself (add / rename / delete), and a free-text
  **note** for what a clip is *for*. The star in the table header is the filter —
  press it to show only what you marked. Notes save as you type; the group and the
  star save on the spot.

  The pipeline decodes what a video *is*; none of it knows what you intend to do
  with it, and that intent is the part worth typing by hand.

  Same three fields from the CLI (`reel-scout note <ref> --text … --group … --star`,
  `reel-scout group list|add|rename|rm`) and over MCP (`annotate`,
  `list_annotations`), all going through one operations module so the rules —
  group names unique case-insensitively, notes rejected rather than truncated —
  are enforced once instead of three times, slightly differently.

### Notes
- **Annotations live in their own tables** (`video_annotations`,
  `annotation_groups`; schema v11), never as columns on `videos`. Everything else
  in the database is derived from the source clip and is safe to regenerate;
  these rows are the operator's judgement. Re-crawling, re-analyzing and
  re-scoring rewrite the pipeline's output and cannot touch a note — there is a
  test that holds that line. Deleting a group clears the filing and keeps every
  note and star.
- **The take-home export ships no annotations.** They are working state, not
  something a reader should receive; the exported single-file HTML has no server
  to write to and stays entirely read-only, and its strapline still says so. The
  served list gets its own strapline, because claiming "read-only" on a page that
  writes would be a lie.
- **The HTTP write surface is narrow on purpose**: `POST` is accepted only on the
  annotation endpoints, bodies over 64 KB are refused before being read, and
  everything the pipeline produced remains read-only over HTTP.

## 1.3.0 — 2026-07-21

### Added
- **Interface speaks Traditional Chinese now — a toggle, not a rebuild.** The
  inspector *and* the read-only viewer (library list + take-home bundle) carry
  both `en` and `zh-Hant` dictionaries in the page, so `EN / 中文` is an instant
  client swap with nothing to fetch — a bundle stays bilingual offline. It follows
  the browser's language on first load and remembers the choice. The line held
  everywhere: only interface chrome carries a `data-i18n` key; the model's own
  output — reasoning, transcript, decoded-structure *values* like `educational`,
  OCR text — is never touched, because translating it would mean silently
  re-running the model. One `reel_scout/i18n.py` is the single source both pages
  read, so they cannot drift.
- **Re-weight the craft score without re-running anything.** The inspector's score
  block hides a collapsed panel of weight sliders: drag them and `overall`
  recomputes live against the four stored dimensions, showing your result beside
  the stored default. The dimensions themselves never move — they come from the
  model, and the rubric behind them is prose, not a tunable threshold — so the
  honest thing to expose is the *blend*, and the panel says so. Weights renormalize
  to sum to 100 % so the number can't leave the 0–10 axis. The weights used to live
  in three hand-kept copies; they now collapse to one `config.SCORE_WEIGHTS` that
  both the scorer prompt and the recompute read.
- **MCP server — an agent can drive reel-scout without a shell.** The tools cover
  the read side (`list_videos`, `show_video`, `get_transcript`, and a `keyframes`
  tool so an agent with no filesystem can still see the frames) and the write side
  (`ingest` vision/score/analysis, a background `batch`, and `inspect`). `reel-scout
  mcp install` / `mcp path` register the server in the client's JSON without
  hand-editing it.
- **`ingest analysis` — the third thing only a model can give you.** `merge_analysis`
  needs a reachable LLM; without one it fails with a connection error and the
  `analyses` row is never written, so the 4-beat timeline, hook type and CTA type —
  most of the point — are silently absent. An agent can now supply that structure in
  the merge prompt's own shape. The low-cardinality fields are validated as enums,
  because they become columns `stats` and `patterns` group on and an invented value
  adds a one-member category to every aggregate. Provenance rides in `full_json`
  as `_source`.
- **The exported page now shows what was seen in each frame.** The filmstrip carries
  each keyframe's description (hover, plus a caption that tracks playback). A bundle
  that showed thumbnails and a score with no observations behind it asked the reader
  to take the number on faith — and at L1 those descriptions *are* the analysis.
- **`batch` — a doc full of links, one bundle each.** Point it at a Google
  Doc/Sheet (or a file, or stdin) and every IG/TikTok/Shorts link in it gets
  analyzed and exported. `/edit` URLs are rewritten to Google's export endpoints,
  so "anyone with the link" sharing is enough — no API key, no OAuth, no publish
  step. `--dry-run` shows what was parsed before anything runs. It **picks nothing
  for you**: a reachable VLM makes `--mode full` unambiguous, but without one it
  stops and presents `agent` / `transcript` / `full` rather than quietly shipping
  transcript-only bundles with the craft score missing. In `agent` mode it ends by
  listing the videos still needing a visual layer with the `ingest` command for
  each. Entries are paired to videos by set difference, never by URL equality —
  shared links carry tracking parameters, and giving one person's analysis to
  another is worse than producing one bundle fewer.
- **`skill install` — the skill now ships with the package.** Measured on a clean
  venv: `pip install reel-scout` produced a working CLI and *none* of `SKILL.md`,
  `commands/scout.md`, `prompts/` or `scripts/setup.py`, so an agent had nothing to
  load and `/scout` did not exist. Those assets are vendored into the wheel and
  `reel-scout skill install` lays them down in `~/.claude/skills/reel-scout`
  (`--dest`, `--force`; `skill path` shows the source). A clone still installs from
  the working tree rather than a stale snapshot. Full install is now two commands.
- **`ingest {vision,score}` — an agent can be the backend.** Keyframe extraction is
  ffmpeg, not a model, so the frames are on disk before the VLM stage runs. On a
  machine with no `oMLX`/`ollama`, an agent that can see images now supplies the
  visual layer and the craft score itself and writes them back, so the result lands
  in `show` / `view` / `inspect` / `export` instead of living in a chat log. No API
  key, no cloud, no local model. Rows are stamped `agent:<model>` because craft
  scores are model-dependent (7.43 vs 5.5 on the same clip across two VLMs), and
  `overall` is recomputed with `score`'s own weights rather than trusted from input.
  `SKILL.md` documents this as tier **L1** between web-only (L0) and full local (L2).
- **`show` now lists keyframes and the score.** Frame ids, timestamps and paths,
  with `*` marking frames that have no description yet — the ids are how anything
  outside the process addresses a specific frame.
- **One app instead of two.** The library index and the interactive inspector now
  share a single server and port — a row in the list opens straight into the
  player/waveform view. `view` lands on the library, `inspect <id>` opens one clip.
- **vulture.s shell.** `theme.py` carries the brand tokens (warm paper, warm-black
  ink, three-step rules, mono uppercase chrome) from the brand SSOT. Deviations are
  narrated in-file: a wider tool column, and no cyan (canon caps it at the tv./
  wordmark, which reel-scout doesn't carry).
- **Bundled brand fonts.** Archivo Black / Inter / JetBrains Mono ship with the
  package (78 KB total, OFL). Served as `/font/<file>` live, inlined as base64 in
  exports. CJK is subset per-export from that export's own text.
- **`export --format bundle`** — the take-home: one *self-contained* HTML per reel
  (video, keyframes, waveform peaks, fonts and a CJK subset all inlined) plus an
  index. Move it, rename it, email it — nothing to lose and no server to run.
  Verified: a bundled page issues exactly one network request, for itself.
  Reels over `--max-mb` (default 25) are skipped with a reason.

### Fixed
- `view` served requests single-threaded, so one idle browser keep-alive could
  stall the whole viewer. Now `ThreadingHTTPServer`.
- Windows console printed mojibake when its code page wasn't UTF-8 (the default on
  many machines); output is now forced to UTF-8 regardless of the console codepage.
- Re-weighting showed a contradiction at the zero-weight edge — the panel said
  "no verdict" while the overall meter still displayed the last computed number.
  It now blanks to `—` with an empty bar. (Return-value parity tests could not
  catch it; it was a DOM-state bug, found by driving the real sliders.)

## 1.2.0 — 2026-07-19

> ⚠️ **Craft scores are not comparable across this boundary.** §4E changes how
> `pacing` is scored — it now reasons on measured cut rhythm instead of pure LLM
> judgment, so the same video can score differently than it did on 1.1.0. Re-score
> a video before comparing it with anything scored on an older version.

### Added
- **§4E evidence-based pacing** — `shots.py` measures cut rhythm (cuts/min, shot
  count, avg shot length) via a dedicated full-clip ffmpeg scene pass; `audio/rhythm.py`
  adds RMS energy + best-effort BPM (numpy-gated, no librosa). Stored in the new
  `shot_metrics` table and folded into the analysis so the `pacing` craft score
  rests on measured evidence, not LLM vibes. Config `SHOT_METRICS_ENABLED`.
- **§4F on-screen text (L3.5)** — `ocr.py` collects burned-in captions with
  timestamps: `OCR_ENGINE=vlm` (default) reuses the VLM's `text_in_frame`;
  `tesseract` is an opt-in engine (`ocr` extra, guarded). Stored in `ocr_captions`,
  fed into merge as an L3.5 signal layer; new L3.5 tier in the reliability cheatsheet.
- **`patterns --channel`** — per-channel pattern analysis: length, hook/CTA/structure
  mix, top-vs-bottom-half structural contrast, posting cadence. (3B)
- **`inspire --based-on [--angle]`** — generate a fresh content variant (titles,
  hook script, structure outline, length) from a high-scoring video. (4B)
- **`track --my-video --views --likes`** — record real performance and get
  deterministic structural iteration suggestions vs the top-scored corpus. (4D)
- **IG browse instaloader fallback** when yt-dlp's Instagram extractor breaks. (3A)
- **MCP tools** `patterns`, `inspire`, `research` (5 → 8 tools). (4C)

### Changed
- DB schema v6 → v9 (added `shot_metrics`, `ocr_captions`, `performance` tables).

## 1.1.0 — 2026-07-17

### Added
- **Read-only viewer** for decoded analyses — two surfaces sharing one renderer:
  - **`export --format html`** — a self-contained single-file HTML (keyframes
    base64-embedded, all CSS inline, zero external assets) that opens in any
    browser, works offline, and survives being moved. Built as a take-home
    artifact for people who don't install reel-scout. `--video <id|prefix>`
    exports one video; otherwise all analyzed videos.
  - **`reel-scout view`** — a local read-only HTTP server rendering the library
    live (index → per-video pages, keyframes served by URL). `--host/--port/
    --no-open`.
  Both show each video's decoded structure (hook/beats/CTA), keyframes + what
  the VLM saw, craft scores, and transcript. Deliberately read-only — no action
  surfaces; scores are labelled a reference, not an authority.
- `db.get_keyframes_with_descriptions` (keyframes ⟕ vision_descriptions).

## 1.0.0 — 2026-07-17

First stable release. Completes the Batch Intelligence, Content Strategy (4A),
and Tool Hygiene milestones — the tool is now installable, CI-covered, and
feature-complete for cross-video/-channel analysis.

### Added
- **`stats`** — corpus statistics: tag distributions (content_type,
  content_structure, format, pacing, hook/cta type, emotion) + craft-score
  aggregates (avg/min/max), with `--channel` scoping, `--json`, and `--csv`
  (roadmap 3D).
- **`research --niche --channels --depth`** — cross-channel competitor research:
  lists each channel → analyzes → aggregates per channel and niche-wide → `--out`
  renders an LLM markdown report (common patterns / differentiation / strategy),
  falling back to a deterministic data-only report when no LLM is reachable
  (roadmap 4A). `--json` emits the aggregate; `--no-analyze` reuses the DB.
- **content-structure classification** — hook-body-cta / problem-solution /
  listicle / story-arc / raw-moment, emitted by the merger (roadmap 3C).
- **normalized analysis tags** — content_type / opening_type / cta_type / style
  format+pacing / emotion / content_structure mirrored from full_json into
  indexed columns for filtering and stats; migrations backfill existing rows
  (roadmap 3C, DB schema v4→v6).
- **GitHub Actions CI** — pytest across Python 3.9–3.13 (roadmap 5B).
- **MIT LICENSE** file + full PyPI packaging metadata (urls, classifiers,
  dynamic version); `pip install`-ready (roadmap 5A).

### Changed
- **`config check`** now covers all *configured* backends: yt-dlp via the
  resolved binary, LLM reachability keyed off `LLM_BACKEND`, and the optional
  audio/diarize/instagram groups when enabled (roadmap 5B).
- Version is single-sourced from `reel_scout/__init__.py` via hatchling dynamic
  version (fixes the prior 0.2.0/0.3.0 drift).

## 0.3.0 — 2026-07-17

### Added
- `analyze <local-path>` — the `analyze` pipeline now accepts a local video file,
  not just a URL. Registers a `platform="local"` row (`url == file_path == abspath`,
  `platform_id` = content hash, so identical content at two paths dedups) and runs
  transcribe / vision / merge unchanged. This is the platform-lockout insurance:
  when a yt-dlp extractor breaks, the core pipeline still runs on files you already
  have. Duration is probed independently and stays `None` on probe failure (no
  fabricated fallback written to the DB); a missing path raises a clear
  `FileNotFoundError` instead of the crawler's opaque "Unsupported platform".
- `compare <id1> <id2> ...` — cross-video comparison table (duration, format,
  pacing, hook/CTA type, content type, and the craft scores). Transposed table
  plus `--json`; accepts an exact id or a unique prefix; missing analysis/score
  renders as an em dash rather than a fabricated value. Pure DB read path — no
  crawler, no LLM — so it also survives a platform lockout.
- `YTDLP_BIN` config (mirrors the `FFMPEG_BIN` convention).

### Changed
- yt-dlp is now invoked via the copy pinned in this environment
  (`python -m yt_dlp`) instead of whatever `yt-dlp` is first on PATH — a stale
  PATH build silently produced baffling extractor errors. Override with
  `YTDLP_BIN`. All three crawlers (youtube / tiktok / instagram) routed through
  the new `crawl/ytdlp.py` helper.
- yt-dlp error messages surface the real failure: `ERROR:` lines are kept first
  (instead of a blind `stderr[:500]` that buried them under leading warnings),
  with a fallback to the stderr tail and an update hint when the failure looks
  like a broken extractor.

## 0.2.0 — 2026-07-14

### Added
- Opt-in Whisper language controls for bilingual / code-switching audio
  (中英對照 interviews): `WHISPER_LANGUAGE`, `WHISPER_TASK`,
  `WHISPER_MULTILINGUAL`, `WHISPER_CHUNK_LENGTH`.
  - Working recipe for a ZH-host / EN-guest interview:
    `WHISPER_MULTILINGUAL=1 WHISPER_CHUNK_LENGTH=15`.
  - Fixes long-form language-lock drift where whisper `large-v3` "translates"
    the guest's English into garbled Chinese. Verified on a 40-min interview:
    latin-char recovery 56% -> 90%.
  - Defaults reproduce prior single-pass behavior; leave OFF for single-language
    short-form.
- `config check` now surfaces the new `WHISPER_*` values.
- `tests/test_transcribe.py` pins the config -> transcribe() kwargs mapping.

### Changed
- `faster-whisper` floor raised `>=0.10.0` -> `>=1.1.0` (the `multilingual`
  transcribe arg the fix relies on was added in 1.1).
