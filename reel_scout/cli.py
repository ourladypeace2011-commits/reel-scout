from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

from . import __version__, batch, config, mcp_install, skill_install
from .utils import paths as media_paths
from .utils.stderr import force_utf8_stdio


def main(argv: List[str] = None) -> None:
    force_utf8_stdio()
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
    p_crawl.add_argument("--file", "-f", help="File with URLs (one per line; '-' for stdin)")
    p_crawl.add_argument("--channel", metavar="URL",
                         help="Channel/profile page: list its videos and download them")
    p_crawl.add_argument("--playlist", metavar="URL",
                         help="Playlist page: list its videos and download them")
    p_crawl.add_argument("--limit", "-n", type=int, default=30,
                         help="Max videos to take from --channel/--playlist (default: 30)")
    p_crawl.add_argument("--cookies", help="Path to cookies file (for IG)")

    # --- analyze ---
    p_analyze = sub.add_parser("analyze", help="Full pipeline: crawl + transcribe + vision + merge")
    p_analyze.add_argument("urls", nargs="*", help="Video URLs or local file paths")
    p_analyze.add_argument("--file", "-f", help="File with URLs (one per line)")
    p_analyze.add_argument("--resume", action="store_true", help="Resume interrupted batch")
    p_analyze.add_argument("--skip-vision", action="store_true", help="Skip VLM analysis")
    p_analyze.add_argument("--skip-transcribe", action="store_true", help="Skip transcription")
    p_analyze.add_argument("--whisper-backend", help="Whisper backend (faster-whisper, whisper-cpp)")
    p_analyze.add_argument("--vlm-backend", help="VLM backend (omlx, ollama)")
    p_analyze.add_argument("--vlm-model", help="VLM model name")
    p_analyze.add_argument("--keyframe-strategy", help="Keyframe strategy (scene, interval, hybrid)")
    p_analyze.add_argument("--keyframe-max", type=int, help="Max keyframes per video (overrides auto duration budget)")
    p_analyze.add_argument(
        "--force-keyframes", action="store_true",
        help="Re-extract keyframes for clips that already have them. The previous "
             "run's frames and descriptions are kept and marked superseded, not "
             "deleted. Needed when the sampling itself changed but the request "
             "looks the same as last time.")
    p_analyze.add_argument("--resolution", type=int, default=0, help="Upscale keyframes to this long-edge px so the VLM can read small on-screen text (0 = native)")
    p_analyze.add_argument("--start", type=float, default=0.0, help="Focus window start (sec); only extract keyframes from [start,end]")
    p_analyze.add_argument("--end", type=float, default=0.0, help="Focus window end (sec); 0 = clip end")
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

    # --- note / group (user annotations) ---
    p_note = sub.add_parser(
        "note", help="Annotate a video: what it's for, which group, starred or not")
    p_note.add_argument("video", help="URL, video id, or unique id prefix")
    p_note.add_argument("--text", help="Set the note (empty string clears it)")
    p_note.add_argument("--group", help="File under this group (name or id)")
    p_note.add_argument("--new-group", action="store_true",
                        help="Create --group if it does not exist yet")
    p_note.add_argument("--no-group", action="store_true", help="Remove the grouping")
    star = p_note.add_mutually_exclusive_group()
    star.add_argument("--star", dest="star", action="store_true", default=None)
    star.add_argument("--unstar", dest="star", action="store_false")
    p_note.add_argument("--json", action="store_true", help="Print the annotation as JSON")

    p_group = sub.add_parser("group", help="Manage the annotation groups")
    p_group_sub = p_group.add_subparsers(dest="group_cmd")
    p_group_sub.add_parser("list", help="List groups and how many videos each holds")
    p_group_add = p_group_sub.add_parser("add", help="Create a group")
    p_group_add.add_argument("name")
    p_group_rename = p_group_sub.add_parser("rename", help="Rename a group")
    p_group_rename.add_argument("group", help="Group id or current name")
    p_group_rename.add_argument("name", help="New name")
    p_group_rm = p_group_sub.add_parser(
        "rm", help="Delete a group (notes and stars are kept, only the filing is cleared)")
    p_group_rm.add_argument("group", help="Group id or name")

    p_mark = sub.add_parser(
        "mark", help="Mark a moment on a clip's timeline: --at <sec> --label <text>")
    p_mark.add_argument("video", help="URL, video id, or unique id prefix")
    p_mark.add_argument("--at", type=float, metavar="SEC",
                        help="Seconds into the clip")
    p_mark.add_argument("--label", help="One line, shown on the timeline")
    p_mark.add_argument("--note", help="Why this second (a sentence, not a document)")
    p_mark.add_argument("--list", dest="mark_list", action="store_true",
                        help="List this clip's marks (also what happens with no "
                             "other action)")
    p_mark.add_argument("--rm", dest="mark_rm", type=int, metavar="ID",
                        help="Delete one mark by id")
    p_mark.add_argument("--import", dest="mark_import", metavar="FILE",
                        help='Replace this --source\'s marks from a JSON file '
                             '({"marks":[{"t":7.0,"label":"..."}]}); \'-\' reads stdin')
    p_mark.add_argument("--clear", dest="mark_clear", action="store_true",
                        help="Delete this clip's marks (narrow it with --source)")
    p_mark.add_argument("--source", default=None,
                        help="Name the writer, so a re-import replaces only its own "
                             "rows and never the hand-typed ones")
    p_mark.add_argument("--json", action="store_true", help="Print as JSON")

    # --- view ---
    p_view = sub.add_parser(
        "view", help="Serve the local library viewer (analysis read-only; notes save)")
    p_view.add_argument("--host", default="127.0.0.1")
    p_view.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    p_view.add_argument("--no-open", dest="open_browser", action="store_false",
                        help="Don't auto-open the browser")

    # --- inspect ---
    p_inspect = sub.add_parser(
        "inspect",
        help="Interactive single-clip inspector web app (player + waveform + "
             "filmstrip + transcript, all time-synced)")
    p_inspect.add_argument("video", help="Video id (exact or unique prefix)")
    p_inspect.add_argument("--host", default="127.0.0.1")
    p_inspect.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    p_inspect.add_argument("--no-open", dest="open_browser", action="store_false",
                           help="Don't auto-open the browser")

    # --- export ---
    p_size = sub.add_parser("shot-size", help="Label each shot with a shot size")
    p_size.add_argument("video", help="Video id or unique prefix")
    p_size.add_argument("--model", default="qwen2.5vl:7b",
                        help="VLM to ask (ollama). Stamped on every stored row.")
    p_size.add_argument("--base-url", default=None, help="ollama base URL")
    p_size.add_argument("--from-json", default=None,
                        help='Supply labels instead of asking a model: {"<keyframe id>": "CU"}')

    p_export = sub.add_parser("export", help="Export analyses")
    p_export.add_argument("--format", choices=["json", "csv", "html", "bundle", "skeleton", "storyboard"],
                          default="json")
    p_export.add_argument("--output", "-o", default="./export")
    p_export.add_argument("--video", help="Single video id (html/bundle: exact or unique prefix)")
    p_export.add_argument("--cjk-font", dest="cjk_font", default="",
                          help="bundle: Noto Sans TC .ttf to subset for Chinese "
                               "(else falls back to the reader's system face)")
    p_export.add_argument("--with-marks", dest="with_marks", action="store_true",
                          help="Include timeline marks in the exported bundle. "
                               "Off by default: marks are working state, not "
                               "something a reader should receive.")
    p_export.add_argument("--max-mb", dest="max_mb", type=float, default=25.0,
                          help="bundle: skip reels whose video exceeds this (default 25)")

    # --- score ---
    p_score = sub.add_parser("score", help="Score a video using LLM analysis")
    p_score.add_argument("video_id", help="Video ID to score")
    p_score.add_argument("--backend", help="LLM backend (omlx, ollama, openclaw)")

    # --- ingest (agent-produced analysis, no local model required) ---
    p_ingest = sub.add_parser(
        "ingest", help="Write agent-produced vision/score JSON back into the DB")
    p_ing_sub = p_ingest.add_subparsers(dest="ingest_command")

    p_ing_vision = p_ing_sub.add_parser("vision", help="Frame descriptions from an agent")
    p_ing_vision.add_argument("video_id", help="Video ID the frames belong to")
    p_ing_vision.add_argument("--from-json", dest="from_json", required=True,
                              help="JSON file, or - to read stdin")
    p_ing_vision.add_argument("--model", default="",
                              help="Model that produced it (stamped as agent:<model>)")

    p_ing_analysis = p_ing_sub.add_parser(
        "analysis", help="Structured analysis from an agent (merge_analysis's shape)")
    p_ing_analysis.add_argument("video_id", help="Video ID the analysis is for")
    p_ing_analysis.add_argument("--from-json", dest="from_json", required=True,
                                help="JSON file, or - to read stdin")
    p_ing_analysis.add_argument("--model", default="",
                                help="Model that produced it (stamped as agent:<model>)")

    p_ing_score = p_ing_sub.add_parser("score", help="Craft score from an agent")
    p_ing_score.add_argument("video_id", help="Video ID to score")
    p_ing_score.add_argument("--from-json", dest="from_json", required=True,
                             help="JSON file, or - to read stdin")
    p_ing_score.add_argument("--model", default="",
                             help="Model that produced it (stamped as agent:<model>)")

    # --- compare ---
    p_compare = sub.add_parser("compare", help="Compare analyzed videos side by side")
    p_compare.add_argument("video_ids", nargs="+", help="Video IDs (exact or unique prefix)")
    p_compare.add_argument("--json", action="store_true", help="Emit JSON instead of a table")

    # --- research ---
    p_research = sub.add_parser("research", help="Competitor research across channels → aggregate")
    p_research.add_argument("--niche", required=True, help="Niche label for the report")
    p_research.add_argument("--channels", nargs="+", required=True, help="Channel/profile URLs")
    p_research.add_argument("--depth", type=int, default=20, help="Videos per channel (default 20)")
    p_research.add_argument("--llm-backend", help="LLM backend (omlx, ollama, openclaw)")
    p_research.add_argument("--no-analyze", dest="analyze", action="store_false",
                            help="Skip crawl+analyze; aggregate only what's already in the DB")
    p_research.add_argument("--json", action="store_true", help="Emit the aggregate as JSON")
    p_research.add_argument("--out", help="Write a synthesized markdown report to this path")

    # --- stats ---
    p_stats = sub.add_parser("stats", help="Corpus statistics (tag distributions + score aggregates)")
    p_stats.add_argument("--channel", help="Scope to one channel (matches videos.uploader substring)")
    p_stats.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    p_stats.add_argument("--csv", help="Write long-format CSV to this path")

    p_patterns = sub.add_parser(
        "patterns",
        help="Per-channel patterns (length, hook/CTA/structure mix, high-vs-low, cadence)")
    p_patterns.add_argument(
        "--channel", required=True,
        help="Channel to analyze (matches videos.uploader substring)")
    p_patterns.add_argument("--json", action="store_true", help="Emit JSON instead of a table")

    p_inspire = sub.add_parser(
        "inspire", help="Generate a fresh variant from a high-scoring video (LLM)")
    p_inspire.add_argument(
        "--based-on", required=True, dest="based_on",
        help="Video id or unique prefix to base the variant on")
    p_inspire.add_argument("--angle", default="", help="Optional twist/angle for the variant")
    p_inspire.add_argument("--json", action="store_true", help="Emit JSON instead of a table")

    p_track = sub.add_parser(
        "track", help="Record your video's real metrics + structural diff vs the top corpus")
    p_track.add_argument(
        "--my-video", required=True, dest="my_video",
        help="Your video: a URL already in the DB, or a video id / unique prefix")
    p_track.add_argument("--views", type=int, default=None)
    p_track.add_argument("--likes", type=int, default=None)
    p_track.add_argument("--comments", type=int, default=None)
    p_track.add_argument("--notes", default=None, help="Free-text note")
    p_track.add_argument("--json", action="store_true", help="Emit JSON instead of a table")

    # --- db ---
    # --- batch (a doc full of links -> one bundle each) ---
    p_batch = sub.add_parser(
        "batch", help="Analyze every reel listed in a Google Doc/Sheet, file or stdin")
    p_batch_src = p_batch.add_mutually_exclusive_group(required=True)
    p_batch_src.add_argument("--doc", help="Google Doc/Sheet URL (or any text/CSV URL)")
    p_batch_src.add_argument("--file", dest="src_file", help="Local .txt/.csv")
    p_batch_src.add_argument("--stdin", action="store_true", help="Read the list from stdin")
    p_batch.add_argument("--out", default=os.path.expanduser("~/reel-scout-batch"),
                         help="Output root (default: %(default)s)")
    p_batch.add_argument("--mode", choices=list(batch.MODES),
                         help="full | agent | transcript (see docs); asked for when "
                              "no local VLM is reachable")
    p_batch.add_argument("--dry-run", dest="dry_run", action="store_true",
                         help="Show what was parsed and planned; analyze nothing")
    p_batch.add_argument("--limit", type=int, default=0, help="Only the first N entries")
    p_batch.add_argument("--max-mb", dest="batch_max_mb", default="25",
                         help="Skip reels whose video exceeds this (default 25)")
    p_batch.add_argument("--no-score", dest="batch_score", action="store_false",
                         default=True,
                         help="Skip the scoring step (mode=full scores each reel by default)")
    p_batch.add_argument("--verbose", action="store_true", help="Echo each sub-command")

    # --- skill (install the agent-facing half) ---
    p_skill = sub.add_parser(
        "skill", help="Install the Claude skill (SKILL.md, /scout, prompt pack)")
    p_skill_sub = p_skill.add_subparsers(dest="skill_command")

    p_skill_install = p_skill_sub.add_parser("install", help="Copy the skill into ~/.claude/skills")
    p_skill_install.add_argument("--dest", default=skill_install.DEFAULT_DEST,
                                 help="Destination (default: %(default)s)")
    p_skill_install.add_argument("--force", action="store_true",
                                 help="Overwrite a non-empty destination")

    p_skill_sub.add_parser("path", help="Show where the skill assets are read from")

    # --- mcp ---
    p_mcp = sub.add_parser(
        "mcp", help="Register the MCP server with Claude Desktop / Claude Code")
    p_mcp_sub = p_mcp.add_subparsers(dest="mcp_command")

    p_mcp_install = p_mcp_sub.add_parser(
        "install", help="Write the MCP server config so a client can launch it")
    p_mcp_install.add_argument(
        "--client", choices=list(mcp_install.CLIENTS) + ["both"],
        default=mcp_install.CLAUDE_DESKTOP)
    p_mcp_install.add_argument(
        "--data", default="",
        help="REEL_SCOUT_DATA to pin (default: this directory's data dir, made absolute)")
    p_mcp_install.add_argument("--name", default=mcp_install.DEFAULT_SERVER_NAME)
    p_mcp_install.add_argument(
        "--path", default="",
        help="Write to this file instead (e.g. ./.mcp.json for a project-scoped config)")
    p_mcp_install.add_argument("--force", action="store_true",
                               help="Overwrite an existing reel-scout entry")
    p_mcp_install.add_argument("--dry-run", action="store_true",
                               help="Print the JSON that would be written and stop")

    p_mcp_sub.add_parser("path", help="Show where MCP config lives and what is configured")

    p_db = sub.add_parser("db", help="Database operations")
    p_db_sub = p_db.add_subparsers(dest="db_command")
    p_db_sub.add_parser("stats", help="Show database stats")
    p_db_sub.add_parser("reset", help="Reset database (destructive)")
    p_db_sub.add_parser("migrate", help="Run pending migrations")
    p_db_norm = p_db_sub.add_parser(
        "normalize-paths",
        help="Rewrite stored media paths to the portable data-root-relative form",
    )
    p_db_norm.add_argument("--dry-run", action="store_true",
                           help="Report what would change without writing")
    p_db_backfill = p_db_sub.add_parser(
        "backfill-text",
        help="Recover on-screen text from frames a pre-fix VLM backend described",
    )
    p_db_backfill.add_argument(
        "--dry-run", action="store_true", help="Report what would be filled; write nothing"
    )
    p_db_shots = p_db_sub.add_parser(
        "backfill-shots",
        help="Compute the shot table for analyzed clips that predate schema v14",
    )
    p_db_shots.add_argument(
        "--dry-run", action="store_true", help="Report what would be filled; write nothing"
    )
    p_db_shots.add_argument(
        "--limit", type=int, default=0, help="Stop after N clips (0 = all)"
    )
    p_db_invalid = p_db_sub.add_parser(
        "check-invalid",
        help="Find stored videos whose media never processed (0 keyframes but a "
             "transcript) and would otherwise score as a real 0.0",
    )
    p_db_invalid.add_argument(
        "--apply", action="store_true",
        help="Mark the listed videos status=invalid. Nothing is ever deleted; "
             "the media, transcript and any scores stay on disk.",
    )

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
        "inspect": _cmd_inspect,
        "view": _cmd_view,
        "score": _cmd_score,
        "ingest": _cmd_ingest,
        "batch": _cmd_batch,
        "skill": _cmd_skill,
        "mcp": _cmd_mcp,
        "compare": _cmd_compare,
        "stats": _cmd_stats,
        "patterns": _cmd_patterns,
        "inspire": _cmd_inspire,
        "track": _cmd_track,
        "note": _cmd_note,
        "group": _cmd_group,
        "mark": _cmd_mark,
        "shot-size": _cmd_shot_size,
        "research": _cmd_research,
        "db": _cmd_db,
        "config": _cmd_config,
    }
    # A handler that returns a number is telling the shell the work did not all
    # land. Everything else returns None and exits 0, exactly as before -- the
    # four handlers that already raise SystemExit themselves are untouched.
    code = handlers[args.command](args)
    if code:
        raise SystemExit(code)


def _read_url_lines(path: str) -> List[str]:
    """Read URL lines from a file, or from stdin when path is '-'.

    The `browse --urls-only | crawl --file -` pipe advertised in browse's output
    needs the '-' case; plain open('-') raises FileNotFoundError.
    """
    if path == "-":
        return sys.stdin.read().splitlines()
    with open(path, "r", encoding="utf-8") as f:
        return f.read().splitlines()


def _collect_urls(args) -> List[str]:
    urls = list(args.urls) if args.urls else []
    path = getattr(args, "file", None)
    if path:
        for line in _read_url_lines(path):
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def _expand_listing(url: str, limit: int, require_profile: bool) -> List[str]:
    """Expand a channel/playlist URL into the video URLs it lists.

    Raises ValueError with a user-facing message; the caller prints it. The
    profile check runs after get_crawler so an unsupported *platform* reports as
    such, and browse's NotImplementedError surfaces for platforms that have no
    listing support (TikTok) rather than being masked as "not a profile URL".
    """
    from .crawl import get_crawler, is_profile_url

    crawler = get_crawler(url)  # ValueError for unsupported platforms
    if require_profile and not is_profile_url(url):
        raise ValueError(
            "not a channel/profile URL: {}\n"
            "  Use --playlist for playlists, or pass video URLs directly.".format(url)
        )
    try:
        entries = crawler.browse(url, limit=limit)
    except NotImplementedError as e:
        raise ValueError(str(e))
    # v1 forwards URLs only. browse already carries title/uploader/duration, so
    # download() re-fetches them via yt-dlp --dump-json; threading VideoMeta
    # through download()'s URL-shaped signature is a refactor, not a freebie.
    return [e.url for e in entries if e.url]


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

    if args.channel and args.playlist:
        print("Error: use --channel or --playlist, not both.")
        return

    if args.cookies:
        os.environ["IG_COOKIES_FILE"] = args.cookies

    urls = _collect_urls(args)

    listing = args.channel or args.playlist
    if listing:
        try:
            found = _expand_listing(
                listing, args.limit, require_profile=bool(args.channel)
            )
        except ValueError as e:
            print("Error: {}".format(e))
            return
        except Exception as e:
            print("Error listing {}: {}".format(listing, e))
            return
        if not found:
            print("No videos found at {}".format(listing))
            return
        print("Found {} videos at {}".format(len(found), listing))
        urls.extend(found)

    if not urls:
        print(
            "No URLs provided. Use: reel-scout crawl <url>, --file urls.txt, "
            "--channel <url>, or --playlist <url>"
        )
        return

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


def _cmd_analyze(args):
    from .analyze.pipeline import PipelineOptions, run

    urls = _collect_urls(args)
    if not urls and not args.resume:
        print("No URLs provided. Use: reel-scout analyze <url> or --file urls.txt")
        return 1

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
        force_keyframes=getattr(args, "force_keyframes", False),
        resolution=args.resolution,
        start_sec=args.start,
        end_sec=args.end,
    )
    # `batch` learned this already; `analyze` had the identical hole and kept
    # exiting 0 with every item errored, which is how a wrapper script can
    # cheerfully carry on with nothing analysed.
    return 1 if run(urls, options) else 0


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
            result = transcriber.transcribe(media_paths.resolve_media_path(v["file_path"]))
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


#: `show` prints every keyframe, but a 5-minute clip can carry 170 shots and a
#: wall of spans buries the sections under it. Truncate, and say how many were
#: hidden — a silently shortened list is the thing this repo keeps paying for.
_SHOW_MAX_SHOTS = 20

#: VO and on-screen text are shown inline under their shot, so they get clipped
#: rather than allowed to wrap and swallow the table.
_SHOW_TEXT_WIDTH = 78


def _clip(text: str, width: int) -> str:
    """Truncate to `width`, saying so. A silently shortened line reads as the
    whole line, which is how a reader ends up quoting half a sentence."""
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width - 1] + "…"


def _cmd_shot_size(args) -> None:
    from . import db
    from .compare import resolve_ref
    from .label_shots import label_video

    conn = db.init_db()
    video_id, matches = resolve_ref(conn, args.video)
    if video_id is None:
        print("Video not found: %s" % args.video)
        if matches:
            print("  did you mean: %s" % ", ".join(m[:12] for m in matches[:5]))
        conn.close()
        return
    supplied = None
    if args.from_json:
        with open(args.from_json, encoding="utf-8") as f:
            supplied = {str(k): v for k, v in (json.load(f) or {}).items()}
    tally = label_video(conn, video_id, args.model,
                        base_url=args.base_url, supplied=supplied)
    print("shot sizes: %d labelled, %d UNKNOWN, %d refused, %d skipped, %d missing"
          % (tally["labelled"], tally["unknown"], tally["refused"],
             tally["skipped"], tally["missing"]))
    if tally["missing"]:
        print("  missing = the keyframe file could not be read (paths in this DB "
              "are relative to the checkout that wrote them) — not a model problem",
              file=sys.stderr)
    if tally["unknown"]:
        # Stored on purpose: "asked, no answer" and "never asked" are different
        # states and only one is worth re-running.
        print("  UNKNOWN is stored, not dropped — a title card or a graphic has "
              "no subject to frame against")
    if tally["refused"]:
        print("  refused = the model answered something that was not one of the "
              "codes; it will answer the same way on a retry", file=sys.stderr)
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

    # A silent clip used to print the header and then nothing -- and the header
    # named a language, which for most of these is Whisper's guess at silence.
    # Say there are no words instead of implying some were heard.
    words = (transcript["text_full"] or "").strip() if transcript else ""
    if words:
        print(f"\n--- Transcript ({transcript['language']}) ---")
        print(words[:500])
    else:
        print("\n--- Transcript ---")
        print("(no transcript — music-only / no narration; the score draws on "
              "the visual layer and the measured rhythm alone)")

    # Keyframes are listed with their ids because that is the only way to address
    # a specific frame from outside (`ingest vision --from-json`), and an agent
    # supplying the visual layer needs both the id and the path it should read.
    keyframes = db.get_keyframes_with_descriptions(conn, args.video_id)
    if keyframes:
        described = sum(1 for k in keyframes if k["description"])
        print(f"\n--- Keyframes ({described}/{len(keyframes)} described) ---")
        for k in keyframes:
            mark = " " if k["description"] else "*"
            print(f" {mark}[id {k['id']:>5}  #{k['frame_index']:<3} {k['timestamp_sec']:>6.1f}s] "
                  f"{k['file_path']}")
            if k["description"]:
                print(f"        {k['description'][:100]}")
        if described < len(keyframes):
            print("  * = no description yet")

    # Shots (schema v14). The binding to keyframes is derived here rather than
    # stored: `save_shots` replaces a clip's spans on every re-analyze, so a
    # persisted shot_id would point at a deleted row without anything raising.
    shot_rows = db.get_shots(conn, args.video_id)
    if shot_rows:
        from .shots import Shot, bind_frames_to_shots, bind_spans_to_shots
        spans = [Shot(index=r["idx"], start_sec=r["start_sec"],
                      end_sec=r["end_sec"], dur_sec=r["dur_sec"]) for r in shot_rows]
        per_shot, unassigned = bind_frames_to_shots(spans, keyframes or [])

        # VO is a span and can straddle a cut, so a line lands under every shot
        # it covers: a storyboard cut asks "what is said over this shot", and
        # filing a twelve-second line only under the shot it began in leaves the
        # next two reading as silent — wrong information, not missing.
        vo_segments = []
        if transcript and transcript["segments_json"]:
            try:
                vo_segments = json.loads(transcript["segments_json"]) or []
            except ValueError:
                vo_segments = []
        vo_by_shot, _vo_out = bind_spans_to_shots(spans, vo_segments)
        # On-screen text is a point, same shape as a keyframe.
        sup_by_shot, _sup_out = bind_frames_to_shots(
            spans, db.get_ocr_captions(conn, args.video_id) or [])

        with_frames = sum(1 for s in per_shot if s.frames)
        with_vo = sum(1 for v in vo_by_shot if v)
        with_sup = sum(1 for s in sup_by_shot if s.frames)
        print("\n--- Shots (%d spans · %d with a keyframe · %d with VO · "
              "%d with on-screen text) ---"
              % (len(per_shot), with_frames, with_vo, with_sup))
        for s in per_shot[:_SHOW_MAX_SHOTS]:
            rep = s.representative
            rep_txt = ("frame id %d @ %.1fs" % (rep["id"], rep["timestamp_sec"])
                       if rep is not None else "—")
            print("  #%-3d %7.2f-%-7.2f (%5.2fs)  %s"
                  % (s.shot.index, s.shot.start_sec, s.shot.end_sec,
                     s.shot.dur_sec, rep_txt))
            vo = " / ".join((seg.get("text") or "").strip()
                            for seg in vo_by_shot[s.shot.index])
            if vo.strip(" /"):
                print("        vo : %s" % _clip(vo, _SHOW_TEXT_WIDTH))
            sup = " / ".join((c["text"] or "").strip()
                             for c in sup_by_shot[s.shot.index].frames)
            if sup.strip(" /"):
                print("        sup: %s" % _clip(sup, _SHOW_TEXT_WIDTH))
        if len(per_shot) > _SHOW_MAX_SHOTS:
            print("  ... %d more not shown" % (len(per_shot) - _SHOW_MAX_SHOTS))
        if unassigned:
            # Never silent: a frame outside every span means the frame table and
            # the shot table were built from different measurements of the clip.
            print("  !! %d keyframe(s) fall outside every span — frames and "
                  "shots disagree about this clip" % len(unassigned))

    score = db.get_score(conn, args.video_id)
    if score:
        src = score["model_used"] or "(unknown)"
        print(f"\n--- Score ({src}) ---")
        print(f"  Overall: {score['overall']:.2f}   Hook: {score['hook_strength']:.1f}   "
              f"Visual: {score['visual_storytelling']:.1f}   "
              f"Pacing: {score['pacing']:.1f}   Structure: {score['structure']:.1f}")
        if score["reasoning"]:
            print(f"  {score['reasoning']}")

    if analysis:
        print(f"\n--- Analysis ---")
        full = json.loads(analysis["full_json"]) if analysis["full_json"] else {}
        print(json.dumps(full, ensure_ascii=False, indent=2))

    conn.close()


def _cmd_inspect(args) -> None:
    from . import db, inspector
    from .compare import resolve_ref

    conn = db.init_db()
    video_id, matches = resolve_ref(conn, args.video)
    conn.close()
    if video_id is None:
        if matches:
            print("Ambiguous '%s' — matches %d videos: %s"
                  % (args.video, len(matches), ", ".join(matches[:8])))
        else:
            print("No video matches '%s'." % args.video)
        sys.exit(1)

    inspector.serve(video_id, host=args.host, port=args.port,
                    open_browser=args.open_browser)


def _cmd_view(args) -> None:
    from . import db, viewer

    config.ensure_dirs()
    db.init_db().close()  # ensure schema/migrations, then serve per-request conns
    viewer.serve(host=args.host, port=args.port, open_browser=args.open_browser)


def _cmd_export(args) -> None:
    from . import db
    from .export.json_export import export_csv, export_html, export_json

    config.ensure_dirs()
    conn = db.init_db()

    if args.format == "json":
        count = export_json(conn, args.output)
        print(f"Exported {count} analyses to {args.output}/")
    elif args.format == "skeleton":
        from .export.skeleton import export_skeleton
        video_id = None
        if getattr(args, "video", None):
            from .compare import resolve_ref
            video_id, _ = resolve_ref(conn, args.video)
            if video_id is None:
                print(f"Video not found: {args.video}")
                conn.close()
                return
        count = export_skeleton(conn, args.output, video_id=video_id)
        print(f"Wrote {count} skeleton JSON file(s) to {args.output}/")
    elif args.format == "storyboard":
        from .export.storyboard import export_storyboard
        video_id = None
        if getattr(args, "video", None):
            from .compare import resolve_ref
            video_id, _ = resolve_ref(conn, args.video)
            if video_id is None:
                print(f"Video not found: {args.video}")
                conn.close()
                return
        count = export_storyboard(conn, args.output, video_id=video_id)
        print(f"Wrote {count} storyboard project(s) to {args.output}/")
        if count:
            print("  every cut is marked REF: with its source URL and timecode — "
                  "these are teardowns of someone else's footage, not treatments")
    elif args.format == "csv":
        count = export_csv(conn, args.output)
        print(f"Exported {count} rows to {args.output}")
    elif args.format == "html":
        video_id = None
        if getattr(args, "video", None):
            from .compare import resolve_ref
            video_id, _ = resolve_ref(conn, args.video)
            if video_id is None:
                print(f"Video not found: {args.video}")
                conn.close()
                return
        path = export_html(conn, args.output, video_id=video_id)
        print(f"Wrote self-contained viewer to {path}")
    elif args.format == "bundle":
        from .bundle import build_bundle
        ids = None
        if getattr(args, "video", None):
            from .compare import resolve_ref
            vid, _ = resolve_ref(conn, args.video)
            if vid is None:
                print(f"Video not found: {args.video}")
                conn.close()
                return
            ids = [vid]
        summary = build_bundle(conn, args.output, video_ids=ids,
                               cjk_ttf=args.cjk_font,
                               max_bytes=int(args.max_mb * 1048576),
                               with_marks=args.with_marks)
        for e in summary["written"]:
            print("  %-42s %6.1f MB" % (e["file"], e["bytes"] / 1048576.0))
        for e in summary["skipped"]:
            print("  SKIPPED %s — %s" % (e["title"][:40], e["reason"]))
        print("Wrote %d self-contained reel(s) + index.html to %s/ (%.1f MB total)"
              % (len(summary["written"]), summary["out_dir"],
                 summary["total_bytes"] / 1048576.0))

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
        print(f"  Visual:     {existing['visual_storytelling']:.1f}")
        print(f"  Pacing:     {existing['pacing']:.1f}")
        print(f"  Structure:  {existing['structure']:.1f}")
        print(f"  Reasoning:  {existing['reasoning']}")
        conn.close()
        return

    try:
        score = score_video(conn, args.video_id, llm_backend=args.backend)
        print(f"Score for: {video['title'] or '(untitled)'}")
        print(f"  Overall:    {score.overall:.1f}")
        print(f"  Hook:       {score.hook_strength:.1f}")
        print(f"  Visual:     {score.visual_storytelling:.1f}")
        print(f"  Pacing:     {score.pacing:.1f}")
        print(f"  Structure:  {score.structure:.1f}")
        print(f"  Reasoning:  {score.reasoning}")
    except ValueError as e:
        print(f"Error: {e}")

    conn.close()


def _cmd_ingest(args) -> None:
    import json
    import sys

    from . import db, ingest

    if not getattr(args, "ingest_command", None):
        print("Usage: reel-scout ingest {vision|analysis|score} <video_id> "
              "--from-json <path|->")
        return

    raw = sys.stdin.read() if args.from_json == "-" else open(
        args.from_json, encoding="utf-8").read()
    try:
        payload = json.loads(raw)
    except ValueError as e:
        print(f"Error: input is not valid JSON: {e}")
        return

    config.ensure_dirs()
    conn = db.init_db()
    try:
        if not db.get_video(conn, args.video_id):
            print(f"Video not found: {args.video_id}")
            return

        if args.ingest_command == "vision":
            written, warnings = ingest.ingest_vision(
                conn, args.video_id, payload, model=args.model)
            print(f"Wrote {written} frame description(s) for {args.video_id}")
            for w in warnings:
                print(f"  warning: {w}")
        elif args.ingest_command == "analysis":
            data = ingest.ingest_analysis(
                conn, args.video_id, payload, model=args.model)
            print(f"Analysis for: {args.video_id}  (source: {data['_source']})")
            print(f"  {data.get('summary', '')[:160]}")
            hook = data.get("hook") or {}
            style = data.get("style") or {}
            print(f"  opening: {hook.get('opening_type', '—')}   "
                  f"cta: {hook.get('cta_type', '—')}   "
                  f"structure: {data.get('content_structure', '—')}   "
                  f"format: {style.get('format', '—')}")
        else:
            score = ingest.ingest_score(
                conn, args.video_id, payload, model=args.model)
            print(f"Score for: {args.video_id}  (source: {score.model_used})")
            print(f"  Overall:    {score.overall:.1f}   <- recomputed from the dimensions")
            print(f"  Hook:       {score.hook_strength:.1f}")
            print(f"  Visual:     {score.visual_storytelling:.1f}")
            print(f"  Pacing:     {score.pacing:.1f}")
            print(f"  Structure:  {score.structure:.1f}")
    except ValueError as e:
        print(f"Error: {e}")
    finally:
        conn.close()


def _cmd_batch(args) -> Optional[int]:
    """Analyze a list of reels. Returns 1 when the run did not fully land.

    The exit code answers one question: did this command produce what it was
    asked for? Not "whose fault was it". Every path below that prints an
    explanation and produces no bundles returns 1, because a shell reading exit
    0 is being told the work is done -- and a batch that silently reports
    success over a hole is the entire family of bugs this release is about.

    Two paths deliberately stay 0. `--dry-run` succeeded: the listing *is* the
    deliverable. And a run whose items all landed but left `pending_completion`
    is the designed terminal state of `--mode agent`, which SKILL.md recommends;
    painting it red would make the recommended path permanently look broken.
    """
    if args.doc:
        try:
            text = batch.fetch(args.doc)
        except (RuntimeError, OSError) as e:
            print(f"Error: {e}")
            return 1
    elif args.src_file:
        with open(args.src_file, encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    entries = batch.parse_rows(text)
    if args.limit:
        entries = entries[:args.limit]
    if not entries:
        # The second line is the same remedy the fetch failure above prints,
        # which is the tell: this state is usually a fetch that succeeded into a
        # sign-in page rather than a genuinely empty list.
        print("No Instagram / TikTok / YouTube Shorts links found in that source.")
        print("  If it's a Google doc, check sharing is 'Anyone with the link - Viewer'.")
        return 1

    print("Found %d:" % len(entries))
    for i, (label, url) in enumerate(entries, 1):
        print("  %2d. %-14s %s" % (i, label or "(unlabelled)", url))

    caps = batch.probe()
    mode, msg = batch.resolve_mode(args.mode, caps)
    print("\nVLM reachable: %s   Whisper: %s"
          % ("yes" if caps["vlm"] else "no", "yes" if caps["whisper"] else "no"))
    if mode is None:
        # One of these messages says "That is a choice, not an error". It is
        # right about blame and says nothing about state: nothing was analyzed.
        # Exiting 0 here would tell a wrapper the batch is done, on the exact
        # path a first-time user hits. The extra line keeps the message and the
        # status from contradicting each other.
        print("\n" + msg)
        print("\n(Nothing was analyzed, so this exits non-zero.)")
        return 1
    print("Mode: %s" % mode)

    if args.dry_run:
        print("\n--dry-run: nothing was analyzed. Bundles would land in %s/<label>/"
              % args.out)
        return None

    result = batch.run_batch(entries, args.out, mode,
                             max_mb=args.batch_max_mb, verbose=args.verbose,
                             score=getattr(args, "batch_score", True))

    print("\n---")
    print("%d/%d exported to %s" % (len(result["done"]), len(entries), args.out))
    if result["pending_completion"]:
        print("\n%d still need a visual layer (mode=agent). For each, read the "
              "keyframes and write your findings back:" % len(result["pending_completion"]))
        for e in result["pending_completion"]:
            print("  reel-scout ingest vision %s --from-json - --model <you>" % e["video_id"])
        print("  (then `ingest score`, and re-export to refresh the bundle — "
              "see SKILL.md Step 2b)")
    if result["failed"]:
        print("\n%d failed:" % len(result["failed"]))
        for e in result["failed"]:
            print("  %-14s %s\n                 %s" % (e["label"], e["url"], e["reason"]))
    if result.get("not_attempted"):
        print("\n%d never attempted (the run stopped early):"
              % len(result["not_attempted"]))
        for e in result["not_attempted"]:
            print("  %-14s %s" % (e["label"], e["url"]))
    if result["failed"] or result.get("not_attempted"):
        return 1
    return None


def _cmd_skill(args) -> None:
    cmd = getattr(args, "skill_command", None)
    root, which = skill_install.source_root()

    if cmd == "path":
        if which == "missing":
            print("Skill assets: NOT FOUND in this install")
        else:
            print(f"Skill assets ({which}): {root}")
        print(f"Default destination: {os.path.expanduser(skill_install.DEFAULT_DEST)}")
        return

    if cmd != "install":
        print("Usage: reel-scout skill {install|path} [--dest PATH] [--force]")
        return

    try:
        dest, copied = skill_install.install(args.dest, force=args.force)
    except RuntimeError as e:
        print(f"Error: {e}")
        return

    print(f"Installed the reel-scout skill ({which} source) to:")
    print(f"  {dest}")
    print(f"  {', '.join(copied)}")
    print("\nRestart Claude Code to pick it up, then: /scout <video-url>")


def _cmd_mcp(args) -> None:
    cmd = getattr(args, "mcp_command", None)

    if cmd == "path":
        summary = mcp_install.status_summary()
        print("This directory resolves to:")
        videos = summary["here_videos"]
        print("  REEL_SCOUT_DATA: %s   (%s)" % (
            summary["here_data_dir"],
            "no database yet" if videos is None else "%d videos" % videos))
        print("  would install:   %s %s   (%s)" % (
            summary["would_command"], " ".join(summary["would_args"]),
            summary["would_how"]))
        print()
        mismatch = False
        for row in summary["clients"]:
            print("%s:" % row["client"])
            print("  config: %s%s" % (row["path"], "" if row["exists"] else "   (not created yet)"))
            if row["error"]:
                print("  !! %s" % row["error"])
                mismatch = True
                continue
            if not row["configured"]:
                print("  reel-scout: not configured — run `reel-scout mcp install`")
            else:
                print("  command:         %s %s" % (row["command"], " ".join(row.get("args") or [])))
                count = row["configured_videos"]
                print("  REEL_SCOUT_DATA: %s   (%s)" % (
                    row["data_dir"] or "NOT SET",
                    "no database" if count is None else "%d videos" % count))
                if row["command_matches"] is False:
                    print("  !! that command is not the one this install would use — "
                          "you probably reinstalled into a different environment")
                    mismatch = True
                if row["data_dir_matches"] is False:
                    print("  !! that data dir is not the one this directory resolves to — "
                          "the client is reading a different library")
                    mismatch = True
                if not row["data_dir"]:
                    print("  !! REEL_SCOUT_DATA is unset, so the server will resolve "
                          "./data against whatever directory the client launches it from")
                    mismatch = True
            if row["other_servers"]:
                print("  (%d other MCP server(s) in this file, left alone)" % row["other_servers"])
            print()
        if mismatch:
            raise SystemExit(1)
        return

    if cmd != "install":
        print("Usage: reel-scout mcp {install|path} [--client claude-desktop|claude-code|both]")
        return

    if args.dry_run:
        data_dir = mcp_install.resolve_data_dir(args.data)
        entry = mcp_install.server_entry(data_dir)
        targets = ([args.path] if args.path
                   else [mcp_install.client_paths()[c] for c in
                         (mcp_install.CLIENTS if args.client == "both" else [args.client])])
        for target in targets:
            print("would write to %s:" % target)
        print(json.dumps({"mcpServers": {args.name: entry}}, indent=2))
        return

    clients = list(mcp_install.CLIENTS) if args.client == "both" else [args.client]
    installed = []
    for client in clients:
        try:
            installed.append(mcp_install.install(
                client, data_dir=args.data, name=args.name,
                force=args.force, path=args.path))
        except RuntimeError as e:
            print(f"Error: {e}")
            return

    for row in installed:
        print("Configured '%s' in %s:" % (args.name, row["client"]))
        print("  %s" % row["path"])
        print("  command:         %s %s   (%s)" % (
            row["command"], " ".join(row["args"]), row["how"]))
        count = mcp_install._video_count(row["data_dir"])
        print("  REEL_SCOUT_DATA: %s   (%s)" % (
            row["data_dir"],
            "no database yet" if count is None else "%d videos" % count))
        if row["backup"]:
            print("  backup:          %s" % row["backup"])
        print()

    # Load-bearing: both clients only re-read their config on a real relaunch,
    # and "I ran it and nothing happened" is otherwise the first support question.
    print("Fully quit the client (not just close the window) and reopen it,")
    print('then ask: "list my reel-scout videos"')


def _cmd_compare(args) -> None:
    from . import db
    from .compare import build_comparison, format_table

    config.ensure_dirs()
    conn = db.init_db()
    try:
        comparison = build_comparison(conn, args.video_ids)
        if args.json:
            print(json.dumps(comparison, ensure_ascii=False, indent=2))
        else:
            print(format_table(comparison))
    finally:
        conn.close()


def _cmd_research(args) -> None:
    from . import db, research

    config.ensure_dirs()
    conn = db.init_db()
    try:
        report = research.run_research(
            conn, niche=args.niche, channel_urls=args.channels,
            depth=args.depth, llm_backend=args.llm_backend,
            do_analyze=args.analyze,
        )
        if args.out:
            md = research.render_report(report, llm_backend=args.llm_backend)
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(md)
            print("Wrote research report to %s (%d chars)" % (args.out, len(md)))
        elif args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(_format_research_summary(report))
    finally:
        conn.close()


def _format_research_summary(report) -> str:
    lines = ["Research: %s" % report["niche"],
             "=" * 40,
             "Channels: %d | niche videos: %d (analyzed %d)" % (
                 report["channel_count"],
                 report["niche_wide"]["video_count"],
                 report["niche_wide"]["analyzed_count"])]
    for ch in report["channels"]:
        lines.append("\n- %s (%s)" % (ch.get("uploader") or "?", ch["channel_url"]))
        lines.append("  videos=%d analyzed=%d modal_format=%s modal_pacing=%s avg_overall=%s" % (
            ch["video_count"], ch["analyzed_count"],
            ch["modal_format"], ch["modal_pacing"], ch["avg_overall"]))
    nw = report["niche_wide"]
    lines.append("\n-- niche-wide --")
    lines.append("modal_format=%s modal_structure=%s avg_overall=%s" % (
        nw["modal_format"], nw["modal_structure"], nw["avg_overall"]))
    return "\n".join(lines)


def _cmd_stats(args) -> None:
    from . import db, stats as stats_mod

    config.ensure_dirs()
    conn = db.init_db()
    try:
        result = stats_mod.compute_stats(conn, channel=args.channel)
        if args.csv:
            n = stats_mod.write_csv(result, args.csv)
            print("Wrote %d stat rows to %s" % (n, args.csv))
        elif args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(stats_mod.format_stats(result))
    finally:
        conn.close()


def _cmd_track(args) -> None:
    from . import db, track as track_mod

    config.ensure_dirs()
    conn = db.init_db()
    try:
        video_id = track_mod.record_performance(
            conn, args.my_video, views=args.views, likes=args.likes,
            comments=args.comments, notes=args.notes)
        cmp = track_mod.compare_to_corpus(conn, video_id)
        perf = db.get_performance(conn, video_id)
        if args.json:
            print(json.dumps(
                {"performance": dict(perf) if perf else None, "comparison": cmp},
                ensure_ascii=False, indent=2))
        else:
            print(track_mod.format_track(perf, cmp))
    finally:
        conn.close()


def _fmt_annotation(ann: Dict[str, Any]) -> str:
    star = "★" if ann.get("starred") else "☆"
    group = ann.get("group_name") or "—"
    note = ann.get("note") or ""
    return "%s  [%s]  %s" % (star, group, note) if note else "%s  [%s]" % (star, group)


def _cmd_note(args) -> None:
    from . import annotate as ann_mod, db

    config.ensure_dirs()
    conn = db.init_db()
    try:
        video_id = ann_mod.resolve_video(conn, args.video)
        ann = ann_mod.set_annotation(
            conn, video_id,
            note=args.text,
            group=args.group,
            starred=args.star,
            clear_group=args.no_group,
            create_group=args.new_group,
        )
        if args.json:
            print(json.dumps(ann, ensure_ascii=False, indent=2))
        else:
            print("%s  %s" % (video_id, _fmt_annotation(ann)))
    except ann_mod.AnnotateError as exc:
        raise SystemExit("error: %s" % exc)
    finally:
        conn.close()


def _cmd_group(args) -> None:
    from . import annotate as ann_mod, db

    config.ensure_dirs()
    conn = db.init_db()
    try:
        cmd = getattr(args, "group_cmd", None) or "list"
        if cmd == "list":
            groups = ann_mod.list_groups(conn)
            if not groups:
                print("no groups yet — reel-scout group add <name>")
            for g in groups:
                print("%4d  %-30s %d" % (g["id"], g["name"], g["video_count"]))
            return
        if cmd == "add":
            g = ann_mod.add_group(conn, args.name)
            print("added %d  %s" % (g["id"], g["name"]))
            return
        if cmd == "rename":
            gid = ann_mod.resolve_group(conn, args.group)
            g = ann_mod.rename_group(conn, int(gid), args.name)
            print("renamed %d  %s" % (g["id"], g["name"]))
            return
        if cmd == "rm":
            gid = ann_mod.resolve_group(conn, args.group)
            ann_mod.remove_group(conn, int(gid))
            print("removed %s — notes and stars kept" % args.group)
            return
    except ann_mod.AnnotateError as exc:
        raise SystemExit("error: %s" % exc)
    finally:
        conn.close()


def _fmt_mark(m: Dict[str, Any]) -> str:
    src = m.get("source") or "manual"
    tail = "  %s" % m["note"] if m.get("note") else ""
    return "%5d  %7.2fs  %-28s [%s]%s" % (
        m["id"], m["t_sec"], m["label"], src, tail)


def _cmd_mark(args) -> Optional[int]:
    """Marks on one clip's timeline. Returns 1 when the command did not land.

    The exit code is the whole point of the return: `mark` is meant to be driven
    from a generator (`--import` on a file another tool writes), and a writer
    that exits 0 having written nothing is how a broken pipeline stays invisible
    for a week. Every path below that prints an error returns non-zero -- the
    same hole that `batch` and `analyze` had to be fixed for in #68.
    """
    from . import db, marks as marks_mod

    config.ensure_dirs()
    conn = db.init_db()
    try:
        video_id = marks_mod.resolve(conn, args.video)

        if args.mark_rm is not None:
            marks_mod.remove(conn, args.mark_rm, video_id=video_id)
            print("removed mark %d" % args.mark_rm)
            return None

        if args.mark_clear:
            n = marks_mod.clear(conn, video_id, source=args.source)
            scope = ("source %r" % marks_mod.source_name(args.source)) \
                if args.source else "all sources"
            print("removed %d mark(s) from %s (%s)" % (n, video_id, scope))
            return None

        if args.mark_import:
            raw = (sys.stdin.read() if args.mark_import == "-"
                   else open(args.mark_import, encoding="utf-8").read())
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                print("error: %s is not valid JSON: %s" % (args.mark_import, exc))
                return 1
            source = args.source or "import"
            result = marks_mod.import_marks(conn, video_id, payload, source=source)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print("%s  %s: removed %d, added %d"
                      % (video_id, result["source"], result["removed"], result["added"]))
                if not result["duration_checked"]:
                    # Not a warning about this import -- a statement about what
                    # could not be checked, so nobody reads "added 12" as "12
                    # times verified to be inside the clip".
                    print("  (clip has no recorded duration — "
                          "times were not checked against its end)")
            return None

        if args.at is not None:
            m = marks_mod.add(conn, video_id, args.at, args.label,
                              note=args.note,
                              source=args.source or "manual")
            if args.json:
                print(json.dumps(m, ensure_ascii=False, indent=2))
            else:
                print("%s  %s" % (video_id, _fmt_mark(m)))
            return None

        if args.label and args.at is None:
            print("error: --label needs --at <seconds>")
            return 1

        rows = marks_mod.list_for(conn, video_id, source=args.source)
        if args.json:
            print(json.dumps({"video_id": video_id, "marks": rows},
                             ensure_ascii=False, indent=2))
        elif not rows:
            print("no marks on %s yet — reel-scout mark %s --at 7.0 --label \"...\""
                  % (video_id, video_id))
        else:
            for m in rows:
                print(_fmt_mark(m))
        return None
    except marks_mod.MarkError as exc:
        raise SystemExit("error: %s" % exc)
    except OSError as exc:
        print("error: %s" % exc)
        return 1
    finally:
        conn.close()


def _cmd_inspire(args) -> None:
    from . import db, inspire as inspire_mod

    config.ensure_dirs()
    conn = db.init_db()
    try:
        result = inspire_mod.generate_inspiration(conn, args.based_on, angle=args.angle)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(inspire_mod.format_inspiration(result))
    finally:
        conn.close()


def _cmd_patterns(args) -> None:
    from . import db, patterns as patterns_mod

    config.ensure_dirs()
    conn = db.init_db()
    try:
        result = patterns_mod.compute_patterns(conn, channel=args.channel)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(patterns_mod.format_patterns(result))
    finally:
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

    elif args.db_command == "normalize-paths":
        config.ensure_dirs()
        conn = db.init_db()
        changed, missing = db.normalize_media_paths(conn, dry_run=args.dry_run)
        verb = "Would rewrite" if args.dry_run else "Rewrote"
        print("%s %d path(s) to the portable form." % (verb, changed))
        if missing:
            print("Left %d unresolvable path(s) untouched:" % len(missing))
            for row in missing[:20]:
                print("  %s" % row)
            if len(missing) > 20:
                print("  ... and %d more" % (len(missing) - 20))
        conn.close()

    elif args.db_command == "backfill-text":
        from . import ocr

        config.ensure_dirs()
        conn = db.init_db()
        try:
            r = ocr.backfill_from_descriptions(conn, dry_run=args.dry_run)
        finally:
            conn.close()
        verb = "would fill" if args.dry_run else "filled"
        print("Scanned %d frame(s) with a description and no on-screen text."
              % r["scanned"])
        print("  %s %d frame(s) across %d video(s)"
              % (verb, r["filled"], r["videos"]))
        if args.dry_run:
            print("  would write %d caption(s) — re-run without --dry-run to apply"
                  % r["captions"])
        else:
            print("  wrote %d caption(s) for %d video(s)"
                  % (r["captions"], r["videos_with_captions"]))

    elif args.db_command == "backfill-shots":
        from .backfill_shots import backfill

        config.ensure_dirs()
        conn = db.init_db()
        try:
            r = backfill(conn, dry_run=args.dry_run, limit=args.limit or None,
                         progress=True)
        finally:
            conn.close()
        print("clips needing a shot table: %d" % r["candidates"])
        print("  %s %d, skipped %d (media gone), failed %d"
              % ("would fill" if args.dry_run else "filled",
                 r["filled"], r["skipped_no_media"], r["failed"]))
        if r["skipped_no_media"]:
            # Not a failure and not a success: the clip is still analyzed, the
            # media it was measured from is simply not on this machine any more.
            print("  media-gone clips keep their cut count and stay without spans "
                  "— re-crawl to fill them", file=sys.stderr)
        if args.dry_run:
            print("  nothing written — re-run without --dry-run to apply")

    elif args.db_command == "check-invalid":
        from . import validity

        config.ensure_dirs()
        conn = db.init_db()
        try:
            hits = validity.scan(conn)
            if not hits:
                print("No videos match the invalid shape "
                      "(0 keyframes with a non-empty transcript).")
                return
            print("%d video(s) match the invalid shape "
                  "(0 keyframes with a non-empty transcript):\n" % len(hits))
            for h in hits:
                print("  %s  [%s]" % (h["id"], h["platform_id"]))
                print("    title:      %s" % (h["title"] or "—"))
                print("    uploader:   %s" % (h["uploader"] or "—"))
                print("    duration:   %s s" % (h["duration_sec"]
                                                if h["duration_sec"] is not None else "—"))
                print("    keyframes:  %d   transcript: %d chars"
                      % (h["keyframes"], h["transcript_chars"]))
                print("    status:     %s   stored overall score: %s"
                      % (h["status"], h["overall"] if h["overall"] is not None else "—"))
                print("")
            if args.apply:
                marked = 0
                for h in hits:
                    if h["status"] != validity.INVALID_STATUS:
                        validity.mark_invalid(conn, h["id"], h["reason"])
                        marked += 1
                print("Marked %d video(s) status=invalid. Nothing was deleted — the "
                      "media, transcript, keyframe rows and any existing score are "
                      "untouched, and the mark is reversible." % marked)
                print("They are now excluded from `stats` and `patterns`.")
            else:
                print("Reported only; nothing was written. Re-run with --apply to "
                      "mark these status=invalid (still no deletion), or leave them "
                      "as-is if you disagree with the call.")
        finally:
            conn.close()

    else:
        print("Use: reel-scout db "
              "{stats|reset|migrate|normalize-paths|backfill-text|check-invalid|backfill-shots}")


def _probe_cmd(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            first = (r.stdout.strip().split("\n") or [""])[0]
            return True, first or "ok"
        return False, "exit %d" % r.returncode
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _probe_http(url, timeout=3):
    import urllib.request
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True, "%s (reachable)" % url
    except Exception as e:  # noqa: BLE001
        return False, "%s (NOT reachable: %s)" % (url, e)


def _probe_import(module):
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:  # noqa: BLE001 - a broken parent package shouldn't crash the check
        return False


def _run_config_checks():
    """Return [(name, ok, detail)] for every backend the current config selects.

    Only the *configured* backends are probed (VLM/LLM keyed off their backend
    setting; the optional audio/diarize/instagram groups only when enabled), and
    yt-dlp is resolved the same way the runtime resolves it (crawl/ytdlp)."""
    from .crawl import ytdlp
    checks = []

    checks.append(("ffmpeg", *_probe_cmd([config.FFMPEG_BIN, "-version"])))
    # yt-dlp: probe the SAME binary the crawlers use, not a hardcoded "yt-dlp".
    checks.append(("yt-dlp", *_probe_cmd(list(ytdlp.base_cmd()) + ["--version"])))

    if config.WHISPER_BACKEND == "faster-whisper":
        ok = _probe_import("faster_whisper")
        checks.append(("whisper", ok, "faster-whisper %s" % ("installed" if ok else "NOT installed")))
    else:
        ok, _ = _probe_cmd(["whisper", "--help"])
        checks.append(("whisper", ok, "whisper.cpp %s" % ("found" if ok else "NOT found")))

    vlm_url = config.OMLX_BASE_URL if config.VLM_BACKEND == "omlx" else config.OLLAMA_BASE_URL
    checks.append(("VLM (%s)" % config.VLM_BACKEND, *_probe_http(vlm_url)))

    # LLM reachability keyed off LLM_BACKEND (previously never checked as such).
    llm_url = {
        "omlx": config.OMLX_BASE_URL,
        "ollama": config.OLLAMA_BASE_URL,
        "openclaw": config.OPENCLAW_BASE_URL,
    }.get(config.LLM_BACKEND)
    if llm_url:
        checks.append(("LLM (%s)" % config.LLM_BACKEND, *_probe_http(llm_url)))
    else:
        checks.append(("LLM (%s)" % config.LLM_BACKEND, False, "unknown backend"))

    # Optional backends — only probed when configured/enabled.
    if config.PANNS_MODEL_PATH:
        ok = _probe_import("onnxruntime")
        checks.append(("audio/PANNs", ok, "onnxruntime %s" % ("installed" if ok else "NOT installed")))
    if config.DIARIZE_ENABLED:
        ok = _probe_import("pyannote.audio")
        tok = "token set" if config.PYANNOTE_AUTH_TOKEN else "TOKEN MISSING"
        checks.append(("diarize", ok and bool(config.PYANNOTE_AUTH_TOKEN),
                       "pyannote.audio %s, %s" % ("installed" if ok else "NOT installed", tok)))
    if config.IG_COOKIES_FILE:
        ok = _probe_import("instaloader")
        cookies_ok = os.path.exists(config.IG_COOKIES_FILE)
        checks.append(("instagram", ok and cookies_ok,
                       "instaloader %s, cookies %s" % (
                           "installed" if ok else "NOT installed",
                           "found" if cookies_ok else "NOT found")))
    return checks


def _cmd_config(args) -> None:
    if args.config_command == "show":
        print(config.show())

    elif args.config_command == "check":
        print("Checking configured backends...\n")
        for name, ok, detail in _run_config_checks():
            print("  %s %-14s %s" % ("OK" if ok else "!!", name + ":", detail))

    else:
        print("Use: reel-scout config {show|check}")


if __name__ == "__main__":
    main()
