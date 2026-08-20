# Changelog

## Unreleased

### Fixed

- **`stats` averaged two rulers into one number.** A craft score is only
  meaningful against the model that produced it — the same clip scores 7.43
  under `qwen3-vl:8b` and 5.5 under `qwen2.5vl:7b` — and `ingest` stamps every
  agent-supplied row `agent:<model>` precisely so the origin survives. `stats`
  then ignored that column entirely: a library holding agent-scored and
  locally-scored videos reported a single blended mean, and the blend can land
  in a gap where no video sits. Six videos scoring 8.5/8.0/8.3 (agent) and
  5.0/5.5/5.2 (local) reported `overall 6.8 / 5.0-8.5 (n=6)` — more than a full
  point away from every video in the corpus, with nothing on screen to say two
  scales were in play. Score aggregates are now grouped by `model_used`
  (`score_aggregates_by_model`, `score_sources`), and the pooled block is kept
  but labelled as pooled whenever `mixed_score_sources` is true, in the table,
  in `--json`, and in `--csv`. Grouping is on the exact model string, the finest
  grain that is correct — two local VLMs are no more comparable to each other
  than an agent is to either. Per-video reads and the existing `score,*` CSV
  rows are byte-for-byte unchanged.
- **`compare` put two scores side by side without saying which ruler each came
  from.** Same defect as above, one surface over: the comparison table is the
  single place a reader is most likely to turn two numbers into "A beats B",
  and two clips scored by different models cannot support that reading at all.
  The table now carries a `Score source` row, directly above the numbers it
  qualifies — the same placement and the same reasoning as the `Transcript` row
  added earlier. A score written before provenance existed reads as an em dash,
  which is "scored, origin unrecorded" and deliberately not the same as having
  no score.
- **A stored media path stopped meaning anything once you changed directory.**
  `DATA_DIR` defaults to `./data` — relative to whatever cwd the process started
  in — so rows written from the repo root recorded `./data/videos/x.mp4` while
  rows written anywhere else recorded an absolute path. A live database holds
  both. `analyze` checked the stored path with a bare `os.path.exists()`, so run
  from any other directory it concluded the file was missing and downloaded it
  again; `resolve_video_file` looked cwd-relative first and then fell back to
  `VIDEOS_DIR`, which is itself cwd-relative, so both of its candidates missed
  too. Resolution is now anchored to the data root and tries every shape a
  stored path can have — portable, legacy, cwd, and basename-under-`VIDEOS_DIR`
  for files that moved between data directories. `reel-scout db normalize-paths
  [--dry-run]` rewrites existing rows to the portable form; rows whose path
  cannot be resolved are reported and deliberately left alone, because
  rewriting a path you cannot verify turns a recoverable row into a confidently
  wrong one.
- **An LLM call that timed out took the clip down with it, and the timeout was
  unreachable from outside the source.** The 600s limit was a hardcoded constant
  with no retry, so a transient condition produced a permanent loss: on
  2026-08-11 a 96-clip re-merge dropped 3 clips to Ollama contention, and all
  three re-ran fine at 84/125/101s once the machine was idle. Contention is an
  external condition and a fixed constant is an internal choice; welding them
  together meant the only way to survive a busy machine was to edit the code.
  `LLM_TIMEOUT`, `LLM_MAX_RETRIES` and `LLM_RETRY_BACKOFF` are now environment
  variables and appear in `config show`. Retries are for timeouts **only** — a
  malformed request or a missing model fails identically on attempt three, so
  retrying those would just delay the same error by timeout × retries.
- **A 403 during the download killed the ingest even though the format chain had
  three more selectors left.** The `/` chain in `-f` degrades while *choosing* a
  format; once a format is chosen and the transfer itself dies, yt-dlp does not
  walk to the next selector — it fails, and the clip never enters the library.
  Seen in the wild on E8Bx9OlpmdM, where the separate video stream 403'd while
  the same video downloaded fine as progressive `18`. The fallback therefore has
  to live at the download layer, not in the selector string: if the quality-first
  attempt produces no file, a progressive format is tried before the failure is
  raised. Progressive formats come from a different URL than the split streams,
  which is exactly why they survive when the split ones do not. Quality-first
  stays first; this is the "something beats nothing" floor, the same trade the
  existing chain already makes for AV1.
- **Opening a database that did not exist created an empty one and said nothing.**
  `DB_PATH` is cwd-relative, so running from the wrong directory produced a fresh
  empty database that then reported zero pending work — indistinguishable from a
  library that is fully up to date. Three separate incidents traced back to this.
  `init_db` now prints a one-line notice with the **absolute** path when it
  creates the file, which is the piece that makes the mistake visible: the path
  is either the one you expected or obviously not. Creating on demand is kept
  deliberately — raising instead was checked and rejected, because 40+ call sites
  and first-run setup all depend on it, so the fix is to remove the silence, not
  the behaviour.
- **A Chinese transcript could change script partway through a single file, and
  searching it found only the first half.** One 28,331-character transcript in
  the reference library is traditional up to 83% and simplified from there, with
  no interleaving and no error: searching it for `這` returns the first 83% and
  reports nothing wrong. `save_transcript` now scans for script mixing and warns
  with the position of the flip. It **reports and does not convert** — converting
  requires first deciding which script the library standardises on, and it would
  add a dependency the core install deliberately does not have. The detector's
  character sets exclude 後/后 and 麼/么, whose "simplified" forms are also real
  traditional characters (皇后, 幺么); a detector that fires on clean text teaches
  people to ignore it. Validated against the library: 18/18 mixed files found,
  zero false positives.
- **A warning that could not be printed took the transcript down with it.** The
  mixed-script notice above carries an emoji and an em dash, and it printed
  *before* the `INSERT`. `cli.py` reconfigures both streams to UTF-8, but it is
  the **CLI** entry point — `mcp/server.py` never runs it, and the MCP server is
  exactly how the Windows students reach this package, on the cp950/cp1252/cp437
  consoles this repo has already been bitten by once. On those, the print raised
  `UnicodeEncodeError` and the transcript was never stored: a detector that
  deleted the data it was watching. Two independent fixes, because one of them
  being enough is not a reason to leave the other broken. `utils.stderr.warn`
  degrades characters the console cannot encode and never raises at its caller —
  whether the operator sees a line is not worth the caller's work. And the scan
  now runs before the write while the report happens after it, so even a failure
  `warn` deliberately does not absorb costs nothing but the message.
- **The download retry could resume a different format's partial file.** Both
  attempts write to the same `yt_<id>.mp4.part` and yt-dlp resumes partials by
  default, so a first selector that picked a progressive format and died
  mid-transfer left bytes that the second attempt would extend with a Range
  request against a *different* stream. Nothing validates that the two halves
  came from the same video, and the result is a corrupt file that exists — which
  the caller's `os.path.exists` check reads as success, the exact failure this
  release is about. The fallback now passes `--no-continue`.
- **The fallback quietly handed back a lower-quality clip.** It usually lands on
  format 18 (360p). Silently substituting it is its own small version of
  reporting success for the wrong result, so it now says on stderr that the
  preferred formats produced nothing and that quality will be lower.
- **A 200 response with no `response` field was returned as an empty string.** An
  empty `response` is legitimate — the model produced nothing. A *missing* one
  means the reply is not a generate response at all: an error envelope, a proxy
  in front of Ollama, a schema that moved. Both arrived at the merger as an empty
  result that read as a successful call. The missing case now says so. It is not
  raised, because a compatible backend may legitimately differ and a hard error
  would break a setup that works today.
- **ffmpeg and whisper.cpp still reported the wrong end of stderr.** `crawl/ytdlp.py`
  fixed this for yt-dlp, but the audio-extraction and whisper.cpp paths kept a
  blind `stderr[:300]` / `[:500]`. Both print banners and progress first and the
  cause last, so the head shown was reliably the part carrying no information —
  an ffmpeg permission error sat past the cut behind five lines of build
  configuration. Both now report error-looking lines, falling back to the tail.
- **A batch where every item failed exited 0.** Both `batch` and `analyze`
  printed an explanation and returned `None`, so the dispatcher's
  `if code: raise SystemExit(code)` never fired and a wrapper reading `$?` was
  told the work was done. `analyze` also printed `Batch <id> completed.` on a run
  where the only item had errored on the line above — "completed" on its own
  reads as "worked", and it now says `completed with N/M failed.`, the same
  distinction the MCP surface draws with `completed_with_failures`. A Ctrl-C is
  deliberately still not counted: it leaves work undone too, but the operator
  pressed it and the screen says so.
- **One message stood in for four different reasons the vision fallback did not
  run.** Whatever blocked it, the log said `fallback '<model>' unavailable`. On a
  run whose backend was not ollama the first condition already settled it and the
  availability probe was never called — so the line named a missing model that
  was installed and answering, and acting on it means installing what is already
  there. Each reason now says its own name, and the "not installed" one says
  which host it looked at so the claim can be checked. The probe stays last,
  because it is a network call and a cheaper reason usually settles the question.
- **The console-encoding fix only ever ran on the CLI entry point.** `main()` in
  `cli.py` called it; the MCP server did not, so a description carrying a
  character the console could not encode took the server down instead of the
  frame. It now runs on both, and the warning path degrades unencodable
  characters rather than raising inside the warning itself.
- **A leftover file at the destination let a download report success without
  transferring a byte.** yt-dlp exits 0 when the target already exists, so a
  truncated or unusable file from an earlier attempt was read as a completed
  download. Unusable outputs are now renamed aside (`.unusable`, never deleted,
  `.vtt` siblings moved with them) and success is judged on both the return code
  and the file actually being there; the format fallback passes `--no-continue`
  so it cannot resume a different format's partial file.
- **Keyframes collapsed onto the opening seconds of a clip** (`scene` strategy,
  the default). The cause was in the ffmpeg pass that *detects* scenes, not the
  one that cuts them: `-frames:v N` makes ffmpeg exit after N outputs, so
  detection stopped early and everything after that point was invisible to the
  sampler. Measured before the change, 27 clips had a single unsampled gap larger
  than half their length; measured after, across a library that had grown to 110,
  one — and that one is a 22-second clip whose first 13 seconds are a single
  unchanging shot, so there is nothing there to sample. Mean largest gap went
  from 35.6% of clip length to 17.3%. Detection now runs unbounded and writes no
  images at all (`-f null -`); the second pass seeks to each selected timestamp.
  Selection
  spreads across the time axis rather than the ordinal one, so a clip whose cuts
  cluster at the front no longer spends its whole budget there. **The `motion`
  strategy still has the same defect** — `_extract_motion` caps its emitting pass
  with `-frames:v max_frames`, so it keeps the first N high-motion frames rather
  than a spread. It is not the default and was left for its own change, since the
  fix is the same two-pass restructure and deserves its own tests.
- **A model that reasons before answering spent the entire token budget
  reasoning.** At the old ceiling the reply came back `done_reason: "length"`
  with zero characters, which downstream is indistinguishable from a frame with
  nothing to say. Reasoning length varies far more than answer length — usually
  around 440 tokens, with a tail well past 1200 — so raising the default further
  is racing a distribution with no upper bound. A frame that hit the ceiling now
  gets one retry at a larger budget; a frame that finished on its own does not,
  because retrying it returns the same thing more slowly. `think: false` was
  measured and does not work here (ollama 0.30.7 + qwen3-vl:8b keeps reasoning
  and leaks a raw `<think>` tag). The warning that fires when even the retry came
  back empty says how far it already got, so nobody raises a number twice.
- **One wedged step could block a batch for as long as the machine stayed up.**
  `batch.py` ran its child steps with no timeout at all, so a stuck ffmpeg or a
  model that never answered held every remaining clip behind it — and the run
  looked busy the whole time, which is the worst shape a stall can take. Each
  step now has its own deadline and the batch has one overall: `analyze` at
  1800s (just under the 1815s worst case a single merge can spend burning its
  full retry budget — a call that reaches that number *is* the pathology),
  `export` at 300s, and `score` derived from `LLM_TIMEOUT` rather than fixed,
  because a subprocess kill that pre-empts the backend's own error path throws
  away the diagnosis the retry existed to produce.
- **A run stopped by the clock reported the same thing as a run that finished.**
  `incomplete` is now its own state, separate from `completed` (the list was not
  finished), `failed` (the worker did its job) and `completed_with_failures`
  (nothing failed). Folding it into any of those asks the reader either to
  re-run URLs that are fine or to assume URLs are fine when nobody looked at
  them; the payload now names which were never attempted.
- **The timeout could kill the process that was enforcing it.** Steps run in
  their own process group so a wedged grandchild dies with its parent, but a
  naive `killpg` on a process that was never detached reaches the whole tree
  including the runner. The kill now resolves which group it may signal and
  refuses to touch its own. (This was found by mutation testing, and the
  mutation that found it took down the tool chain that was running it — the
  file stayed mutated, so every later run silently tested code nobody wrote.
  The runner now restores on startup as well as in `finally`.)

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
### Added

- **Keyframe extraction is a run with an identity, so re-sampling is possible
  without destroying the evidence it replaces.** Frames used to be tied to the
  clip alone, which made "extract again with different settings" either
  impossible or a deletion. A run (`keyframe_runs`, schema v12) owns its frames
  and its descriptions; a new run supersedes the previous one instead of
  overwriting it, everything downstream reads the current run, and the old frames
  and their descriptions stay on disk to compare against. `--force-keyframes`
  asks for a new run explicitly. Frames are committed only after they have landed,
  so an interrupted extraction cannot leave a run pointing at files that are not
  there.
- **`retry_call`** — the retry helper had a decorator with no call sites and no
  way to say *which* failures are worth retrying. It now takes a `should_retry`
  predicate and an `on_retry` hook, and the decorator delegates to it. (It also
  raised `None` when configured with zero attempts.)
- **Near-duplicate keyframes are dropped instead of described.** `frame_cap`
  decides how many frames a clip may spend; this decides how many it actually
  needs. They fix different defects — the cap fixed "long clips sampled too
  sparsely", this fixes "consecutive samples look identical and each one still
  costs a local VLM call".

  **Nothing is topped up.** A version that backfilled to the cap would cost
  exactly what it cost before, and would also hide whether the pass works at
  all, because the count would always equal the cap. Coming in under budget is
  the point.

  Two guards stop this re-creating the sparse-timeline defect the cap exists to
  fix. A frame is only ever dropped while the previous **kept** frame is within
  `KEYFRAME_DEDUPE_MAX_GAP_SEC` (120s) — two identical-looking frames seventeen
  minutes apart are information ("nothing changed for seventeen minutes"), two
  seconds apart is noise. And `KEYFRAME_DEDUPE_MIN` (4) floors the count so a
  static short clip cannot collapse to a single frame. First and last are never
  dropped, and a frame ffmpeg cannot read is never dropped either.

  The hash is a 64-bit dHash computed **through ffmpeg**, not Pillow: ffmpeg is
  already a hard requirement, Pillow is only in the `ocr` extra, and a dedupe
  pass is not worth promoting an optional dependency to a core one. One extra
  ffmpeg call per frame, milliseconds each, against the seconds a VLM call
  costs — removing one frame pays for the whole pass.

  **Measured on the real 100-video library: 1,202 → 1,148 frames, 54 VLM calls
  avoided (4.5%), 22 videos affected.** That is deliberately modest. The default
  distance threshold (4 of 64 bits) only removes frames a person would call the
  same frame, and this library is mostly fast-cut short-form, where consecutive
  keyframes genuinely differ. The one long clip in the set (52 min) dropped 6 of
  40. Expect the saving to grow with long static footage — lectures, interviews,
  streams — which is exactly where a flat cap wastes the most.

  Off with `KEYFRAME_DEDUPE=0`; `KEYFRAME_DEDUPE_DISTANCE` loosens or tightens
  it. `reel-scout config` prints all four values.

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
