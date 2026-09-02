"""Corpus health — every gap in one place, each next to the command that fixes it.

Three separate times in one day this library was found holding a remedy it had
never been asked to run: a fake 0.0 score that `db check-invalid` was written to
catch, 113 clips without spans that `db backfill-shots` fills, and 102 relative
media paths that `db normalize-paths` rewrites. None of those were bugs. Each
tool worked. Nothing ever said the gap was there.

That is the same shape §4E already cost six weeks twice over, and #87 wrote the
lesson down: **a capability in use that no surface reports is indistinguishable
from one that is not in use.** Four remedies and four separate opportunities to
notice you need one is the same defect with a wider blast radius, so this is the
surface.

## Rules the report follows

* **Counts, never a bare percentage.** 100% of two clips and 100% of two hundred
  are different claims.
* **Every gap names its remedy** in the same line. A number with no next action
  tells the reader something is wrong and leaves them to guess what to do.
* **"Cannot" and "not yet" stay apart.** A clip whose media is gone from this
  machine is not a backlog item — nothing anyone runs here will fill it — and
  filing it next to work that *can* be done makes a finishable list look
  hopeless.
* **`actionable` drives the exit code, not the total gap count.** `--strict` is
  meant to be safe in a heartbeat, and a check that goes red over a clip nobody
  can fix would be turned off within a week.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from . import db, label_shots, shot_size, validity
from .utils import paths as media_paths


#: Rows already marked invalid are excluded from every count that describes
#: work, exactly as `stats` does. Without it this surface reports the clip we
#: already dealt with as an open gap -- and a dashboard that shows a gap which
#: is not there trains people to stop reading it, which is worse than not
#: having one.
_NOT_INVALID = validity.exclude_sql("v")


def _one(conn, sql: str, *params: Any) -> int:
    return conn.execute(sql, params).fetchone()[0]


def collect(conn) -> Dict[str, Any]:
    """Measure every gap. Pure read — nothing here writes or repairs."""
    total = _one(conn, "SELECT COUNT(*) FROM videos v WHERE 1=1" + _NOT_INVALID)
    analyzed = _one(conn, "SELECT COUNT(*) FROM videos v "
                          "WHERE v.status = 'analyzed'" + _NOT_INVALID)

    missing_media = 0
    for r in conn.execute("SELECT v.file_path FROM videos v WHERE 1=1" + _NOT_INVALID):
        # Through the resolver, never `os.path.exists` on the stored string: a
        # path written as `./data/...` by a process running in the repo root
        # does not exist relative to anywhere else, and `resolve_media_path`
        # is the thing that already knows that. Reading it raw is how four
        # separate modules reported a healthy library as broken in one day.
        if not media_paths.exists(r[0]):
            missing_media += 1

    # Only clips whose media is here can gain a shot table, so the denominator
    # is "measurable", not "all". Reporting 114/117 against a denominator that
    # includes three clips nobody can measure invites chasing an impossible 3.
    # Media present is part of "measurable", not a separate excuse. Counting a
    # clip whose file is gone as a shot-table gap puts a row nobody here can
    # close into the actionable list -- which is the one distinction this whole
    # module exists to keep, and the first version got it wrong.
    measurable = 0
    for r in conn.execute(
            "SELECT v.file_path FROM videos v WHERE v.status = 'analyzed' "
            "AND v.duration_sec > 0" + _NOT_INVALID):
        if media_paths.exists(r[0]):
            measurable += 1
    with_shots = _one(
        conn, "SELECT COUNT(DISTINCT s.video_id) FROM shots s "
              "JOIN videos v ON v.id = s.video_id WHERE 1=1" + _NOT_INVALID)

    scored = _one(conn, "SELECT COUNT(*) FROM scores s "
                        "JOIN videos v ON v.id = s.video_id WHERE 1=1" + _NOT_INVALID)
    scored_measured = _one(
        conn,
        "SELECT COUNT(*) FROM scores s JOIN videos v ON v.id = s.video_id "
        "WHERE EXISTS (SELECT 1 FROM shot_metrics m WHERE m.video_id = s.video_id)"
        + _NOT_INVALID)

    return {
        "schema": _one(conn, "SELECT version FROM schema_version"),
        "schema_current": db.SCHEMA_VERSION,
        "videos": total,
        "analyzed": analyzed,
        "missing_media": missing_media,
        "relative_video_paths": _one(
            conn, "SELECT COUNT(*) FROM videos v WHERE v.file_path LIKE './%'"
                  + _NOT_INVALID),
        "relative_keyframe_paths": _one(
            conn, "SELECT COUNT(*) FROM keyframes k JOIN videos v ON v.id = k.video_id "
                  "WHERE k.file_path LIKE './%'" + _NOT_INVALID),
        "invalid_marked": _one(
            conn, "SELECT COUNT(*) FROM videos WHERE status = ?",
            validity.INVALID_STATUS),
        # 🔴 `validity.scan` returns everything matching the shape, including
        # clips already marked. Reporting its raw length here showed the one
        # clip we had already dealt with as an outstanding gap -- measured, not
        # hypothetical, on the first real run of this module.
        "invalid_unmarked": len([
            r for r in validity.scan(conn)
            if _one(conn, "SELECT COUNT(*) FROM videos WHERE id = ? AND status != ?",
                    r["id"], validity.INVALID_STATUS)
        ]),
        "measurable": measurable,
        "with_shots": with_shots,
        "with_shot_labels": _one(
            conn, "SELECT COUNT(DISTINCT l.video_id) FROM shot_labels l "
                  "JOIN videos v ON v.id = l.video_id WHERE 1=1" + _NOT_INVALID),
        # Labels produced by a prompt that is no longer the current one. The
        # prompt moved the answer ten-to-one on the same images, so this is not
        # cosmetic drift — it is two different measurements sharing a column.
        "labels_from_an_old_prompt": _one(
            conn,
            "SELECT COUNT(*) FROM shot_labels l JOIN videos v ON v.id = l.video_id "
            "WHERE " + label_shots.STALE_PROMPT_SQL + _NOT_INVALID,
            *label_shots.stale_prompt_params()),
        "with_ocr": _one(
            conn, "SELECT COUNT(DISTINCT o.video_id) FROM ocr_captions o "
                  "JOIN videos v ON v.id = o.video_id WHERE 1=1" + _NOT_INVALID),
        "scored": scored,
        "scored_with_measured": scored_measured,
    }


def findings(h: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Turn measurements into rows: label, state, and the command that fixes it.

    A row is `actionable` only when running something on this machine would
    change it.
    """
    out: List[Dict[str, Any]] = []

    def add(label, ok, detail, fix=None, actionable=False):
        out.append({"label": label, "ok": ok, "detail": detail,
                    "fix": fix, "actionable": actionable})

    add("schema", h["schema"] == h["schema_current"],
        "v%s (current v%s)" % (h["schema"], h["schema_current"]),
        None if h["schema"] == h["schema_current"] else "any command runs the migration ladder",
        actionable=h["schema"] != h["schema_current"])

    # Not actionable: the media is not on this machine, and no local command
    # brings it back. Re-crawling is a decision, not a repair.
    add("media", h["missing_media"] == 0,
        "%d present, %d gone" % (h["videos"] - h["missing_media"], h["missing_media"]),
        "re-crawl to restore" if h["missing_media"] else None)

    # Informational, never actionable. `utils.paths.resolve_media_path` reads
    # both the relative and the absolute form from any working directory, so a
    # mixed library is untidy, not broken. This row was actionable in the first
    # version and it was wrong for the most instructive reason available: four
    # modules had reported a healthy library as broken that same day, and every
    # one of them had bypassed that resolver and called `os.path.exists` on the
    # stored string. The gap was in the readers, not in the data — and a
    # dashboard that points at the data would have sent the next person to
    # rewrite 2,986 rows for nothing.
    rel = h["relative_video_paths"] + h["relative_keyframe_paths"]
    add("paths", True,
        "%d video + %d keyframe path(s) stored relative (reads resolve both)"
        % (h["relative_video_paths"], h["relative_keyframe_paths"]),
        "db normalize-paths tidies them; nothing depends on it" if rel else None)

    add("invalid rows", h["invalid_unmarked"] == 0,
        "%d marked, %d matching but unmarked"
        % (h["invalid_marked"], h["invalid_unmarked"]),
        "db check-invalid --apply" if h["invalid_unmarked"] else None,
        actionable=h["invalid_unmarked"] > 0)

    gap = max(0, h["measurable"] - h["with_shots"])
    add("shot table", gap == 0,
        "%d / %d measurable clip(s)" % (h["with_shots"], h["measurable"]),
        "db backfill-shots" if gap else None, actionable=gap > 0)

    add("shot sizes", True,
        "%d clip(s) labelled" % h["with_shot_labels"],
        "shot-size <video> (interviews are skipped by default)")

    old_prompt = h.get("labels_from_an_old_prompt") or 0
    add("shot-size prompt", old_prompt == 0,
        "%d label(s) from a prompt that is no longer current" % old_prompt,
        "shot-size <video> re-runs them" if old_prompt else None,
        actionable=old_prompt > 0)

    add("on-screen text", True, "%d clip(s)" % h["with_ocr"])

    ev_gap = h["scored"] - h["scored_with_measured"]
    add("evidence under scores", ev_gap == 0,
        "%d / %d scored clip(s) have measurements"
        % (h["scored_with_measured"], h["scored"]),
        "analyze re-run fills shot_metrics" if ev_gap else None,
        actionable=ev_gap > 0)
    return out


def format_report(h: Dict[str, Any], rows: List[Dict[str, Any]]) -> str:
    lines = ["Corpus health — %d video(s), %d analyzed" % (h["videos"], h["analyzed"]),
             "=" * 52]
    for r in rows:
        mark = "  " if r["ok"] else "! "
        lines.append("%s%-22s %s" % (mark, r["label"], r["detail"]))
        if r["fix"]:
            lines.append("%-24s → %s" % ("", r["fix"]))
    todo = [r for r in rows if r["actionable"]]
    lines.append("")
    if todo:
        lines.append("%d gap(s) something on this machine can close." % len(todo))
    else:
        lines.append("Nothing here is waiting on a command.")
    return "\n".join(lines)
