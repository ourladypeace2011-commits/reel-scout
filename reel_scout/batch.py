"""Batch a list of reels from a shared doc — `reel-scout batch`.

The input is a Google Doc, a Sheet, a plain URL, a local file or stdin: anywhere
a person keeps the clips they want decoded. Everything that looks like an IG /
TikTok / YouTube Shorts link gets analyzed and exported as a self-contained
bundle, one directory per entry.

**The machine decides nothing on the user's behalf.** A box with a reachable VLM
runs the full pipeline. A box without one is not silently downgraded to a
transcript — that would quietly drop the craft score, which is the part worth
having. Instead the capability is reported and `--mode` has to be chosen:

    full        local VLM does the visual layer and the score
    agent       skip the VLM; an agent reads the keyframes and writes its own
                findings back with `reel-scout ingest` (see SKILL.md Step 2b)
    transcript  transcript and structure only, and say so

`full` is assumed only when a VLM actually answers, because then there is nothing
to choose.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import sys
import unicodedata
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

#: Only the platforms the pipeline can actually ingest. A shared doc collects
#: Drive links and long-form YouTube too; those are not silently attempted.
URL_RE = re.compile(
    r"https?://(?:www\.)?("
    # The account-scoped form (instagram.com/<handle>/reel/<code>/) is what
    # Instagram's share button emits. Missing it here is worse than the
    # crawler's parse error: a batch list silently drops those rows.
    r"instagram\.com/(?:[\w.]+/)?(?:reel|reels|p)/[\w\-]+"
    r"|(?:vm\.|vt\.)?tiktok\.com/[^\s,\"'）)]+"
    r"|youtube\.com/shorts/[\w\-]+"
    r"|youtu\.be/[\w\-]+"
    r")[^\s,\"'）)]*",
    re.IGNORECASE,
)

DOC_ID_RE = re.compile(r"docs\.google\.com/(document|spreadsheets)/d/([\w\-]+)")

MODES = ("full", "agent", "transcript")

FETCH_TIMEOUT = 30


# --- source ----------------------------------------------------------------

def export_url(url: str) -> str:
    """Rewrite a Google `/edit` link to its no-auth export endpoint.

    Doc → `export?format=txt`, Sheet → `export?format=csv`. Both work on a file
    shared as "anyone with the link", so nobody has to Publish to web or set up
    OAuth just to read a list. Non-Google URLs pass through.
    """
    m = DOC_ID_RE.search(url)
    if not m:
        return url
    kind, doc_id = m.group(1), m.group(2)
    return "https://docs.google.com/%s/d/%s/export?format=%s" % (
        kind, doc_id, "txt" if kind == "document" else "csv")


def fetch(url: str) -> str:
    real = export_url(url)
    req = urllib.request.Request(real, headers={"User-Agent": "reel-scout-batch"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    if "docs.google.com" in real and "<html" in text[:400].lower():
        raise RuntimeError(
            "got a Google sign-in page instead of the file.\n"
            "  Set the doc's sharing to 'Anyone with the link - Viewer' and retry.\n"
            "  (tried: %s)" % real)
    return text


# --- parsing ---------------------------------------------------------------

_HEADERS = {"name", "姓名", "學員", "暱稱", "email", "timestamp", "時間戳記", "url", "連結"}


def _clean_name(raw: str) -> str:
    name = raw.strip().strip("-–—·•[]()（）「」:：,，\t ")
    return "" if name.lower() in _HEADERS else name[:40]


def _is_timestamp(cell: str) -> bool:
    return bool(re.match(r"^\s*\d{4}[/\-]\d{1,2}[/\-]\d{1,2}", cell))


def _name_from_cells(cells: Sequence[str], url_idx: int) -> str:
    """Nearest preceding cell that looks like a label, skipping form timestamps."""
    for cell in reversed(list(cells[:url_idx])):
        if not cell.strip() or _is_timestamp(cell):
            continue
        name = _clean_name(cell)
        if name:
            return name
    return ""


def parse_rows(text: str) -> List[Tuple[str, str]]:
    """[(label, url)] in document order, one entry per distinct URL.

    CSV and free text need different rules for where the label lives, so they are
    handled separately: a spreadsheet row is read cell-wise (flattening it makes
    the form's timestamp column look like part of the name), while a free-text
    line takes whatever precedes the link.
    """
    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()

    def add(label: str, raw: str) -> None:
        url = raw.rstrip(".,;、。")
        if url not in seen:
            seen.add(url)
            out.append((label, url))

    rows: List[List[str]] = []
    if "," in text and "\n" in text:
        try:
            rows = [r for r in csv.reader(io.StringIO(text)) if len(r) > 1]
        except csv.Error:
            rows = []

    if rows:
        for cells in rows:
            for idx, cell in enumerate(cells):
                for m in URL_RE.finditer(cell):
                    add(_name_from_cells(cells, idx), m.group(0))
        if out:
            return out

    for line in text.splitlines():
        for m in URL_RE.finditer(line):
            add(_clean_name(line[:m.start()]), m.group(0))
    return out


def slugify(label: str, index: int) -> str:
    """Filesystem-safe directory name. CJK is kept — most labels are names."""
    if not label:
        return "clip-%02d" % index
    kept: List[str] = []
    for ch in unicodedata.normalize("NFKC", label):
        if ch.isalnum():
            kept.append(ch)
        elif ch in " _-" and kept and kept[-1] != "-":
            kept.append("-")
    return "".join(kept).strip("-") or ("clip-%02d" % index)


# --- capability ------------------------------------------------------------

def probe() -> Dict[str, bool]:
    """{'vlm': bool, 'whisper': bool} from the same checks `config check` runs."""
    from .cli import _run_config_checks

    state = {"vlm": False, "whisper": False}
    for name, ok, _detail in _run_config_checks():
        if name.startswith("VLM"):
            state["vlm"] = bool(ok)
        elif name == "whisper":
            state["whisper"] = bool(ok)
    return state


def resolve_mode(requested: Optional[str], caps: Dict[str, bool]) -> Tuple[Optional[str], str]:
    """(mode, message). mode None means: stop and let the user choose.

    A reachable VLM makes `full` unambiguous, so it is assumed. Without one there
    is a real decision — install a model, let an agent do it, or accept less — and
    guessing on the user's behalf is how the score silently goes missing.
    """
    if requested:
        if requested not in MODES:
            return None, "unknown mode %r (choose from: %s)" % (requested, ", ".join(MODES))
        if requested == "full" and not caps["vlm"]:
            return None, ("--mode full needs a reachable VLM backend and there isn't one.\n"
                          "  Start it (e.g. `ollama serve`), or use --mode agent.")
        return requested, ""

    if caps["vlm"]:
        return "full", ""

    return None, (
        "No local VLM is reachable, so the visual layer and the craft score cannot\n"
        "be produced by this machine. That is a choice, not an error — pick one:\n"
        "\n"
        "  --mode agent       an agent reads the keyframes and writes its own\n"
        "                     descriptions and score back (SKILL.md Step 2b).\n"
        "                     No model, no API key. Recommended if you got here\n"
        "                     through Claude.\n"
        "  --mode transcript  transcript and structure only, no visual layer.\n"
        "  --mode full        after starting a local VLM (e.g. `ollama serve`).\n"
        "\n"
        "Re-run with one of those.")


# --- run -------------------------------------------------------------------

def self_cmd(*args: str) -> List[str]:
    """Invoke this same install, not whatever `reel-scout` PATH happens to hold.

    Shelling out to the bare name assumes the venv's bin directory is on PATH.
    It often isn't — `./env/bin/reel-scout batch ...` without activating the venv
    is a completely ordinary thing to do, and it used to die on a raw
    FileNotFoundError traceback partway through a batch.
    """
    return [sys.executable, "-m", "reel_scout.cli"] + list(args)


def _run(cmd: Sequence[str], verbose: bool) -> int:
    if verbose:
        print("    $ " + " ".join(cmd))
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if verbose:
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        if out:
            print("      " + out.replace("\n", "\n      "))
    return proc.returncode


def _video_ids(conn) -> Set[str]:
    return {r[0] for r in conn.execute("SELECT id FROM videos")}


def resolve_video_id(conn, before: Set[str], url: str) -> Optional[str]:
    """Which row this run produced, or None.

    Matched by set difference rather than URL equality: shared links routinely
    carry `?igsh=`-style tracking parameters and will not compare equal to what
    was stored. None means the caller must skip — handing one entry's analysis to
    another is worse than producing one bundle fewer.
    """
    new = _video_ids(conn) - before
    if len(new) == 1:
        return next(iter(new))
    if len(new) > 1:
        return None
    row = conn.execute("SELECT id FROM videos WHERE url = ?", (url,)).fetchone()
    return row["id"] if row else None


def needs_completion(conn, video_id: str) -> bool:
    """True when a video has keyframes but nothing has described them yet."""
    total = conn.execute(
        "SELECT COUNT(*) FROM keyframes WHERE video_id = ?", (video_id,)).fetchone()[0]
    if not total:
        return False
    described = conn.execute(
        """SELECT COUNT(*) FROM vision_descriptions vd
           JOIN keyframes k ON k.id = vd.keyframe_id WHERE k.video_id = ?""",
        (video_id,)).fetchone()[0]
    return described < total


def has_score(conn, video_id: str) -> bool:
    """True when this video already carries a score row."""
    row = conn.execute(
        "SELECT 1 FROM scores WHERE video_id = ? LIMIT 1", (video_id,)).fetchone()
    return row is not None


def run_batch(entries: List[Tuple[str, str]], out_root: str, mode: str,
              max_mb: str = "25", verbose: bool = False,
              on_progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
              score: bool = True,
              ) -> Dict[str, Any]:
    """Analyze and bundle every entry. Returns the manifest it also writes.

    `on_progress` is called at each state transition with a dict carrying an
    "event" key. Without it nothing changes -- but with it a caller can persist
    progress as it happens, which is the difference between a run interrupted at
    15/20 leaving a recoverable record and leaving a database that says zero
    beside a directory holding fifteen finished bundles. Returning the string
    "cancel" from an item_start stops the run before the next entry; the one in
    flight is never interrupted, because half an analysis is worse than one more
    finished video.
    """
    from . import config, db
    import sqlite3

    def emit(event: str, **fields: Any) -> Any:
        if on_progress is None:
            return None
        fields["event"] = event
        try:
            return on_progress(fields)
        except Exception:
            # A broken progress sink must not take down a job that is working.
            return None

    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)

    os.makedirs(out_root, exist_ok=True)
    done: List[Dict[str, str]] = []
    failed: List[Dict[str, str]] = []
    pending: List[Dict[str, str]] = []
    cancelled = False

    for i, (label, url) in enumerate(entries, 1):
        slug = slugify(label, i)
        if emit("item_start", index=i, total=len(entries),
                label=label, slug=slug, url=url) == "cancel":
            cancelled = True
            break
        print("\n[%d/%d] %s" % (i, len(entries), label or slug))
        print("    %s" % url)

        before = _video_ids(conn)
        cmd = self_cmd("analyze", url)
        if mode != "full":
            cmd.append("--skip-vision")
        if _run(cmd, verbose) != 0:
            print("    x analyze failed")
            failed.append({"label": label, "url": url, "reason": "analyze exited non-zero"})
            emit("item_failed", url=url, label=label, reason="analyze exited non-zero")
            continue

        vid = resolve_video_id(conn, before, url)
        if not vid:
            print("    x could not tell which video this produced — skipped, not guessed")
            failed.append({"label": label, "url": url, "reason": "video id unresolved"})
            emit("item_failed", url=url, label=label, reason="video id unresolved")
            continue
        emit("item_analyzed", url=url, label=label, slug=slug, video_id=vid)

        entry = {"label": label, "slug": slug, "url": url, "video_id": vid}
        if mode == "agent" and needs_completion(conn, vid):
            pending.append(entry)
            emit("item_needs_vision", url=url, video_id=vid)

        # Score before exporting, so the bundle carries the verdict. Only in
        # `full` mode: the other modes leave the visual layer unfinished, and a
        # score computed over a half-built analysis is worse than no score.
        # A scorer failure is never fatal to the item -- the analysis is the
        # expensive part and it is already safe in the database.
        if score and mode == "full" and not has_score(conn, vid):
            emit("item_scoring", url=url, video_id=vid)
            if _run(self_cmd("score", vid), verbose) != 0:
                print("    ! scoring failed -- keeping the analysis, continuing")
                emit("item_score_failed", url=url, video_id=vid)
            else:
                emit("item_scored", url=url, video_id=vid)

        dest = os.path.join(out_root, slug)
        emit("item_exporting", url=url, video_id=vid, bundle_dir=dest)
        if _run(self_cmd("export", "--format", "bundle", "--video", vid,
                          "-o", dest, "--max-mb", str(max_mb)), verbose) != 0:
            print("    x export failed")
            failed.append({"label": label, "url": url, "reason": "export exited non-zero"})
            emit("item_failed", url=url, label=label, reason="export exited non-zero")
            continue

        entry["bundle_dir"] = dest
        done.append(entry)
        print("    ok %s" % dest)
        emit("item_done", url=url, label=label, video_id=vid, bundle_dir=dest)

    conn.close()
    result = {"mode": mode, "done": done, "failed": failed, "pending_completion": pending}
    if cancelled:
        result["cancelled"] = True
    with open(os.path.join(out_root, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    emit("batch_done", result=result, cancelled=cancelled)
    return result
