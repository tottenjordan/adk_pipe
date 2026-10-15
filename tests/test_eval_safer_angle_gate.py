"""creative_eval: the safer_angle_respected gate (ad copy + visual).

Judged only on a safer-angle run (``state["safer_angle_active"] == "yes"``: the
user deliberately picked a sensitive trend and opted in to a respectful,
positive angle on its subject). Otherwise passed in code with note
"no safer angle", whatever the judge says; omitted by the judge on an active
run it fails as "not checked" with a report warning. Also pins the
JUDGE_VERSION bump that came with it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import creative_eval.agent as ev_agent
from creative_eval import JUDGE_VERSION
from creative_eval import prompts as eval_prompts
from creative_eval.brief import format_brief_for_judge
from creative_eval.config import EvalConfig
from creative_eval.dimensions import (
    AD_COPY_GATES,
    ADVISORY_GATES,
    BRIEF_GATES,
    GATE_LABELS,
    PERSON_GATES,
    SAFER_ANGLE_GATES,
    TIMELY_GATES,
    TREND_GATES,
    VISUAL_GATES,
)
from creative_eval.evaluate import (
    NO_SAFER_ANGLE_NOTE,
    NOT_CHECKED_NOTE,
    evaluate_ad_copy,
    evaluate_all_concurrently,
    evaluate_visual_concept,
    normalize_gates,
    safer_angle_active,
    unreported_gates_warning,
)
from creative_eval.schemas import AdCopyEvaluation, CreativeScore, GateResult
from tests._eval_fakes import all_verdict_dicts
from tests._fakes import FakeToolContext

GATE = "safer_angle_respected"
CAMPAIGN = {
    "brand": "Hohner",
    "target_product": "Clavinet D7",
    "target_audience": "funk players",
    "key_selling_points": "iconic tone",
    "target_search_trend": "tupac",
}
AD_COPY = {
    "original_id": 1,
    "headline": "Chase the sound",
    "body_text": "A legacy of chasing your music dreams.",
    "tone_style": "Inspirational",
    "trend_connection": "Tupac's legacy",
    "audience_appeal_rationale": "players",
    "social_caption": "Play on",
    "call_to_action": "Find yours",
    "detailed_performance_rationale": "r",
}
CONCEPT = {
    "ad_copy_id": 1,
    "concept_name": "Studio",
    "visual_style": "Photoreal / editorial",
    "trend": "tupac",
    "trend_reference": "r",
    "markets_product": "m",
    "audience_appeal": "a",
    "selection_rationale": "s",
    "headline": "Chase the sound",
    "social_caption": "Play on",
    "call_to_action": "Find yours",
    "concept_summary": "sum",
    "image_generation_prompt": "a Clavinet in a late-night studio",
}
BRIEF = {
    "single_minded_proposition": "Chase your sound.",
    "trend_bridge": {
        "fit_score": 3,
        "fit_mode": "adjacent",
        "bridge": "music dreams",
        "safer_angle": "Tupac's legacy of chasing your music {dreams}",
    },
}
REASON = "trial over a real person's murder"


def test_gate_is_registered_labelled_and_the_version_bumped():
    assert GATE in AD_COPY_GATES and GATE in VISUAL_GATES
    assert SAFER_ANGLE_GATES == frozenset({GATE})
    assert GATE not in (
        ADVISORY_GATES | BRIEF_GATES | PERSON_GATES | TREND_GATES | TIMELY_GATES
    )
    assert GATE_LABELS[GATE] == "Respectful reference"
    assert JUDGE_VERSION == "2026-10-15.1"


def test_prompts_name_the_gate_and_the_new_counts():
    ad, vis = eval_prompts.AD_COPY_EVAL_USER, eval_prompts.VISUAL_CONCEPT_EVAL_USER
    assert len(AD_COPY_GATES) == 9 and len(VISUAL_GATES) == 10
    assert "Return all 9 gates" in ad and "Return all 10 gates" in vis
    for text in (ad, vis):
        assert f"**{GATE}**" in text
        assert "Safer Angle: {safer_angle}" in text
        assert "never references the sensitive event, its victims or harm" in text
        assert "respectful tribute or inspiration" in text
        assert "no endorsement, partnership, licensing" in text
        assert "quotes or words put in their mouth" in text
        assert 'pass with the note "no safer angle"' in text
    tie = "For a safer-angle run, a clear tie through the brief's safer angle "
    assert tie + "(the subject's positive legacy) counts as connected." in ad
    assert tie + "(the subject's positive legacy) counts as recognisable" in vis
    assert "no likeness of the real person and no estate-owned imagery" in vis


def _raw(gates, passed: bool, note="looks fine"):
    return [GateResult(gate=g, passed=passed, note=note) for g in gates]


def _gate(gates):
    return next(g for g in gates if g.gate == GATE)


@pytest.mark.parametrize("expected", [AD_COPY_GATES, VISUAL_GATES])
@pytest.mark.parametrize("judged", [True, False, None], ids=["pass", "fail", "omit"])
def test_inactive_passes_in_code_whatever_the_judge_says(expected, judged):
    raw = (
        [GateResult(gate=g, passed=True) for g in expected if g != GATE]
        if judged is None
        else _raw(expected, judged)
    )
    gate = _gate(normalize_gates(raw, expected, brief_used=True))
    assert gate.passed is True and gate.note == NO_SAFER_ANGLE_NOTE


@pytest.mark.parametrize("expected", [AD_COPY_GATES, VISUAL_GATES])
@pytest.mark.parametrize("judged", [True, False])
def test_active_keeps_the_judges_verdict(expected, judged):
    gates = normalize_gates(
        _raw(expected, judged, "names the trial"),
        expected,
        brief_used=True,
        safer_angle=True,
    )
    gate = _gate(gates)
    assert gate.passed is judged and gate.note == "names the trial"


@pytest.mark.parametrize("expected", [AD_COPY_GATES, VISUAL_GATES])
def test_active_omitted_fails_not_checked_with_a_warning(expected):
    raw = [GateResult(gate=g, passed=True) for g in expected if g != GATE]
    gates = normalize_gates(raw, expected, brief_used=True, safer_angle=True)
    gate = _gate(gates)
    assert gate.passed is False and gate.note == NOT_CHECKED_NOTE
    ev = AdCopyEvaluation(
        original_id=1,
        headline="h",
        tone_style="t",
        score=CreativeScore(
            overall_score=0.9,
            passed=False,
            verdicts=[],
            strengths=[],
            improvements=[],
            gates=gates,
            gates_passed=False,
        ),
    )
    assert unreported_gates_warning([ev], []) == [
        "1 safer-angle check not reported by the judge (failed as not checked)"
    ]


def test_safer_angle_active_only_for_yes():
    assert safer_angle_active({"safer_angle_active": "yes"})
    for value in ("", None, "no", True, "YES "):
        assert not safer_angle_active({"safer_angle_active": value})
    assert not safer_angle_active({})


def test_brief_block_lines_only_when_active():
    active = format_brief_for_judge(BRIEF, safer_angle=True, sensitive_reason=REASON)
    assert "Safer angle: Tupac's legacy of chasing your music {dreams}" in active
    assert f"Sensitive event (never referenced): {REASON}" in active
    inactive = format_brief_for_judge(BRIEF, sensitive_reason=REASON)
    assert "Safer angle" not in inactive and "Sensitive event" not in inactive
    assert inactive == format_brief_for_judge(BRIEF)
    # Missing values: no empty lines.
    old = {**BRIEF, "trend_bridge": {"bridge": "b"}}
    block = format_brief_for_judge(old, safer_angle=True)
    assert "Safer angle" not in block and "Sensitive event" not in block
    # No brief: the sensitive event is still named.
    no_brief = format_brief_for_judge(None, safer_angle=True, sensitive_reason=REASON)
    assert f"Sensitive event (never referenced): {REASON}" in no_brief


def _score(gates, gate_passed: bool) -> dict:
    return {
        "verdicts": all_verdict_dicts(9),
        "strengths": [],
        "improvements": [],
        "gates": [
            {"gate": g, "passed": g != GATE or gate_passed, "note": "n"} for g in gates
        ],
    }


def _client(body: dict) -> MagicMock:
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text=json.dumps(body))
    return client


def _prompt(client) -> str:
    contents = client.models.generate_content.call_args.kwargs["contents"]
    if isinstance(contents, str):
        return contents
    return "\n".join(getattr(p, "text", None) or "" for p in contents)


def test_active_ad_copy_sees_the_safer_angle_and_keeps_the_verdict():
    client = _client(
        {
            "original_id": 1,
            "headline": "h",
            "tone_style": "t",
            "score": _score(AD_COPY_GATES, False),
        }
    )
    result = evaluate_ad_copy(
        AD_COPY,
        CAMPAIGN,
        EvalConfig(),
        client=client,
        brief=BRIEF,
        safer_angle=True,
        sensitive_reason=REASON,
    )
    prompt = _prompt(client)
    assert "Safer Angle: yes" in prompt
    assert "Safer angle: Tupac's legacy of chasing your music {dreams}" in prompt
    assert f"Sensitive event (never referenced): {REASON}" in prompt
    assert _gate(result.score.gates).passed is False and not result.score.passed


def test_inactive_visual_sees_no_safer_angle_and_passes_in_code():
    client = _client(
        {
            "ad_copy_id": 1,
            "concept_name": "Studio",
            "score": _score(VISUAL_GATES, False),
        }
    )
    result = evaluate_visual_concept(
        CONCEPT, CAMPAIGN, EvalConfig(), client=client, brief=BRIEF
    )
    prompt = _prompt(client)
    assert "Safer Angle: no" in prompt and "Sensitive event" not in prompt
    gate = _gate(result.score.gates)
    assert gate.passed is True and gate.note == NO_SAFER_ANGLE_NOTE


def test_concurrent_eval_passes_the_flag_to_both_kinds():
    def generate(**kwargs):
        if kwargs["config"].system_instruction == eval_prompts.AD_COPY_EVAL_SYSTEM:
            body = {
                "original_id": 1,
                "headline": "h",
                "tone_style": "t",
                "score": _score(AD_COPY_GATES, False),
            }
        else:
            body = {
                "ad_copy_id": 1,
                "concept_name": "Studio",
                "score": _score(VISUAL_GATES, False),
            }
        return MagicMock(text=json.dumps(body))

    client = MagicMock()
    client.models.generate_content.side_effect = generate
    ads, visuals = evaluate_all_concurrently(
        [AD_COPY],
        [CONCEPT],
        CAMPAIGN,
        EvalConfig(),
        client,
        brief=BRIEF,
        safer_angle=True,
        sensitive_reason=REASON,
    )
    for ev in (*ads, *visuals):
        gate = _gate(ev.score.gates)
        assert gate.passed is False and gate.note == "n"


def _state(**extra) -> dict:
    return {
        "brand": "Hohner",
        "target_product": "Clavinet D7",
        "target_audience": "funk players",
        "key_selling_points": "iconic tone",
        "target_search_trends": "tupac",
        "ad_copy_critique": {"ad_copies": [AD_COPY]},
        "creative_brief": BRIEF,
        **extra,
    }


ACTIVE = {
    "safer_angle_active": "yes",
    "safer_angle_sensitive_reason": REASON,
    "safer_angle_named_copies": [1],
}


def test_fingerprint_changes_with_the_safer_angle_fields():
    fp = ev_agent.ad_copy_eval_fingerprint
    base = fp(_state(**ACTIVE))
    assert base == fp(_state(**ACTIVE))
    assert base != fp(_state())
    assert base != fp(_state(**{**ACTIVE, "safer_angle_active": ""}))
    assert base != fp(_state(**{**ACTIVE, "safer_angle_sensitive_reason": "other"}))
    assert base != fp(_state(**{**ACTIVE, "safer_angle_named_copies": []}))
    other = {**BRIEF, "trend_bridge": {**BRIEF["trend_bridge"], "safer_angle": "x"}}
    assert base != fp(_state(**ACTIVE, creative_brief=other))


def test_agent_passes_the_safer_angle_state_through(monkeypatch):
    seen: list[dict] = []

    def fake(*args, **kwargs):
        seen.append(kwargs)
        return [], []

    monkeypatch.setattr(ev_agent, "evaluate_all_concurrently", fake)
    ev_agent.evaluate_ad_copies_only(FakeToolContext(_state(**ACTIVE)))
    ev_agent.evaluate_all_creatives(FakeToolContext(_state(**ACTIVE)))
    ev_agent.evaluate_all_creatives(FakeToolContext(_state()))
    assert [(k["safer_angle"], k["sensitive_reason"]) for k in seen] == [
        (True, REASON),
        (True, REASON),
        (False, ""),
    ]
