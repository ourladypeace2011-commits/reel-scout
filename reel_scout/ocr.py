"""On-screen (burned-in) text collection — the §4F "L3.5" signal layer.

Short-form videos often carry their message as burned-in captions / big on-screen
text rather than (or in addition to) spoken dialogue. That text is:
  - stronger than the L2 platform caption (it's IN the video, not authored for reach)
  - timestamp-alignable like the L3 transcript
  - the only textual signal for low-dialogue / pure-visual reels (the documented L3
    gap in prompts/signal-reliability-cheatsheet.md)

so it slots between L3 and L4 as "L3.5". Two engines:

  * "vlm" (default, zero new deps): reuse what the vision model already read into
    `vision_descriptions.text_in_frame`.
  * "tesseract" (opt-in, guarded): OCR the keyframe JPEGs directly — stronger CJK,
    but a heavy dependency, so off by default and falls back to vlm if unavailable.

Borrowed from crv Pro's `--ocr`; implementation our own.
"""
from __future__ import annotations

import importlib.util
from typing import Dict, List, Optional

from . import config, db


def _tesseract_available() -> bool:
    return (
        importlib.util.find_spec("pytesseract") is not None
        and importlib.util.find_spec("PIL") is not None
    )


def _ocr_image(image_path: str) -> str:
    """Dedicated-engine OCR of one keyframe. Best-effort: returns '' if pytesseract
    / Pillow / the tesseract binary are missing or the call errors."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    try:
        return pytesseract.image_to_string(Image.open(image_path)).strip()
    except Exception:  # noqa: BLE001 — OCR is best-effort, never fatal
        return ""


def collect_captions(
    conn: db.sqlite3.Connection,
    video_id: str,
    engine: Optional[str] = None,
) -> List[Dict]:
    """Gather timestamped on-screen text for a video as the L3.5 layer.

    Default engine reuses the VLM's text_in_frame (no new deps). When engine is
    'tesseract' and it's actually installed, each keyframe JPEG is OCR'd instead,
    falling back to the VLM text for any frame the engine reads as empty.
    """
    engine = engine or config.OCR_ENGINE
    use_tess = engine == "tesseract" and _tesseract_available()

    captions = []  # type: List[Dict]
    for r in db.get_keyframes_with_descriptions(conn, video_id):
        text = ""
        src = "vlm"
        if use_tess and r["file_path"]:
            text = _ocr_image(r["file_path"])
            if text:
                src = "tesseract"
        if not text:
            # vlm default, or tesseract-found-nothing fallback
            text = (r["text_in_frame"] or "").strip()
            src = "vlm"
        if text:
            captions.append(
                {"timestamp_sec": r["timestamp_sec"], "text": text, "engine": src}
            )
    return captions


def backfill_from_descriptions(conn, dry_run=False):
    # type: (db.sqlite3.Connection, bool) -> Dict[str, int]
    """Recover the L3.5 layer for frames a pre-fix backend described.

    Until `vision.parse` existed both backends dropped the model's answer into
    `description` and left `text_in_frame` at "", so `collect_captions` had
    nothing to return and `ocr_captions` stayed empty for the whole corpus. The
    text was never lost -- it is in the prose -- so this re-reads it rather than
    paying for another VLM pass over every keyframe.

    Pure DB work: no model, no network, no video files. Only rows whose
    `text_in_frame` is already empty are touched, so a real read from a fixed
    backend is never overwritten, and captions are rewritten per video by
    `save_ocr_captions`, which replaces rather than appends -- both make this
    safe to run twice.
    """
    from .vision.parse import text_from_prose

    rows = conn.execute(
        """SELECT vd.keyframe_id, vd.description, k.video_id
             FROM vision_descriptions vd
             JOIN keyframes k ON k.id = vd.keyframe_id
            WHERE vd.description IS NOT NULL AND TRIM(vd.description) != ''
              AND (vd.text_in_frame IS NULL OR TRIM(vd.text_in_frame) = '')"""
    ).fetchall()

    touched_videos = set()
    filled = 0
    for r in rows:
        text = text_from_prose(r["description"])
        if not text:
            continue
        filled += 1
        touched_videos.add(r["video_id"])
        if not dry_run:
            conn.execute(
                "UPDATE vision_descriptions SET text_in_frame = ? WHERE keyframe_id = ?",
                (text, r["keyframe_id"]),
            )
    if not dry_run:
        conn.commit()

    # A dry run stops here: rebuilding captions means reading back the column it
    # deliberately did not write. Every filled frame becomes one caption, so
    # `filled` is the count it would have produced.
    if dry_run:
        return {
            "scanned": len(rows),
            "filled": filled,
            "videos": len(touched_videos),
            "captions": filled,
            "videos_with_captions": len(touched_videos),
        }

    captions = 0
    videos_with_captions = 0
    for vid in sorted(touched_videos):
        caps = collect_captions(conn, vid, engine="vlm")
        if caps:
            db.save_ocr_captions(conn, vid, caps)
            captions += len(caps)
            videos_with_captions += 1

    return {
        "scanned": len(rows),
        "filled": filled,
        "videos": len(touched_videos),
        "captions": captions,
        "videos_with_captions": videos_with_captions,
    }
