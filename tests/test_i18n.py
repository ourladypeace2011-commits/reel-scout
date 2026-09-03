

def test_the_shot_grammar_vocabularies_are_translatable():
    # Both are closed vocabularies picked from a fixed list, so both are a
    # display mapping rather than a re-run of anything.
    from reel_scout import i18n
    for code in ("ECU", "CU", "MCU", "MS", "MLS", "LS", "ELS", "UNKNOWN"):
        assert i18n.value_key(code) is not None, code
    for state in ("static", "still_subject_moves", "camera_moves", "unsteady"):
        assert i18n.value_key(state) is not None, state


def test_a_missing_shot_value_carries_no_dead_i18n_key():
    from reel_scout import inspector
    assert 'data-i18n' not in inspector._cell("sgz", "-")
    assert 'data-i18n' in inspector._cell("sgz", "MCU")


def test_unknown_is_translated_because_it_is_most_of_the_column():
    # 69% of this corpus's shot-size labels are UNKNOWN, so "the question does
    # not apply here" is the most common thing the page says.
    from reel_scout import i18n
    assert i18n.STRINGS["zh"]["val.UNKNOWN"] != "UNKNOWN"
