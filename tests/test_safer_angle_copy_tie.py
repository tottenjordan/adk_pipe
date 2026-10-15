"""Safer angle: at least half of the final copies tie to the safer angle.

On a ``safer_angle_active`` run the copy gate requires ceil(n/2) copies to name
the trend's subject or use a distinctive word of the brief's
``trend_bridge.safer_angle`` (generic words such as "soul" / "studio",
product / brand words and risk / avoid words never count); below that, every
untied copy is flagged. Inactive runs are unchanged.
"""

from __future__ import annotations

from creative_agent.copy_gate import gate_copies, safer_angle_tie_words
from creative_agent.run_policy import RunPolicy

TREND = "tupac shakur"
PRODUCT = "Fender Rhodes Stage Piano"
BRAND = "Fender"
ANGLE = (
    "Tupac Shakur's enduring artistic legacy of soulful, sample-driven analog "
    "groove, organic studio craftsmanship, and poetic musical expression."
)
RISKS = ["Never reference the murder, the shooting or his death."]
ISSUE_PREFIX = "not tied to the safer angle"

# Generic-only copies from the live 2026-10-15 runs (vague ties).
GENERIC_1 = {
    "headline": "Built by Hand. Played with Heart.",
    "body_text": "Fender Rhodes Stage Piano craftsmanship for your studio.",
}
GENERIC_2 = {
    "headline": "Own the Spark of Golden-Era Soul",
    "body_text": "The Fender Rhodes Stage Piano brings analog groove.",
}
GENERIC_3 = {
    "headline": "Soulful keys, organic sound",
    "body_text": "Fender Rhodes Stage Piano: musical expression, studio legacy.",
}
NAMED = {
    "headline": "Keys inspired by Tupac's legacy",
    "body_text": "The Fender Rhodes Stage Piano for your next track.",
}
ANGLE_WORD = {
    "headline": "Poetic keys for your next verse",
    "body_text": "The Fender Rhodes Stage Piano for sample-built beats.",
}


def _copy(original_id, fields):
    return {
        "original_id": original_id,
        "tone_style": "Inspirational",
        "social_caption": "Play it your way.",
        "call_to_action": "Find your sound",
        **fields,
    }


def _gate(*fields, **kwargs):
    kwargs.setdefault("target_product", PRODUCT)
    kwargs.setdefault("brand", BRAND)
    kwargs.setdefault("trend", TREND)
    kwargs.setdefault("risks", RISKS)
    kwargs.setdefault("safer_angle_text", ANGLE)
    kwargs.setdefault("safer_angle_active", True)
    kwargs.setdefault("trend_sensitive", True)
    copies = [_copy(i, f) for i, f in enumerate(fields, start=1)]
    issues = gate_copies(copies, RunPolicy(**kwargs))
    return {
        k: [str(i) for i in v if str(i).startswith(ISSUE_PREFIX)]
        for k, v in issues.items()
        if any(str(i).startswith(ISSUE_PREFIX) for i in v)
    }


def test_one_of_four_tied_flags_every_untied_copy():
    issues = _gate(NAMED, GENERIC_1, GENERIC_2, GENERIC_3)
    assert sorted(issues) == ["2", "3", "4"]
    (issue,) = issues["2"]
    assert issue.startswith(
        "not tied to the safer angle (Tupac Shakur's enduring artistic legacy"
    )
    assert "name the subject in tribute" in issue
    assert "at least 2 of the 4 copies" in issue
    # The examples are the subject and the angle's distinctive words.
    assert "(e.g. Tupac, Sample, Poetic)" in issue
    assert issue == next(iter(issues["4"]))


def test_two_of_four_tied_is_enough():
    assert _gate(NAMED, ANGLE_WORD, GENERIC_1, GENERIC_2) == {}
    assert _gate(GENERIC_1, NAMED, GENERIC_2, NAMED) == {}


def test_none_tied_flags_all():
    issues = _gate(GENERIC_1, GENERIC_2, GENERIC_3, GENERIC_1)
    assert sorted(issues) == ["1", "2", "3", "4"]


def test_naming_the_subject_counts():
    shakur = {"headline": "For Shakur fans", "body_text": "Fender Rhodes Stage Piano."}
    assert _gate(shakur, NAMED, GENERIC_1, GENERIC_2) == {}


def test_distinctive_angle_word_counts():
    assert _gate(ANGLE_WORD, ANGLE_WORD, GENERIC_1, GENERIC_2) == {}


def test_generic_words_never_count():
    words = safer_angle_tie_words(ANGLE, trend_terms=TREND, target_product=PRODUCT)
    assert words == ["sample", "poetic"]
    for generic in ("soul", "soulful", "studio", "analog", "legacy", "groove"):
        assert generic not in words


def test_product_and_brand_words_never_count():
    angle = "Rhodes-era warmth, Fender poetic keys and sample flips"
    words = safer_angle_tie_words(
        angle, trend_terms=TREND, target_product=PRODUCT, brand=BRAND
    )
    assert "rhodes" not in words and "fender" not in words
    assert "poetic" in words and "flips" in words
    # The product's own words in the copy do not tie it.
    product_only = {
        "headline": "Rhodes Stage Piano",
        "body_text": "The Fender Rhodes Stage Piano.",
    }
    issues = _gate(
        product_only, product_only, product_only, NAMED, safer_angle_text=angle
    )
    assert sorted(issues) == ["1", "2", "3"]


def test_risk_and_avoid_words_never_count():
    risks = ["Never reference the sample clearance lawsuit."]
    words = safer_angle_tie_words(
        ANGLE, trend_terms=TREND, target_product=PRODUCT, risks=risks
    )
    assert words == ["poetic"]
    assert safer_angle_tie_words(
        ANGLE, trend_terms=TREND, target_product=PRODUCT, avoid=["poetic"]
    ) == ["sample"]
    # "sample" in a copy no longer ties it once a risk names it.
    sample = {
        "headline": "Sample flips",
        "body_text": "Fender Rhodes Stage Piano.",
    }
    issues = _gate(sample, sample, NAMED, GENERIC_1, risks=risks)
    assert sorted(issues) == ["1", "2", "4"]
    assert _gate(sample, sample, NAMED, GENERIC_1) == {}


def test_without_an_angle_only_naming_counts():
    issues = _gate(NAMED, ANGLE_WORD, GENERIC_1, GENERIC_2, safer_angle_text="")
    assert sorted(issues) == ["2", "3", "4"]


def test_odd_count_needs_the_ceiling():
    assert sorted(_gate(NAMED, GENERIC_1, GENERIC_2)) == ["2", "3"]
    assert _gate(NAMED, ANGLE_WORD, GENERIC_1) == {}


def test_inactive_or_no_trend_is_unchanged():
    copies = (NAMED, GENERIC_1, GENERIC_2, GENERIC_3)
    assert _gate(*copies, safer_angle_active=False) == {}
    assert _gate(*copies, trend="") == {}


def test_issue_is_deterministic_and_residual():
    from creative_agent.copy_gate import residual_issues

    copies = [_copy(i, f) for i, f in enumerate((NAMED, GENERIC_1, GENERIC_2), start=1)]
    issues = gate_copies(
        copies,
        RunPolicy(
            target_product=PRODUCT,
            trend=TREND,
            safer_angle_text=ANGLE,
            safer_angle_active=True,
        ),
    )
    residual = residual_issues(issues)
    assert any(str(i).startswith(ISSUE_PREFIX) for i in residual["2"])


# --- agent wiring: the gate and interactive's residual path ------------------
def _state(*fields, **extra):
    from tests.test_creative_agent_graph import _BRIEF

    brief = dict(_BRIEF)
    brief["trend_bridge"] = {
        **brief["trend_bridge"],
        "risks": RISKS,
        "safer_angle": ANGLE,
        "motifs": ["soulful analog groove"],
    }
    brief["avoid"] = []
    brief["mandatories"] = []
    copies = []
    for i, f in enumerate(fields, start=1):
        copy = _copy(i, f)
        copy["brief_checks"] = [
            {"item": item, "passed": True, "note": ""}
            for item in ("proposition", "mandatories", "risks", "trend_bridge")
        ]
        copies.append(copy)
    return {
        "ad_copy_critique": {"ad_copies": copies},
        "target_product": PRODUCT,
        "brand": BRAND,
        "target_search_trends": TREND,
        "creative_brief": brief,
        **extra,
    }


def test_gate_decision_routes_untied_copies_to_the_reviser():
    from creative_agent.agent import copy_gate_decision

    state = _state(
        NAMED,
        GENERIC_1,
        GENERIC_2,
        GENERIC_3,
        safer_angle_active="yes",
        trend_sensitive_hint="yes",
    )
    route, delta = copy_gate_decision(state, 1)
    assert route == "revise"
    assert delta["ad_copy_flagged_ids"] == ["2", "3", "4"]
    assert ISSUE_PREFIX in delta["ad_copy_issues"]
    # Inactive: the motif ties them, the sensitive hint skips the tie.
    off = _state(NAMED, GENERIC_1, GENERIC_2, GENERIC_3, trend_sensitive_hint="yes")
    assert copy_gate_decision(off, 1)[0] == "ok"


def test_interactive_residual_applies_the_half_rule():
    from creative_agent.agent import cap_and_residual_copies

    fields = (NAMED, GENERIC_1, GENERIC_2, GENERIC_3)
    delta = cap_and_residual_copies(_state(*fields, safer_angle_active="yes"))
    residual = delta["ad_copy_critique__issues"]
    assert len([i for i in residual if ISSUE_PREFIX in i]) == 3
    off = cap_and_residual_copies(_state(*fields))
    assert not any(ISSUE_PREFIX in i for i in off["ad_copy_critique__issues"] or [])
