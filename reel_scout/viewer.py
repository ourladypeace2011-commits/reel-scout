"""Read-only viewer for decoded analyses.

Renders a video's reverse-decoded structure (hook / beats / CTA), keyframes,
craft scores, and transcript as HTML. Two surfaces share this one renderer:

  * a self-contained single-file HTML export (keyframes base64-embedded, zero
    external assets) that opens in any browser with no install — the take-home
    artifact for course students; and
  * a local `reel-scout view` server (keyframes served from disk).

Deliberately READ-ONLY: it shows the decoded craft, it never offers an action
(no edit / re-analyze / run buttons). Scores are labelled a reference, not an
authority, per the project's honesty line.
"""
from __future__ import annotations

import base64
import html
import json
import os
from typing import Any, Callable, Dict, List, Optional

from . import config, db, i18n, theme

# A keyframe-src strategy maps a keyframe row → the string that goes in <img src>.
# Export uses a base64 data URI (self-contained); the server uses a URL.
KeyframeSrc = Callable[[Dict[str, Any]], str]

_SCORE_DIMS = [
    ("overall", "Overall"),
    ("hook_strength", "Hook"),
    ("visual_storytelling", "Visual"),
    ("pacing", "Pacing"),
    ("structure", "Structure"),
]


def keyframe_data_uri(file_path: Optional[str]) -> Optional[str]:
    """Base64 data URI for a keyframe image (JPEG), or None if unreadable.

    file_path is stored cwd-relative by default; resolve it, and fall back to
    KEYFRAMES_DIR by basename if the recorded path has moved. A missing frame
    degrades to None (the caller shows a placeholder) rather than erroring."""
    if not file_path:
        return None
    candidates = [os.path.abspath(file_path)]
    base = os.path.basename(file_path)
    parent = os.path.basename(os.path.dirname(file_path))
    candidates.append(os.path.join(config.KEYFRAMES_DIR, parent, base))
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                return "data:image/jpeg;base64,%s" % b64
            except OSError:
                return None
    return None


def build_video_view(conn: db.sqlite3.Connection, video_id: str) -> Optional[Dict[str, Any]]:
    """Assemble the full read-only record for one video, or None if not found."""
    video = db.get_video(conn, video_id)
    if video is None:
        return None
    analysis = db.get_analysis(conn, video_id)
    score = db.get_score(conn, video_id)
    transcript = db.get_transcript(conn, video_id)
    keyframes = db.get_keyframes_with_descriptions(conn, video_id)

    full = {}
    if analysis is not None and analysis["full_json"]:
        try:
            full = json.loads(analysis["full_json"])
        except (ValueError, TypeError):
            full = {}

    return {
        "video_id": video_id,
        "title": video["title"] or "(untitled)",
        "platform": video["platform"],
        "url": video["url"],
        "uploader": video["uploader"],
        "duration_sec": video["duration_sec"],
        "summary": full.get("summary", ""),
        "topics": full.get("topics", []) or [],
        "content_type": full.get("content_type"),
        "content_structure": full.get("content_structure"),
        "hook": full.get("hook", {}) or {},
        "style": full.get("style", {}) or {},
        "timeline": full.get("timeline", []) or [],
        "score": dict(score) if score is not None else None,
        "transcript": transcript["text_full"] if transcript is not None else "",
        # A clip with no speech is not the same as a clip nobody transcribed,
        # and neither is the same as a clip whose words are simply short. Every
        # surface below used to collapse all three into an empty string and then
        # render nothing at all -- so a craft score computed with the transcript
        # missing looked exactly like one computed with it present. Carry the
        # fact explicitly instead. Computed here rather than persisted because
        # the rows that need it most are the ones already in the database.
        "has_transcript": (
            bool((transcript["text_full"] or "").strip())
            if transcript is not None else False
        ),
        "keyframes": [dict(k) for k in keyframes],
    }


#: English baseline for the empty-transcript note. Must match i18n.STRINGS["en"]
#: ["noTranscriptNote"] -- applyLang() replaces textContent, so a drifted
#: baseline would silently flip back to the wrong words on a language toggle.
NO_TRANSCRIPT_EN = i18n.STRINGS["en"]["noTranscriptNote"]


def _e(value: Any) -> str:
    """HTML-escape any value (None → empty)."""
    return html.escape("" if value is None else str(value))


def _fmt_dur(sec: Any) -> str:
    if sec is None:
        return "—"
    return "%ds" % int(sec)


def render_video_section(view: Dict[str, Any], keyframe_src: KeyframeSrc) -> str:
    parts: List[str] = []
    parts.append('<section class="video" id="v-%s">' % _e(view["video_id"]))
    parts.append('<h2>%s</h2>' % _e(view["title"]))
    parts.append(
        '<p class="meta">%s · %s · <a href="%s" data-i18n="sourcePlain">source</a></p>' % (
            _e(view["platform"]), _fmt_dur(view["duration_sec"]), _e(view["url"])))

    if view["summary"]:
        parts.append('<p class="summary">%s</p>' % _e(view["summary"]))

    # Decoded structure — the craft payload.
    hook = view["hook"]
    style = view["style"]
    rows = [
        ("Structure", view["content_structure"]),
        ("Content type", view["content_type"]),
        ("Format", style.get("format")),
        ("Pacing", style.get("pacing")),
        ("Hook type", hook.get("opening_type")),
        ("Hook text", hook.get("opening_text")),
        ("CTA type", hook.get("cta_type")),
        ("CTA text", hook.get("cta_text")),
    ]
    parts.append('<h3 data-i18n="decoded">Decoded structure</h3><table class="kv">')
    for label, val in rows:
        if val:
            # Only the label is translatable; the value is model output.
            parts.append('<tr><th data-i18n="row.%s">%s</th><td>%s</td></tr>'
                         % (_e(label), _e(label), _e(val)))
    parts.append('</table>')

    if view["topics"]:
        parts.append('<p class="topics"><span data-i18n="topics">Topics:</span> %s</p>'
                     % _e(", ".join(view["topics"])))

    # Timeline / narrative arc.
    if view["timeline"]:
        parts.append('<h3 data-i18n="timeline">Timeline</h3><ul class="timeline">')
        for item in view["timeline"]:
            if isinstance(item, dict):
                parts.append('<li><span class="ts">%s</span> %s</li>' % (
                    _e(item.get("timestamp", "")), _e(item.get("event", ""))))
        parts.append('</ul>')

    # Scores — reference, not authority.
    if view["score"]:
        parts.append('<h3><span data-i18n="craftScores">Craft scores</span> '
                     '<small>(<span data-i18n="craftScoresNote">reference, not '
                     'authority — human judgment leads</span>)</small></h3>'
                     '<table class="scores">')
        for key, label in _SCORE_DIMS:
            val = view["score"].get(key)
            if val is not None:
                parts.append('<tr><th data-i18n="dim.%s">%s</th><td>%.1f</td></tr>'
                             % (_e(key), _e(label), val))
        parts.append('</table>')

    # Keyframes.
    if view["keyframes"]:
        parts.append('<h3 data-i18n="keyframes">Keyframes</h3><div class="frames">')
        for kf in view["keyframes"]:
            src = keyframe_src(kf)
            img = ('<img src="%s" alt="keyframe" loading="lazy">' % _e(src)
                   if src else '<div class="noimg" data-i18n="imgUnavailable">image unavailable</div>')
            desc = kf.get("description") or ""
            text = kf.get("text_in_frame") or ""
            # desc / on-screen text are model output; only the "on-screen:" label swaps.
            parts.append('<figure>%s<figcaption>'
                         '<span class="ts">%ss</span> %s%s</figcaption></figure>' % (
                             img, _e(int(kf["timestamp_sec"]) if kf["timestamp_sec"] is not None else 0),
                             _e(desc),
                             (' <em><span data-i18n="onScreen">on-screen:</span> %s</em>'
                              % _e(text)) if text else ''))
        parts.append('</div>')

    # Transcript. An empty one is stated, not skipped -- see build_video_view.
    if view["transcript"]:
        parts.append('<h3 data-i18n="transcript">Transcript</h3>'
                     '<div class="transcript">%s</div>' % _e(view["transcript"]))
    else:
        parts.append('<h3 data-i18n="noTranscript">\u26a0 no transcript</h3>'
                     '<p class="transcript empty" data-i18n="noTranscriptNote">%s</p>'
                     % _e(NO_TRANSCRIPT_EN))

    parts.append('</section>')
    return "\n".join(parts)


_COMPONENTS = """
/* Language toggle: pinned to the header's top-right, aligned to .inner padding. */
header.top .inner{position:relative}
header.top .inner .lang{position:absolute;top:24px;right:24px}

/* Library index — a list of rules, not a grid of cards. The row is the chrome;
   the title is the loud part. */
nav.index{margin:8px 0 0}
nav.index a{display:flex;align-items:baseline;gap:10px;padding:11px 2px;
  border-bottom:1px solid var(--rule-soft);text-decoration:none}
nav.index a:hover{background:var(--surface)}
nav.index a:hover .ttl{text-decoration:underline;text-underline-offset:3px}
nav.index .ttl{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
nav.index .sc.warn{color:var(--ink-2)}
.transcript.empty{color:var(--quiet);background:none;border-style:dashed}
nav.index .sc{font-family:var(--mono);font-size:11px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--quiet);flex:none}

section.video{padding:8px 0 40px}
section.video h2{margin:.1rem 0;font-family:var(--display);font-weight:400;
  font-size:clamp(22px,2.6vw,30px);letter-spacing:-.02em;line-height:1.15}
.meta{font-family:var(--mono);font-size:11px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--quiet);margin:.35rem 0 1.1rem}
.summary{font-size:17px;max-width:var(--col)}
h3{margin:34px 0 10px;font-family:var(--mono);font-size:11px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--quiet);font-weight:400;
  border-bottom:1px solid var(--rule);padding-bottom:6px}
h3 small{letter-spacing:.08em}
table.kv,table.scores{border-collapse:collapse;font-size:14px}
table.kv th,table.scores th{text-align:left;padding:5px 20px 5px 0;font-weight:400;
  font-family:var(--mono);font-size:11px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--quiet);vertical-align:top;white-space:nowrap}
table.kv td,table.scores td{padding:5px 0;font-variant-numeric:tabular-nums}
ul.timeline{margin:.2rem 0;padding-left:1.1rem;max-width:var(--col)}
.ts{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--quiet);
  font-size:11px;letter-spacing:.06em;margin-right:.5rem}
.frames{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:18px}
.frames figure{margin:0}
.frames img{width:100%;height:auto;display:block;background:var(--surface-2)}
.frames .noimg{aspect-ratio:16/9;display:grid;place-items:center;
  background:var(--surface-2);font-family:var(--mono);font-size:11px;color:var(--quiet)}
figcaption{font-size:13px;color:var(--ink-2);margin-top:.4rem;line-height:1.45}
.transcript{font-size:14px;white-space:pre-wrap;max-height:17rem;overflow:auto;
  padding:14px 16px;background:var(--surface);border:1px solid var(--rule-soft);
  max-width:var(--col)}
.topics{font-family:var(--mono);font-size:11px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--quiet)}
"""

_STYLE = theme.stylesheet(_COMPONENTS)


def render_page(sections: List[str], index_html: str, title: str,
                embed_fonts: bool = False) -> str:
    # Server pages link the fonts (/font/...); the take-home export inlines
    # them so the file still looks right offline and after being moved.
    style = theme.stylesheet(_COMPONENTS, embed_fonts=embed_fonts) + i18n.TOGGLE_CSS
    # Both dictionaries travel with the page (boot island) so the toggle is a
    # pure client swap and a take-home bundle stays bilingual offline. English is
    # the baseline text; applyLang() swaps to the reader's language on load.
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>%s</title><style>%s</style></head><body>'
        '<header class="top"><div class="inner">%s<h1>%s</h1>'
        '<div class="sub">reel-scout · <span data-i18n="sub">decoded structure '
        '· read-only</span></div>'
        '</div></header>'
        '<main>%s%s</main>%s<script>%s</script></body></html>' % (
            _e(title), style, i18n.TOGGLE_HTML, _e(title), index_html,
            "\n".join(sections), i18n.boot_island(), i18n.APPLY_JS)
    )


def render_index(views: List[Dict[str, Any]], href: Callable[[str], str]) -> str:
    if len(views) <= 1:
        return ""
    items = ['<nav class="index">']
    for v in views:
        overall = ""
        if v["score"] and v["score"].get("overall") is not None:
            overall = '<span class="sc"> · %.1f</span>' % v["score"]["overall"]
        struct = '<span class="sc"> · %s</span>' % _e(v["content_structure"]) if v["content_structure"] else ""
        # The score chip sits right here, so the caveat belongs beside it: a
        # reader comparing two rows should be able to see that one of the
        # numbers had less to work with.
        notx = ('<span class="sc warn"> · <span data-i18n="noTranscript">'
                '\u26a0 no transcript</span></span>') if not v.get("has_transcript") else ""
        items.append('<a href="%s"><span class="ttl">%s</span>%s%s%s</a>' % (
            _e(href(v["video_id"])), _e(v["title"]), struct, overall, notx))
    items.append('</nav>')
    return "\n".join(items)


def render_bundle(conn: db.sqlite3.Connection, video_id: Optional[str] = None,
                  title: str = "reel-scout") -> str:
    """Self-contained HTML for one or all analyzed videos (keyframes base64
    embedded). This is the take-home export string."""
    if video_id:
        ids = [video_id]
    else:
        ids = [v["id"] for v in db.list_videos(conn, status="analyzed", limit=9999)]
    views = [v for v in (build_video_view(conn, vid) for vid in ids) if v]

    def src(kf: Dict[str, Any]) -> str:
        return keyframe_data_uri(kf.get("file_path")) or ""

    sections = [render_video_section(v, src) for v in views]
    index = render_index(views, href=lambda vid: "#v-%s" % vid)
    if not views:
        sections = ['<section class="video"><p data-i18n="emptyBundle">'
                    'No analyzed videos to show.</p></section>']
    # Take-home export: inline the fonts too, so it holds its look offline.
    return render_page(sections, index, title, embed_fonts=True)


# --- Live server surface (reel-scout view) ---
# Same renderer as the bundle, but keyframes are served from disk by URL instead
# of base64-embedded, and each video is its own page.

def render_index_page(conn: db.sqlite3.Connection, title: str = "reel-scout",
                      href: Optional[Callable[[str], str]] = None) -> str:
    """Library index. `href` decides where a row points — the unified server
    sends rows straight into the inspector; the static export keeps /video/."""
    if href is None:
        href = lambda vid: "/video/%s" % vid  # noqa: E731
    views = [v for v in (build_video_view(conn, r["id"])
                         for r in db.list_videos(conn, status="analyzed", limit=9999)) if v]
    if not views:
        body = ('<section class="video"><p data-i18n="emptyLibrary">'
                'No analyzed videos yet.</p></section>')
        return render_page([body], "", title)
    nav = render_index(views, href=href)
    # render_index returns "" for a single video; always show the list on the server.
    if not nav:
        nav = ('<nav class="index"><a href="%s">%s</a></nav>'
               % (_e(href(views[0]["video_id"])), _e(views[0]["title"])))
    return render_page([], nav, title)


def render_video_page(conn: db.sqlite3.Connection, video_id: str) -> Optional[str]:
    view = build_video_view(conn, video_id)
    if view is None:
        return None
    section = render_video_section(view, keyframe_src=lambda kf: "/keyframe/%s" % kf["id"])
    back = ('<nav class="index"><a href="/" data-i18n="allVideos">'
            '&larr; all videos</a></nav>')
    return render_page([section], back, view["title"])


def _keyframe_path(conn: db.sqlite3.Connection, keyframe_id: str) -> Optional[str]:
    row = conn.execute("SELECT file_path FROM keyframes WHERE id = ?", (keyframe_id,)).fetchone()
    if row is None or not row[0]:
        return None
    for path in (os.path.abspath(row[0]),
                 os.path.join(config.KEYFRAMES_DIR,
                              os.path.basename(os.path.dirname(row[0])),
                              os.path.basename(row[0]))):
        if os.path.exists(path):
            return path
    return None


def make_server(host: str = "127.0.0.1", port: int = 0):
    """Build (but don't start) the read-only HTTP server. Each request opens its
    own short-lived connection (via db.get_connection → config.DB_PATH), so the
    handler is thread-safe regardless of which thread serve_forever runs on.
    Split from serve() so tests can drive it over a real socket."""
    import http.server

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the console quiet
            pass

        def _send(self, code, body, content_type="text/html; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            conn = db.get_connection()
            try:
                if path == "/":
                    self._send(200, render_index_page(conn))
                elif path.startswith("/video/"):
                    page = render_video_page(conn, path[len("/video/"):])
                    self._send(200, page) if page else self._send(404, "not found")
                elif path.startswith("/keyframe/"):
                    fp = _keyframe_path(conn, path[len("/keyframe/"):])
                    if fp:
                        with open(fp, "rb") as f:
                            self._send(200, f.read(), "image/jpeg")
                    else:
                        self._send(404, "not found")
                elif path.startswith("/font/"):
                    fp = theme.font_path(path[len("/font/"):])
                    if os.path.exists(fp):
                        with open(fp, "rb") as f:
                            self._send(200, f.read(), "font/woff2")
                    else:
                        self._send(404, "not found")
                else:
                    self._send(404, "not found")
            finally:
                conn.close()

    # Threading, not the plain HTTPServer: a video page pulls its keyframes over
    # separate requests, so a single-threaded server serialized them and ANY held
    # -open connection (a browser keep-alive is enough) blocked every other
    # request until it timed out. The handler is per-request thread-safe as noted
    # above, so threading is free. ThreadingHTTPServer sets daemon_threads.
    return http.server.ThreadingHTTPServer((host, port), _Handler)


def serve(host: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> None:
    """Start the read-only local viewer. Blocks until interrupted.

    This is the unified app: the library index plus the interactive inspector on
    one port, so a row in the list opens straight into the player/waveform view
    instead of making you run a second command with a video id.
    """
    from .inspector import make_inspect_server  # lazy: inspector imports viewer
    httpd = make_inspect_server(host=host, port=port, default_id=None)
    url = "http://%s:%d/" % (host, httpd.server_address[1])
    print("reel-scout view (read-only) serving at %s  — Ctrl-C to stop" % url)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - headless / no browser is fine
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
