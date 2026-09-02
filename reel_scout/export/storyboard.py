"""Storyboard project export — the reel-scout → storyboard-app hand-off (§6D).

reel-scout takes a finished video apart; a storyboard tool assembles one that has
not been shot yet. Both describe a clip as an ordered list of cuts carrying a
size, a duration, a line of VO and a caption — the same structure pointed in
opposite directions in time. This writes the deconstruction out in the shape the
storyboard tool reads, so "拆爆款 → 抽 SOP → 倒推自己的分鏡" stops being a thing
the course describes and becomes a file.

The interface is a data format, not a function call: neither side imports the
other. Same contract `export --format skeleton` established for smart-edit.

## 🔴 The provenance rule, and why it is not optional

A reel-scout shot is **somebody else's footage**. A storyboard `project.json` is
**what gets shown to a client as a PPM**. Once this bridge exists, "reference"
and "template" are one save apart, and nothing in the file would say which one
you are holding.

So every generated project is marked, in three places that a human cannot
plausibly miss all of:

* `meta.title` is prefixed `REF:`
* `meta.client` states it is a teardown, not an original
* **every single cut** carries the source URL and its in/out timecode in `note`

`note` is the right field for it because the storyboard app prints it and a
person editing that cut sees it. Stripping the marks takes deliberate work on
every row, which is the point — the failure this guards against is not malice,
it is somebody opening a file six weeks later and forgetting where it came from.

## What is deliberately left empty

`gear`, `scoutRef`, `scoutMeta` (sensor format, focal length) stay unset. Those
are filled by the person standing on the set. Estimating focal length from
finished footage is a guess, and a guessed number in a PPM is indistinguishable
from a measured one to everyone downstream. `imageRef` is also null: the schema
warns that field holds image data, and the representative frame's id goes in
`note` instead so it can be looked up rather than fabricated.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

from .. import db, ffprobe
from ..shot_size import normalize as normalize_shot_size
from ..shots import Shot, bind_frames_to_shots, bind_spans_to_shots

#: Marks a project as a teardown of someone else's clip. See the module docstring.
REF_PREFIX = "REF:"
REF_CLIENT = "teardown / reference — not an original treatment"


def _tc(seconds: float) -> str:
    """`m:ss.s`, the form a person reads back against a scrub bar."""
    m, s = divmod(max(0.0, float(seconds)), 60.0)
    return "%d:%04.1f" % (int(m), s)


def _aspect(file_path: Optional[str]) -> Optional[str]:
    """"9:16" / "16:9", or None when the media is gone or unreadable.

    None means the key is omitted, and the storyboard app then applies its own
    documented default of 16:9. That default is wrong for a vertical reel, so
    the caller warns rather than letting the omission pass as a decision.
    """
    if not file_path or not os.path.exists(file_path):
        return None
    dims = ffprobe.probe_dimensions(file_path)
    if not dims:
        return None
    w, h = dims
    if not w or not h:
        return None
    return "9:16" if h > w else "16:9"


def build_project(
    video: Any,
    shots: List[Shot],
    keyframes: List[Any],
    vo_segments: List[Dict[str, Any]],
    captions: List[Any],
    descriptions: Optional[Dict[int, str]] = None,
    aspect: Optional[str] = None,
    shot_sizes: Optional[Dict[int, str]] = None,
) -> Dict[str, Any]:
    """One storyboard project from one analyzed clip. Pure — no DB, no ffmpeg."""
    descriptions = descriptions or {}
    # A deliberate label beats one salvaged out of prose. Measured on the real
    # library: pulling a size out of existing descriptions covers 17.3% of
    # frames and 97.7% of what it does return is "CU" -- that is the VLM's habit
    # of calling things close-ups, not the clip's framing. So a stored label
    # wins, and the fallback stays as a fallback.
    shot_sizes = shot_sizes or {}
    url = video["url"] or ""
    title = (video["title"] or "(untitled)").strip()

    per_shot, _ = bind_frames_to_shots(shots, keyframes or [])
    vo_by_shot, _ = bind_spans_to_shots(shots, vo_segments or [])
    sup_by_shot, _ = bind_frames_to_shots(shots, captions or [])

    cuts: List[Dict[str, Any]] = []
    for i, sf in enumerate(per_shot):
        rep = sf.representative
        desc = descriptions.get(rep["id"]) if rep is not None else None
        vo = " ".join((seg.get("text") or "").strip()
                      for seg in vo_by_shot[i]).strip()
        sup = " / ".join((c["text"] or "").strip()
                         for c in sup_by_shot[i].frames).strip(" /")
        note = "%s %s  [%s–%s]" % (
            REF_PREFIX, url, _tc(sf.shot.start_sec), _tc(sf.shot.end_sec))
        if rep is not None:
            note += "  (frame id %d)" % rep["id"]
        cut = {
            "id": "c%d" % (i + 1),
            "filmId": "f1",
            # Every cut its own group: consecutive-shot grouping is a judgement
            # this export has no basis for, and guessing it would silently merge
            # cuts a person then has to pull apart.
            "groupId": "g%d" % (i + 1),
            "shot": (shot_sizes.get(rep["id"]) if rep is not None else None)
                    or normalize_shot_size(desc) or "",
            "desc": (desc or "").strip(),
            "vo": vo,
            "sup": sup,
            "imageRef": None,
            "prompt": "",
            "props": "",
            "note": note,
        }
        # `sec` is an integer field the storyboard app totals in the page footer.
        # A 0.33s cut rounds to 0, and a row reading "0 seconds" is worse than a
        # row with no duration — so sub-second cuts omit the optional field
        # rather than assert a zero. (A music video at 30 cuts/min has plenty.)
        secs = int(round(sf.shot.dur_sec))
        if secs >= 1:
            cut["sec"] = secs
        cuts.append(cut)

    project: Dict[str, Any] = {
        "meta": {
            "title": "%s %s" % (REF_PREFIX, title),
            "client": REF_CLIENT,
            "version": 1,
            "logo": None,
        },
        "contacts": [],
        "films": [{"id": "f1", "name": title}],
        "cuts": cuts,
        "days": [],
        "milestones": [],
        "refPages": {},
        "hiddenChapters": [],
        "mode": "ppm",
    }
    if aspect:
        project["aspect"] = aspect
    return project


def export_storyboard(conn, output_dir: str, video_id: Optional[str] = None) -> int:
    """Write one `<video_id>.project.json` per analyzed clip that has shots.

    A clip with no `shots` row is skipped rather than exported as a
    single-cut project: one cut spanning four minutes is not a storyboard, and
    emitting it would look like the export worked.
    """
    os.makedirs(output_dir, exist_ok=True)
    if video_id:
        videos = [v for v in [db.get_video(conn, video_id)] if v is not None]
    else:
        videos = db.list_videos(conn, status="analyzed", limit=9999)

    count = 0
    for video in videos:
        vid = video["id"]
        rows = db.get_shots(conn, vid)
        if not rows:
            print("  skipped %s: no shot table (run analyze to build one)" % vid,
                  file=sys.stderr)
            continue
        shots = [Shot(r["idx"], r["start_sec"], r["end_sec"], r["dur_sec"])
                 for r in rows]
        kfs = db.get_keyframes_with_descriptions(conn, vid) or []
        descriptions = {k["id"]: k["description"] for k in kfs if k["description"]}
        transcript = db.get_transcript(conn, vid)
        segs: List[Dict[str, Any]] = []
        if transcript and transcript["segments_json"]:
            try:
                segs = json.loads(transcript["segments_json"]) or []
            except ValueError:
                segs = []
        aspect = _aspect(video["file_path"])
        if aspect is None:
            print("  %s: aspect unknown (media missing) — the storyboard app will "
                  "assume 16:9, which is wrong for a vertical clip" % vid,
                  file=sys.stderr)
        # UNKNOWN is stored so "asked, no answer" stays distinct from "never
        # asked" -- but it is not a shot size, so it does not reach the cut.
        sizes = {}
        for lab in db.get_shot_labels(conn, vid, kind="shot_size"):
            if lab["value"] in (None, "UNKNOWN"):
                continue
            for k in kfs:
                if abs(k["timestamp_sec"] - lab["t_sec"]) < 1e-6:
                    sizes[k["id"]] = lab["value"]
                    break
        project = build_project(
            video, shots, kfs, segs, db.get_ocr_captions(conn, vid) or [],
            descriptions=descriptions, aspect=aspect, shot_sizes=sizes)
        with open(os.path.join(output_dir, "%s.project.json" % vid),
                  "w", encoding="utf-8") as f:
            json.dump(project, f, ensure_ascii=False, indent=2)
        count += 1
    return count
