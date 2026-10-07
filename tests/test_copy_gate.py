"""Unit tests for creative_agent.copy_gate (pure ad-copy checks + safety net)."""

import json

import pytest

from creative_agent.copy_gate import (
    brief_avoid,
    copy_keys,
    flatten_copy_issues,
    format_copy_issues,
    gate_copies,
    parse_copies,
    product_words,
    residual_issues,
    restore_unflagged,
)


def _copy(original_id=1, **overrides):
    copy = {
        "original_id": original_id,
        "tone_style": "Humorous",
        "headline": "Beep beep, finally",
        "body_text": "Rocket Skates make you faster than the bird.",
        "trend_connection": "t",
        "audience_appeal_rationale": "a",
        "social_caption": "Zoom zoom.",
        "call_to_action": "Order yours today",
        "detailed_performance_rationale": "r",
    }
    copy.update(overrides)
    return copy


def _gate(*copies, **kwargs):
    kwargs.setdefault("target_product", "Rocket Skates")
    return gate_copies(list(copies), **kwargs)


def _gate_texts(*copies, **kwargs):
    return {k: [str(i) for i in v] for k, v in _gate(*copies, **kwargs).items()}


# --- parsing ------------------------------------------------------------------


def test_parse_copies_accepts_list_dict_and_json():
    copies = [_copy(1), _copy(2)]
    assert parse_copies(copies) == copies
    assert parse_copies({"ad_copies": copies}) == copies
    assert parse_copies(json.dumps({"ad_copies": copies})) == copies
    assert parse_copies(json.dumps(copies)) == copies


@pytest.mark.parametrize(
    "value", [None, "", "not json", 3, {"ad_copies": None}, {"other": []}]
)
def test_parse_copies_never_raises(value):
    assert parse_copies(value) == []


def test_parse_copies_drops_malformed_entries():
    assert parse_copies({"ad_copies": [_copy(1), "junk", 3]}) == [_copy(1)]


def test_gate_accepts_every_state_shape():
    bad = _copy(4, body_text="Go fast.")
    for value in ([bad], {"ad_copies": [bad]}, json.dumps({"ad_copies": [bad]})):
        assert list(gate_copies(value, target_product="Rocket Skates")) == ["4"]


def test_clean_copies_have_no_issues():
    assert _gate(_copy(1), _copy(2)) == {}
    assert gate_copies(None, target_product="Rocket Skates") == {}


# --- product named --------------------------------------------------------------


def test_product_named_by_full_name_or_any_significant_word():
    assert _gate(_copy(body_text="Strap on Rocket Skates.")) == {}
    # Any significant word is enough, case-insensitively.
    assert _gate(_copy(body_text="Go ROCKET-powered.")) == {}
    assert _gate(_copy(body_text="Go fast.", headline="Rocket time")) == {}
    assert _gate(_copy(body_text="Go fast.", social_caption="#rocket")) == {}


def test_product_not_named_is_flagged():
    (issues,) = _gate_texts(
        _copy(7, body_text="Go fast.", call_to_action="Order today")
    ).values()
    assert issues == [
        "product not named: mention 'Rocket Skates' in the headline, body text, "
        "social caption or call to action."
    ]


def test_product_word_must_be_a_whole_word():
    # "rocketry" is not "rocket".
    issues = _gate_texts(
        _copy(body_text="Pure rocketry.", call_to_action="Order today")
    )
    assert "product not named" in issues["1"][0]


def test_product_named_in_the_cta_counts():
    copy = _copy(body_text="Go fast.", call_to_action="Buy Rocket Skates")
    assert _gate(copy) == {}


def _names(product, text):
    copy = _copy(body_text=text, call_to_action="Order today")
    return gate_copies([copy], target_product=product) == {}


@pytest.mark.parametrize(
    ("product", "text"),
    [
        # Brand "Powerball": a brand word inside the product phrase counts.
        ("Powerball tickets", "Play Powerball tonight"),
        # Plural/singular forms match both ways.
        ("Powerball ticket", "Grab Powerball tickets"),
        ("Rocket Skates", "Lace up your skates"),
        ("Running shoe", "New running shoes drop"),
        ("Boxes", "Pack the lunch box"),
        # Pure numbers/years are not significant; another word is enough.
        ("2026 eco running shoes", "New running shoes drop"),
        ("PRS SE Custom 24", "Meet the new PRS SE"),  # brand "PRS"
        # Possessives are stripped.
        ("Rocket Skates", "The rocket's red glare"),
        ("Rocket Skates", "The rocket’s red glare"),
    ],
)
def test_product_named_after_light_normalisation(product, text):
    assert _names(product, text)


@pytest.mark.parametrize(
    ("product", "text"),
    [
        ("PRS SE Custom 24", "Play loud all night"),
        # Stopwords, numbers and words under 3 characters never count.
        ("The New SE 24", "The new year, 24 hours, se habla"),
        # A brand outside the product phrase (brand "Acme") does not count.
        ("Rocket Skates", "Acme makes you fast"),
    ],
)
def test_product_not_named_after_light_normalisation(product, text):
    assert not _names(product, text)


def test_product_words_skip_stopwords_numbers_and_short_words():
    assert product_words("The New Acme Rocket Skates") == ["acme", "rocket", "skates"]
    assert product_words("PRS SE Custom 24") == ["prs", "custom"]
    assert product_words("2026 eco running shoes") == ["eco", "running", "shoes"]
    assert product_words("Bob's Burgers") == ["bob", "burgers"]
    assert product_words("SE 24") == []
    # No significant word: only the full product phrase can match.
    assert "1" in gate_copies(
        [_copy(call_to_action="Order today")], target_product="SE 24"
    )
    assert gate_copies([_copy(body_text="SE 24!")], target_product="SE 24") == {}


def test_blank_target_product_skips_the_product_check():
    assert gate_copies([_copy(body_text="Go fast.")], target_product="") == {}


# --- CTA, headline, caption -----------------------------------------------------


def test_empty_cta_is_flagged():
    (issue,) = _gate_texts(_copy(call_to_action="  "))["1"]
    assert issue.startswith("call_to_action is empty")


def test_cta_word_limit():
    assert (
        _gate_texts(_copy(call_to_action="one two three four five six seven eight"))
        == {}
    )
    (issue,) = _gate_texts(
        _copy(call_to_action="one two three four five six seven eight nine")
    )["1"]
    assert issue.startswith("call_to_action has 9 words (")
    assert "at most 8 words" in issue


def test_headline_char_limit():
    assert _gate_texts(_copy(headline="x" * 60)) == {}
    (issue,) = _gate_texts(_copy(headline="x" * 61))["1"]
    assert issue.startswith("headline is 61 characters")
    assert "at most 60 characters" in issue


def test_social_caption_char_limit():
    assert _gate_texts(_copy(social_caption="x" * 2200)) == {}
    (issue,) = _gate_texts(_copy(social_caption="x" * 2201))["1"]
    assert issue.startswith("social_caption is 2201 characters")


# --- avoid terms ----------------------------------------------------------------


def test_avoid_term_matched_as_word_or_phrase_case_insensitively():
    copy = _copy(body_text="Rocket Skates: no more Cliff  Falls.")
    (issue,) = _gate_texts(copy, avoid=["cliff falls"])["1"]
    assert issue.startswith("contains the avoided term 'cliff falls'")
    # Whole words only: "anvils" does not hit "anvil", nor a different phrase.
    assert _gate_texts(_copy(body_text="Rocket Skates, anvils."), avoid=["anvil"]) == {}
    assert _gate_texts(copy, avoid=["cliff diving"]) == {}


def test_avoid_term_in_the_cta_is_flagged():
    copy = _copy(call_to_action="Buy cheap skates")
    assert "avoided term 'cheap'" in _gate_texts(copy, avoid=["cheap"])["1"][0]


def test_avoid_as_string_is_split():
    copy = _copy(body_text="Rocket Skates beat anvils and cliffs.")
    issues = _gate_texts(copy, avoid="anvils, cliffs\nfalls")["1"]
    assert len(issues) == 2


def test_brief_avoid_reads_dict_or_json_brief():
    brief = {"avoid": ["cliff falls", " ", 3, "anvils"]}
    assert brief_avoid(brief) == ["cliff falls", "anvils"]
    assert brief_avoid(json.dumps(brief)) == ["cliff falls", "anvils"]
    for value in (None, "", "not json", {"avoid": "x"}, {}):
        assert brief_avoid(value) == []


# --- brief checks ---------------------------------------------------------------


def _texts(issues):
    return {key: [str(i) for i in items] for key, items in issues.items()}


def test_only_proposition_and_mandatories_self_reports_gate():
    checks = [
        {"item": "proposition", "passed": False, "note": "two ideas"},
        {"item": "mandatories", "passed": False, "note": ""},
        {"item": "reason_to_believe", "passed": False, "note": "no RTB"},
        {"item": "trend_bridge", "passed": False, "note": "forced"},
        {"item": "tone", "passed": False, "note": "too sarcastic"},
        {"item": "cta", "passed": False, "note": "generic 'Learn more'"},
        {"item": "product", "passed": False, "note": "no product"},
        {"item": "avoid", "passed": False, "note": "says cheap"},
        {"item": "proposition", "passed": True, "note": "on message"},
        "junk",
    ]
    (issues,) = _gate(_copy(brief_checks=checks)).values()
    assert [str(i) for i in issues] == [
        "brief check failed: proposition — two ideas",
        "brief check failed: mandatories",
    ]
    assert {i.kind for i in issues} == {"self_reported"}


def test_advisory_self_reports_alone_do_not_flag_a_copy():
    checks = [
        {"item": item, "passed": False, "note": "meh"}
        for item in ("reason_to_believe", "trend_bridge", "tone", "cta", "product")
    ]
    assert _gate(_copy(brief_checks=checks)) == {}


def test_deterministic_issues_are_marked_deterministic():
    (issues,) = _gate(_copy(body_text="Go fast.", headline="x" * 61)).values()
    assert [i.kind for i in issues] == ["deterministic", "deterministic"]


def test_failed_product_or_avoid_check_is_listed_once():
    """The deterministic check and the critic's own item never double-list."""
    checks = [
        {"item": "product", "passed": False, "note": "no product"},
        {"item": "avoid", "passed": False, "note": "says cliff"},
    ]
    copy = _copy(body_text="No more cliff falls.", brief_checks=checks)
    (issues,) = _gate_texts(copy, avoid=["cliff falls"]).values()
    assert len(issues) == 2
    assert issues[0].startswith("product not named")
    assert issues[1].startswith("contains the avoided term 'cliff falls'")


def test_residual_issues_keep_only_deterministic_failures():
    checks = [{"item": "proposition", "passed": False, "note": "two ideas"}]
    issues = _gate(
        _copy(1, brief_checks=checks),
        _copy(2, body_text="Go fast.", brief_checks=checks),
    )
    assert set(issues) == {"1", "2"}
    assert _texts(residual_issues(issues)) == {
        "2": [
            "product not named: mention 'Rocket Skates' in the headline, body "
            "text, social caption or call to action."
        ]
    }
    assert residual_issues({}) == {}


def test_issues_keyed_by_original_id_and_only_for_flagged_copies():
    result = _gate(_copy(1), _copy(5, body_text="x"), _copy(9, headline="y" * 80))
    assert set(result) == {"5", "9"}


def test_copy_without_original_id_is_keyed_by_position():
    copy = _copy(body_text="x")
    del copy["original_id"]
    assert list(_gate(_copy(1), copy)) == ["#1"]


# --- formatting -----------------------------------------------------------------


def test_format_copy_issues_groups_per_copy_with_headline():
    copies = {"ad_copies": [_copy(1), _copy(2, headline="Fast")]}
    text = format_copy_issues(copies, {"2": ["a", "b"]})
    assert text == '- **Copy 2 ("Fast"):**\n  - a\n  - b'


def test_flatten_copy_issues_prefixes_each_issue():
    copies = [_copy(2, headline="Fast")]
    assert flatten_copy_issues(copies, {"2": ["a", "b"], "8": ["c"]}) == [
        'Copy 2 ("Fast"): a',
        'Copy 2 ("Fast"): b',
        "Copy 8: c",
    ]


# --- restore_unflagged ----------------------------------------------------------


def _before():
    return {"ad_copies": [_copy(1), _copy(2), _copy(3), _copy(4)]}


def test_restore_keeps_a_compliant_revision():
    after = _before()
    after["ad_copies"][1] = _copy(2, body_text="Rocket Skates, revised.")
    restored, notes = restore_unflagged(_before(), after, ["2"])
    assert restored == after
    assert notes == []


def test_restore_reverts_unflagged_changes():
    after = _before()
    after["ad_copies"][1] = _copy(2, body_text="Rocket Skates, revised.")
    after["ad_copies"][3] = _copy(4, headline="Sneaky edit")
    restored, notes = restore_unflagged(_before(), after, ["2"])
    assert restored["ad_copies"][1]["body_text"] == "Rocket Skates, revised."
    assert restored["ad_copies"][3] == _copy(4)
    assert notes == ["restored copy 4: it was not flagged for revision"]


def test_restore_puts_back_missing_and_drops_extra_or_duplicate_ids():
    revised = _copy(2, body_text="Rocket Skates, revised.")
    after = {"ad_copies": [revised, _copy(2, body_text="dup"), _copy(9), _copy(1)]}
    restored, notes = restore_unflagged(_before(), after, ["2", "3"])
    assert [c["original_id"] for c in restored["ad_copies"]] == [1, 2, 3, 4]
    assert restored["ad_copies"][1] == revised
    assert restored["ad_copies"][2] == _copy(3)  # flagged but dropped
    assert restored["ad_copies"][3] == _copy(4)
    assert sorted(notes) == sorted(
        [
            "dropped a duplicate of copy 2",
            "dropped copy 9: not in the pre-revision copies",
            "restored flagged copy 3: missing from the revision",
            "restored copy 4: missing from the revision",
        ]
    )


def test_restore_keeps_the_original_order():
    after = {"ad_copies": list(reversed(_before()["ad_copies"]))}
    restored, notes = restore_unflagged(_before(), after, ["1"])
    assert restored == _before()
    assert notes == ["restored the original copy order"]


def test_restore_with_unparseable_revision_returns_the_pre_revision_copies():
    restored, notes = restore_unflagged(_before(), "garbage", ["1"])
    assert restored == _before()
    assert len(notes) == 4


def test_restore_without_a_snapshot_passes_the_revision_through():
    after = {"ad_copies": [_copy(1)]}
    assert restore_unflagged(None, after, ["1"]) == (after, [])
    assert restore_unflagged(json.dumps(_before()), json.dumps(_before()), []) == (
        _before(),
        [],
    )


# --- duplicate original_ids -----------------------------------------------------


def test_copy_keys_disambiguate_duplicate_ids():
    copies = [_copy(1), _copy(2), _copy(1), _copy(1)]
    del copies[1]["original_id"]
    assert copy_keys(copies) == ["1", "#1", "1#2", "1#3"]


def test_gate_keys_a_duplicate_id_separately():
    result = _gate(_copy(1), _copy(1, headline="y" * 80))
    assert list(result) == ["1#2"]
    text = format_copy_issues([_copy(1), _copy(1, headline="y" * 80)], result)
    assert text.startswith(f'- **Copy 1#2 ("{"y" * 80}"):**')


def test_restore_keeps_duplicate_id_copies_distinct():
    before = {"ad_copies": [_copy(1, headline="First"), _copy(1, headline="Second")]}
    fixed = _copy(1, headline="Second, fixed")
    after = {"ad_copies": [_copy(1, headline="First"), fixed]}
    restored, notes = restore_unflagged(before, after, ["1#2"])
    assert restored == {"ad_copies": [_copy(1, headline="First"), fixed]}
    assert notes == []


def test_restore_never_collapses_duplicate_ids_into_one_copy():
    before = {"ad_copies": [_copy(1, headline="First"), _copy(1, headline="Second")]}
    # The reviser returned only one copy with id 1 (a rewrite of the second).
    after = {"ad_copies": [_copy(1, headline="Second, fixed")]}
    restored, notes = restore_unflagged(before, after, ["1#2"])
    assert [c["headline"] for c in restored["ad_copies"]] == ["First", "Second"]
    assert sorted(notes) == [
        "restored copy 1: it was not flagged for revision",
        "restored flagged copy 1#2: missing from the revision",
    ]
