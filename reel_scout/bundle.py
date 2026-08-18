"""Take-home bundle export — one self-contained file per reel.

The course case: hand a student a decoded reel they can just open. So every reel
becomes ONE html file with nothing outside it — video, keyframes, waveform peaks,
fonts and a CJK subset all inlined. Move it, rename it, email it, drop it in
Drive: it still works, because there is no sibling file to lose and no server to
run. `file://` blocks fetch(), which is exactly why the peaks travel inside the
page rather than being requested.

Sizing is what makes this safe, and it only works because course material is
short-form: measured on the current corpus, reels come out at 1.4–11.4 MB each
once base64'd. A 40-minute video would not — so anything over `max_bytes` is
skipped with a reason rather than silently producing an unopenable file.

There is no second renderer here: inspector.render_inspector does both the live
and the frozen page, so the two can't drift.
"""
from __future__ import annotations

import base64
import os
import re
from typing import Any, Dict, List, Optional

from . import db, theme
from .inspector import (build_inspect_view, render_inspector, resolve_video_file,
                        _waveform_payload)
from .viewer import _e, keyframe_data_uri

# Past this, a single inlined file stops being something you can reasonably open.
MAX_EMBED_BYTES = 25 * 1024 * 1024


def slugify(text: str, fallback: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (text or "").strip().lower(), flags=re.UNICODE)
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:60] or fallback


def _video_data_uri(path: str) -> str:
    with open(path, "rb") as f:
        return "data:video/mp4;base64,%s" % base64.b64encode(f.read()).decode("ascii")


def _page_text(view: Dict[str, Any]) -> str:
    """Every string that will appear in the page — drives the CJK subset so the
    font carries exactly the glyphs this reel needs and nothing more."""
    bits = [view.get("title") or "", view.get("summary") or "",
            view.get("uploader") or "", view.get("transcript") or ""]
    bits += [str(t) for t in (view.get("topics") or [])]
    for seg in view.get("segments") or []:
        bits.append(seg.get("text") or "")
    for kf in view.get("keyframes") or []:
        bits.append(kf.get("description") or "")
        bits.append(kf.get("text_in_frame") or "")
    for key in ("content_type", "content_structure"):
        bits.append(str(view.get(key) or ""))
    # Read from the view AFTER build_reel_page has applied the --with-marks
    # gate, so a withheld mark contributes no glyphs -- and a mark that does
    # ship is not rendered in tofu because the subset was built without it.
    for mark in view.get("marks") or []:
        bits.append(mark.get("label") or "")
        bits.append(mark.get("note") or "")
    return "\n".join(bits)


def build_reel_page(conn: db.sqlite3.Connection, video_id: str,
                    cjk_ttf: str = "",
                    max_bytes: int = MAX_EMBED_BYTES,
                    back_href: Optional[str] = None,
                    with_marks: bool = False) -> Dict[str, Any]:
    """Render one frozen reel page. Returns {ok, html, reason, bytes}.

    `with_marks` defaults to False for the same reason notes and stars are not
    in here at all: a mark is working state — where someone decided the meaning
    turns, written while studying the clip — and a take-home file is something
    a reader receives. The default is the whole safety property, so it is a
    parameter with one obvious value rather than a caller-by-caller decision,
    and a test pins it.
    """
    view = build_inspect_view(conn, video_id)
    if view is None:
        return {"ok": False, "reason": "no such video", "html": "", "bytes": 0}
    if not with_marks:
        view["marks"] = []

    path = resolve_video_file(view.get("file_path"))
    video_src = None
    if path:
        size = os.path.getsize(path)
        if size > max_bytes:
            return {"ok": False, "html": "", "bytes": size,
                    "reason": "video is %.1f MB (limit %.0f MB) — too big to inline; "
                              "this is course-reel sized tooling, not long-form"
                              % (size / 1048576.0, max_bytes / 1048576.0)}
        video_src = _video_data_uri(path)
    else:
        view["has_video"] = False

    peaks = _waveform_payload(conn, video_id).get("peaks") or []

    cjk = theme.cjk_subset(_page_text(view), source_ttf=cjk_ttf)
    html = render_inspector(
        view, video_src=video_src, peaks=peaks, embed_fonts=True, cjk_woff2=cjk,
        # keyframes inline too, or the page would still call /keyframe/<id>
        keyframe_src=lambda kf: keyframe_data_uri(kf.get("file_path")) or "",
        back_href=back_href)
    return {"ok": True, "html": html, "reason": "", "bytes": len(html.encode("utf-8"))}


def _index_html(entries: List[Dict[str, Any]], title: str = "reel-scout") -> str:
    rows = []
    for e in entries:
        size = "%.1f MB" % (e["bytes"] / 1048576.0)
        rows.append('<a href="%s"><span class="ttl">%s</span>'
                    '<span class="sc">%s</span></a>'
                    % (_e(e["file"]), _e(e["title"]), _e(size)))
    body = ('<nav class="index">%s</nav>' % "\n".join(rows)) if rows else \
        '<section class="video"><p>Nothing bundled.</p></section>'
    style = theme.stylesheet("""
nav.index a{display:flex;align-items:baseline;gap:10px;padding:11px 2px;
  border-bottom:1px solid var(--rule-soft);text-decoration:none}
nav.index a:hover{background:var(--surface)}
nav.index .ttl{flex:1}
nav.index .sc{font-family:var(--mono);font-size:11px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--quiet)}
p.note{font-family:var(--mono);font-size:11px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--quiet);margin-top:28px}
""", embed_fonts=True)
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>%s</title><style>%s</style></head><body>'
            '<header class="top"><div class="inner"><h1>%s</h1>'
            '<div class="sub">reel-scout · decoded reels · read-only</div>'
            '</div></header><main>%s'
            '<p class="note">each file is self-contained — open it anywhere, '
            'move it anywhere</p></main></body></html>'
            % (_e(title), style, _e(title), body))


def build_bundle(conn: db.sqlite3.Connection, out_dir: str,
                 video_ids: Optional[List[str]] = None,
                 cjk_ttf: str = "",
                 max_bytes: int = MAX_EMBED_BYTES,
                 with_marks: bool = False) -> Dict[str, Any]:
    """Write one self-contained page per reel plus an index. Returns a summary
    with what was written and what was skipped (and why)."""
    if video_ids is None:
        video_ids = [r["id"] for r in db.list_videos(conn, status="analyzed", limit=9999)]

    os.makedirs(out_dir, exist_ok=True)
    written: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    used: set = set()

    for vid in video_ids:
        # build_bundle always writes an index next to the pages, so link to it.
        # (If a single page is later moved away on its own the link dangles —
        # the folder is the intended unit; the page itself still works.)
        result = build_reel_page(conn, vid, cjk_ttf=cjk_ttf, max_bytes=max_bytes,
                                 back_href="index.html", with_marks=with_marks)
        view = build_inspect_view(conn, vid)
        title = (view or {}).get("title") or vid
        if not result["ok"]:
            skipped.append({"video_id": vid, "title": title, "reason": result["reason"]})
            continue
        name = slugify(title, vid[:8])
        while name in used:
            name = "%s-%s" % (name, vid[:4])
        used.add(name)
        filename = "%s.html" % name
        with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as f:
            f.write(result["html"])
        written.append({"video_id": vid, "title": title,
                        "file": filename, "bytes": result["bytes"]})

    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(_index_html(written))

    return {"out_dir": out_dir, "written": written, "skipped": skipped,
            "total_bytes": sum(e["bytes"] for e in written)}
