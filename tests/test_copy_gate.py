"""Unit tests for creative_agent.copy_gate (pure ad-copy checks + safety net)."""

import json

import pytest

from creative_agent.copy_gate import (
    brief_avoid,
    flatten_copy_issues,
    format_copy_issues,
    gate_copies,
    parse_copies,
    product_token,
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
        "call_to_action": "Order your skates today",
        "detailed_performance_rationale": "r",
    }
    copy.update(overrides)
    return copy


def _gate(*copies, **kwargs):
    kwargs.setdefault("target_product", "Rocket Skates")
    kwargs.setdefault("brand", "Acme")
    return gate_copies(list(copies), **kwargs)


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


def test_product_named_by_full_name_or_first_significant_token():
    assert _gate(_copy(body_text="Strap on Rocket Skates.")) == {}
    # First significant token ("rocket") is enough, case-insensitively.
    assert _gate(_copy(body_text="Go ROCKET-powered.")) == {}
    # Also counted in the headline or the social caption.
    assert _gate(_copy(body_text="Go fast.", headline="Rocket time")) == {}
    assert _gate(_copy(body_text="Go fast.", social_caption="#rocket")) == {}


def test_product_not_named_is_flagged():
    (issues,) = _gate(_copy(7, body_text="Go fast.")).values()
    assert issues == [
        "product not named: mention 'Rocket Skates' in the headline, body text "
        "or social caption."
    ]


def test_product_token_must_be_a_whole_word():
    # "rocketry" is not "rocket".
    issues = _gate(_copy(body_text="Pure rocketry."))
    assert "product not named" in issues["1"][0]


def test_product_not_named_ignores_the_cta():
    issues = _gate(_copy(body_text="Go fast.", call_to_action="Buy Rocket Skates"))
    assert "product not named" in issues["1"][0]


def test_product_token_skips_stopwords_brand_and_short_words():
    assert product_token("The New Acme Rocket Skates", "Acme") == "rocket"
    assert product_token("PRS SE Custom 24", "PRS") == "custom"
    assert product_token("Acme", "Acme") == ""
    # Only the brand: the full product name must appear.
    assert "1" in gate_copies([_copy()], target_product="Acme", brand="Acme")
    assert gate_copies([_copy(body_text="Acme!")], target_product="Acme") == {}


def test_blank_target_product_skips_the_product_check():
    assert gate_copies([_copy(body_text="Go fast.")], target_product="") == {}


# --- CTA, headline, caption -----------------------------------------------------


def test_empty_cta_is_flagged():
    (issue,) = _gate(_copy(call_to_action="  "))["1"]
    assert issue.startswith("call_to_action is empty")


def test_cta_word_limit():
    assert _gate(_copy(call_to_action="one two three four five six seven eight")) == {}
    (issue,) = _gate(
        _copy(call_to_action="one two three four five six seven eight nine")
    )["1"]
    assert issue.startswith("call_to_action has 9 words (")
    assert "at most 8 words" in issue


def test_headline_char_limit():
    assert _gate(_copy(headline="x" * 60)) == {}
    (issue,) = _gate(_copy(headline="x" * 61))["1"]
    assert issue.startswith("headline is 61 characters")
    assert "at most 60 characters" in issue


def test_social_caption_char_limit():
    assert _gate(_copy(social_caption="x" * 2200)) == {}
    (issue,) = _gate(_copy(social_caption="x" * 2201))["1"]
    assert issue.startswith("social_caption is 2201 characters")


# --- avoid terms ----------------------------------------------------------------


def test_avoid_term_matched_as_word_or_phrase_case_insensitively():
    copy = _copy(body_text="Rocket Skates: no more Cliff  Falls.")
    (issue,) = _gate(copy, avoid=["cliff falls"])["1"]
    assert issue.startswith("contains the avoided term 'cliff falls'")
    # Whole words only: "anvils" does not hit "anvil", nor a different phrase.
    assert _gate(_copy(body_text="Rocket Skates, anvils."), avoid=["anvil"]) == {}
    assert _gate(copy, avoid=["cliff diving"]) == {}


def test_avoid_term_in_the_cta_is_flagged():
    copy = _copy(call_to_action="Buy cheap skates")
    assert "avoided term 'cheap'" in _gate(copy, avoid=["cheap"])["1"][0]


def test_avoid_as_string_is_split():
    copy = _copy(body_text="Rocket Skates beat anvils and cliffs.")
    issues = _gate(copy, avoid="anvils, cliffs\nfalls")["1"]
    assert len(issues) == 2


def test_brief_avoid_reads_dict_or_json_brief():
    brief = {"avoid": ["cliff falls", " ", 3, "anvils"]}
    assert brief_avoid(brief) == ["cliff falls", "anvils"]
    assert brief_avoid(json.dumps(brief)) == ["cliff falls", "anvils"]
    for value in (None, "", "not json", {"avoid": "x"}, {}):
        assert brief_avoid(value) == []


# --- brief checks ---------------------------------------------------------------


def test_failed_brief_checks_become_issues():
    checks = [
        {"item": "proposition", "passed": True, "note": "on message"},
        {"item": "cta", "passed": False, "note": "generic 'Learn more'"},
        {"item": "tone", "passed": False, "note": ""},
        "junk",
    ]
    issues = _gate(_copy(brief_checks=checks))["1"]
    assert issues == [
        "brief check failed: cta — generic 'Learn more'",
        "brief check failed: tone",
    ]


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
