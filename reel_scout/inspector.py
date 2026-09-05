"""Interactive single-clip inspector — a small local web app.

Ported from arkiv's live Inspector: the video player is the single source of
truth, and a waveform, keyframe filmstrip, and transcript all sync to (and seek)
it. `reel-scout inspect <id>` starts a local server and opens the clip's page.

Unlike the read-only `viewer` (a static bundle / browsing server), this is an
interactive review surface for ONE reel:

  * a real <video> player streaming the downloaded file (HTTP range requests);
  * a waveform (ffmpeg peaks, cached) with a playhead and click-to-seek;
  * a keyframe filmstrip that seeks the player and tracks playback;
  * transcript segments that seek on click and highlight as they are spoken;
  * IN/OUT markers you can set/drag, materialized into a trimmed SRT on export.

It needs the video file present + a running server (it is not an offline file).
Craft scores stay labelled a reference, not an authority, per the honesty line.
"""
from __future__ import annotations

import array
import html
import json
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from . import (annotate, config, db, i18n, label_shots,
               marks as marks_mod, theme)
from .viewer import build_video_view
from .utils import paths as media_paths

_SCORE_DIMS = [
    ("overall", "Overall"),
    ("hook_strength", "Hook"),
    ("visual_storytelling", "Visual"),
    ("pacing", "Pacing"),
    ("structure", "Structure"),
]

_WAVEFORM_BINS = 200

# UI-chrome translations now live in reel_scout.i18n (shared with the viewer so
# the two pages cannot drift). Kept here as I18N for the existing call sites and
# tests. Scope is unchanged: interface chrome only, never model output.
I18N = i18n.STRINGS


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _t(key: str, en: Optional[str] = None) -> str:
    """Inline translatable label: English baseline text + a data-i18n key that
    applyLang() swaps client-side. `en` defaults to the English dict entry."""
    text = en if en is not None else I18N["en"].get(key, key)
    return '<span data-i18n="%s">%s</span>' % (_e(key), _e(text))


def _fmt_ts(sec: Any) -> str:
    try:
        s = int(round(float(sec)))
    except (TypeError, ValueError):
        s = 0
    if s < 0:
        s = 0
    return "%d:%02d" % (s // 60, s % 60)


# --- Media file resolution ---

def resolve_video_file(file_path: Optional[str]) -> Optional[str]:
    """Absolute path to a video's file, or None.

    Delegates to the shared resolver so this no longer depends on the working
    directory: ``os.path.abspath`` used to anchor a stored ``./data/videos/x``
    to whatever cwd the process started in, and the VIDEOS_DIR fallback was
    itself cwd-relative, so both candidates missed from anywhere but the repo
    root."""
    if not file_path:
        return None
    return media_paths.resolve_media_path(file_path) if media_paths.exists(file_path) else None


# --- Data assembly ---

def _shot_grammar(conn: db.sqlite3.Connection, video_id: str) -> Dict[str, Any]:
    """Per-shot size and movement, plus how much of the clip the question fits.

    🔴 **The ratio ships beside the codes, never without them.** Measured on
    2026-09-03: across 94 labelled clips, 40 have *no* frame the shot-size
    vocabulary applies to -- they are screen recordings, title cards and
    composite layouts, and their codes mean nothing. A page showing the codes
    alone would present those forty as confidently as the two that scored 100%.

    Reads what is stored; measures nothing. A clip that has been through
    neither `shot-size` nor `motion` renders no block rather than an empty one.
    """
    # Precedence is stated here, not read off the sort order. `supplied` is a
    # person's own call and outranks the model's -- but that used to hold only
    # because 'supplied' sorted after 'vlm'. It does not sort after 'gate', so
    # the moment a gated UNKNOWN and a human correction shared a frame, the
    # UNKNOWN would have covered the correction. The old test passed on the
    # alphabet, not on the rule.
    sizes: Dict[float, Any] = {}
    ranked: Dict[float, int] = {}
    for row in db.get_shot_labels(conn, video_id, kind="shot_size"):
        rank = 2 if row["source"] == "supplied" else 1
        if rank >= ranked.get(row["t_sec"], 0):
            sizes[row["t_sec"]] = row["value"]
            ranked[row["t_sec"]] = rank
    # An orphan -- a label whose frame is gone after `--force-keyframes` --
    # keeps its row but must not win a span from a label that was actually
    # measured this run. Sizes are matched to a shot by span, not equality, so
    # without this the earliest timestamp in the span wins and that is often
    # the orphan.
    live = db.live_label_timestamps(conn, video_id)
    motions = {r["t_sec"]: r for r in db.get_shot_motion(conn, video_id)}
    shots = conn.execute(
        "SELECT idx, start_sec, end_sec FROM shots WHERE video_id = ? "
        "ORDER BY idx", (video_id,)).fetchall()
    rows = []
    for shot in shots:
        start = shot["start_sec"]
        # A size hangs off its representative frame, which sits *inside* the
        # span rather than on its edge -- so match by span, not by equality.
        # Motion hangs off the span start, so that lookup is exact.
        size = None
        for want_live in (True, False):
            for t_sec, value in sorted(sizes.items()):
                if (t_sec in live) is want_live and start <= t_sec < shot["end_sec"]:
                    size = value
                    break
            if size is not None:
                break
        m = motions.get(start)
        if size is None and m is None:
            continue
        rows.append({"idx": shot["idx"], "start": start,
                     "end": shot["end_sec"], "size": size,
                     "movement": m["movement"] if m else None,
                     "speed": m["speed"] if m else None,
                     "agreement": m["agreement"] if m else None})
    scaled, labelled = label_shots.analysable(conn, video_id)
    return {"rows": rows, "scaled": scaled, "labelled": labelled}


def build_inspect_view(conn: db.sqlite3.Connection, video_id: str) -> Optional[Dict[str, Any]]:
    """One clip's inspector payload, or None if unknown. Extends the viewer view
    with per-segment transcript, a resolved duration, and video-file presence."""
    view = build_video_view(conn, video_id)
    if view is None:
        return None

    video = db.get_video(conn, video_id)
    transcript = db.get_transcript(conn, video_id)
    segments: List[Dict[str, Any]] = []
    language = None
    if transcript is not None:
        language = transcript["language"]
        raw = transcript["segments_json"]
        if raw:
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = []
            for s in parsed:
                if not isinstance(s, dict):
                    continue
                try:
                    start = float(s.get("start", 0.0) or 0.0)
                    end = float(s.get("end", start) or start)
                except (TypeError, ValueError):
                    continue
                text = (s.get("text") or "").strip()
                if not text:
                    continue
                segments.append({"start": start, "end": end, "text": text})

    candidates = [view.get("duration_sec") or 0.0]
    if segments:
        candidates.append(max(s["end"] for s in segments))
    for kf in view["keyframes"]:
        if kf.get("timestamp_sec") is not None:
            candidates.append(float(kf["timestamp_sec"]))
    duration = max(candidates) if candidates else 0.0

    file_path = video["file_path"] if video is not None else None
    # The operator's own timeline. Read here rather than in the renderer so the
    # frozen export and the live page get it from the same place -- and so the
    # one caller that must NOT ship them (the take-home bundle) has a single,
    # visible thing to withhold.
    view["shot_grammar"] = _shot_grammar(conn, video_id)
    view["marks"] = marks_mod.list_for(conn, video_id)
    view["segments"] = segments
    view["language"] = language
    view["duration"] = duration
    view["file_path"] = file_path
    view["has_video"] = resolve_video_file(file_path) is not None
    # Belt and braces: no row in the corpus today has segments with an empty
    # text_full, but if one ever did the page would otherwise render the
    # segments AND a note saying there is no transcript, side by side.
    if segments:
        view["has_transcript"] = True
    return view


# --- Waveform ---

def compute_waveform(path: str, bins: int = _WAVEFORM_BINS) -> Optional[List[float]]:
    """Decode mono 8kHz PCM via ffmpeg and return `bins` peak amplitudes (0..1).

    Ported from arkiv, minus the numpy dependency: s16le samples are little-
    endian (ffmpeg emits LE; reel-scout's fleet is x86/arm little-endian), read
    via array('h'). Returns None on ffmpeg failure so the caller degrades to a
    flat bar rather than erroring."""
    cmd = [config.FFMPEG_BIN, "-v", "quiet", "-i", path,
           "-ac", "1", "-ar", "8000", "-f", "s16le", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    samples = array.array("h")
    try:
        samples.frombytes(r.stdout[: len(r.stdout) - (len(r.stdout) % 2)])
    except ValueError:
        return None
    n = len(samples)
    if n == 0:
        return [0.0] * bins
    peaks: List[float] = []
    for i in range(bins):
        a = n * i // bins
        b = n * (i + 1) // bins
        if b <= a:
            peaks.append(0.0)
            continue
        hi = 0
        for x in samples[a:b]:
            ax = -x if x < 0 else x
            if ax > hi:
                hi = ax
        peaks.append(hi / 32768.0)
    return peaks


def _waveform_payload(conn: db.sqlite3.Connection, video_id: str,
                      bins: int = _WAVEFORM_BINS) -> Dict[str, Any]:
    """Peaks for a video, cached under DATA_DIR/waveforms/<id>_<bins>.json."""
    bins = max(8, min(500, bins))
    video = db.get_video(conn, video_id)
    path = resolve_video_file(video["file_path"]) if video is not None else None
    if path is None:
        return {"video_id": video_id, "bins": bins, "peaks": [0.0] * bins}
    cache_dir = os.path.join(config.DATA_DIR, "waveforms")
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, "%s_%d.json" % (video_id, bins))
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    peaks = compute_waveform(path, bins) or [0.0] * bins
    payload = {"video_id": video_id, "bins": bins, "peaks": peaks}
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError:
        pass
    return payload


# --- HTML rendering ---

def _render_scores(view: Dict[str, Any]) -> str:
    score = view.get("score")
    if not score:
        return ""
    meters: List[str] = []
    for key, label in _SCORE_DIMS:
        val = score.get(key)
        if val is None:
            continue
        pct = max(0.0, min(100.0, float(val) * 10.0))
        # `overall` is the only meter JS ever rewrites — the dimension meters are
        # model output and stay frozen no matter where the sliders go.
        meters.append(
            '<div class="meter" data-dim="%s"><span class="mlabel" data-i18n="dim.%s">%s</span>'
            '<span class="mbar"><i%s style="width:%.1f%%"></i></span>'
            '<span class="mval"%s>%.1f</span></div>'
            % (_e(key), _e(key), _e(label),
               ' id="obar"' if key == "overall" else "",
               pct,
               ' id="oval"' if key == "overall" else "",
               float(val)))
    if not meters:
        return ""
    reasoning = score.get("reasoning")
    note = ('<p class="reasoning">%s</p>' % _e(reasoning)) if reasoning else ""
    # Which model produced these (from master). The same clip scores 7.43 under
    # one VLM and 5.5 under another, so a number with no origin cannot be compared
    # with anything — and `stats` averages agent-scored and locally-scored rows
    # together. This is a model identifier, not translatable chrome.
    model = score.get("model_used")
    source = ('<span class="q">%s</span>' % _e(model)) if model else ""

    # Weight sliders. Deliberately collapsed by default: the point is that the
    # weighting is *available* for inspection, not that every reader must tune it.
    sliders: List[str] = []
    for key, label in _SCORE_DIMS:
        if key == "overall" or score.get(key) is None:
            continue
        w = config.SCORE_WEIGHTS.get(key)
        if w is None:
            continue
        sliders.append(
            '<div class="wrow"><label for="w_%s" data-i18n="dim.%s">%s</label>'
            '<input type="range" id="w_%s" data-dim="%s" min="0" max="100" step="1" value="%d">'
            '<span class="wval" id="wv_%s">%d%%</span></div>'
            % (_e(key), _e(key), _e(label), _e(key), _e(key),
               round(w * 100), _e(key), round(w * 100)))

    weights_block = ""
    if sliders:
        weights_block = (
            '<details class="weights" id="wpanel"><summary data-i18n="reweightSummary">'
            '%s</summary>'
            '<p class="wnote" data-i18n="reweightNote">%s</p>'
            '<div class="wrows">%s</div>'
            '<div class="wfoot"><span id="wdelta" class="wdelta"></span>'
            '<button type="button" id="wreset" class="tbtn" data-i18n="reset">%s</button>'
            '</div></details>'
            % (_e(I18N["en"]["reweightSummary"]), _e(I18N["en"]["reweightNote"]),
               "".join(sliders), _e(I18N["en"]["reset"])))

    return ('<section class="block"><div class="eyebrow">%s '
            '<span class="q" data-i18n="refNotAuthority">reference, not authority</span>%s</div>'
            '<div class="meters">%s</div>%s%s</section>'
            % (_t("craftScores", "Craft scores"), source,
               "".join(meters), note, weights_block))


def _render_structure(view: Dict[str, Any]) -> str:
    hook = view.get("hook") or {}
    style = view.get("style") or {}
    rows = [
        ("Structure", view.get("content_structure")),
        ("Content", view.get("content_type")),
        ("Format", style.get("format")),
        ("Pacing", style.get("pacing")),
        ("Hook", hook.get("opening_type")),
        ("Hook text", hook.get("opening_text")),
        ("CTA", hook.get("cta_type")),
        ("CTA text", hook.get("cta_text")),
    ]
    # The row label is always translatable. The value is too when it comes from
    # a closed vocabulary; `value_key` returns None for free text (a hook line,
    # a CTA phrase), which then renders raw in both languages.
    cells = []
    for k, v in rows:
        if not v:
            continue
        vk = i18n.value_key(v)
        mv = ('<div class="mv" data-i18n="%s">%s</div>' % (vk, _e(v))) if vk \
            else '<div class="mv">%s</div>' % _e(v)
        cells.append('<div class="mk" data-i18n="row.%s">%s</div>%s' % (_e(k), _e(k), mv))
    if not cells:
        return ""
    return ('<section class="block"><div class="eyebrow" data-i18n="decoded">Decoded structure</div>'
            '<div class="metagrid">%s</div></section>' % "".join(cells))


def _cell(css: str, value: str) -> str:
    """A value cell, translatable only when the value is in a closed vocabulary.

    Follows `_render_structure`: `value_key` returns None for anything outside
    the vocabularies, and a `data-i18n` key that does not exist is dead markup
    — harmless today only because `applyLang` skips unknown keys.
    """
    key = i18n.value_key(value)
    if key is None:
        return '<span class="%s">%s</span>' % (css, _e(value))
    return '<span class="%s" data-i18n="%s">%s</span>' % (css, key, _e(value))


def _render_shot_grammar(view: Dict[str, Any]) -> str:
    grammar = view.get("shot_grammar") or {}
    rows = grammar.get("rows") or []
    if not rows:
        return ""
    labelled = grammar.get("labelled") or 0
    scaled = grammar.get("scaled") or 0
    note = ""
    if labelled:
        pct = int(round(100.0 * scaled / labelled))
        cls = "warn" if scaled * 2 < labelled else ""
        note = ('<div class="q %s"><span data-i18n="analysable">analysable</span> '
                '%d%% (%d/%d)</div>' % (cls, pct, scaled, labelled))
    cells = []
    for r in rows:
        size = r["size"] or "-"
        move = r["movement"] or "-"
        # Both vocabularies are closed, so both translate. The numbers travel
        # with the movement class because a class with no measurement under it
        # is the shape this project keeps paying for.
        ev = ""
        if r.get("speed") is not None:
            ev = ('<span class="q">%.1f px &middot; %d%%</span>'
                  % (r["speed"], int(round(100 * (r["agreement"] or 0)))))
        cells.append(
            '<div class="sgrow"><span class="q">%s</span>%s%s%s</div>'
            % (_fmt_ts(r["start"]), _cell("sgz", size), _cell("sgm", move), ev))
    return ('<section class="block"><div class="eyebrow" data-i18n="shotGrammar">'
            'Shot grammar</div>%s<div class="sglist">%s</div></section>'
            % (note, "".join(cells)))


def render_inspector(view: Dict[str, Any], base: str = "",
                     video_src: Optional[str] = None,
                     peaks: Optional[List[float]] = None,
                     embed_fonts: bool = False,
                     cjk_woff2: bytes = b"",
                     keyframe_src: Optional[Any] = None,
                     back_href: Optional[str] = None) -> str:
    """Full inspector page for one clip.

    Live server: `base` prefixes API/asset URLs and the page fetches its
    waveform and streams its video from the server.

    Frozen export: pass `video_src` (a data URI), `peaks` (inlined, because
    file:// blocks fetch) and the font options — the page then depends on
    nothing outside itself. One renderer, two modes; there is deliberately no
    second frozen renderer to drift out of sync."""
    vid = view["video_id"]
    dur = float(view.get("duration") or 0.0)
    if keyframe_src is None:
        keyframe_src = lambda kf: "%s/keyframe/%s" % (base, kf["id"])  # noqa: E731
    # Only rendered when the caller knows an index exists to go back to — a
    # single-reel export has none, and `inspect <id>` pins "/" to this very page.
    back = ('<a class="back" href="%s" data-i18n="allReels">&larr; all reels</a>' % _e(back_href)) \
        if back_href else ""

    meta_bits = [_e(view["platform"])]
    if view.get("uploader"):
        meta_bits.append("@%s" % _e(view["uploader"]))
    # Whisper reports a language even when it transcribed nothing, so a
    # music-only reel used to be labelled `en` in the header while the
    # transcript panel below said there was no speech. Show it only when there
    # are words for it to describe.
    if view.get("language") and view.get("has_transcript"):
        meta_bits.append(_e(view["language"]))
    meta_bits.append(_fmt_ts(dur))
    meta = " / ".join(meta_bits)

    # Preview: a real player when the file is present, else the frames-only note.
    if view.get("has_video"):
        src = video_src if video_src else ("%s/api/stream/%s" % (base, _e(vid)))
        preview = ('<video id="player" class="player" preload="metadata" '
                   'controls playsinline src="%s"></video>' % src)
    else:
        preview = ('<div class="noplayer" data-i18n="noVideo">video file not on disk &mdash; '
                   'keyframes &amp; transcript only</div>')

    # Marks — the operator's own timeline, in two places that must agree: ticks
    # on the waveform (where in the clip) and a list under it (what and why).
    # Both are built from the same rows, so a mark cannot appear in one and not
    # the other.
    #
    # x is in the waveform's own coordinate space (viewBox 0..bins), not pixels
    # or percent, so the ticks sit in the same system as the bars and the
    # playhead. drawBars() re-writes the viewBox when the served peak count
    # differs from _WAVEFORM_BINS, which is why each line also carries data-t
    # and the script repositions them there rather than trusting this number
    # forever.
    mark_rows = list(view.get("marks") or [])
    wf_marks = ""
    marks_block = ""
    if mark_rows:
        ticks = []
        for m in mark_rows:
            t = float(m.get("t_sec") or 0.0)
            x = (t / dur * _WAVEFORM_BINS) if dur > 0 else 0.0
            ticks.append(
                '<line class="mktick" data-t="%.3f" x1="%.3f" y1="0" x2="%.3f" y2="60">'
                '<title>%s</title></line>'
                % (t, x, x, _e("%s — %s" % (_fmt_ts(t), m.get("label") or ""))))
        wf_marks = '<g id="marks">%s</g>' % "".join(ticks)

        rows = []
        for m in mark_rows:
            t = float(m.get("t_sec") or 0.0)
            note = m.get("note") or ""
            rows.append(
                '<button class="mkrow" data-ts="%.3f">'
                '<span class="mkts">%s</span>'
                '<span class="mklabel">%s</span>'
                '<span class="mknote">%s</span></button>'
                % (t, _e(_fmt_ts(t)), _e(m.get("label") or ""), _e(note)))
        marks_block = ('<section class="block"><div class="eyebrow">%s '
                       '<span class="q">%d &middot; '
                       '<span data-i18n="seek">click to seek</span></span></div>'
                       '<div class="mklist">%s</div></section>'
                       % (_t("marks", "Marks"), len(rows), "".join(rows)))

    # Filmstrip. Each cell carries what was seen in that frame, so the exported
    # page keeps the evidence and not just the thumbnails — a bundle that shows a
    # score with no observations behind it is asking to be taken on faith.
    strip: List[str] = []
    described = 0
    for j, kf in enumerate(view["keyframes"]):
        ts = kf.get("timestamp_sec")
        desc = (kf["description"] if "description" in kf.keys() else "") or ""
        if desc:
            described += 1
        strip.append(
            '<button class="cell" data-frame="%d" data-ts="%.3f" data-desc="%s" title="%s">'
            '<img src="%s" alt="" loading="lazy">'
            '<span class="ct">%s</span></button>'
            % (j, float(ts) if ts is not None else 0.0, _e(desc),
               _e(("%s — %s" % (_fmt_ts(ts), desc)) if desc else _fmt_ts(ts)),
               _e(keyframe_src(kf) or ""), _e(_fmt_ts(ts))))
    if strip:
        # Numbers stay language-neutral; only the words carry data-i18n, so the
        # count survives a language switch untouched.
        note = '<span data-i18n="seek">click to seek</span>'
        if described:
            note += (' &middot; %d <span data-i18n="described">described</span>' % described)
        caption = ('<div class="kfdesc" id="kfdesc"></div>' if described else "")
        filmstrip = ('<section class="block"><div class="eyebrow">%s '
                     '<span class="q">%d &middot; %s</span></div>'
                     '<div class="strip">%s</div>%s</section>'
                     % (_t("keyframes", "Keyframes"), len(strip), note,
                        "".join(strip), caption))
    else:
        filmstrip = ""

    # Transcript.
    if view.get("segments"):
        rows = ['<button class="seg" data-start="%.3f" data-end="%.3f">'
                '<span class="tc">%s</span><span class="tx">%s</span></button>'
                % (s["start"], s["end"], _e(_fmt_ts(s["start"])), _e(s["text"]))
                for s in view["segments"]]
        transcript = ('<section class="block"><div class="eyebrow">%s '
                      '<span class="q" data-i18n="seek">click to seek</span></div>'
                      '<div class="segs">%s</div></section>'
                      % (_t("transcript", "Transcript"), "".join(rows)))
    elif view.get("transcript"):
        transcript = ('<section class="block"><div class="eyebrow">%s</div>'
                      '<div class="flat">%s</div></section>'
                      % (_t("transcript", "Transcript"), _e(view["transcript"])))
    else:
        # Rendering nothing here is what made a music-only reel indistinguishable
        # from one whose transcription simply never ran -- and the craft score
        # above says nothing either way. Say it plainly instead: the score is
        # still shown, it just rests on the visual layer alone.
        transcript = ('<section class="block"><div class="eyebrow">%s</div>'
                      '<div class="flat empty" data-i18n="noTranscriptNote">%s</div>'
                      '</section>'
                      % (_t("noTranscript"), _e(I18N["en"]["noTranscriptNote"])))

    summary = ('<p class="summary">%s</p>' % _e(view["summary"])) if view.get("summary") else ""

    # Waveform peaks + segments are handed to JS via a JSON island (escaped).
    seg_data = [[s["start"], s["end"]] for s in view.get("segments", [])]
    boot_data = {"id": vid, "dur": dur, "bins": _WAVEFORM_BINS,
                 "base": base, "segs": seg_data,
                 "hasVideo": bool(view.get("has_video"))}
    # Re-weighting happens entirely client-side: the four dimensions are already
    # on the page, so the reader gets an instant answer with no request and no
    # model call. Handing over the default weights too is what makes the
    # "yours vs default" delta honest rather than a moving baseline.
    _score = view.get("score") or {}
    if _score:
        boot_data["dims"] = {d: _score.get(d) for d in config.SCORE_DIMENSIONS
                             if _score.get(d) is not None}
        boot_data["defaultWeights"] = dict(config.SCORE_WEIGHTS)
        boot_data["storedOverall"] = _score.get("overall")
    if peaks is not None:
        # file:// blocks fetch(), so a frozen page carries its peaks inline.
        boot_data["peaks"] = [round(float(x), 4) for x in peaks]
    # Both languages travel with the page so the toggle is a pure client swap —
    # no round-trip, and a frozen export stays fully bilingual offline.
    boot_data["i18n"] = I18N
    boot = json.dumps(boot_data, ensure_ascii=False)

    # Language toggle (shared markup). Inspector's own inline JS wires it and
    # drives the waveform/reweight dynamic strings; see _SCRIPT.
    langtoggle = i18n.TOGGLE_HTML

    body = (
        '<header class="top">%s%s<div class="eyebrow"><span data-i18n="brand">'
        'reel-scout inspect</span> <span class="q">%s</span></div><h1>%s</h1>'
        '<p class="meta">%s &middot; <a href="%s" rel="noopener" data-i18n="source">'
        'source &#8599;</a></p>'
        '%s</header>'
        '<section class="block preview">%s</section>'
        '<section class="block"><div class="eyebrow">%s '
        '<span class="q" id="io" data-i18n="noIO">no in/out</span></div>'
        '<div class="wf" id="wf"><svg id="wfsvg" viewBox="0 0 %d 60" preserveAspectRatio="none">'
        '<g id="bars"></g><rect id="sel" class="sel" x="0" y="0" width="0" height="60"></rect>'
        '%s'
        '<line id="head" class="head" x1="0" y1="0" x2="0" y2="60"></line></svg></div>'
        '<div class="wfbtns"><button id="setin" class="tbtn" data-i18n="setIn">set IN</button>'
        '<button id="setout" class="tbtn" data-i18n="setOut">set OUT</button>'
        '<button id="clrio" class="tbtn" data-i18n="clear">clear</button>'
        '<button id="srt" class="tbtn" data-i18n="exportSrt">export SRT (window)</button>'
        '</div></section>'
        '%s%s%s%s%s%s'
        % (back, langtoggle, _e(vid), _e(view["title"]), meta, _e(view["url"]),
           summary, preview, _t("waveform", "Waveform"), _WAVEFORM_BINS, wf_marks,
           marks_block, filmstrip, transcript,
           _render_scores(view), _render_structure(view),
           _render_shot_grammar(view)))

    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>%s &mdash; reel-scout inspect</title><style>%s</style></head>'
        '<body><main>%s</main>'
        '<script id="boot" type="application/json">%s</script>'
        '<script>%s</script></body></html>'
        % (_e(view["title"]),
           theme.stylesheet(_COMPONENTS, embed_fonts=embed_fonts, cjk_woff2=cjk_woff2),
           body, boot, _SCRIPT))


# --- Server ---

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _parse_range(header: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    """Parse a single-range 'bytes=a-b' into (start, end) inclusive, or None."""
    if not header:
        return None
    m = _RANGE_RE.match(header.strip())
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    if a == "":
        if b == "":
            return None
        length = min(int(b), size)
        return (size - length, size - 1)
    start = int(a)
    end = int(b) if b else size - 1
    end = min(end, size - 1)
    if start > end:
        return None
    return (start, end)


def make_inspect_server(host: str = "127.0.0.1", port: int = 0,
                        default_id: Optional[str] = None):
    """Build (but don't start) the inspector HTTP server. Each request opens its
    own short-lived connection, so the handler is thread-safe. Split from serve()
    so tests can drive it over a real socket."""
    import http.server

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the console quiet
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _stream_file(self, path, ctype):
            size = os.path.getsize(path)
            rng = _parse_range(self.headers.get("Range"), size)
            if rng is None:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                if self.command == "HEAD":
                    return
                with open(path, "rb") as f:
                    self._copy(f, size)
                return
            start, end = rng
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(path, "rb") as f:
                f.seek(start)
                self._copy(f, length)

        def _copy(self, f, remaining):
            chunk = 64 * 1024
            while remaining > 0:
                buf = f.read(min(chunk, remaining))
                if not buf:
                    break
                try:
                    self.wfile.write(buf)
                except (BrokenPipeError, ConnectionResetError):
                    return  # client seeked/closed — normal for <video>
                remaining -= len(buf)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            conn = db.get_connection()
            try:
                self._route(path, conn)
            finally:
                conn.close()

        def do_POST(self):
            """The only write surface in the app, and it is narrow on purpose:
            the annotation endpoints and nothing else. Everything the pipeline
            produced stays read-only over HTTP."""
            path = self.path.split("?", 1)[0]
            if not (path.startswith("/api/annotate/") or path == "/api/groups"
                    or path.startswith("/api/groups/")):
                self._send(404, "not found")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            # A note is small; a multi-megabyte body here is a mistake or an
            # abuse, and reading it first would be the damage.
            if length > 64 * 1024:
                self._send(413, json.dumps({"error": "payload too large"}),
                           "application/json")
                return
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                self._send(400, json.dumps({"error": "invalid JSON"}),
                           "application/json")
                return
            if not isinstance(payload, dict):
                self._send(400, json.dumps({"error": "expected a JSON object"}),
                           "application/json")
                return
            conn = db.get_connection()
            try:
                status, body = annotate.handle_api(conn, "POST", path, payload)
            finally:
                conn.close()
            self._send(status, json.dumps(body, ensure_ascii=False),
                       "application/json; charset=utf-8")

        def _route(self, path, conn):
            if path == "/api/groups" or path.startswith("/api/groups/"):
                status, body = annotate.handle_api(conn, "GET", path, None)
                self._send(status, json.dumps(body, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            if path.startswith("/api/annotate/"):
                status, body = annotate.handle_api(conn, "GET", path, None)
                self._send(status, json.dumps(body, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            if path in ("/", "/inspect", "/inspect/"):
                # One app, not two servers: `inspect <id>` pins a video and opens
                # straight into it, while `view` (no default) lands on the library
                # index whose rows link into this same inspector.
                if default_id:
                    self._inspect(conn, default_id)
                else:
                    from .viewer import render_index_page
                    self._send(200, render_index_page(
                        conn, href=lambda vid: "/inspect/%s" % vid))
                return
            if path.startswith("/video/"):
                # Legacy static detail route — kept so old links/exports resolve,
                # but the inspector is the real detail surface now.
                from .viewer import render_video_page
                page = render_video_page(conn, path[len("/video/"):])
                self._send(200, page) if page else self._send(404, "not found")
                return
            if path.startswith("/inspect/"):
                self._inspect(conn, path[len("/inspect/"):])
                return
            if path.startswith("/api/stream/"):
                self._stream(conn, path[len("/api/stream/"):])
                return
            if path.startswith("/api/waveform/"):
                vid = path[len("/api/waveform/"):]
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                bins = int(qs.get("bins", [_WAVEFORM_BINS])[0])
                self._send(200, json.dumps(_waveform_payload(conn, vid, bins)),
                           "application/json")
                return
            if path.startswith("/keyframe/"):
                self._keyframe(conn, path[len("/keyframe/"):])
                return
            if path.startswith("/font/"):
                self._font(path[len("/font/"):])
                return
            self._send(404, "not found")

        def _font(self, name):
            fp = theme.font_path(name)
            if not os.path.exists(fp):
                self._send(404, "not found")
                return
            with open(fp, "rb") as f:
                self._send(200, f.read(), "font/woff2",
                           {"Cache-Control": "public, max-age=604800"})

        def _inspect(self, conn, vid):
            from .compare import resolve_ref
            resolved, _ = resolve_ref(conn, vid)
            view = build_inspect_view(conn, resolved) if resolved else None
            if view is None:
                self._send(404, "no video matches '%s'" % vid)
                return
            # `view` mode: "/" is the library, so offer a way back. With a
            # pinned default_id, "/" renders THIS page — a link would loop.
            self._send(200, render_inspector(
                view, back_href=None if default_id else "/"))

        def _stream(self, conn, vid):
            video = db.get_video(conn, vid)
            path = resolve_video_file(video["file_path"]) if video is not None else None
            if path is None:
                self._send(404, "video file not available")
                return
            self._stream_file(path, "video/mp4")

        def _keyframe(self, conn, kf_id):
            from .viewer import _keyframe_path
            fp = _keyframe_path(conn, kf_id)
            if not fp:
                self._send(404, "not found")
                return
            with open(fp, "rb") as f:
                self._send(200, f.read(), "image/jpeg")

    return http.server.ThreadingHTTPServer((host, port), _Handler)


def serve(video_id: str, host: str = "127.0.0.1", port: int = 0,
          open_browser: bool = True) -> None:
    """Start the inspector server for one clip. Blocks until interrupted."""
    httpd = make_inspect_server(host=host, port=port, default_id=video_id)
    actual = httpd.server_address[1]
    url = "http://%s:%d/inspect/%s" % (host, actual, video_id)
    print("reel-scout inspect serving at %s  — Ctrl-C to stop" % url)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - headless is fine
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()


_COMPONENTS = """
a{color:inherit}
.eyebrow .q{text-transform:none;letter-spacing:0;color:var(--quiet)}
.top{position:relative;padding:26px 0 16px;border-bottom:2px solid var(--rule)}
.lang{position:absolute;top:26px;right:0;display:flex;gap:2px;font-family:var(--mono)}
.lang .langbtn{border:1px solid var(--rule-soft);background:none;color:var(--quiet);
  font-family:inherit;font-size:10px;letter-spacing:.1em;padding:3px 8px;cursor:pointer;
  line-height:1.4}
.lang .langbtn:first-child{border-radius:3px 0 0 3px}
.lang .langbtn:last-child{border-radius:0 3px 3px 0;border-left:0}
.lang .langbtn:hover{color:var(--ink)}
.lang .langbtn.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.back{display:inline-block;margin-bottom:12px;font-family:var(--mono);font-size:11px;
  letter-spacing:.16em;text-transform:uppercase;color:var(--quiet);text-decoration:none}
.back:hover{color:var(--ink);text-decoration:underline;text-underline-offset:3px}
.top h1{margin:.35rem 0 .3rem;font-family:var(--display);font-weight:400;
  font-size:clamp(24px,3vw,34px);letter-spacing:-.02em;line-height:1.1}
.meta{margin:.1rem 0;color:var(--quiet);font-family:var(--mono);font-size:11px;
  letter-spacing:.1em}
.summary{margin:.7rem 0 0;color:var(--ink-2);max-width:var(--col)}
.block{padding:20px 0;border-bottom:1px solid var(--rule-soft)}
.block .eyebrow{margin-bottom:.6rem}
/* content is the loud part: the player gets the room, no chrome around it */
.preview{display:flex;justify-content:center}
.player{width:100%;max-height:64vh;background:var(--frame)}
.noplayer{width:100%;aspect-ratio:16/9;background:var(--surface-2);
  display:grid;place-items:center;color:var(--quiet);
  font-family:var(--mono);font-size:11px;letter-spacing:.12em;text-transform:uppercase}
/* waveform — playhead is ink, not cyan (canon caps cyan at the tv./wordmark) */
.wf{height:60px;cursor:pointer;user-select:none}
.wf svg{width:100%;height:60px;display:block}
.wf rect.bar{fill:var(--rule-soft)}
.wf rect.bar.in{fill:var(--ink)}
.wf .sel{fill:var(--ink);opacity:.08}
.wf .head{stroke:var(--ink);stroke-width:1.5}
.wf .mktick{stroke:var(--ink);stroke-width:1;stroke-dasharray:2 2;opacity:.55}
.wf .mktick:hover{opacity:1}
.wfbtns{display:flex;gap:6px;margin-top:10px;flex-wrap:wrap}
/* marks — the operator's timeline. Same row grammar as the transcript, so the
   two lists under the player read as one page rather than two components. */
.mklist{border:1px solid var(--rule-soft)}
.mkrow{display:flex;gap:10px;align-items:baseline;width:100%;text-align:left;
  padding:7px 10px;border:0;border-bottom:1px solid var(--rule-soft);background:none;
  color:inherit;font:inherit;cursor:pointer}
.mkrow:last-child{border-bottom:0}
.mkrow:hover{background:var(--surface)}
.mkrow.active{background:var(--surface);box-shadow:inset 2px 0 0 var(--ink)}
.mkts{flex:0 0 2.8rem;color:var(--quiet);font-family:var(--mono);font-size:11px}
.mklabel{flex:0 0 auto;color:var(--ink)}
.mknote{flex:1 1 auto;min-width:0;color:var(--quiet);font-size:12px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tbtn{font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.12em;
  color:var(--ink-2);background:var(--surface);border:1px solid var(--rule-soft);
  padding:6px 10px;cursor:pointer}
.tbtn:hover{border-color:var(--rule);color:var(--ink)}
.tbtn.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.strip{display:flex;gap:3px;overflow-x:auto;padding-bottom:6px}
.strip .cell{position:relative;flex:0 0 auto;padding:0;border:1px solid transparent;
  background:none;cursor:pointer;overflow:hidden}
.strip .cell img{height:88px;width:auto;display:block}
.strip .cell.active{border-color:var(--ink)}
.kfdesc{margin-top:10px;min-height:1.2em;font-size:13px;line-height:1.55;color:var(--ink-2)}
.strip .ct{position:absolute;left:0;bottom:0;background:var(--ink);color:var(--bg);
  font-family:var(--mono);font-size:9px;letter-spacing:.06em;padding:1px 4px}
.weights{margin-top:12px;max-width:440px;border-top:1px solid var(--rule-soft);padding-top:8px}
.weights summary{cursor:pointer;font-size:12px;color:var(--muted)}
.weights summary:hover{color:inherit}
.wnote{font-size:11px;color:var(--muted);line-height:1.5;margin:8px 0 10px}
.wrows{display:flex;flex-direction:column;gap:6px}
.wrow{display:flex;align-items:center;gap:8px;font-size:12px}
.wrow label{flex:0 0 76px;color:var(--muted)}
.wrow input[type=range]{flex:1 1 auto;min-width:0}
.wval{flex:0 0 38px;text-align:right;font-variant-numeric:tabular-nums;color:var(--muted)}
.wfoot{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:10px}
.wdelta{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.wdelta.on{color:inherit}
.meter.custom .mval{font-style:italic}
.segs{max-height:44vh;overflow:auto;border:1px solid var(--rule-soft)}
.seg{display:flex;gap:10px;width:100%;text-align:left;padding:7px 10px;border:0;
  border-bottom:1px solid var(--rule-soft);background:none;color:inherit;font:inherit;
  cursor:pointer}
.seg:last-child{border-bottom:0}
.seg:hover{background:var(--surface)}
.seg.active{background:var(--surface);box-shadow:inset 2px 0 0 var(--ink)}
.seg .tc{flex:0 0 2.8rem;color:var(--quiet);font-family:var(--mono);font-size:11px}
.flat{white-space:pre-wrap;color:var(--ink-2);font-size:13px;max-height:44vh;overflow:auto;
  padding:12px;border:1px solid var(--rule-soft)}
.flat.empty{color:var(--quiet)}
/* scores — mono bars; status colours are a data layer, never brand chrome */
.meters{display:flex;flex-direction:column;gap:6px;max-width:440px}
.meter{display:flex;align-items:center;gap:12px}
.mlabel{width:5rem;color:var(--quiet);font-family:var(--mono);font-size:10px;
  letter-spacing:.12em;text-transform:uppercase}
.mbar{flex:1;height:6px;background:var(--surface-2)}
.mbar i{display:block;height:100%;background:var(--ink)}
.mval{width:2.4rem;text-align:right;font-family:var(--mono);font-size:12px;
  font-variant-numeric:tabular-nums}
.reasoning{margin:12px 0 0;color:var(--quiet);font-size:13px;max-width:62ch}
.metagrid{display:grid;grid-template-columns:6rem 1fr;gap:3px 16px;font-size:13px}
.sglist{display:flex;flex-direction:column;gap:2px;margin-top:8px}
.sgrow{display:grid;grid-template-columns:64px 72px 1fr auto;gap:8px;align-items:baseline;padding:3px 0;border-bottom:1px solid var(--line)}
.sgz{font-weight:600}
.q.warn{color:var(--warn,#c2410c)}
.mk{color:var(--quiet);font-family:var(--mono);font-size:10px;letter-spacing:.12em;
  text-transform:uppercase;padding-top:3px}
.mv{color:var(--ink)}
"""

_STYLE = theme.stylesheet(_COMPONENTS)

_SCRIPT = r"""
(function(){
  var boot=JSON.parse(document.getElementById('boot').textContent);
  var dur=boot.dur||0, base=boot.base||'';
  var player=document.getElementById('player');
  var wf=document.getElementById('wf'), svg=document.getElementById('wfsvg');
  var bars=document.getElementById('bars'), sel=document.getElementById('sel'), head=document.getElementById('head');
  var segEls=[].slice.call(document.querySelectorAll('.seg'));
  var cells=[].slice.call(document.querySelectorAll('.cell'));
  var mkRows=[].slice.call(document.querySelectorAll('.mkrow'));
  var mkTicks=[].slice.call(document.querySelectorAll('.mktick'));
  var ioLabel=document.getElementById('io');
  var inSec=null, outSec=null;
  var BINS=boot.bins||200;

  // --- i18n: pure client-side chrome swap. Both dictionaries ride in boot, so
  //     switching is instant and works offline in a frozen export. Model output
  //     (reasoning, transcript, decoded values) has no data-i18n and never moves. ---
  var LANG='en';
  function T(k){ var t=boot.i18n&&(boot.i18n[LANG]||boot.i18n.en); return (t&&t[k]!=null)?t[k]:k; }
  function applyLang(lang){
    if(!boot.i18n||!boot.i18n[lang]) lang='en';
    LANG=lang;
    var d=boot.i18n[lang];
    document.documentElement.lang=(lang==='zh'?'zh-Hant':'en');
    [].forEach.call(document.querySelectorAll('[data-i18n]'),function(el){
      var k=el.getAttribute('data-i18n'); if(d[k]!=null) el.textContent=d[k];
    });
    [].forEach.call(document.querySelectorAll('#lang .langbtn'),function(b){
      b.classList.toggle('on', b.getAttribute('data-lang')===lang);
    });
    if(window.__rsRepaintIO) window.__rsRepaintIO();  // io label is JS-built
    if(window.__rsReapply) window.__rsReapply();       // reweight delta is JS-built
    try{ localStorage.setItem('rs_lang', lang); }catch(e){}
  }
  [].forEach.call(document.querySelectorAll('#lang .langbtn'),function(b){
    b.addEventListener('click',function(){ applyLang(b.getAttribute('data-lang')); });
  });

  function fmt(t){t=Math.max(0,t|0);return (t/60|0)+':'+('0'+(t%60)).slice(-2);}
  function cur(){return player?player.currentTime:0;}
  function seek(t){ if(!player)return; player.currentTime=Math.max(0,Math.min(dur||t,t)); player.play&&player.play().catch(function(){}); }

  // --- waveform bars: inlined when frozen (file:// blocks fetch), else fetched ---
  function drawBars(d){
    var peaks=d.peaks||[]; var n=peaks.length||1; var w=1; // viewBox width == n via unit bars
    svg.setAttribute('viewBox','0 0 '+n+' 60');
    var frag='';
    for(var i=0;i<peaks.length;i++){
      var h=Math.max(1, peaks[i]*56); var y=(60-h)/2;
      frag+='<rect class="bar" x="'+(i+0.1)+'" y="'+y.toFixed(2)+'" width="0.8" height="'+h.toFixed(2)+'"></rect>';
    }
    bars.innerHTML=frag;
    paintMarks();
    paintWindow();
  }

  /* The server renders each tick at t/dur*BINS, but drawBars() re-writes the
     viewBox to however many peaks actually came back. Re-solving x against the
     live viewBox is what keeps the tick over the same bar it was drawn for
     instead of drifting whenever the two counts disagree. */
  function paintMarks(){
    if(!mkTicks.length||!svg) return;
    var n=svg.viewBox.baseVal.width||1;
    mkTicks.forEach(function(l){
      var x=(dur>0)?((+l.dataset.t)/dur*n):0;
      l.setAttribute('x1',x); l.setAttribute('x2',x);
    });
  }

  if(boot.peaks){            // frozen export: peaks travel inside the file
    drawBars({peaks:boot.peaks});
  } else {                   // live server: ask the API
    fetch(base+'/api/waveform/'+boot.id+'?bins='+BINS)
      .then(function(r){return r.json();}).then(drawBars).catch(function(){});
  }

  function xToFrac(clientX){var r=wf.getBoundingClientRect();return Math.max(0,Math.min(1,(clientX-r.left)/r.width));}
  function paintHead(){ if(dur<=0||!svg)return; var n=svg.viewBox.baseVal.width||1; var x=cur()/dur*n; head.setAttribute('x1',x);head.setAttribute('x2',x); }
  function paintWindow(){
    var n=svg.viewBox.baseVal.width||1;
    if(inSec!=null&&outSec!=null&&dur>0){
      var a=inSec/dur*n, b=outSec/dur*n; sel.setAttribute('x',a); sel.setAttribute('width',Math.max(0,b-a));
    } else { sel.setAttribute('width',0); }
    // in-window bars get .in
    var rects=bars.querySelectorAll('rect');
    for(var i=0;i<rects.length;i++){
      var t=(i+0.5)/rects.length*dur;
      var inw = (inSec!=null&&outSec!=null)? (t>=inSec&&t<=outSec) : false;
      rects[i].classList.toggle('in', inw);
    }
    ioLabel.textContent = (inSec!=null||outSec!=null)
      ? (T('in')+' '+(inSec!=null?fmt(inSec):'—')+'  '+T('out')+' '+(outSec!=null?fmt(outSec):'—'))
      : T('noIO');
  }
  window.__rsRepaintIO=paintWindow;

  wf.addEventListener('click',function(e){ if(dur<=0)return; seek(xToFrac(e.clientX)*dur); });

  // --- player as source of truth ---
  if(player){
    player.addEventListener('loadedmetadata',function(){
      if(player.duration&&isFinite(player.duration)){ dur=player.duration; paintMarks(); }
    });
    player.addEventListener('timeupdate',function(){ paintHead(); syncActive(); });
  }
  function syncActive(){
    var t=cur();
    segEls.forEach(function(s){
      var a=+s.dataset.start,b=+s.dataset.end; s.classList.toggle('active', t>=a&&t<b);
    });
    var bestIdx=-1,bd=1e9;
    cells.forEach(function(c,i){var d=Math.abs((+c.dataset.ts)-t); if(d<bd){bd=d;bestIdx=i;}});
    cells.forEach(function(c,i){c.classList.toggle('active', i===bestIdx);});
    var kd=document.getElementById('kfdesc');
    if(kd&&bestIdx>=0){ kd.textContent=cells[bestIdx].dataset.desc||''; }
    /* A mark is a point, not a span, so "current" is the last one passed --
       highlighting only an exact second would mean the row lights up for one
       frame and then nothing is current for the rest of the clip. */
    var mkBest=-1;
    mkRows.forEach(function(r,i){ if(t+0.001>=(+r.dataset.ts)) mkBest=i; });
    mkRows.forEach(function(r,i){ r.classList.toggle('active', i===mkBest); });
  }

  segEls.forEach(function(s){ s.addEventListener('click',function(){ seek(+s.dataset.start); }); });
  cells.forEach(function(c){ c.addEventListener('click',function(){ seek(+c.dataset.ts); }); });
  mkRows.forEach(function(r){ r.addEventListener('click',function(){ seek(+r.dataset.ts); }); });

  // --- IN / OUT ---
  function setIn(){ inSec=cur(); if(outSec!=null&&inSec>outSec) outSec=null; paintWindow(); }
  function setOut(){ outSec=cur(); if(inSec!=null&&outSec<inSec) inSec=null; paintWindow(); }
  var bIn=document.getElementById('setin'),bOut=document.getElementById('setout'),bClr=document.getElementById('clrio'),bSrt=document.getElementById('srt');
  if(bIn)bIn.addEventListener('click',setIn);
  if(bOut)bOut.addEventListener('click',setOut);
  if(bClr)bClr.addEventListener('click',function(){inSec=outSec=null;paintWindow();});

  // --- SRT export of the [IN,OUT] window, rebased to the IN point ---
  function srtTime(t){t=Math.max(0,t);var h=t/3600|0,m=(t%3600)/60|0,s=t%60|0,ms=Math.round((t-(t|0))*1000);
    return ('0'+h).slice(-2)+':'+('0'+m).slice(-2)+':'+('0'+s).slice(-2)+','+('00'+ms).slice(-3);}
  if(bSrt)bSrt.addEventListener('click',function(){
    var lo=inSec!=null?inSec:0, hi=outSec!=null?outSec:(dur||1e9);
    var segs=boot.segs||[], out='', k=1;
    for(var i=0;i<segEls.length;i++){
      var a=+segEls[i].dataset.start,b=+segEls[i].dataset.end;
      if(b<lo||a>hi)continue;
      var txt=segEls[i].querySelector('.tx')?segEls[i].querySelector('.tx').textContent:'';
      out+=(k++)+'\n'+srtTime(Math.max(a,lo)-lo)+' --> '+srtTime(Math.min(b,hi)-lo)+'\n'+txt+'\n\n';
    }
    var blob=new Blob([out||'(no segments in window)'],{type:'text/plain'});
    var a2=document.createElement('a'); a2.href=URL.createObjectURL(blob);
    a2.download='inspect-'+boot.id+'.srt'; a2.click();
  });

  /* --- live re-weighting -------------------------------------------------
     Pure arithmetic over the four dimensions already on the page. No fetch,
     no model. Mirrors ingest.normalize_weights/compute_overall exactly: clamp
     negatives, rescale to sum 1, round to 2dp. If those ever diverge the page
     would quietly disagree with the CLI, so tests pin both to the same cases. */
  var wpanel=document.getElementById('wpanel');
  if(wpanel&&boot.dims&&boot.defaultWeights){
    var sliders=[].slice.call(wpanel.querySelectorAll('input[type=range]'));
    var obar=document.getElementById('obar'), oval=document.getElementById('oval');
    var delta=document.getElementById('wdelta');
    var ometer=obar?obar.closest('.meter'):null;
    var stored=(boot.storedOverall==null)?null:+boot.storedOverall;

    function computeOverall(w){
      var total=0,d;
      for(d in w){ if(w.hasOwnProperty(d)) total+=Math.max(0,w[d]); }
      if(total<=0) return null;
      var sum=0;
      for(d in boot.dims){
        if(!boot.dims.hasOwnProperty(d)) continue;
        var wd=(w[d]==null)?0:Math.max(0,w[d]);
        sum+=(+boot.dims[d])*(wd/total);
      }
      return Math.round(sum*100)/100;
    }
    function currentWeights(){
      var w={};
      sliders.forEach(function(s){ w[s.dataset.dim]=+s.value; });
      return w;
    }
    function isDefault(){
      return sliders.every(function(s){
        return Math.round(boot.defaultWeights[s.dataset.dim]*100)===+s.value;
      });
    }
    function apply(){
      var w=currentWeights();
      sliders.forEach(function(s){
        var lbl=document.getElementById('wv_'+s.dataset.dim);
        if(lbl) lbl.textContent=s.value+'%';
      });
      var v=computeOverall(w);
      if(v==null){
        /* Every weight at zero: there is no blend, so there is no number. Blank
           the meter rather than leaving the last computed value sitting there —
           otherwise the panel says "no verdict" while still showing one, which
           is worse than either alone. (Caught by driving the real sliders; the
           JS/Python parity check only compared return values, not DOM state.) */
        if(oval) oval.textContent='—';
        if(obar) obar.style.width='0%';
        if(ometer) ometer.className='meter custom';
        if(delta){delta.textContent=T('zeroWeights'); delta.className='wdelta on';}
        return;
      }
      if(obar) obar.style.width=Math.max(0,Math.min(100,v*10)).toFixed(1)+'%';
      if(oval) oval.textContent=v.toFixed(1);
      var def=isDefault();
      if(ometer) ometer.className='meter'+(def?'':' custom');
      if(delta){
        if(def||stored==null){ delta.textContent=''; delta.className='wdelta'; }
        else{
          var diff=v-stored;
          delta.textContent=T('wDefault')+' '+stored.toFixed(1)+'  ·  '+T('wYours')+' '+v.toFixed(1)+
            '  ('+(diff>=0?'+':'')+diff.toFixed(1)+')';
          delta.className='wdelta on';
        }
      }
    }
    sliders.forEach(function(s){ s.addEventListener('input',apply); });
    var wreset=document.getElementById('wreset');
    if(wreset) wreset.addEventListener('click',function(){
      sliders.forEach(function(s){ s.value=Math.round(boot.defaultWeights[s.dataset.dim]*100); });
      apply();
    });
    // Exposed so a language switch re-renders the JS-built delta text in place.
    window.__rsReapply=apply;
  }

  // --- initial language: remembered choice > browser language > English ---
  var initLang;
  try{ initLang=localStorage.getItem('rs_lang'); }catch(e){}
  if(!initLang) initLang=((navigator.language||'').toLowerCase().indexOf('zh')===0)?'zh':'en';
  applyLang(initLang);
})();
"""
