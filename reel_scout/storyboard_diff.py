"""Compare an edited storyboard against the teardown it started from (§7C).

§6D writes a reference out; nothing read one back. That left the loop the course
describes — 拆爆款 → 抽 SOP → 倒推自己的分鏡 — half built: you could export
somebody else's clip as a project, but nothing could tell you how far your
version had moved away from it.

## The number this exists to produce

A diff is the obvious output and it is not the interesting one. The interesting
one falls out of §6E: every exported cut carries the source URL in `note`, so
counting the cuts that *still* carry it measures how much of the storyboard is
still borrowed.

    38 / 42 cuts still carry the source URL

That is not a compliance check. It is the honest state of a draft: those cuts
are still describing someone else's frame, and the person holding the file is
the only one who can decide whether that is fine at this stage. The number just
stops it being invisible.

## Matching

Cuts are matched on `id`, which the export assigns (`c1`, `c2`, …). An id that
survived was edited or kept; one that vanished was removed; one that appears
with an unknown id was added. Position is deliberately not used: reordering is
an edit a storyboard tool makes constantly, and treating a moved cut as
"removed and added" would drown the real changes.

Comparison ignores whitespace but nothing else. A `vo` that gained a comma is a
change, because the person who typed the comma meant to.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .export.storyboard import REF_PREFIX

#: Fields worth reporting a change on. `imageRef` and `sketch` are excluded:
#: the schema warns they hold image data, and a storyboard tool rewrites them
#: whenever somebody draws, which would mark every cut as edited forever.
COMPARED_FIELDS: Tuple[str, ...] = ("shot", "sec", "desc", "vo", "sup",
                                    "props", "prompt", "groupId", "filmId")


def _norm(value: Any) -> str:
    """Whitespace-insensitive, nothing else. A comma somebody typed is a change."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def _by_id(cuts: Optional[List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for c in cuts or []:
        cid = c.get("id")
        if cid:
            out[str(cid)] = c
    return out


def carries_source(cut: Dict[str, Any]) -> bool:
    """True when this cut still carries the teardown marking §6E writes."""
    return _norm(cut.get("note")).startswith(REF_PREFIX)


def diff(reference: Dict[str, Any], edited: Dict[str, Any]) -> Dict[str, Any]:
    """Compare two storyboard projects. Pure — no DB, no files."""
    ref_cuts = _by_id(reference.get("cuts"))
    yours = _by_id(edited.get("cuts"))

    kept: List[str] = []
    changed: List[Dict[str, Any]] = []
    for cid, mine in yours.items():
        base = ref_cuts.get(cid)
        if base is None:
            continue
        fields = [f for f in COMPARED_FIELDS
                  if _norm(base.get(f)) != _norm(mine.get(f))]
        if fields:
            changed.append({"id": cid, "fields": fields})
        else:
            kept.append(cid)

    added = [cid for cid in yours if cid not in ref_cuts]
    removed = [cid for cid in ref_cuts if cid not in yours]

    still_ref = [cid for cid, c in yours.items() if carries_source(c)]
    field_counts: Dict[str, int] = {}
    for ch in changed:
        for f in ch["fields"]:
            field_counts[f] = field_counts.get(f, 0) + 1

    return {
        "reference_cuts": len(ref_cuts),
        "your_cuts": len(yours),
        "kept": sorted(kept),
        "changed": sorted(changed, key=lambda c: c["id"]),
        "added": sorted(added),
        "removed": sorted(removed),
        "changed_fields": field_counts,
        "still_carrying_source": sorted(still_ref),
        "title_still_marked": _norm(
            (edited.get("meta") or {}).get("title")).startswith(REF_PREFIX),
    }


def format_diff(d: Dict[str, Any], title: str = "") -> str:
    lines = ["Storyboard diff%s" % (" — %s" % title if title else ""), "=" * 52]
    lines.append("reference  %d cut(s)" % d["reference_cuts"])
    lines.append("yours      %d cut(s)" % d["your_cuts"])
    lines.append("")
    lines.append("  unchanged  %d" % len(d["kept"]))
    if d["changed"]:
        detail = ", ".join("%s %d" % (f, n)
                           for f, n in sorted(d["changed_fields"].items()))
        lines.append("  edited     %d   (%s)" % (len(d["changed"]), detail))
    else:
        lines.append("  edited     0")
    lines.append("  added      %d" % len(d["added"]))
    lines.append("  removed    %d" % len(d["removed"]))
    lines.append("")

    n_ref = len(d["still_carrying_source"])
    if n_ref:
        lines.append("%d / %d of your cuts still carry the source URL."
                     % (n_ref, d["your_cuts"]))
        lines.append("Those cuts are still describing someone else's frame. "
                     "Whether that is fine at this stage is your call — this "
                     "only stops it being invisible.")
    else:
        lines.append("No cut carries the source URL any more.")
    if d["title_still_marked"]:
        lines.append("The project title is still marked %s." % REF_PREFIX)
    return "\n".join(lines)
