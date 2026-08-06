"""Viewer for decoded analyses.

Renders a video's reverse-decoded structure (hook / beats / CTA), keyframes,
craft scores, and transcript as HTML. Two surfaces share this one renderer:

  * a self-contained single-file HTML export (keyframes base64-embedded, zero
    external assets) that opens in any browser with no install — the take-home
    artifact for course students; and
  * a local `reel-scout view` server (keyframes served from disk).

The ANALYSIS is read-only on both: neither surface offers to edit, re-analyze or
re-run anything, and scores stay labelled a reference rather than an authority.
The served library list is the one exception, and only for the operator's OWN
layer — star, group and note (see `annotate`). Those write to their own tables
and change nothing the model produced. The export has no server to write to and
stays entirely read-only, annotations included.
"""
from __future__ import annotations

import base64
import html
import json
import os
from typing import Any, Callable, Dict, List, Optional

from . import annotate, config, db, i18n, theme

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

/* --- served library table (annotations) --- */
table.library{width:100%;border-collapse:collapse;font-size:14px;margin-bottom:1rem}
table.library th{text-align:left;font-family:var(--mono);font-size:10px;
  letter-spacing:.12em;text-transform:uppercase;color:var(--quiet);
  font-weight:400;padding:6px 10px;border-bottom:1px solid var(--rule)}
table.library td{padding:7px 10px;border-bottom:1px solid var(--rule-soft);
  vertical-align:middle}
table.library tr:hover td{background:var(--surface)}
table.library .c-star{width:2.4rem;text-align:center;padding-left:4px;padding-right:4px}
table.library .c-score{width:4rem;text-align:right;font-family:var(--mono);
  font-size:12px;color:var(--ink-2)}
table.library .c-group{width:11rem}
table.library .c-note{width:38%}
table.library .c-title a{color:var(--ink);text-decoration:none;font-weight:500}
table.library .c-title a:hover{text-decoration:underline}
table.library .c-title .sc{font-family:var(--mono);font-size:10px;color:var(--quiet);
  letter-spacing:.08em;margin-left:.5rem}
table.library .c-title .sc.warn{color:var(--warn,#b45309)}
.starbtn{background:none;border:0;cursor:pointer;padding:2px 4px;line-height:1;
  color:var(--quiet);font-size:16px}
.starbtn:hover{color:var(--ink)}
.starbtn[aria-pressed="true"]{color:var(--accent,#c2871a)}
.starbtn.hdr{font-size:13px}
select.groupsel,input.noteinput,#newgroup{font-family:inherit;font-size:13px;
  color:var(--ink);background:var(--bg);border:1px solid var(--rule-soft);
  border-radius:3px;padding:3px 6px;width:100%}
input.noteinput::placeholder,#newgroup::placeholder{color:var(--quiet);opacity:.7}
select.groupsel:focus,input.noteinput:focus,#newgroup:focus{outline:0;
  border-color:var(--ink-2)}
.libtools{display:flex;align-items:center;gap:8px;margin:0 0 1rem;
  font-family:var(--mono);font-size:11px;flex-wrap:wrap}
.libtools #newgroup{width:14rem}
.libtools #delgroup{font-family:inherit;font-size:11px;color:var(--ink);
  background:var(--bg);border:1px solid var(--rule-soft);border-radius:3px;
  padding:4px 6px;max-width:16rem}
.toolsep{width:1px;height:18px;background:var(--rule-soft);margin:0 4px}
.libtools button{font-family:var(--mono);font-size:10px;letter-spacing:.1em;
  text-transform:uppercase;padding:5px 10px;border:1px solid var(--rule-soft);
  background:none;color:var(--quiet);cursor:pointer;border-radius:3px}
.libtools button:hover{color:var(--ink);border-color:var(--ink-2)}
.savehint{color:var(--quiet);letter-spacing:.08em}
.savehint.bad{color:var(--warn,#b45309)}
/* The same word, said next to the row you are actually looking at. The global
   hint sits in the header, so on a long library it reports into empty space.
   The note input is width:100%, so the hint needs a slot of its own or it is
   in the markup and never on screen — which is how the first cut of this
   shipped. The slot is reserved whether or not it holds text, so a save does
   not reflow the row under the cursor; an error is allowed to grow past it,
   because an error you cannot read is not worth the stability.
   Flex goes on a wrapper, never on the <td>: a table cell made display:flex
   drops out of the table layout algorithm and the column collapses. */
.notewrap{display:flex;align-items:center;gap:8px}
.notewrap input.noteinput{flex:1 1 auto;width:auto;min-width:0}
.rowhint{flex:0 0 auto;min-width:4.5em;text-align:right;
  font-family:var(--mono);font-size:10px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--quiet);white-space:nowrap}
.rowhint.bad{color:var(--warn,#b45309)}
"""

_STYLE = theme.stylesheet(_COMPONENTS)


def render_page(sections: List[str], index_html: str, title: str,
                embed_fonts: bool = False, extra_js: str = "",
                sub_key: str = "sub") -> str:
    # Server pages link the fonts (/font/...); the take-home export inlines
    # them so the file still looks right offline and after being moved.
    style = theme.stylesheet(_COMPONENTS, embed_fonts=embed_fonts) + i18n.TOGGLE_CSS
    # `sub_key` is how the export stays honest: the take-home file really is
    # read-only, while the served library now writes annotations back, and the
    # strapline should not claim otherwise on either.
    sub_en = i18n.STRINGS["en"].get(sub_key, i18n.STRINGS["en"]["sub"])
    # Both dictionaries travel with the page (boot island) so the toggle is a
    # pure client swap and a take-home bundle stays bilingual offline. English is
    # the baseline text; applyLang() swaps to the reader's language on load.
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>%s</title><style>%s</style></head><body>'
        '<header class="top"><div class="inner">%s<h1>%s</h1>'
        '<div class="sub">reel-scout · <span data-i18n="%s">%s</span></div>'
        '</div></header>'
        '<main>%s%s</main>%s<script>%s</script>%s</body></html>' % (
            _e(title), style, i18n.TOGGLE_HTML, _e(title), _e(sub_key), _e(sub_en),
            index_html, "\n".join(sections), i18n.boot_island(), i18n.APPLY_JS,
            ('<script>%s</script>' % extra_js) if extra_js else "")
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


#: Client script for the served library table. Vanilla, inline, no fetch of any
#: external asset — same constraint the rest of this tool holds itself to.
#: Only ever shipped by `render_index_page`; the export never includes it.
ANNOTATE_JS = r"""
(function(){
  var table=document.getElementById('library');
  if(!table) return;
  var hint=document.getElementById('savehint');
  var hintTimer;
  var rowTimers={};
  function say(msg, bad, tr){
    if(hint){
      hint.textContent=msg; hint.className='savehint'+(bad?' bad':'');
      clearTimeout(hintTimer);
      hintTimer=setTimeout(function(){ hint.textContent=''; }, bad?6000:1500);
    }
    // Also report into the row itself. The header hint is out of view once the
    // library is longer than a screen, which made a working save look like
    // nothing happened — the reason this was mistaken for a missing button.
    if(!tr) return;
    var rh=tr.querySelector('.rowhint'); if(!rh) return;
    var vid=tr.getAttribute('data-vid');
    rh.textContent=msg; rh.className='rowhint'+(bad?' bad':'');
    clearTimeout(rowTimers[vid]);
    rowTimers[vid]=setTimeout(function(){ rh.textContent=''; }, bad?6000:1500);
  }
  function t(key, fallback){
    try{
      var isl=document.getElementById('rsboot');
      var lang=localStorage.getItem('rs_lang')||'en';
      var d=JSON.parse(isl.textContent).i18n[lang]||{};
      return d[key]||fallback;
    }catch(e){ return fallback; }
  }
  function post(url, body, ok, tr){
    var x=new XMLHttpRequest();
    x.open('POST', url, true);
    x.setRequestHeader('Content-Type','application/json');
    x.onreadystatechange=function(){
      if(x.readyState!==4) return;
      if(x.status>=200 && x.status<300){
        var data={};
        try{ data=JSON.parse(x.responseText); }catch(e){}
        say(t('saved','saved'), false, tr);
        if(ok) ok(data);
      } else {
        // Loudly, and without clearing the field: the text the user typed is
        // the only copy if the write did not land.
        var msg=t('saveFailed','not saved');
        try{ msg += ' — ' + (JSON.parse(x.responseText).error||x.status); }
        catch(e){ msg += ' — ' + x.status; }
        say(msg, true, tr);
      }
    };
    x.send(JSON.stringify(body||{}));
  }
  function rowId(el){
    var tr=el; while(tr && tr.tagName!=='TR') tr=tr.parentNode;
    return tr ? tr : null;
  }
  // --- star toggle ---
  table.addEventListener('click', function(ev){
    var btn=ev.target;
    while(btn && btn!==table && !(btn.classList && btn.classList.contains('starbtn'))) btn=btn.parentNode;
    if(!btn || btn===table || btn.id==='starfilter') return;
    var tr=rowId(btn); if(!tr) return;
    var on=tr.getAttribute('data-starred')==='1';
    var next=!on;
    tr.setAttribute('data-starred', next?'1':'0');
    btn.setAttribute('aria-pressed', next?'true':'false');
    btn.querySelector('.glyph').textContent = next?'★':'☆';
    applyFilter();
    post('/api/annotate/'+tr.getAttribute('data-vid'), {starred: next}, null, tr);
  });
  // --- group select ---
  table.addEventListener('change', function(ev){
    var sel=ev.target;
    if(!sel.classList || !sel.classList.contains('groupsel')) return;
    var tr=rowId(sel); if(!tr) return;
    post('/api/annotate/'+tr.getAttribute('data-vid'),
         {group_id: sel.value ? parseInt(sel.value,10) : null}, null, tr);
  });
  // --- note, debounced ---
  // `saved` holds the value the server has confirmed for a row. It is what
  // makes "is this row dirty?" answerable at any moment — including while a
  // request is still in flight, which a pending-timer check cannot see.
  var timers={}, saved={};
  function sendNote(vid, tr, inp){
    var v=inp.value;   // capture: inp.value can change before the reply lands
    post('/api/annotate/'+vid, {note: v}, function(){ saved[vid]=v; }, tr);
  }
  table.addEventListener('input', function(ev){
    var inp=ev.target;
    if(!inp.classList || !inp.classList.contains('noteinput')) return;
    var tr=rowId(inp); if(!tr) return;
    var vid=tr.getAttribute('data-vid');
    clearTimeout(timers[vid]);
    timers[vid]=setTimeout(function(){
      timers[vid]=0;
      sendNote(vid, tr, inp);
    }, 600);
  });
  // Typing then closing the tab inside the debounce window would lose the note.
  table.addEventListener('focusout', function(ev){
    var inp=ev.target;
    if(!inp.classList || !inp.classList.contains('noteinput')) return;
    var tr=rowId(inp); if(!tr) return;
    var vid=tr.getAttribute('data-vid');
    if(timers[vid]){
      clearTimeout(timers[vid]); timers[vid]=0;
      sendNote(vid, tr, inp);
    }
  });
  // focusout only covers leaving the field. Closing the tab or the window
  // fires neither it nor the pending timer, so a note typed in the last 600ms
  // was simply lost — and silently, which is the worst version of it. A beacon
  // is the one send the browser still delivers while the page is going away;
  // an XHR at this point is not guaranteed to leave. The server reads the body
  // as JSON regardless of Content-Type, so this needs no endpoint of its own.
  //
  // The test is "does this row differ from what the server confirmed", not "is
  // a timer pending". A pending timer is only one of the three ways a row can
  // be dirty at this moment: the debounce may have already fired and left a
  // request in flight, or an earlier save may have failed outright. Checking
  // the value covers all three; checking the timer covered one and quietly
  // dropped the others.
  window.addEventListener('pagehide', function(){
    if(!navigator.sendBeacon) return;
    [].forEach.call(table.querySelectorAll('.noteinput'), function(inp){
      var tr=rowId(inp); if(!tr) return;
      var vid=tr.getAttribute('data-vid');
      // defaultValue is what the server rendered, so an untouched row is clean
      // without needing to seed `saved` for all of them up front.
      var known=(vid in saved) ? saved[vid] : inp.defaultValue;
      if(inp.value===known) return;
      if(timers[vid]){ clearTimeout(timers[vid]); timers[vid]=0; }
      try{
        navigator.sendBeacon('/api/annotate/'+vid,
          new Blob([JSON.stringify({note: inp.value})], {type:'application/json'}));
      }catch(e){}
    });
  });
  // --- header star = filter ---
  var filter=document.getElementById('starfilter');
  function applyFilter(){
    var on=filter && filter.getAttribute('aria-pressed')==='true';
    [].forEach.call(table.tBodies[0].rows, function(tr){
      tr.style.display = (on && tr.getAttribute('data-starred')!=='1') ? 'none' : '';
    });
  }
  if(filter){
    filter.addEventListener('click', function(){
      var on=filter.getAttribute('aria-pressed')==='true';
      filter.setAttribute('aria-pressed', on?'false':'true');
      filter.querySelector('.glyph').textContent = on?'☆':'★';
      applyFilter();
    });
  }
  // --- add a group ---
  var ng=document.getElementById('newgroup'), add=document.getElementById('addgroup');
  function addGroup(){
    var name=(ng.value||'').trim();
    if(!name) return;
    post('/api/groups', {name: name}, function(data){
      if(!data.group) return;
      [].forEach.call(table.querySelectorAll('select.groupsel'), function(sel){
        var o=document.createElement('option');
        o.value=data.group.id; o.textContent=data.group.name;
        sel.appendChild(o);
      });
      // The delete picker is offered the new group too, or it would only ever
      // list what happened to exist when the page was rendered.
      var del=document.getElementById('delgroup');
      if(del){
        var d=document.createElement('option');
        d.value=data.group.id; d.textContent=data.group.name+' (0)';
        del.appendChild(d);
      }
      ng.value='';
    });
  }
  if(add) add.addEventListener('click', addGroup);
  if(ng) ng.addEventListener('keydown', function(ev){ if(ev.key==='Enter') addGroup(); });
  // --- delete a group ---
  // Deliberately no blocking browser dialog: a modal freezes the whole page
  // (and anything driving it), and this action is cheap to undo — notes and
  // stars survive, only the filing is cleared. The row count in each label
  // carries the warning instead.
  var del=document.getElementById('delgroup'), rm=document.getElementById('rmgroup');
  if(rm && del){
    rm.addEventListener('click', function(){
      var gid=del.value;
      if(!gid) return;
      post('/api/groups/'+gid, {delete: true}, function(){
        // Drop it everywhere it is offered and un-file the rows that used it:
        // the server already did, and a stale <select> would lie about it.
        [].forEach.call(table.querySelectorAll('select.groupsel'), function(sel){
          var opt=sel.querySelector('option[value="'+gid+'"]');
          if(!opt) return;
          if(sel.value===gid) sel.value='';
          opt.parentNode.removeChild(opt);
        });
        var picked=del.querySelector('option[value="'+gid+'"]');
        if(picked) picked.parentNode.removeChild(picked);
        del.value='';
      });
    });
  }
})();
"""


def _row_meta(v: Dict[str, Any]) -> str:
    """The chips that follow a title: decoded structure, and the no-transcript
    caveat. Same content as the nav list, so the two surfaces read alike."""
    struct = ('<span class="sc">%s</span>' % _e(v["content_structure"])
              if v["content_structure"] else "")
    notx = ('<span class="sc warn"><span data-i18n="noTranscript">'
            '⚠ no transcript</span></span>') if not v.get("has_transcript") else ""
    return struct + notx


def render_library_table(views: List[Dict[str, Any]], href: Callable[[str], str],
                         annotations: Dict[str, Dict[str, Any]],
                         groups: List[Dict[str, Any]]) -> str:
    """The served library list: a real table, because it now carries per-row
    controls (star, group, note) that a list of links has nowhere to put.

    The take-home export keeps `render_index` — it has no server to write to, and
    the decision was that annotations are working state, not something that ships
    to a reader. Two renderers, one deliberate difference.
    """
    opts_blank = ('<option value="" data-i18n="groupNone">— none —</option>')
    rows = []
    for v in views:
        vid = v["video_id"]
        ann = annotations.get(vid) or {}
        starred = bool(ann.get("starred"))
        note = ann.get("note") or ""
        gid = ann.get("group_id")
        opts = [opts_blank]
        for g in groups:
            opts.append('<option value="%d"%s>%s</option>' % (
                int(g["id"]), " selected" if gid == g["id"] else "", _e(g["name"])))
        overall = ""
        if v["score"] and v["score"].get("overall") is not None:
            overall = "%.1f" % v["score"]["overall"]
        rows.append(
            '<tr data-vid="%s" data-starred="%d">'
            '<td class="c-star"><button type="button" class="starbtn" '
            'aria-pressed="%s"><span class="glyph">%s</span></button></td>'
            '<td class="c-title"><a href="%s">%s</a>%s</td>'
            '<td class="c-group"><select class="groupsel">%s</select></td>'
            '<td class="c-score">%s</td>'
            '<td class="c-note"><div class="notewrap">'
            '<input type="text" class="noteinput" value="%s" '
            'maxlength="%d" data-i18n-ph="notePlaceholder" '
            'placeholder="what is this one for?">'
            '<span class="rowhint" aria-live="polite"></span></div></td>'
            '</tr>' % (
                _e(vid), 1 if starred else 0,
                "true" if starred else "false", "★" if starred else "☆",
                _e(href(vid)), _e(v["title"]), _row_meta(v),
                "".join(opts), overall, _e(note), annotate.MAX_NOTE_LEN))

    head = (
        '<table class="library" id="library"><thead><tr>'
        # The header star is the filter: one glyph that means "show me only the
        # ones I marked". Its pressed state is what the rows below react to.
        '<th class="c-star"><button type="button" id="starfilter" class="starbtn hdr" '
        'aria-pressed="false" data-i18n-title="onlyStarred" title="show starred only">'
        '<span class="glyph">☆</span></button></th>'
        '<th class="c-title" data-i18n="col.title">Title</th>'
        '<th class="c-group" data-i18n="col.group">Group</th>'
        '<th class="c-score" data-i18n="col.score">Score</th>'
        '<th class="c-note" data-i18n="col.note">Note</th>'
        '</tr></thead><tbody>')
    tail = '</tbody></table>'
    # Above the table, not below it: the library is the whole corpus, and a
    # control parked after 96 rows is a control nobody finds. The save hint
    # lives here too, so it is on screen while you are typing in a top row.
    # Deleting is a picker plus a button rather than an X on each dropdown row:
    # a group is a library-wide object, and putting its destructor inside a
    # per-video control invites deleting the group when you meant to unfile one clip.
    manage = ['<select id="delgroup"><option value="" data-i18n="groupPick">'
              '— pick a group —</option>']
    for g in groups:
        manage.append('<option value="%d">%s (%d)</option>'
                      % (int(g["id"]), _e(g["name"]), int(g.get("video_count") or 0)))
    manage.append('</select>')
    newgroup = (
        '<div class="libtools">'
        '<input type="text" id="newgroup" maxlength="%d" data-i18n-ph="newGroupPh" '
        'placeholder="new group name">'
        '<button type="button" id="addgroup" data-i18n="addGroup">add group</button>'
        '<span class="toolsep"></span>'
        '%s'
        '<button type="button" id="rmgroup" data-i18n="removeGroup">delete group</button>'
        '<span class="savehint" id="savehint"></span>'
        '</div>' % (annotate.MAX_GROUP_NAME_LEN, "".join(manage)))
    return newgroup + head + "".join(rows) + tail


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
    # One query for every annotation rather than one per row: the library is the
    # page most likely to hold the whole corpus at once.
    table = render_library_table(views, href=href,
                                 annotations=annotate.all_annotations(conn),
                                 groups=annotate.list_groups(conn))
    return render_page([], table, title, extra_js=ANNOTATE_JS, sub_key="subLive")


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
