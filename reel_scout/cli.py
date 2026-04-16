from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import List

from . import __version__, config


def main(argv: List[str] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="reel-scout",
        description="Short-form video analysis tool",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    # --- browse ---
    p_browse = sub.add_parser("browse", help="List videos from a profile/channel page")
    p_browse.add_argument("url", help="Profile/channel URL (e.g. instagram.com/user/reels/)")
    p_browse.add_argument("--limit", "-n", type=int, default=30, help="Max videos to list (default: 30)")
    p_browse.add_argument("--cookies", help="Path to cookies file (for IG)")
    p_browse.add_argument("--json", dest="output_json", action="store_true", help="Output as JSON")
    p_browse.add_argument("--urls-only", action="store_true", help="Output only URLs (pipe to crawl --file)")

    # --- crawl ---
    p_crawl = sub.add_parser("crawl", help="Download videos")
    p_crawl.add_argument("urls", nargs="*", help="Video URLs")
    p_crawl.add_argument("--file", "-f", help="File with URLs (one per line)")
    p_crawl.add_argument("--cookies", help="Path to cookies file (for IG)")

    # --- analyze ---
    p_analyze = sub.add_parser("analyze", help="Full pipeline: crawl + transcribe + vision + merge")
    p_analyze.add_argument("urls", nargs="*", help="Video URLs")
    p_analyze.add_argument("--file", "-f", help="File with URLs (one per line)")
    p_analyze.add_argument("--resume", action="store_true", help="Resume interrupted batch")
    p_analyze.add_argument("--skip-vision", action="store_true", help="Skip VLM analysis")
    p_analyze.add_argument("--skip-transcribe", action="store_true", help="Skip transcription")
    p_analyze.add_argument("--whisper-backend", help="Whisper backend (faster-whisper, whisper-cpp)")
    p_analyze.add_argument("--vlm-backend", help="VLM backend (omlx, ollama)")
    p_analyze.add_argument("--vlm-model", help="VLM model name")
    p_analyze.add_argument("--keyframe-strategy", help="Keyframe strategy (scene, interval, hybrid)")
    p_analyze.add_argument("--keyframe-max", type=int, help="Max keyframes per video")
    p_analyze.add_argument("--llm-backend", help="LLM backend (omlx, ollama, openclaw)")
    p_analyze.add_argument("--score", action="store_true", help="Score video after analysis")
    p_analyze.add_argument("--skip-audio", action="store_true", default=True, help="Skip audio analysis (default: skip)")
    p_analyze.add_argument("--no-skip-audio", dest="skip_audio", action="store_false", help="Enable audio analysis")
    p_analyze.add_argument("--skip-diarize", action="store_true", default=True, help="Skip diarization (default: skip)")
    p_analyze.add_argument("--no-skip-diarize", dest="skip_diarize", action="store_false", help="Enable diarization")

    # --- transcribe ---
    p_transcribe = sub.add_parser("transcribe", help="Transcribe a local video/audio")
    p_transcribe.add_argument("path", nargs="?", help="Path to video/audio file")
    p_transcribe.add_argument("--pending", action="store_true", help="Transcribe all pending videos")
    p_transcribe.add_argument("--backend", help="Whisper backend")

    # --- vision ---
    p_vision = sub.add_parser("vision", help="Extract keyframes and describe with VLM")
    p_vision.add_argument("path", help="Path to video file")
    p_vision.add_argument("--backend", help="VLM backend (omlx, ollama)")
    p_vision.add_argument("--model", help="VLM model name")

    # --- list ---
    p_list = sub.add_parser("list", help="List analyzed videos")
    p_list.add_argument("--status", help="Filter by status")
    p_list.add_argument("--platform", help="Filter by platform")
    p_list.add_argument("--limit", type=int, default=50)

    # --- show ---
    p_show = sub.add_parser("show", help="Show full analysis for a video")
    p_show.add_argument("video_id", help="Video ID")

    # --- export ---
    p_export = sub.add_parser("export", help="Export analyses")
    p_export.add_argument("--format", choices=["json", "csv"], default="json")
    p_export.add_argument("--output", "-o", default="./export")

    # --- score ---
    p_score = sub.add_parser("score", help="Score a video using LLM analysis")
    p_score.add_argument("video_id", help="Video ID to score")
    p_score.add_argument("--backend", help="LLM backend (omlx, ollama, openclaw)")

    # --- db ---
    p_db = sub.add_parser("db", help="Database operations")
    p_db_sub = p_db.add_subparsers(dest="db_command")
    p_db_sub.add_parser("stats", help="Show database stats")
    p_db_sub.add_parser("reset", help="Reset database (destructive)")
    p_db_sub.add_parser("migrate", help="Run pending migrations")

    # --- config ---
    p_config = sub.add_parser("config", help="Configuration")
    p_cfg_sub = p_config.add_subparsers(dest="config_command")
    p_cfg_sub.add_parser("show", help="Show resolved config")
    p_cfg_sub.add_parser("check", help="Check external tools")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return

    handlers = {
        "browse": _cmd_browse,
        "crawl": _cmd_crawl,
        "analyze": _cmd_analyze,
        "transcribe": _cmd_transcribe,
        "vision": _cmd_vision,
        "list": _cmd_list,
        "show": _cmd_show,
        "export": _cmd_export,
        "score": _cmd_score,
        "db": _cmd_db,
        "config": _cmd_config,
    }
    handlers[args.command](args)


def _collect_urls(args) -> List[str]:
    urls = list(args.urls) if args.urls else []
    if getattr(args, "file", None):
        with open(args.file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    return urls


def _cmd_browse(args) -> None:
    from .crawl import get_crawler

    if args.cookies:
        os.environ["IG_COOKIES_FILE"] = args.cookies

    try:
        crawler = get_crawler(args.url)
        entries = crawler.browse(args.url, limit=args.limit)
    except NotImplementedError as e:
        print(f"Error: {e}")
        return
    except Exception as e:
        print(f"Error browsing: {e}")
        return

    if not entries:
        print("No videos found.")
        return

    if args.urls_only:
        for e in entries:
            print(e.url)
        return

    if args.output_json:
        import dataclasses
        data = [dataclasses.asdict(e) for e in entries]
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    print(f"Found {len(entries)} videos from @{entries[0].uploader or '?'}:\n")
    for i, e in enumerate(entries, 1):
        title = (e.title or "(untitled)")[:60]
        dur = f"{e.duration_sec:.0f}s" if e.duration_sec else "?"
        date = e.upload_date or "?"
        print(f"  {i:3d}. [{dur:>5s}] {title}")
        print(f"       {e.url}")
    print(f"\nTip: pipe URLs to crawl with: reel-scout browse {args.url} --urls-only | reel-scout crawl --file -")


def _cmd_crawl(args) -> None:
    from . import db
    from .crawl import get_crawler

    urls = _collect_urls(args)
    if not urls:
        print("No URLs provided. Use: reel-scout crawl <url> or --file urls.txt")
        return

    if args.cookies:
        os.environ["IG_COOKIES_FILE"] = args.cookies

    config.ensure_dirs()
    conn = db.init_db()

    for i, url in enumerate(urls):
        print(f"[{i+1}/{len(urls)}] {url}")
        try:
            crawler = get_crawler(url)
            meta = crawler.download(url, config.VIDEOS_DIR)
            vid = db.upsert_video(
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
            print(f"  OK: {meta.title[:60]} ({meta.duration_sec:.0f}s) -> {vid}")
        except Exception as e:
            print(f"  Error: {e}")

    conn.close()


def _cmd_analyze(args) -> None:
    from .analyze.pipeline import PipelineOptions, run

    urls = _collect_urls(args)
    if not urls and not args.resume:
        print("No URLs provided. Use: reel-scout analyze <url> or --file urls.txt")
        return

    options = PipelineOptions(
        skip_vision=args.skip_vision,
        skip_transcribe=args.skip_transcribe,
        skip_audio=args.skip_audio,
        skip_diarize=args.skip_diarize,
        score=args.score,
        resume=args.resume,
        whisper_backend=args.whisper_backend,
        vlm_backend=args.vlm_backend,
        vlm_model=args.vlm_model,
        keyframe_strategy=args.keyframe_strategy,
        keyframe_max=args.keyframe_max,
    )
    run(urls, options)


def _cmd_transcribe(args) -> None:
    from . import db
    from .transcribe import get_transcriber

    if args.pending:
        config.ensure_dirs()
        conn = db.init_db()
        videos = db.list_videos(conn, status="downloaded", limit=9999)
        if not videos:
            print("No pending videos to transcribe.")
            return
        transcriber = get_transcriber(args.backend)
        for v in videos:
            print(f"Transcribing: {v['title'] or v['id']}")
            result = transcriber.transcribe(v["file_path"])
            segments_data = [
                {"start": s.start, "end": s.end, "text": s.text,
                 "confidence": s.confidence}
                for s in result.segments
            ]
            db.save_transcript(
                conn, v["id"],
                language=result.language,
                text_full=result.text_full,
                segments_json=json.dumps(segments_data, ensure_ascii=False),
                whisper_model=result.model,
                duration_sec=result.duration_sec,
            )
            print(f"  Language: {result.language}, Duration: {result.duration_sec:.1f}s")
        conn.close()
    elif args.path:
        transcriber = get_transcriber(args.backend)
        result = transcriber.transcribe(args.path)
        print(f"Language: {result.language}")
        print(f"Duration: {result.duration_sec:.1f}s")
        print(f"---")
        print(result.text_full)
    else:
        print("Provide a file path or use --pending")


def _cmd_vision(args) -> None:
    from .vision import get_vlm
    from .vision.keyframe import extract_keyframes

    import tempfile
    kf_dir = tempfile.mkdtemp(prefix="reel_scout_kf_")
    print(f"Extracting keyframes from: {args.path}")
    keyframes = extract_keyframes(args.path, kf_dir, "temp")
    print(f"  Extracted {len(keyframes)} keyframes")

    vlm = get_vlm(args.backend)
    for kf in keyframes:
        desc = vlm.describe_frame(kf.file_path)
        print(f"\n[{kf.timestamp_sec:.1f}s] {desc.description}")


def _cmd_list(args) -> None:
    from . import db

    config.ensure_dirs()
    conn = db.init_db()
    videos = db.list_videos(conn, status=args.status, platform=args.platform, limit=args.limit)
    if not videos:
        print("No videos found.")
        return

    for v in videos:
        title = (v["title"] or "(untitled)")[:50]
        print(f"  {v['id']}  {v['platform']:10s}  {v['status']:12s}  {title}")

    print(f"\nTotal: {len(videos)}")
    conn.close()


def _cmd_show(args) -> None:
    from . import db

    config.ensure_dirs()
    conn = db.init_db()
    video = db.get_video(conn, args.video_id)
    if not video:
        print(f"Video not found: {args.video_id}")
        return

    analysis = db.get_analysis(conn, args.video_id)
    transcript = db.get_transcript(conn, args.video_id)

    print(f"Video: {video['title'] or '(untitled)'}")
    print(f"Platform: {video['platform']}")
    print(f"URL: {video['url']}")
    print(f"Duration: {video['duration_sec']}s")
    print(f"Status: {video['status']}")

    if transcript:
        print(f"\n--- Transcript ({transcript['language']}) ---")
        print(transcript["text_full"][:500])

    if analysis:
        print(f"\n--- Analysis ---")
        full = json.loads(analysis["full_json"]) if analysis["full_json"] else {}
        print(json.dumps(full, ensure_ascii=False, indent=2))

    conn.close()


def _cmd_export(args) -> None:
    from . import db
    from .export.json_export import export_csv, export_json

    config.ensure_dirs()
    conn = db.init_db()

    if args.format == "json":
        count = export_json(conn, args.output)
        print(f"Exported {count} analyses to {args.output}/")
    elif args.format == "csv":
        count = export_csv(conn, args.output)
        print(f"Exported {count} rows to {args.output}")

    conn.close()


def _cmd_score(args) -> None:
    from . import db
    from .scorer import score_video

    config.ensure_dirs()
    conn = db.init_db()

    video = db.get_video(conn, args.video_id)
    if not video:
        print(f"Video not found: {args.video_id}")
        conn.close()
        return

    existing = db.get_score(conn, args.video_id)
    if existing:
        print("Score already exists for this video:")
        print(f"  Overall:    {existing['overall']:.1f}")
        print(f"  Hook:       {existing['hook_strength']:.1f}")
        print(f"  Info:       {existing['information_density']:.1f}")
        print(f"  Emotion:    {existing['emotional_impact']:.1f}")
        print(f"  Share:      {existing['shareability']:.1f}")
        print(f"  Reasoning:  {existing['reasoning']}")
        conn.close()
        return

    try:
        score = score_video(conn, args.video_id, llm_backend=args.backend)
        print(f"Score for: {video['title'] or '(untitled)'}")
        print(f"  Overall:    {score.overall:.1f}")
        print(f"  Hook:       {score.hook_strength:.1f}")
        print(f"  Info:       {score.information_density:.1f}")
        print(f"  Emotion:    {score.emotional_impact:.1f}")
        print(f"  Share:      {score.shareability:.1f}")
        print(f"  Reasoning:  {score.reasoning}")
    except ValueError as e:
        print(f"Error: {e}")

    conn.close()


def _cmd_db(args) -> None:
    from . import db

    if args.db_command == "stats":
        config.ensure_dirs()
        conn = db.init_db()
        stats = db.db_stats(conn)
        print("Database Statistics")
        print("=" * 40)
        for table, count in stats.items():
            if isinstance(count, dict):
                print(f"  {table}:")
                for k, v in count.items():
                    print(f"    {k}: {v}")
            else:
                print(f"  {table}: {count}")
        conn.close()

    elif args.db_command == "reset":
        answer = input("This will DELETE all data. Type 'yes' to confirm: ")
        if answer.strip().lower() == "yes":
            if os.path.exists(config.DB_PATH):
                os.remove(config.DB_PATH)
                print("Database reset.")
            else:
                print("No database file found.")
        else:
            print("Cancelled.")

    elif args.db_command == "migrate":
        config.ensure_dirs()
        conn = db.init_db()
        print("Migrations complete.")
        conn.close()

    else:
        print("Use: reel-scout db {stats|reset|migrate}")


def _cmd_config(args) -> None:
    if args.config_command == "show":
        print(config.show())

    elif args.config_command == "check":
        print("Checking external tools...\n")
        # ffmpeg
        try:
            r = subprocess.run(
                [config.FFMPEG_BIN, "-version"],
                capture_output=True, text=True, timeout=5,
            )
            ver = r.stdout.split("\n")[0] if r.returncode == 0 else "ERROR"
            print(f"  ffmpeg:    {ver}")
        except Exception as e:
            print(f"  ffmpeg:    NOT FOUND ({e})")

        # yt-dlp
        try:
            r = subprocess.run(
                ["yt-dlp", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            print(f"  yt-dlp:    {r.stdout.strip()}")
        except Exception as e:
            print(f"  yt-dlp:    NOT FOUND ({e})")

        # Whisper
        if config.WHISPER_BACKEND == "faster-whisper":
            try:
                import faster_whisper
                print(f"  whisper:   faster-whisper (installed)")
            except ImportError:
                print(f"  whisper:   faster-whisper NOT installed")
        else:
            try:
                r = subprocess.run(
                    ["whisper", "--help"],
                    capture_output=True, text=True, timeout=5,
                )
                print(f"  whisper:   whisper.cpp (found)")
            except Exception:
                print(f"  whisper:   whisper.cpp NOT found")

        # VLM endpoint
        import urllib.request
        vlm_url = config.OMLX_BASE_URL if config.VLM_BACKEND == "omlx" else config.OLLAMA_BASE_URL
        try:
            urllib.request.urlopen(vlm_url, timeout=3)
            print(f"  VLM:       {config.VLM_BACKEND} @ {vlm_url} (reachable)")
        except Exception:
            print(f"  VLM:       {config.VLM_BACKEND} @ {vlm_url} (NOT reachable)")

        # OpenClaw endpoint
        try:
            urllib.request.urlopen(config.OPENCLAW_BASE_URL, timeout=3)
            print(f"  OpenClaw:  {config.OPENCLAW_BASE_URL} (reachable)")
        except Exception:
            print(f"  OpenClaw:  {config.OPENCLAW_BASE_URL} (NOT reachable)")

    else:
        print("Use: reel-scout config {show|check}")


if __name__ == "__main__":
    main()
