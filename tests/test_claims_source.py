"""CA-2: absolute claims are allowed only from the user's key_selling_points.

The brief writer writes the brief's mandatories, so a claim it invents there
must not license the same claim in the ad copy (creative_agent.agent wiring).
"""

from __future__ import annotations

import copy

from tests.test_creative_agent_graph import _BRIEF

PRODUCT = "PRS SE Custom 24"


def _copy(original_id=1, **overrides):
    item = {
        "original_id": original_id,
        "tone_style": "Humorous",
        "headline": "Roadrunners, meet PRS",
        "body_text": f"{PRODUCT}: guaranteed in tune.",
        "trend_connection": "t",
        "audience_appeal_rationale": "a",
        "social_caption": "Play loud.",
        "call_to_action": "Play yours today",
        "detailed_performance_rationale": "r",
        "brief_checks": [],
    }
    item.update(overrides)
    return item


def _state(**extra):
    brief = copy.deepcopy(_BRIEF)
    brief["mandatories"] = [
        f"name {PRODUCT}",
        f"say the {PRODUCT} is guaranteed to stay in tune",
    ]
    return {
        "ad_copy_critique": {"ad_copies": [_copy()]},
        "target_product": PRODUCT,
        "brand": "PRS",
        "target_search_trends": "roadrunner",
        "creative_brief": brief,
        "key_selling_points": "Great tone",
        **extra,
    }


def _claim_texts(state):
    from creative_agent.copy_stage import _copy_issues

    issues = _copy_issues(state).get("1", [])
    return [str(i) for i in issues if "guaranteed" in str(i)]


def test_copy_claim_licensed_only_by_a_mandatory_is_flagged():
    assert _claim_texts(_state())


def test_copy_claim_in_the_selling_points_is_allowed():
    state = _state(key_selling_points=["Great tone", "Guaranteed to stay in tune"])
    assert _claim_texts(state) == []


def test_brief_issues_flag_the_invented_mandatory():
    from creative_agent.research_stage import _brief_issues

    issues = _brief_issues(_state())
    assert any("mandator" in i and "guaranteed" in i for i in issues)
