from __future__ import annotations

import json
import os
import signal
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


@dataclass
class PipelineOptions:
    skip_vision: bool = False
    skip_transcribe: bool = False
    resume: bool = False
    whisper_backend: Optional[str] = None
    vlm_backend: Optional[str] = None
    vlm_model: Optional[str] = None
    keyframe_strategy: Optional[str] = None
    keyframe_max: Optional[int] = None


def run(urls: List[str], options: Optional[PipelineOptions] = None) -> None:
    if options is None:
        options = PipelineOptions()

    config.ensure_dirs()
    conn = db.init_db()

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
            print("  Transcribing...")
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

    # Step 3: Vision analysis
    if not options.skip_vision:
        existing_kf = db.get_keyframes(conn, video_id)
        if existing_kf:
            print("  Skipping keyframes (already extracted)")
        else:
            print("  Extracting keyframes...")
            kf_dir = os.path.join(config.KEYFRAMES_DIR, video_id)
            kf_infos = extract_keyframes(
                file_path, kf_dir, video_id,
                strategy=options.keyframe_strategy or "",
                max_frames=options.keyframe_max or 0,
            )
            kf_data = [
                {"frame_index": kf.frame_index, "timestamp_sec": kf.timestamp_sec,
                 "file_path": kf.file_path, "strategy": kf.strategy}
                for kf in kf_infos
            ]
            kf_ids = db.save_keyframes(conn, video_id, kf_data)

            print(f"  Describing {len(kf_ids)} keyframes with VLM...")
            vlm = get_vlm(options.vlm_backend)
            for kf_id, kf_info in zip(kf_ids, kf_infos):
                desc = vlm.describe_frame(kf_info.file_path)
                db.save_vision_description(
                    conn, kf_id,
                    description=desc.description,
                    objects_json=json.dumps(desc.objects, ensure_ascii=False),
                    text_in_frame=desc.text_in_frame,
                    vlm_backend=options.vlm_backend or config.VLM_BACKEND,
                    vlm_model=options.vlm_model or config.VLM_MODEL,
                )

    # Step 4: Merge analysis
    existing_analysis = db.get_analysis(conn, video_id)
    if existing_analysis:
        print("  Skipping analysis (already done)")
    else:
        print("  Merging analysis...")
        merge_analysis(conn, video_id)

    return video_id
