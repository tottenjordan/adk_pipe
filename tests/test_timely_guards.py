"""The timely slot's deterministic enforcement (trend-state awareness PR 3):
concept_guard.enforce_timely_cap + the evergreen/pairing concept issues, and
the copy gate's evergreen temporal-term check + enforce_timely_copy_cap."""

import json

from creative_agent.concept_guard import concept_issues, enforce_timely_cap
from creative_agent.copy_gate import enforce_timely_copy_cap, gate_copies
from creative_agent.run_policy import RunPolicy
from creative_agent.trend_situation import timely_allowed


def _concept(ad_copy_id, prompt="Rocket Skates in a roadrunner dust cloud.", **extra):
    return {
        "ad_copy_id": ad_copy_id,
        "concept_name": f"C{ad_copy_id}",
        "trend_motif": "a roadrunner dust cloud",
        "image_generation_prompt": prompt,
        **extra,
    }


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


def _texts(issues, key):
    return [i.text for i in issues.get(key, [])]


# --- timely_allowed --- #
def test_timely_allowed_needs_a_timely_situation_and_a_slot():
    ok = {"trend_situation": {"phase": "upcoming", "timely_ok": True}}
    assert timely_allowed(ok, 1) is True
    assert timely_allowed(ok, 0) is False
    assert timely_allowed({"trend_situation": {"timely_ok": "yes"}}, 1) is False
    assert timely_allowed({}, 2) is False


# --- enforce_timely_cap --- #
def test_cap_keeps_the_first_timely_concepts():
    concepts = [
        _concept(1, uses_trend_state=True, timely_reason="Game tomorrow."),
        _concept(2, uses_trend_state=True, timely_reason="Also timely."),
        _concept(3),
    ]
    out, warns = enforce_timely_cap(concepts, timely_ok=True, max_timely=1)
    assert [c["uses_trend_state"] for c in out[:2]] == [True, False]
    assert out[1]["timely_reason"] == "Also timely. (Not timely: cap reached.)"
    assert out[0]["timely_reason"] == "Game tomorrow."
    assert warns == ["C2: timely flag cleared (cap reached)"]
    assert concepts[1]["uses_trend_state"] is True  # input untouched


def test_not_timely_clears_every_flag():
    concepts = [_concept(1, uses_trend_state=True), _concept(2, uses_trend_state=True)]
    out, warns = enforce_timely_cap(concepts, timely_ok=False, max_timely=2)
    assert [c["uses_trend_state"] for c in out] == [False, False]
    assert out[0]["timely_reason"] == "(Not timely: no validated current moment.)"
    assert len(warns) == 2


def test_cap_without_flags_changes_nothing():
    concepts = [_concept(1), _concept(2, uses_trend_state="true")]
    out, warns = enforce_timely_cap(concepts, timely_ok=True, max_timely=1)
    assert out[0] == concepts[0]
    assert out[1]["uses_trend_state"] is False  # non-bool normalises to False
    assert warns == []


# --- concept_issues --- #
_TIMELY_COPIES = {
    "ad_copies": [
        _copy(1, uses_trend_state=True),
        _copy(2),
        _copy(3),
    ]
}


def test_evergreen_concept_with_temporal_terms_is_flagged():
    concepts = [
        _concept(1, "Fans celebrate as Texas won tonight.", uses_trend_state=True),
        _concept(2, "Rocket Skates after they won tonight in a dust cloud."),
    ]
    issues = concept_issues(concepts, _TIMELY_COPIES, timely_ok=True)
    assert "1" not in issues  # the timely concept may reference the moment
    (text,) = _texts(issues, "2")
    assert text.startswith(
        "evergreen concept mentions a dated or outcome term (won, tonight)"
    )
    # Its paired copy is evergreen: making it timely would only trade this
    # issue for the pairing one, so the fix offered is removing the term.
    assert "make this the timely concept" not in text
    assert text.endswith("remove it.")


def test_evergreen_concept_offers_the_timely_option_only_with_a_timely_copy():
    concepts = [_concept(1, "Rocket Skates after they won tonight in a dust cloud.")]
    (text,) = _texts(concept_issues(concepts, _TIMELY_COPIES, timely_ok=True), "1")
    assert text.endswith("remove it or make this the timely concept.")
    # Unpaired (no copies to pair with): only removing the term is viable.
    (text,) = _texts(concept_issues(concepts, None, timely_ok=True), "1")
    assert text.endswith("remove it.")


def test_evergreen_headline_is_checked_too():
    concepts = [_concept(2, headline="Ready for Saturday")]
    issues = concept_issues(concepts, None, timely_ok=True)
    assert "Saturday" in _texts(issues, "2")[0]


def test_idioms_in_evergreen_concepts_pass():
    concepts = [_concept(2, "A win-win morning: Rocket Skates in a dust cloud.")]
    assert concept_issues(concepts, _TIMELY_COPIES, timely_ok=True) == {}


def test_timely_concept_needs_a_timely_copy():
    concepts = [_concept(2, uses_trend_state=True)]
    issues = concept_issues(concepts, _TIMELY_COPIES, timely_ok=True)
    (text,) = _texts(issues, "2")
    assert text.startswith("timely concept, but its paired ad copy is evergreen")


def test_without_a_timely_moment_concept_checks_are_unchanged():
    concepts = [
        _concept(2, "Rocket Skates after they won tonight in a dust cloud."),
        _concept(3, uses_trend_state=True),
    ]
    assert concept_issues(concepts, _TIMELY_COPIES) == {}
    assert concept_issues(concepts, _TIMELY_COPIES, timely_ok=False) == {}


# --- copy gate --- #
def test_evergreen_copy_with_temporal_terms_is_flagged():
    issues = gate_copies(
        [_copy(1, headline="We won tonight", uses_trend_state=True), _copy(2)],
        RunPolicy(target_product="Rocket Skates", timely_ok=True),
    )
    assert issues == {}
    issues = gate_copies(
        [_copy(2, body_text="Rocket Skates, ready for tomorrow's kickoff.")],
        RunPolicy(target_product="Rocket Skates", timely_ok=True),
    )
    (issue,) = issues["2"]
    assert issue.kind == "deterministic"
    assert issue.text.startswith(
        "evergreen copy mentions a dated or outcome term (tomorrow, kickoff)"
    )


def test_copy_offers_the_timely_option_only_while_the_cap_has_room():
    dated = _copy(2, body_text="Rocket Skates, ready for tomorrow's kickoff.")
    timely = _copy(1, headline="Big game tomorrow", uses_trend_state=True)
    # No timely copy yet: making this one timely is viable.
    (issue,) = gate_copies(
        [dated], RunPolicy(target_product="Rocket Skates", timely_ok=True)
    )["2"]
    assert issue.text.endswith("remove it or make this the timely copy.")
    # The cap (1 by default) is already full: only removing the term is.
    issues = gate_copies(
        [timely, dated], RunPolicy(target_product="Rocket Skates", timely_ok=True)
    )
    (issue,) = issues["2"]
    assert issue.text.endswith("remove it.")
    # A larger cap leaves room again.
    issues = gate_copies(
        [timely, dated],
        RunPolicy(target_product="Rocket Skates", timely_ok=True, max_timely=2),
    )
    assert issues["2"][0].text.endswith("make this the timely copy.")


def test_copy_idioms_and_no_moment_pass():
    assert (
        gate_copies(
            [_copy(2, body_text="Rocket Skates: never miss a beat.")],
            RunPolicy(target_product="Rocket Skates", timely_ok=True),
        )
        == {}
    )
    assert (
        gate_copies(
            [_copy(2, body_text="Rocket Skates won tonight.")],
            RunPolicy(target_product="Rocket Skates"),
        )
        == {}
    )


def test_copy_cap_clears_flags_beyond_the_cap():
    critique = {
        "ad_copies": [
            _copy(1, uses_trend_state=True, timely_reason="r1"),
            _copy(2, uses_trend_state=True, timely_reason="r2"),
        ]
    }
    out, warns = enforce_timely_copy_cap(critique, timely_ok=True, max_timely=1)
    flags = [c["uses_trend_state"] for c in out["ad_copies"]]
    assert flags == [True, False]
    assert out["ad_copies"][1]["timely_reason"] == "r2 (Not timely: cap reached.)"
    assert warns == ["copy 2: timely flag cleared (cap reached)"]
    assert critique["ad_copies"][1]["uses_trend_state"] is True


def test_copy_cap_clears_all_when_not_timely_and_keeps_json_shape():
    critique = json.dumps({"ad_copies": [_copy(1, uses_trend_state=True)]})
    out, warns = enforce_timely_copy_cap(critique, timely_ok=False, max_timely=1)
    assert isinstance(out, str)
    assert json.loads(out)["ad_copies"][0]["uses_trend_state"] is False
    assert len(warns) == 1


_TIMELY_STATE = {
    "trend_situation": {"phase": "upcoming", "timely_ok": True},
    "target_product": "Rocket Skates",
}


def test_copy_gate_writes_back_capped_flags_and_flags_evergreen_terms():
    from creative_agent.agent import copy_gate_decision

    critique = {
        "ad_copies": [
            _copy(1, uses_trend_state=True),
            _copy(2, uses_trend_state=True, body_text="Rocket Skates won tonight."),
        ]
    }
    route, delta = copy_gate_decision(
        {**_TIMELY_STATE, "ad_copy_critique": critique}, max_rounds=1
    )
    # Copy 2 lost its flag (cap 1), so its "won tonight" is now an evergreen issue.
    assert [c["uses_trend_state"] for c in delta["ad_copy_critique"]["ad_copies"]] == [
        True,
        False,
    ]
    assert route == "revise"
    assert delta["ad_copy_flagged_ids"] == ["2"]
    assert "evergreen copy mentions" in delta["ad_copy_issues"]
    snapshot = delta["ad_copy_critique__before_revision"]["ad_copies"]
    assert snapshot[1]["uses_trend_state"] is False


def test_copy_gate_without_a_moment_only_clears_stray_flags():
    from creative_agent.agent import copy_gate_decision

    critique = {"ad_copies": [_copy(1, uses_trend_state=True)]}
    route, delta = copy_gate_decision(
        {"target_product": "Rocket Skates", "ad_copy_critique": critique},
        max_rounds=1,
    )
    assert route == "ok"
    assert delta["ad_copy_critique"]["ad_copies"][0]["uses_trend_state"] is False
    # Untouched copies are not re-written.
    _, clean = copy_gate_decision(
        {
            "target_product": "Rocket Skates",
            "ad_copy_critique": {"ad_copies": [_copy(1)]},
        },
        max_rounds=1,
    )
    assert "ad_copy_critique" not in clean


def test_concept_guard_callback_applies_the_timely_cap():
    from types import SimpleNamespace

    from creative_agent import callbacks

    concepts = [
        _concept(1, uses_trend_state=True, visual_style="Watercolor / gouache"),
        _concept(2, uses_trend_state=True, visual_style="Watercolor / gouache"),
    ]
    state = {**_TIMELY_STATE, "final_visual_concepts": {"visual_concepts": concepts}}
    callbacks.ensure_trend_and_product_callback(SimpleNamespace(state=state))
    flags = [
        c["uses_trend_state"] for c in state["final_visual_concepts"]["visual_concepts"]
    ]
    assert flags == [True, False]

    state = {
        "target_product": "Rocket Skates",
        "final_visual_concepts": {"visual_concepts": concepts},
    }
    callbacks.ensure_trend_and_product_callback(SimpleNamespace(state=state))
    flags = [
        c["uses_trend_state"] for c in state["final_visual_concepts"]["visual_concepts"]
    ]
    assert flags == [False, False]


def test_copy_cap_returns_the_same_value_when_nothing_changes():
    critique = {"ad_copies": [_copy(1)]}
    out, warns = enforce_timely_copy_cap(critique, timely_ok=True, max_timely=1)
    assert out is critique and warns == []
    assert enforce_timely_copy_cap(None, timely_ok=True, max_timely=1) == (None, [])
