"""Tell "this clip is bad" apart from "this clip never actually processed".

A 4h11m livestream (`RwyLahUuGcc`) sat in the corpus carrying a complete-looking
craft score of 0.0 across all four dimensions. It was not a weak video. Keyframe
extraction had produced **zero** frames, so the merge stage had no visual layer
to read, returned unparseable output, and stored an analysis whose entire body
was ``{"summary": "{\\n", "topics": [], "error": "failed to parse JSON"}``. The
scorer then ran on that carcass; the model itself answered "impossible to score
the video accurately" -- and the pipeline wrote that refusal down as 0.0, 0.0,
0.0, 0.0, with ``videos.status = 'analyzed'``.

Three separate stages each produced a plausible-looking artifact from nothing,
and the number that came out the end was indistinguishable from a real one. That
is worse than a crash: a NULL is visibly missing, a 0.0 is silently averaged.
One fake row in 111 dragged every corpus aggregate it touched.

The predicate here is deliberately narrow and structural:

    keyframe_count == 0  AND  transcript_length > 0

Both halves carry weight. Zero frames on its own is ambiguous -- a freshly
downloaded video has zero frames too, and so does one sitting mid-pipeline. The
transcript is what removes the ambiguity: speech came out of this file, so the
container decoded, the media is readable, and ffmpeg still returned no frames.
A readable file that yields no frames is a *contradiction*, not a hard-to-sample
clip. That is the whole argument, and it is why a genuinely terrible video never
matches: a terrible video still has frames.

What this deliberately does NOT key on is the all-zero score. Five zeros are
corroboration, never the test -- a real video can legitimately bottom out, and
scoring on "the score looks bad" would be the tool marking work invalid because
it disliked the answer.

**Where the predicate is allowed to run matters as much as the predicate.**
Inside the pipeline it is evaluated at exactly one point: immediately after the
keyframe stage has run for that video, where "0 frames" is a settled outcome
rather than a not-yet. Evaluated as an ambient query it would flag any video
that has been transcribed but not yet sampled, which mid-run is a normal state.
That is why `scan()` exists as an operator-invoked audit that writes nothing
unless asked, and is not wired into any automatic path.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import db

#: `videos.status` value for a row whose media never really processed. Reusing
#: the existing status column (and `error_message` for the reason) keeps this a
#: zero-migration change -- there was no need to invent a schema for it.
INVALID_STATUS = "invalid"


class InvalidMediaError(RuntimeError):
    """Raised to stop a pipeline item once its media is known to be unusable.

    It carries the video id so the caller can report which item stopped, and it
    is raised *after* the row is marked, so the mark survives regardless of how
    the caller handles the exception.
    """

    def __init__(self, video_id: str, reason: str) -> None:
        super().__init__("%s: %s" % (video_id, reason))
        self.video_id = video_id
        self.reason = reason


def exclude_sql(alias: str = "v") -> str:
    """SQL fragment dropping invalid rows from an aggregate.

    `alias` is the table alias for `videos` in the caller's query, or "" when
    the table is referenced bare. The NULL branch is not defensive noise: rows
    predating the status column read NULL, and `NULL <> 'invalid'` is NULL in
    SQLite, which would quietly filter out exactly the oldest corpus.
    """
    col = ("%s.status" % alias) if alias else "status"
    return " AND (%s IS NULL OR %s <> '%s')" % (col, col, INVALID_STATUS)


def keyframe_count(conn: Any, video_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM keyframes WHERE video_id=?", (video_id,)).fetchone()
    return int(row[0]) if row else 0


def transcript_length(conn: Any, video_id: str) -> int:
    """Characters of transcribed speech, 0 when there is no transcript row."""
    row = conn.execute(
        "SELECT LENGTH(text_full) FROM transcripts WHERE video_id=?",
        (video_id,)).fetchone()
    if not row or row[0] is None:
        return 0
    return int(row[0])


def describe(kf: int, tchars: int) -> str:
    """The reason string stored in `videos.error_message`.

    It states both measurements rather than a category, because the person
    reading it later needs to be able to disagree with the call.
    """
    return (
        "keyframe extraction produced %d frames while the transcript holds %d "
        "characters -- the media decoded far enough to yield speech, so zero "
        "frames is a contradiction rather than a hard-to-sample clip; refusing "
        "to score it as if it were a real 0.0" % (kf, tchars)
    )


def invalid_reason(conn: Any, video_id: str) -> Optional[str]:
    """The reason this video is unusable, or None when it looks fine.

    Callers must only use this where keyframe extraction has already been
    attempted for `video_id` -- see the module docstring.
    """
    kf = keyframe_count(conn, video_id)
    if kf > 0:
        return None
    tchars = transcript_length(conn, video_id)
    if tchars <= 0:
        # No frames and no speech: nothing has been established either way.
        # A fresh download looks exactly like this.
        return None
    return describe(kf, tchars)


def mark_invalid(conn: Any, video_id: str, reason: str) -> None:
    """Flag the row. Nothing is deleted -- the media, transcript and any frames
    stay exactly where they are, and the mark is reversible by hand."""
    db.update_video_status(conn, video_id, INVALID_STATUS, reason)


def is_invalid(conn: Any, video_id: str) -> bool:
    row = conn.execute(
        "SELECT status FROM videos WHERE id=?", (video_id,)).fetchone()
    return bool(row) and row[0] == INVALID_STATUS


def scan(conn: Any) -> List[Dict[str, Any]]:
    """Every stored video matching the invalid shape, longest transcript first.

    Read-only by construction. This is the operator-facing audit path; it has no
    caller inside the pipeline precisely because an ambient sweep cannot tell a
    mid-run video from a broken one.
    """
    rows = conn.execute(
        "SELECT v.id AS id, v.platform_id AS platform_id, v.title AS title, "
        "v.uploader AS uploader, v.duration_sec AS duration_sec, "
        "v.status AS status, "
        "(SELECT COUNT(*) FROM keyframes k WHERE k.video_id = v.id) AS keyframes, "
        "(SELECT LENGTH(t.text_full) FROM transcripts t WHERE t.video_id = v.id) "
        "  AS transcript_chars, "
        "(SELECT s.overall FROM scores s WHERE s.video_id = v.id) AS overall "
        "FROM videos v"
    ).fetchall()

    hits: List[Dict[str, Any]] = []
    for r in rows:
        kf = int(r["keyframes"] or 0)
        tchars = int(r["transcript_chars"] or 0)
        if kf == 0 and tchars > 0:
            hits.append({
                "id": r["id"],
                "platform_id": r["platform_id"],
                "title": r["title"],
                "uploader": r["uploader"],
                "duration_sec": r["duration_sec"],
                "status": r["status"],
                "keyframes": kf,
                "transcript_chars": tchars,
                "overall": r["overall"],
                "reason": describe(kf, tchars),
            })
    hits.sort(key=lambda h: h["transcript_chars"], reverse=True)
    return hits
