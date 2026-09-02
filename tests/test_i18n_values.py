"""Translating decoded VALUES, not just labels."""
from __future__ import annotations

import re

from reel_scout.i18n import STRINGS, VALUE_KEYS, value_key


def test_every_closed_vocabulary_value_has_traditional_chinese():
    for v in VALUE_KEYS:
        key = "val.%s" % v
        assert key in STRINGS["en"] and key in STRINGS["zh"]
        assert STRINGS["en"][key] == v, "English baseline is the value itself"
        assert STRINGS["zh"][key], "%s has no Chinese" % v


def test_the_vocabulary_matches_the_merge_prompt_that_owns_it():
    # The values come from `analyze/merger.py`. Two lists drifting silently is
    # the failure this asserts against — the same reason the shot-size module
    # reads its vocabulary out of the prompt pack instead of copying it.
    from reel_scout.analyze import merger
    src = open(merger.__file__, encoding="utf-8").read()
    for field in ("content_structure", "content_type"):
        m = re.search(r'"%s":\s*"([a-z|\-]+)"' % field, src)
        assert m, "merge prompt no longer states %s where this expects it" % field
        for v in m.group(1).split("|"):
            assert v in VALUE_KEYS, "%s is in the prompt but not translatable" % v


def test_free_text_is_never_a_translatable_value():
    # A hook line, a CTA phrase, a title. Translating these would be a different
    # claim from the one the model made.
    for text in ("快去試試看", "Awich - Wax On Wax Off", "3 ways to edit faster",
                 "", None, "  "):
        assert value_key(text) is None


def test_a_known_value_maps_and_an_unknown_one_does_not():
    assert value_key("talking_head") == "val.talking_head"
    assert value_key(" montage ") == "val.montage"
    # A vocabulary that grows in the merge prompt but not here shows the raw
    # value rather than a stale translation of a neighbour.
    assert value_key("documentary") is None


def test_none_is_shared_by_both_fields_and_means_the_same_thing():
    assert value_key("none") == "val.none"
    assert STRINGS["zh"]["val.none"] == "無"


def _tagged(page):
    return set(re.findall(r'data-i18n="(val\.[^"]*)"', page))


def test_inspector_tags_decoded_values_but_not_the_free_text_beside_them():
    from reel_scout import inspector
    view = {
        "content_structure": "hook-body-cta", "content_type": "educational",
        "style": {"format": "tutorial", "pacing": "fast"},
        "hook": {"opening_type": "question", "opening_text": "你知道嗎？",
                 "cta_type": "visit", "cta_text": "快去試試看"},
    }
    html = inspector._render_structure(view)
    assert _tagged(html) == {"val.hook-body-cta", "val.educational",
                             "val.tutorial", "val.fast", "val.question",
                             "val.visit"}
    assert "你知道嗎？" in html and "val.你知道嗎？" not in html
    assert "快去試試看" in html


def test_an_unknown_value_renders_untagged_rather_than_mistranslated():
    from reel_scout import inspector
    html = inspector._render_structure(
        {"content_structure": "documentary", "style": {}, "hook": {}})
    assert "documentary" in html
    assert _tagged(html) == set()


def test_english_baseline_still_reads_with_js_off():
    # applyLang swaps client-side; the served HTML must already say the English
    # word, or the page is blank without JS and string-contains tests stop
    # meaning anything.
    from reel_scout import inspector
    html = inspector._render_structure(
        {"content_structure": "raw-moment", "style": {}, "hook": {}})
    assert ">raw-moment<" in html
