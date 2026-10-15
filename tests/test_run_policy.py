"""``creative_agent.run_policy.RunPolicy.from_state``: every field derived
from a state snapshot exactly as the gates' former ad-hoc reads did (one
golden state per feature combination)."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from creative_agent.run_policy import RunPolicy, is_trend_sensitive, selling_points

CONFIG = SimpleNamespace(max_timely_concepts=1)

BRIEF = {
    "avoid": ["[anvils]", " cliff falls ", "", 3],
    "mandatories": ["Rocket Skates", "18+ only"],
    "trend_bridge": {
        "motifs": [" dust cloud ", "", "desert highway"],
        "risks": ["Never mock the fall."],
        "safer_angle": "  the coyote's grit  ",
    },
}
TIMELY = {"phase": "upcoming", "timely_ok": True, "outcome": "none"}
SOURCES = {"src-1": {"url": "https://example.com"}}


def _base(**extra):
    return {
        "brand": "ACME",
        "target_product": "Rocket Skates",
        "target_search_trends": "roadrunner race",
        **extra,
    }


def test_empty_state_is_the_default_policy():
    assert RunPolicy.from_state({}, CONFIG) == RunPolicy(max_timely=1)
    assert RunPolicy() == RunPolicy(max_timely=1)


def test_campaign_inputs_and_brief_lists():
    policy = RunPolicy.from_state(
        _base(
            creative_brief=BRIEF,
            brand_colors="red, tan",
            key_selling_points="Guaranteed fastest skates",
            sources=SOURCES,
        ),
        CONFIG,
    )
    assert policy == RunPolicy(
        brand="ACME",
        target_product="Rocket Skates",
        trend="roadrunner race",
        timely_ok=False,
        max_timely=1,
        strictness=(),
        trend_sensitive=False,
        safer_angle_active=False,
        safer_angle_text="the coyote's grit",
        claims_allowed_text="Guaranteed fastest skates",
        avoid=("anvils", "cliff falls"),
        mandatories=("Rocket Skates", "18+ only"),
        motifs=("dust cloud", "desert highway"),
        risks=("Never mock the fall.",),
        brand_colors="red, tan",
        sources=SOURCES,
        trend_situation=None,
    )


def test_brief_as_json_string_reads_the_same():
    as_dict = RunPolicy.from_state(_base(creative_brief=BRIEF), CONFIG)
    as_json = RunPolicy.from_state(_base(creative_brief=json.dumps(BRIEF)), CONFIG)
    assert as_dict == as_json


@pytest.mark.parametrize(
    ("extra", "timely_ok", "sensitive"),
    [
        # A timely moment with the slot on.
        ({"trend_situation": TIMELY}, True, False),
        # Not timely: the moment says so.
        ({"trend_situation": {**TIMELY, "timely_ok": False}}, False, False),
        # Flagged by trend_scout: never timely, and the tie is skipped (CA-1).
        ({"trend_situation": TIMELY, "trend_sensitive_hint": "yes"}, False, True),
        # A sensitive moment.
        ({"trend_situation": {"phase": "sensitive", "timely_ok": False}}, False, True),
        # Only exactly "yes" counts as the hint.
        ({"trend_sensitive_hint": True}, False, False),
        ({"trend_sensitive_hint": ""}, False, False),
        # A malformed moment is ignored.
        ({"trend_situation": "sensitive"}, False, False),
    ],
)
def test_timely_and_sensitive(extra, timely_ok, sensitive):
    policy = RunPolicy.from_state(_base(**extra), CONFIG)
    assert policy.timely_ok is timely_ok
    assert policy.trend_sensitive is sensitive
    assert is_trend_sensitive(_base(**extra)) is sensitive
    situation = extra.get("trend_situation")
    assert policy.trend_situation == (
        situation if isinstance(situation, dict) else None
    )


def test_timely_slot_off_is_never_timely():
    off = SimpleNamespace(max_timely_concepts=0)
    policy = RunPolicy.from_state(_base(trend_situation=TIMELY), off)
    assert policy.timely_ok is False
    assert policy.max_timely == 0
    two = SimpleNamespace(max_timely_concepts=2)
    assert RunPolicy.from_state(_base(trend_situation=TIMELY), two).max_timely == 2


@pytest.mark.parametrize(
    ("value", "active"),
    [("yes", True), ("", False), (True, False), ("YES", False), (None, False)],
)
def test_safer_angle_active_only_for_yes(value, active):
    state = _base(safer_angle_active=value, creative_brief=BRIEF)
    policy = RunPolicy.from_state(state, CONFIG)
    assert policy.safer_angle_active is active
    # The brief's angle is read either way (the gate decides what to do with it).
    assert policy.safer_angle_text == "the coyote's grit"


@pytest.mark.parametrize(
    ("value", "flags"),
    [
        (["weak_cta", "off_brief"], ("weak_cta", "off_brief")),
        (["bogus", "text_problem"], ("text_problem",)),
        ("weak_cta", ()),
        (None, ()),
    ],
)
def test_strictness_keeps_known_flags(value, flags):
    policy = RunPolicy.from_state(_base(rating_strictness=value), CONFIG)
    assert tuple(policy.strictness) == flags


@pytest.mark.parametrize(
    ("points", "allowed"),
    [
        ("  Lifetime warranty  ", "Lifetime warranty"),
        (["Lifetime warranty", 3, " Never breaks "], "Lifetime warranty\nNever breaks"),
        (None, ""),
    ],
)
def test_claims_only_from_the_users_selling_points(points, allowed):
    state = _base(key_selling_points=points, creative_brief=BRIEF)
    assert RunPolicy.from_state(state, CONFIG).claims_allowed_text == allowed


def test_selling_points_shape():
    assert selling_points({"key_selling_points": ["a", 1, "b"]}) == ["a", "b"]
    assert selling_points({"key_selling_points": None}) == ""
    assert selling_points({}) == ""


def test_malformed_values_never_raise():
    state = {
        "brand": None,
        "target_product": 0,
        "target_search_trends": None,
        "creative_brief": "not json",
        "sources": ["src-1"],
        "brand_colors": None,
    }
    policy = RunPolicy.from_state(state, CONFIG)
    assert policy == RunPolicy(max_timely=1)


def test_policy_is_frozen():
    policy = RunPolicy()
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.brand = "x"  # type: ignore[misc]
