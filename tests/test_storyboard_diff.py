"""Storyboard round-trip diff (roadmap §Phase 7C)."""
from __future__ import annotations

from reel_scout import storyboard_diff
from reel_scout.export.storyboard import REF_PREFIX
from reel_scout.storyboard_diff import carries_source, diff, format_diff

REF_NOTE = "%s https://y/abc  [0:00.0–0:05.0]" % REF_PREFIX


def _cut(cid, **kw):
    base = {"id": cid, "filmId": "f1", "groupId": "g" + cid[1:], "shot": "CU",
            "sec": 5, "desc": "a hand", "vo": "a line", "sup": "", "props": "",
            "prompt": "", "note": REF_NOTE}
    base.update(kw)
    return base


def _proj(cuts, title="%s Clip" % REF_PREFIX):
    return {"meta": {"title": title}, "cuts": cuts}


REF = _proj([_cut("c1"), _cut("c2"), _cut("c3")])


def test_an_untouched_copy_reports_no_edits():
    d = diff(REF, _proj([_cut("c1"), _cut("c2"), _cut("c3")]))
    assert len(d["kept"]) == 3
    assert d["changed"] == [] and d["added"] == [] and d["removed"] == []


def test_a_changed_field_is_named():
    d = diff(REF, _proj([_cut("c1", vo="my own line"), _cut("c2"), _cut("c3")]))
    assert d["changed"] == [{"id": "c1", "fields": ["vo"]}]
    assert d["changed_fields"] == {"vo": 1}


def test_reordering_is_not_an_edit():
    # A storyboard tool reorders constantly; treating a moved cut as
    # "removed and added" would drown the real changes.
    d = diff(REF, _proj([_cut("c3"), _cut("c1"), _cut("c2")]))
    assert len(d["kept"]) == 3 and d["added"] == [] and d["removed"] == []


def test_added_and_removed_are_tracked_by_id():
    d = diff(REF, _proj([_cut("c1"), _cut("c9")]))
    assert d["added"] == ["c9"] and d["removed"] == ["c2", "c3"]


def test_whitespace_alone_is_not_a_change_but_punctuation_is():
    d = diff(REF, _proj([_cut("c1", vo="  a   line  "), _cut("c2"), _cut("c3")]))
    assert d["changed"] == []
    d2 = diff(REF, _proj([_cut("c1", vo="a line,"), _cut("c2"), _cut("c3")]))
    assert d2["changed"][0]["fields"] == ["vo"]


def test_image_fields_are_never_compared():
    # The schema warns they hold image data and the app rewrites them whenever
    # somebody draws, which would mark every cut as edited forever.
    d = diff(REF, _proj([_cut("c1", imageRef="data:image/png;base64,zzz"),
                         _cut("c2"), _cut("c3")]))
    assert d["changed"] == []
    assert "imageRef" not in storyboard_diff.COMPARED_FIELDS


def test_the_borrowed_count_is_the_point():
    # §6E writes the source URL into every exported cut; counting the ones that
    # still carry it measures how much of the storyboard is still borrowed.
    d = diff(REF, _proj([_cut("c1"), _cut("c2", note="自己拍的：清晨溪邊"),
                         _cut("c3", note="")]))
    assert d["still_carrying_source"] == ["c1"]


def test_replacing_the_note_clears_the_marking():
    assert carries_source(_cut("c1")) is True
    assert carries_source(_cut("c1", note="my own shot")) is False
    assert carries_source(_cut("c1", note=None)) is False


def test_a_still_marked_title_is_reported():
    d = diff(REF, _proj([_cut("c1")]))
    assert d["title_still_marked"] is True
    d2 = diff(REF, _proj([_cut("c1")], title="My Drink Ad"))
    assert d2["title_still_marked"] is False


def test_report_names_the_borrowed_cuts_and_leaves_the_judgement_to_the_reader():
    text = format_diff(diff(REF, _proj([_cut("c1"), _cut("c2")])), "T")
    assert "2 / 2 of your cuts still carry the source URL" in text
    # It must not moralise or block — it reports state.
    assert "your call" in text


def test_report_says_when_nothing_is_borrowed_any_more():
    text = format_diff(
        diff(REF, _proj([_cut("c1", note="mine"), _cut("c2", note="mine")],
                        title="Mine")), "T")
    assert "No cut carries the source URL any more." in text


def test_cuts_without_an_id_are_ignored_rather_than_crashing():
    d = diff(REF, _proj([{"shot": "CU"}, _cut("c1")]))
    assert d["your_cuts"] == 1


def test_an_empty_project_is_all_removed():
    d = diff(REF, _proj([]))
    assert d["removed"] == ["c1", "c2", "c3"] and d["your_cuts"] == 0
