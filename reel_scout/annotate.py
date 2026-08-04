"""User annotations — the three things the pipeline cannot infer for you.

A note (what this clip is *for*), a group (your own filing, not the model's
tags), and a star (worth coming back to). The pipeline decodes what a video
*is*; none of it knows what you intend to do with it, and that intent is the
part worth typing by hand.

This module is the single operations surface. The HTTP list page, the CLI
(`reel-scout note` / `reel-scout group`) and the MCP tools all call the
functions here rather than touching `db` directly, so a rule like "a group name
is unique case-insensitively" is enforced once instead of three times, slightly
differently.

Storage lives in its own tables (see the v11 migration): re-crawling,
re-analyzing or re-scoring a clip rewrites the pipeline's output and never
touches a note.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Union

from . import db

#: A note is a working label, not a document — the list cell shows one line.
#: Long enough for a sentence of intent, short enough that the column stays a
#: column. Over-long input is rejected, not silently truncated: silently losing
#: the end of what someone typed is worse than making them shorten it.
MAX_NOTE_LEN = 500
MAX_GROUP_NAME_LEN = 60


class AnnotateError(ValueError):
    """Bad input from a user surface. Carries the HTTP status the API should use."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def resolve_video(conn: db.sqlite3.Connection, ref: str) -> str:
    """Accept what a person actually has to hand — a URL, a video id, or a
    unique id prefix — and return the video id. Same resolution `track` uses, so
    the two commands take the same kind of argument."""
    from . import compare

    row = db.get_video_by_url(conn, ref)
    if row is not None:
        return row["id"]
    video_id, matches = compare.resolve_ref(conn, ref)
    if video_id is None:
        if matches:
            raise AnnotateError("ambiguous ref %r matches: %s"
                                % (ref, ", ".join(matches)))
        raise AnnotateError("no video found for %r" % ref, status=404)
    return video_id


# --- Groups ---

def list_groups(conn: db.sqlite3.Connection) -> List[Dict[str, Any]]:
    return [dict(r) for r in db.list_groups(conn)]


def _clean_group_name(name: Any) -> str:
    name = (name or "").strip() if isinstance(name, str) else ""
    if not name:
        raise AnnotateError("group name is empty")
    if len(name) > MAX_GROUP_NAME_LEN:
        raise AnnotateError("group name is longer than %d characters" % MAX_GROUP_NAME_LEN)
    return name


def add_group(conn: db.sqlite3.Connection, name: str) -> Dict[str, Any]:
    name = _clean_group_name(name)
    existing = db.get_group_by_name(conn, name)
    if existing is not None:
        # Case-insensitive: "Course" and "course" would be two rows in the
        # dropdown that a reader cannot tell apart.
        raise AnnotateError("group %r already exists" % existing["name"], status=409)
    return dict(db.create_group(conn, name))


def rename_group(conn: db.sqlite3.Connection, group_id: int, name: str) -> Dict[str, Any]:
    name = _clean_group_name(name)
    if db.get_group(conn, group_id) is None:
        raise AnnotateError("no such group", status=404)
    clash = db.get_group_by_name(conn, name)
    if clash is not None and int(clash["id"]) != int(group_id):
        raise AnnotateError("group %r already exists" % clash["name"], status=409)
    db.rename_group(conn, group_id, name)
    row = db.get_group(conn, group_id)
    return dict(row) if row else {}


def remove_group(conn: db.sqlite3.Connection, group_id: int) -> bool:
    if db.get_group(conn, group_id) is None:
        raise AnnotateError("no such group", status=404)
    return db.delete_group(conn, group_id)


def resolve_group(conn: db.sqlite3.Connection, group: Union[int, str, None],
                  create: bool = False) -> Optional[int]:
    """Turn an id or a name into a group id. `None`/"" means "no group"."""
    if group is None or group == "":
        return None
    if isinstance(group, bool):  # bool is an int subclass; never a group id
        raise AnnotateError("group must be an id or a name")
    if isinstance(group, int) or (isinstance(group, str) and group.isdigit()):
        gid = int(group)
        if db.get_group(conn, gid) is None:
            raise AnnotateError("no such group", status=404)
        return gid
    name = _clean_group_name(group)
    row = db.get_group_by_name(conn, name)
    if row is not None:
        return int(row["id"])
    if not create:
        raise AnnotateError("no group named %r" % name, status=404)
    return int(db.create_group(conn, name)["id"])


# --- Annotations ---

def get(conn: db.sqlite3.Connection, video_id: str) -> Dict[str, Any]:
    row = db.get_annotation(conn, video_id)
    if row is None:
        return {"video_id": video_id, "note": None, "group_id": None,
                "starred": 0, "group_name": None}
    out = dict(row)
    if out.get("group_id"):
        g = db.get_group(conn, int(out["group_id"]))
        out["group_name"] = g["name"] if g else None
    else:
        out["group_name"] = None
    return out


def all_annotations(conn: db.sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    return db.list_annotations(conn)


def set_annotation(conn: db.sqlite3.Connection, video_id: str,
                   note: Optional[str] = None,
                   group: Union[int, str, None] = None,
                   starred: Optional[bool] = None,
                   clear_group: bool = False,
                   create_group: bool = False) -> Dict[str, Any]:
    """Write any subset of {note, group, star} for one video.

    Fields left as `None` are untouched, so a star toggle cannot wipe a note it
    never knew about — which matters here because three different surfaces send
    partial updates at different times.
    """
    if db.get_video(conn, video_id) is None:
        raise AnnotateError("no such video", status=404)
    if note is not None:
        if not isinstance(note, str):
            raise AnnotateError("note must be text")
        note = note.strip()
        if len(note) > MAX_NOTE_LEN:
            raise AnnotateError("note is longer than %d characters" % MAX_NOTE_LEN)
    group_id = None if clear_group else resolve_group(conn, group, create=create_group)
    db.set_annotation(conn, video_id, note=note, group_id=group_id,
                      starred=starred, clear_group=clear_group)
    return get(conn, video_id)


# --- JSON API shared by the server surfaces ---

def handle_api(conn: db.sqlite3.Connection, method: str, path: str,
               payload: Optional[Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
    """Route one annotation API call. Returns (status, json-able body).

    Kept out of the HTTP handler so it can be tested without a socket, and so
    the routes are readable in one place.
    """
    payload = payload or {}
    try:
        if path == "/api/groups":
            if method == "GET":
                return 200, {"groups": list_groups(conn)}
            if method == "POST":
                return 200, {"group": add_group(conn, payload.get("name"))}
            return 405, {"error": "method not allowed"}

        if path.startswith("/api/groups/"):
            raw = path[len("/api/groups/"):]
            if not raw.isdigit():
                return 404, {"error": "no such group"}
            gid = int(raw)
            if method in ("POST", "PATCH"):
                if payload.get("delete"):
                    remove_group(conn, gid)
                    return 200, {"deleted": gid, "groups": list_groups(conn)}
                return 200, {"group": rename_group(conn, gid, payload.get("name"))}
            if method == "DELETE":
                remove_group(conn, gid)
                return 200, {"deleted": gid, "groups": list_groups(conn)}
            return 405, {"error": "method not allowed"}

        if path.startswith("/api/annotate/"):
            video_id = path[len("/api/annotate/"):]
            if not video_id:
                return 404, {"error": "no such video"}
            if method == "GET":
                return 200, {"annotation": get(conn, video_id)}
            if method != "POST":
                return 405, {"error": "method not allowed"}
            starred = payload.get("starred")
            if starred is not None:
                starred = bool(starred)
            # An explicit null group means "file under nothing"; an absent key
            # means "leave the filing alone".
            clear = "group_id" in payload and payload.get("group_id") in (None, "", 0)
            ann = set_annotation(
                conn, video_id,
                note=payload.get("note"),
                group=None if clear else payload.get("group_id"),
                starred=starred,
                clear_group=clear,
            )
            return 200, {"annotation": ann}
    except AnnotateError as exc:
        return exc.status, {"error": str(exc)}

    return 404, {"error": "not found"}
