"""Timeline marks — the seconds of a clip someone decided were the point.

Everything the pipeline writes describes a clip as a whole, or describes a
frame it happened to sample. Neither says "the cut at 0:07 is where the meaning
turns". That sentence is the output of watching the reel with an intent, it is
attached to a moment rather than to the file, and until this module existed
there was nowhere to put it: a note is one line about a whole clip (and
`annotate.MAX_NOTE_LEN` rejects rather than truncates, deliberately), while a
teardown is a list of somewheres.

Same shape as `annotate`: constants, one error type, pure functions over a
connection. No server state, so the CLI, the MCP tool and the inspector all get
the same rules enforced once instead of three times, slightly differently.

Marks live in their own table (see the v13 migration), which is what makes
re-running the pipeline safe: `analyze`, `score` and re-extracting keyframes all
rewrite what the model produced and none of them can reach a mark.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import db
from .annotate import AnnotateError, resolve_video as _resolve_video

#: A label is what fits on a timeline, next to a tick, in one line. Longer than
#: a note's worth of intent would not be readable there anyway, and the same
#: rule as notes applies: over-long input is rejected, never truncated.
MAX_LABEL_LEN = 60
#: A mark's note is a sentence saying why this second — the same order of
#: magnitude as MAX_NOTE_LEN, and for the same reason. The teardown itself lives
#: in a markdown file under version control; this is the pointer to it.
MAX_MARK_NOTE_LEN = 500
#: Past this, a "timeline" is a transcript with extra steps. The cap exists so a
#: mis-generated import file fails loudly instead of writing ten thousand rows
#: nobody asked for.
MAX_MARKS_PER_VIDEO = 200


class MarkError(ValueError):
    """Bad input from a user surface. Carries the HTTP status the API should use."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def resolve(conn: db.sqlite3.Connection, ref: str) -> str:
    """A URL, a video id or a unique id prefix -> the video id.

    Delegates to `annotate.resolve_video` rather than repeating the resolution,
    so `mark` takes exactly the kind of argument `note` and `track` take. The
    error is re-raised as a MarkError only so that every surface calling into
    this module has one exception type to catch.
    """
    try:
        return _resolve_video(conn, ref)
    except AnnotateError as exc:
        raise MarkError(str(exc), status=getattr(exc, "status", 400))


def _clean_label(label: Any) -> str:
    label = label.strip() if isinstance(label, str) else ""
    if not label:
        # A mark with no label is a tick with nothing to read: it would render
        # on the waveform and tell the reader nothing at all.
        raise MarkError("mark label is empty")
    if len(label) > MAX_LABEL_LEN:
        raise MarkError("mark label is longer than %d characters" % MAX_LABEL_LEN)
    return label


def _clean_note(note: Any) -> Optional[str]:
    if note is None:
        return None
    if not isinstance(note, str):
        raise MarkError("mark note must be text")
    note = note.strip()
    if not note:
        return None
    if len(note) > MAX_MARK_NOTE_LEN:
        raise MarkError("mark note is longer than %d characters" % MAX_MARK_NOTE_LEN)
    return note


#: Marks typed by hand. Reserved: an importer may never own this name, or a
#: re-import would delete the rows it exists to keep out of its blast radius.
MANUAL_SOURCE = "manual"
#: Everything an importer writes is namespaced under this, so "was this typed or
#: generated" is answerable from the row alone.
IMPORT_PREFIX = "import:"


def source_name(name: Any) -> str:
    """A source name as the user typed it -> the name as it is stored.

    `teardown` -> `import:teardown`; `import:teardown` -> itself; `manual` stays
    `manual`. One function because both `--import` and `--clear --source` take
    the same word from the same person, and the failure mode of normalizing in
    only one of them is the quiet kind: `--clear --source teardown` matching no
    rows, reporting "removed 0", and leaving every mark exactly where it was.
    """
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        raise MarkError("mark source is empty")
    if len(name) > MAX_LABEL_LEN:
        raise MarkError("mark source is longer than %d characters" % MAX_LABEL_LEN)
    if name == MANUAL_SOURCE or name.startswith(IMPORT_PREFIX):
        return name
    return IMPORT_PREFIX + name


def _clean_t_sec(t_sec: Any, duration: Optional[float]) -> float:
    try:
        t = float(t_sec)
    except (TypeError, ValueError):
        raise MarkError("mark time %r is not a number" % (t_sec,))
    if t != t or t in (float("inf"), float("-inf")):  # NaN / inf
        raise MarkError("mark time %r is not a finite number" % (t_sec,))
    if t < 0:
        raise MarkError("mark time %.3f is before the clip starts" % t)
    # A NULL duration is a clip whose length was never recorded, not a clip of
    # infinite length. Skipping the check there is the honest reading; the
    # caller is told it was skipped rather than being left to assume it passed.
    if duration is not None and duration > 0 and t > duration:
        raise MarkError("mark time %.3f is past the end of the clip (%.3f s)"
                        % (t, duration))
    return t


def _duration(conn: db.sqlite3.Connection, video_id: str) -> Optional[float]:
    """The clip's recorded length, or None when it has none.

    Reads `videos.duration_sec` and not the inspector's computed duration: the
    stored number is what every other write path validates against, and a mark
    should not become invalid because a transcript segment ran long.
    """
    row = db.get_video(conn, video_id)
    if row is None:
        raise MarkError("no such video", status=404)
    raw = row["duration_sec"]
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _count(conn: db.sqlite3.Connection, video_id: str) -> int:
    return len(db.list_clip_marks(conn, video_id))


def add(conn: db.sqlite3.Connection, video_id: str, t_sec: Any, label: str,
        note: Optional[str] = None, source: str = "manual") -> Dict[str, Any]:
    """Put one mark on one clip. Returns the stored row."""
    duration = _duration(conn, video_id)
    t = _clean_t_sec(t_sec, duration)
    label = _clean_label(label)
    note = _clean_note(note)
    source = source_name(source)
    if _count(conn, video_id) >= MAX_MARKS_PER_VIDEO:
        raise MarkError("clip already has %d marks (the limit)" % MAX_MARKS_PER_VIDEO)
    return db.add_clip_mark(conn, video_id, t, label, note=note, source=source)


def list_for(conn: db.sqlite3.Connection, video_id: str,
             source: Optional[str] = None) -> List[Dict[str, Any]]:
    """One clip's marks, in time order. `source` narrows to one writer."""
    if source is not None:
        source = source_name(source)
    return [dict(r) for r in db.list_clip_marks(conn, video_id, source=source)]


def remove(conn: db.sqlite3.Connection, mark_id: int,
           video_id: Optional[str] = None) -> bool:
    """Delete one mark by id.

    `video_id` scopes the delete to one clip. Ids are global, so without it
    `mark <clipA> --rm 12` would happily delete clip B's mark 12 and report
    success -- naming a clip and then deleting something else's row.
    """
    try:
        mark_id = int(mark_id)
    except (TypeError, ValueError):
        raise MarkError("mark id %r is not a number" % (mark_id,))
    if video_id is not None:
        row = conn.execute("SELECT video_id FROM clip_marks WHERE id = ?",
                           (mark_id,)).fetchone()
        if row is None:
            raise MarkError("no such mark", status=404)
        if row["video_id"] != video_id:
            raise MarkError("mark %d is not on %s" % (mark_id, video_id), status=404)
    if not db.delete_clip_mark(conn, mark_id):
        raise MarkError("no such mark", status=404)
    return True


def clear(conn: db.sqlite3.Connection, video_id: str,
          source: Optional[str] = None) -> int:
    """Delete a clip's marks. `source=None` means all of them, imported included.

    Deliberately not "manual only": `--clear` with no `--source` reads as "take
    them all off", and a command that silently left half the ticks on the
    timeline would be worse than either meaning honestly stated. Narrowing is
    what `--source` is for.
    """
    if db.get_video(conn, video_id) is None:
        raise MarkError("no such video", status=404)
    if source is not None:
        source = source_name(source)
    return db.delete_clip_marks(conn, video_id, source=source)


def import_marks(conn: db.sqlite3.Connection, video_id: str,
                 marks: Any, source: str) -> Dict[str, Any]:
    """Replace this source's marks on one clip with `marks`. All or nothing.

    The whole batch is validated BEFORE anything is deleted. Reversing those two
    steps is the bug this docstring exists to prevent: a generator that emits one
    bad row would then have already wiped the good set it was meant to replace,
    and the command would report a failure while having destroyed the state it
    failed to improve.

    Only rows carrying this same `source` are removed, so re-running an importer
    is idempotent and marks someone typed by hand (`manual`) are never in the
    blast radius. That is what makes the imported set a projection of a file
    kept elsewhere rather than a second, competing copy of the truth.
    """
    source = source_name(source)
    if source == MANUAL_SOURCE:
        # Letting an importer own `manual` would make every re-import delete the
        # hand-typed marks — the exact collision `source` exists to prevent.
        raise MarkError("'manual' is reserved for marks typed by hand; "
                        "give the import its own --source name")
    duration = _duration(conn, video_id)

    if isinstance(marks, dict):          # accept the file's shape as well as a list
        marks = marks.get("marks")
    if not isinstance(marks, list):
        raise MarkError("marks must be a list of {t, label} objects")
    if len(marks) > MAX_MARKS_PER_VIDEO:
        raise MarkError("%d marks is over the limit of %d"
                        % (len(marks), MAX_MARKS_PER_VIDEO))

    # --- validate the whole batch first ---
    cleaned: List[Dict[str, Any]] = []
    for i, raw in enumerate(marks):
        if not isinstance(raw, dict):
            raise MarkError("mark %d is not an object" % i)
        if "t" not in raw and "t_sec" not in raw:
            raise MarkError("mark %d has no time (expected 't')" % i)
        try:
            cleaned.append({
                "t_sec": _clean_t_sec(raw.get("t", raw.get("t_sec")), duration),
                "label": _clean_label(raw.get("label")),
                "note": _clean_note(raw.get("note")),
            })
        except MarkError as exc:
            raise MarkError("mark %d: %s" % (i, exc), status=exc.status)

    kept = len(db.list_clip_marks(conn, video_id)) - len(
        db.list_clip_marks(conn, video_id, source=source))
    if kept + len(cleaned) > MAX_MARKS_PER_VIDEO:
        raise MarkError("that would leave the clip with %d marks (the limit is %d)"
                        % (kept + len(cleaned), MAX_MARKS_PER_VIDEO))

    # --- only now is anything destroyed ---
    removed = db.delete_clip_marks(conn, video_id, source=source)
    for m in cleaned:
        db.add_clip_mark(conn, video_id, m["t_sec"], m["label"],
                         note=m["note"], source=source)
    return {"video_id": video_id, "source": source,
            "removed": removed, "added": len(cleaned),
            # Said out loud rather than assumed: with no recorded duration the
            # "is this past the end" check could not run, and a reader comparing
            # two imports should be able to see which one was actually checked.
            "duration_checked": duration is not None}
