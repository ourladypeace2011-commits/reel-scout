"""Shot size as a constrained value (roadmap §Phase 6B)."""
from __future__ import annotations

import re

import pytest

from reel_scout import shot_size
from reel_scout.shot_size import SHOT_SIZES, SHOT_SIZE_NAMES, describe, normalize


def test_vocabulary_matches_the_prompt_pack_that_owns_it():
    # The forward direction (shot list -> image prompt) and this reverse one must
    # never drift into two vocabularies, so the prompt pack is the cited source
    # and this asserts the copy still matches it.
    with open("prompts/storyboard-visualize.md", encoding="utf-8") as f:
        pack = f.read()
    m = re.search(r"景別用標準縮寫（([^）]+)）", pack)
    assert m, "the prompt pack no longer states the vocabulary where this expects it"
    codes = tuple(x.strip() for x in m.group(1).split("/"))
    assert codes == SHOT_SIZES


def test_every_code_has_a_name_and_no_extras():
    assert set(SHOT_SIZE_NAMES) == set(SHOT_SIZES)


@pytest.mark.parametrize("text,code", [
    ("The frame shows a close-up of a person", "CU"),
    ("a closeup of a hand", "CU"),
    ("an extreme close-up of an eye", "ECU"),
    ("medium close up of the singer", "MCU"),
    ("a medium shot of two people", "MS"),
    ("medium long shot across the field", "MLS"),
    ("wide shot of the city", "LS"),
    ("a long shot down the alley", "LS"),
    ("extreme wide shot of the valley", "ELS"),
    ("establishing shot of the temple", "ELS"),
    ("ECU", "ECU"),
    ("mcu", "MCU"),
])
def test_recognised_phrasings(text, code):
    assert normalize(text) == code


def test_longer_name_wins_over_the_shorter_one_inside_it():
    # "extreme close-up" contains "close-up"; first-match-wins would return CU.
    assert normalize("an extreme close-up") == "ECU"
    assert normalize("a medium close-up") == "MCU"


def test_two_letter_codes_only_match_as_whole_words():
    # Without the word boundary "cu" fires inside curtain / document / focus,
    # and a shot size that appears because a wall had curtains is worse than no
    # shot size at all.
    for text in ("the curtain was drawn", "a document on the table",
                 "the subject is in focus", "accurate colours", "a mscript"):
        assert normalize(text) is None


def test_lens_language_is_not_a_shot_size():
    # A wide-angle lens shoots close-ups all the time.
    assert normalize("a wide-angle lens view of a room") is None
    assert normalize("shot on a 35mm wide angle lens") is None


def test_two_different_sizes_in_one_sentence_refuse_rather_than_pick():
    # There is a real answer; it is not recoverable from the sentence, and
    # returning the first match would look identical to knowing.
    assert normalize("a close-up of a hand against a wide shot backdrop") is None
    assert normalize("medium shot cutting to an extreme close-up") is None


def test_the_same_size_named_twice_is_still_unambiguous():
    assert normalize("a close-up; another close up later") == "CU"


def test_nothing_about_framing_returns_none():
    assert normalize("The frame shows a dimly lit alleyway with a figure walking away") is None
    assert normalize("") is None
    assert normalize(None) is None


def test_describe_is_readable_and_survives_an_unknown_code():
    assert describe("CU") == "CU (close-up)"
    assert describe(None) == "—"
    assert describe("XX") == "XX (unknown)"


def test_normalize_is_whitespace_and_case_insensitive():
    assert normalize("  MEDIUM   SHOT  ") == "MS"
    assert normalize("medium_shot") == "MS"


def test_parse_answer_does_not_dig_a_code_out_of_a_sentence():
    # A model that answers "the focus is accurate" has not followed the
    # instruction. Salvaging the CU hiding inside "accurate" would both invent a
    # label and hide how often the constraint is being ignored — which is the
    # number worth watching when the model changes.
    from reel_scout.shot_size import parse_answer as pa
    assert pa("the focus is accurate") is None
    assert pa("I think this is a medium shot of two people") is None
    assert pa("Sorry, I cannot classify this image.") is None
