from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional

from .. import config, db
from ..crawl import get_crawler
from ..transcribe import get_transcriber
from ..transcribe.base import TranscriptResult
from ..vision import get_vlm
from ..vision.keyframe import extract_keyframes
from .merger import merge_analysis

# Platform tag for videos that were registered from a local file instead of
# crawled from a URL. This is the "platform门一关" insurance path: when yt-dlp /
# a platform extractor breaks, the whole crawl→analyze pipeline still runs on a
# file you already have on disk. See roadmap 5B.
LOCAL_PLATFORM = "local"


def _is_local_source(source: str) -> bool:
    """True if `source` is a path to an existing local file (not a URL)."""
    if "://" in source:
        return False
    return os.path.isfile(source)


def _normalize_source(source: str) -> str:
    """Local files become absolute paths so `url == file_path` stays stable
    across cwd changes and batch resume; URLs pass through untouched."""
    if _is_local_source(source):
        return os.path.abspath(source)
    return source


def _probe_duration(path: str) -> Optional[float]:
    """Best-effort duration probe. Returns None (never a fabricated fallback)
    on failure so a bad probe doesn't get written to the DB as if it were real;
    COALESCE downstream keeps duration unset until something真的 measures it."""
    cmd = [
        config.FFMPEG_BIN.replace("ffmpeg", "ffprobe"),
        "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except (ValueError, TypeError, OSError, subprocess.SubprocessError):
        return None


def _hash_file(path: str) -> str:
    """Content hash used as platform_id for local videos, so the same file
    (even at two different paths) resolves to the same video_id / dedups."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _register_local_video(conn: db.sqlite3.Connection, path: str) -> str:
    """Register a local file as a platform='local' video row and return its id.
    Mirrors the shape crawler.download() would have produced so Steps 2-5 of the
    pipeline treat it identically to a downloaded video."""
    abspath = os.path.abspath(path)
    return db.upsert_video(
        conn,
        platform=LOCAL_PLATFORM,
        platform_id=_hash_file(abspath),
        url=abspath,
        title=os.path.splitext(os.path.basename(abspath))[0],
        uploader=None,
        duration_sec=_probe_duration(abspath),
        upload_date=None,
        file_path=abspath,
        file_size_bytes=os.path.getsize(abspath),
    )


@dataclass
class PipelineOptions:
    skip_vision: bool = False
    skip_transcribe: bool = False
    skip_audio: bool = True
    skip_diarize: bool = True
    score: bool = False
    resume: bool = False
    whisper_backend: Optional[str] = None
    vlm_backend: Optional[str] = None
    vlm_model: Optional[str] = None
    keyframe_strategy: Optional[str] = None
    keyframe_max: Optional[int] = None
    resolution: int = 0       # 招④ keyframe upscale long-edge px (0 = native)
    start_sec: float = 0.0    # 招③ focus window start (0 = whole clip)
    end_sec: float = 0.0      # 招③ focus window end (0 = whole clip)


def run(urls: List[str], options: Optional[PipelineOptions] = None) -> None:
    if options is None:
        options = PipelineOptions()

    config.ensure_dirs()
    conn = db.init_db()

    # Local files → absolute paths so the batch url matches file_path exactly
    # (the skip-download seam keys on url), and so resume is cwd-independent.
    urls = [_normalize_source(u) for u in urls]

    # Resume or create batch
    if options.resume:
        batch = db.get_latest_interrupted_batch(conn)
        if batch is None:
            print("No interrupted batch found.")
            return
        batch_id = batch["id"]
        print(f"Resuming batch {batch_id}")
    else:
        batch_id = db.create_batch(conn, urls)
        print(f"Created batch {batch_id} with {len(urls)} URLs")

    # Handle SIGINT for graceful interruption
    interrupted = [False]

    def _on_interrupt(sig, frame):
        if interrupted[0]:
            sys.exit(1)
        interrupted[0] = True
        print("\nInterrupted. Saving progress...")
        db.mark_batch_interrupted(conn, batch_id)

    original_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _on_interrupt)

    try:
        pending = db.get_pending_batch_items(conn, batch_id)
        total = len(pending)
        print(f"Processing {total} pending items...")

        for i, item in enumerate(pending):
            if interrupted[0]:
                break

            url = item["url"]
            print(f"\n[{i+1}/{total}] {url}")

            try:
                video_id = _process_single(conn, url, options)
                db.update_batch_item(conn, batch_id, url, "done", video_id=video_id)
                print(f"  Done: {video_id}")
            except Exception as e:
                db.update_batch_item(conn, batch_id, url, "error", error=str(e))
                print(f"  Error: {e}")

        if not interrupted[0]:
            db.mark_batch_completed(conn, batch_id)
            print(f"\nBatch {batch_id} completed.")
    finally:
        signal.signal(signal.SIGINT, original_handler)
        conn.close()


def _process_single(
    conn: db.sqlite3.Connection,
    url: str,
    options: PipelineOptions,
) -> str:
    # Step 1: Download (skip if already exists)
    existing = db.get_video_by_url(conn, url)
    if existing and existing["file_path"] and os.path.exists(existing["file_path"]):
        video_id = existing["id"]
        print("  Skipping download (already exists)")
    elif _is_local_source(url):
        print("  Registering local file...")
        video_id = _register_local_video(conn, url)
    elif "://" not in url:
        # Looks like a path but isn't a file we can find — say so plainly instead
        # of the crawler's opaque "Unsupported platform for URL".
        raise FileNotFoundError(f"Local file not found: {url}")
    else:
        print("  Downloading...")
        crawler = get_crawler(url)
        meta = crawler.download(url, config.VIDEOS_DIR)
        video_id = db.upsert_video(
            conn,
            platform=meta.platform,
            platform_id=meta.platform_id,
            url=url,
            title=meta.title,
            uploader=meta.uploader,
            duration_sec=meta.duration_sec,
            upload_date=meta.upload_date,
            file_path=meta.file_path,
            file_size_bytes=meta.file_size_bytes,
        )

    video = db.get_video(conn, video_id)
    file_path = video["file_path"]

    # Step 2: Transcribe
    if not options.skip_transcribe:
        existing_transcript = db.get_transcript(conn, video_id)
        if existing_transcript:
            print("  Skipping transcribe (already done)")
        else:
            # 招① subtitle-first: if a native/auto subtitle was downloaded next to the
            # video, parse it instead of spending local Whisper compute. Whisper stays
            # the fallback (still fully local — no cloud ASR).
            from ..transcribe import find_subtitle
            from ..transcribe.vtt import parse_vtt
            sub_path = find_subtitle(file_path)
            if sub_path:
                print(f"  Using native subtitles ({os.path.basename(sub_path)})...")
                result = parse_vtt(sub_path)
                if not result.segments:
                    # Empty/garbled subtitle file — fall back to Whisper.
                    print("  Subtitle empty; falling back to Whisper...")
                    sub_path = None
            if not sub_path:
                print("  Transcribing (local Whisper)...")
                transcriber = get_transcriber(options.whisper_backend)
                result = transcriber.transcribe(file_path)
            segments_data = [
                {"start": s.start, "end": s.end, "text": s.text,
                 "confidence": s.confidence}
                for s in result.segments
            ]
            db.save_transcript(
                conn, video_id,
                language=result.language,
                text_full=result.text_full,
                segments_json=json.dumps(segments_data, ensure_ascii=False),
                whisper_model=result.model,
                duration_sec=result.duration_sec,
            )

    # Step 2.5: Audio analysis
    if not options.skip_audio:
        try:
            from ..audio import get_audio_analyzer
            from ..audio.extract import extract_wav
            existing_audio = db.get_audio_events(conn, video_id)
            if existing_audio:
                print("  Skipping audio analysis (already done)")
            else:
                print("  Analyzing audio events...")
                import tempfile
                wav_path = tempfile.mktemp(suffix=".wav")
                try:
                    extract_wav(file_path, wav_path)
                    analyzer = get_audio_analyzer()
                    timeline = analyzer.analyze(wav_path)
                    events_data = [
                        {"event_type": e.event_type, "label": e.label,
                         "start_sec": e.start_sec, "end_sec": e.end_sec,
                         "confidence": e.confidence}
                        for e in timeline.events
                    ]
                    db.save_audio_events(conn, video_id, events_data)
                finally:
                    import os as _os
                    if _os.path.exists(wav_path):
                        _os.unlink(wav_path)
        except (ImportError, FileNotFoundError) as e:
            print("  Skipping audio analysis: %s" % e, file=sys.stderr)

    # Step 2.7: Speaker diarization
    if not options.skip_diarize and config.DIARIZE_ENABLED:
        try:
            from ..diarize import get_diarizer, align_segments_json
            from ..audio.extract import extract_wav
            transcript = db.get_transcript(conn, video_id)
            if transcript and transcript["segments_json"]:
                segments_json = transcript["segments_json"]
                # Check if already has speaker labels
                import json as _json
                segs = _json.loads(segments_json)
                if segs and "speaker" not in segs[0]:
                    print("  Running speaker diarization...")
                    import tempfile
                    wav_path = tempfile.mktemp(suffix=".wav")
                    try:
                        extract_wav(file_path, wav_path)
                        diarizer = get_diarizer()
                        result = diarizer.diarize(wav_path)
                        # segments_json is a JSON string from the DB column, so
                        # use the JSON-in/JSON-out wrapper (speaker-align split the
                        # primitive into list[dict] vs *_json variants — R-mig).
                        updated_json = align_segments_json(
                            result.segments, segments_json)
                        conn.execute(
                            "UPDATE transcripts SET segments_json=? WHERE video_id=?",
                            (updated_json, video_id))
                        conn.commit()
                        print("  Diarization: %d speakers detected" % result.num_speakers)
                    finally:
                        import os as _os2
                        if _os2.path.exists(wav_path):
                            _os2.unlink(wav_path)
                else:
                    print("  Skipping diarization (already done)")
        except (ImportError, ValueError) as e:
            print("  Skipping diarization: %s" % e, file=sys.stderr)

    # Step 3: Keyframes, then vision analysis
    # Extraction is ffmpeg, not a model, so it runs even under --skip-vision. That
    # flag means "don't call a VLM", and the case where no VLM exists is exactly the
    # case where something else — an agent via `ingest vision`, or a later run with a
    # model available — needs the frames to be on disk. Gating extraction on it left
    # zero keyframes behind and made the whole no-local-model path unusable.
    # Descriptions are separately decoupled: they are (re)generated for any keyframe
    # that lacks one, whether or not extraction happened this run, so a failed or
    # partial VLM pass can be backfilled by re-running analyze.
    existing_kf = db.get_keyframes(conn, video_id)
    if existing_kf:
        print("  Keyframes already extracted")
        frames = [(kf["id"], kf["file_path"]) for kf in existing_kf]
    else:
        print("  Extracting keyframes...")
        kf_dir = os.path.join(config.KEYFRAMES_DIR, video_id)
        kf_infos = extract_keyframes(
            file_path, kf_dir, video_id,
            strategy=options.keyframe_strategy or "",
            max_frames=options.keyframe_max or 0,  # 0 -> 招② auto budget
            resolution=options.resolution,         # 招④
            start_sec=options.start_sec,            # 招③
            end_sec=options.end_sec,                # 招③
        )
        kf_data = [
            {"frame_index": kf.frame_index, "timestamp_sec": kf.timestamp_sec,
             "file_path": kf.file_path, "strategy": kf.strategy}
            for kf in kf_infos
        ]
        kf_ids = db.save_keyframes(conn, video_id, kf_data)
        frames = list(zip(kf_ids, [kf.file_path for kf in kf_infos]))

    if options.skip_vision:
        print("  Skipping VLM descriptions — %d keyframe(s) extracted and waiting "
              "for `reel-scout ingest vision`" % len(frames))
    else:
        backend = options.vlm_backend or config.VLM_BACKEND
        primary_model = options.vlm_model or config.VLM_MODEL

        # only describe keyframes that don't already have a description (backfill)
        described = db.get_described_keyframe_ids(conn, video_id)
        todo = [(kf_id, path) for kf_id, path in frames if kf_id not in described]

        if not todo:
            print("  Vision descriptions already present")
        else:
            def _persist(kf_id, desc, model):
                db.save_vision_description(
                    conn, kf_id,
                    description=desc.description,
                    objects_json=json.dumps(desc.objects, ensure_ascii=False),
                    text_in_frame=desc.text_in_frame,
                    vlm_backend=backend,
                    vlm_model=model,
                )

            print(f"  Describing {len(todo)} keyframe(s) with VLM ({primary_model})...")
            vlm = get_vlm(backend)
            failed = []  # (kf_id, path) — empty/errored on the primary model
            for kf_id, path in todo:
                # per-frame resilience: one bad frame must not abort the batch
                try:
                    desc = vlm.describe_frame(path)
                except Exception as e:  # noqa: BLE001
                    print(f"    frame {kf_id} failed: {e}", file=sys.stderr)
                    desc = None
                if desc and desc.description:
                    _persist(kf_id, desc, primary_model)
                else:
                    failed.append((kf_id, path))

            # graceful fallback (arkiv #83): retry failed frames with the fallback
            # model, but only if it's a different model that's actually installed —
            # otherwise leave frames empty for a later retry instead of 404ing.
            if failed:
                fb = config.VLM_FALLBACK_MODEL
                from ..vision.ollama import model_available, OllamaVLM
                use_fb = (
                    backend == "ollama" and fb and fb != primary_model
                    and model_available(config.OLLAMA_BASE_URL, fb)
                )
                if use_fb:
                    print(f"  {len(failed)} frame(s) failed; retrying with fallback {fb}...")
                    fb_vlm = OllamaVLM(base_url=config.OLLAMA_BASE_URL, model=fb)
                    for kf_id, path in failed:
                        try:
                            desc = fb_vlm.describe_frame(path)
                        except Exception as e:  # noqa: BLE001
                            print(f"    fallback frame {kf_id} failed: {e}", file=sys.stderr)
                            desc = None
                        if desc and desc.description:
                            _persist(kf_id, desc, fb)
                else:
                    print(
                        f"  {len(failed)} frame(s) failed; fallback "
                        f"'{fb or '(none)'}' unavailable — left empty for a later vision retry",
                        file=sys.stderr,
                    )

    # Step 3.5: Shot & audio metrics (§4E measured pacing).
    # Measures cut rhythm (cuts/min) + audio energy/BPM so the pacing score rests
    # on evidence, not LLM vibes; merge_analysis folds these into full_json for the
    # scorer. Best-effort: any failure here must NOT abort the pipeline — pacing
    # simply falls back to pure LLM judgment. Runs before merge so the metrics are
    # available to fold in.
    if config.SHOT_METRICS_ENABLED:
        if db.get_shot_metrics(conn, video_id):
            print("  Shot metrics already computed")
        else:
            try:
                from ..shots import compute_shot_metrics
                print("  Measuring shot rhythm...")
                sm = compute_shot_metrics(file_path, duration_sec=video["duration_sec"])
                bpm = energy = None
                # Audio energy/BPM needs only a decoded WAV (no PANNs model): pure-
                # stdlib energy + numpy-gated best-effort BPM. Skipped cleanly if
                # ffmpeg/numpy are unavailable.
                try:
                    from ..audio.extract import extract_wav
                    from ..audio.rhythm import compute_rhythm
                    import tempfile
                    wav_path = tempfile.mktemp(suffix=".wav")
                    try:
                        extract_wav(file_path, wav_path)
                        rhythm = compute_rhythm(wav_path)
                        bpm, energy = rhythm.get("bpm"), rhythm.get("energy")
                    finally:
                        import os as _os3
                        if _os3.path.exists(wav_path):
                            _os3.unlink(wav_path)
                except Exception as e:  # noqa: BLE001
                    # Audio is optional evidence — swallow ANY failure (incl.
                    # subprocess.TimeoutExpired from a slow extract_wav) so it never
                    # discards the shot metrics we already measured below.
                    print("  Audio rhythm skipped: %s" % e, file=sys.stderr)
                if sm is not None or bpm is not None or energy is not None:
                    db.save_shot_metrics(
                        conn, video_id,
                        shot_count=(sm.shot_count if sm else None),
                        cuts_per_minute=(sm.cuts_per_minute if sm else None),
                        avg_shot_sec=(sm.avg_shot_sec if sm else None),
                        audio_bpm=bpm,
                        audio_energy=energy,
                    )
                    if sm:
                        print("  Shot metrics: %.2f cuts/min, %d shots" % (
                            sm.cuts_per_minute, sm.shot_count))
            except Exception as e:  # noqa: BLE001 — measured pacing is best-effort
                print("  Shot metrics skipped: %s" % e, file=sys.stderr)

    # Step 3.6: On-screen text (§4F, L3.5). Collect burned-in captions (the VLM's
    # text_in_frame, or a dedicated OCR engine) with timestamps so merge can fold
    # them in — the only textual signal for low-dialogue / pure-visual reels.
    # Best-effort: never aborts the run.
    if config.OCR_ENABLED:
        if db.get_ocr_captions(conn, video_id):
            print("  On-screen text already collected")
        else:
            try:
                from ..ocr import collect_captions
                caps = collect_captions(conn, video_id)
                if caps:
                    db.save_ocr_captions(conn, video_id, caps)
                    print("  On-screen text: %d caption(s) (L3.5)" % len(caps))
            except Exception as e:  # noqa: BLE001 — best-effort
                print("  On-screen text skipped: %s" % e, file=sys.stderr)

    # Step 4: Merge analysis
    existing_analysis = db.get_analysis(conn, video_id)
    if existing_analysis:
        print("  Skipping analysis (already done)")
        # Retrofit §4E measured metrics into a pre-existing analysis: merge is
        # skipped here, so without this a re-analyzed (pre-§4E) video would store
        # shot_metrics the scorer never sees. No-op once the blob already has them.
        #
        # Contract note (§4F): on-screen-text (OCR) captions are intentionally NOT
        # retrofitted the same way. Like transcript/vision/audio, OCR only shapes an
        # analysis through the merge prompt — a re-analyzed video's stored analysis
        # won't reflect newly-collected captions until a genuinely fresh merge.
        # `measured` can backfill because the scorer reads it directly; OCR text has
        # nothing downstream to append to. This matches how every merge input folds
        # in only at first merge, so it's a deliberate contract, not a gap.
        from .merger import backfill_measured
        backfill_measured(conn, video_id)
    else:
        print("  Merging analysis...")
        merge_analysis(conn, video_id)

    # Step 5: Scoring (optional)
    if getattr(options, 'score', False):
        existing_score = db.get_score(conn, video_id)
        if existing_score:
            print("  Skipping scoring (already done)")
        else:
            print("  Scoring...")
            from ..scorer import score_video
            score = score_video(conn, video_id)
            print("  Score: %.1f (hook=%.1f visual=%.1f pacing=%.1f structure=%.1f)" % (
                score.overall, score.hook_strength, score.visual_storytelling,
                score.pacing, score.structure))

    return video_id
