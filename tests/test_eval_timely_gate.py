"""creative_eval: the timely_claim_supported gate (ad copy + visual).

Judged only for a timely creative (its own ``uses_trend_state`` flag, or a
visual's paired copy's, under a validated timely ``trend_situation``) against
the moment block; passed in code with note "not timely" for every other
creative. Also pins the JUDGE_VERSION bump that came with it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import creative_eval.agent as ev_agent
from creative_eval import JUDGE_VERSION
from creative_eval import prompts as eval_prompts
from creative_eval.config import EvalConfig
from creative_eval.dimensions import (
    AD_COPY_GATES,
    ADVISORY_GATES,
    BRIEF_GATES,
    GATE_LABELS,
    PERSON_GATES,
    TIMELY_GATES,
    TREND_GATES,
    VISUAL_GATES,
)
from creative_eval.evaluate import (
    NO_TIMELY_NOTE,
    NOT_CHECKED_NOTE,
    creative_is_timely,
    evaluate_ad_copy,
    evaluate_all_concurrently,
    evaluate_visual_concept,
    moment_block,
    normalize_gates,
    unreported_gates_warning,
)
from creative_eval.schemas import (
    AdCopyEvaluation,
    CreativeEvaluationReport,
    CreativeScore,
    GateResult,
    VisualConceptEvaluation,
)
from tests._eval_fakes import all_verdict_dicts
from tests._fakes import FakeToolContext

GATE = "timely_claim_supported"
CAMPAIGN = {
    "brand": "ACME",
    "target_product": "Rocket Skates",
    "target_audience": "Texas fans",
    "key_selling_points": "fast",
    "target_search_trend": "texas vs oklahoma",
}
AD_COPY = {
    "original_id": 3,
    "headline": "Game day speed",
    "body_text": "Skate to the watch party.",
    "tone_style": "Playful",
    "trend_connection": "Red River rivalry",
    "audience_appeal_rationale": "fans",
    "social_caption": "Zoom",
    "call_to_action": "Shop now",
    "detailed_performance_rationale": "r",
}
CONCEPT = {
    "ad_copy_id": 3,
    "concept_name": "Rivalry",
    "visual_style": "Comic panel",
    "trend": "texas vs oklahoma",
    "trend_reference": "r",
    "markets_product": "m",
    "audience_appeal": "a",
    "selection_rationale": "s",
    "headline": "Game day speed",
    "social_caption": "Zoom",
    "call_to_action": "Shop now",
    "concept_summary": "sum",
    "image_generation_prompt": "a fan skating to the stadium",
}
SITUATION = {
    "phase": "upcoming",
    "event_name": "Texas vs Oklahoma",
    "event_start": "2026-10-14",
    "event_end": "",
    "outcome": "none",
    "outcome_summary": "",
    "source_ids": ["src-1", "src-9"],
    "confidence": "high",
    "as_of": "2026-10-13T09:00:00-04:00",
    "timely_ok": True,
    "valid_until": "2026-10-14T23:59:59-04:00",
    "tone_rule": "Anticipation; never predict the score.",
    "abstain_reason": "",
}
SOURCES = {
    "src-1": {"short_id": "src-1", "url": "https://news.example/2026/10/12/game"},
}


def test_gate_is_registered_labelled_and_the_version_bumped():
    assert GATE in AD_COPY_GATES and GATE in VISUAL_GATES
    assert TIMELY_GATES == frozenset({GATE})
    assert GATE not in ADVISORY_GATES | BRIEF_GATES | PERSON_GATES | TREND_GATES
    assert GATE_LABELS[GATE] == "Timely claim supported"
    assert JUDGE_VERSION == "2026-10-15.2"


def test_prompts_name_the_gate_and_the_new_counts():
    ad, vis = eval_prompts.AD_COPY_EVAL_USER, eval_prompts.VISUAL_CONCEPT_EVAL_USER
    assert len(AD_COPY_GATES) == 9 and len(VISUAL_GATES) == 10
    assert "Return all 9 gates" in ad and "Return all 10 gates" in vis
    for text in (ad, vis):
        assert f"**{GATE}**" in text
        assert "Timely: {timely}" in text and "{moment_section}" in text
        assert "no result before an upcoming event" in text
        assert 'pass with the note "not timely"' in text


def _raw(gates, passed: bool, note="looks fine"):
    return [GateResult(gate=g, passed=passed, note=note) for g in gates]


@pytest.mark.parametrize("expected", [AD_COPY_GATES, VISUAL_GATES])
def test_not_timely_passes_with_note_whatever_the_judge_says(expected):
    gates = normalize_gates(_raw(expected, False), expected, brief_used=True)
    gate = next(g for g in gates if g.gate == GATE)
    assert gate.passed is True and gate.note == NO_TIMELY_NOTE


@pytest.mark.parametrize("expected", [AD_COPY_GATES, VISUAL_GATES])
def test_timely_keeps_the_judges_verdict(expected):
    gates = normalize_gates(
        _raw(expected, False, "predicts the score"),
        expected,
        brief_used=True,
        timely=True,
    )
    gate = next(g for g in gates if g.gate == GATE)
    assert gate.passed is False and gate.note == "predicts the score"


def test_timely_missing_the_gate_fails_not_checked_with_a_warning():
    raw = [GateResult(gate=g, passed=True) for g in AD_COPY_GATES if g != GATE]
    gates = normalize_gates(raw, AD_COPY_GATES, brief_used=True, timely=True)
    gate = next(g for g in gates if g.gate == GATE)
    assert gate.passed is False and gate.note == NOT_CHECKED_NOTE
    ev = AdCopyEvaluation(
        original_id=3,
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
        "1 timely check not reported by the judge (failed as not checked)"
    ]
    # Not timely: still the forced pass, and no warning.
    gates = normalize_gates(raw, AD_COPY_GATES, brief_used=True)
    gate = next(g for g in gates if g.gate == GATE)
    assert gate.passed is True and gate.note == NO_TIMELY_NOTE


def test_creative_is_timely_needs_a_flag_and_a_validated_moment():
    flagged = {"uses_trend_state": True}
    assert creative_is_timely(flagged, SITUATION)
    assert creative_is_timely({}, SITUATION, paired_copy_timely=True)
    assert not creative_is_timely({}, SITUATION)
    assert not creative_is_timely({"uses_trend_state": "true"}, SITUATION)
    assert not creative_is_timely(flagged, None)
    assert not creative_is_timely(flagged, {**SITUATION, "timely_ok": False})
    assert not creative_is_timely(flagged, {**SITUATION, "timely_ok": "yes"})
    # Like the engine's timely_allowed: no valid_until does not switch it off.
    assert creative_is_timely(flagged, {**SITUATION, "valid_until": ""})


def test_moment_block_lists_the_moment_and_source_urls():
    block = moment_block(SITUATION, SOURCES)
    assert block.startswith("Current moment")
    assert "Phase: upcoming" in block
    assert "Event: Texas vs Oklahoma" in block
    assert "Valid until: 2026-10-14T23:59:59-04:00" in block
    assert "Tone rule: Anticipation; never predict the score." in block
    # src-9 has no record: left out.
    assert "Sources: https://news.example/2026/10/12/game" in block
    assert "{" not in block and "}" not in block
    assert moment_block(SITUATION, None).endswith("Sources: (none listed)")
    assert moment_block({**SITUATION, "timely_ok": False}, SOURCES) == ""


def _ad_body(gate_passed: bool) -> dict:
    return {
        "original_id": 3,
        "headline": "Game day speed",
        "tone_style": "Playful",
        "score": _score(AD_COPY_GATES, gate_passed),
    }


def _vis_body(gate_passed: bool) -> dict:
    return {
        "ad_copy_id": 3,
        "concept_name": "Rivalry",
        "score": _score(VISUAL_GATES, gate_passed),
    }


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


def _prompt(client, call=-1) -> str:
    contents = client.models.generate_content.call_args_list[call].kwargs["contents"]
    if isinstance(contents, str):
        return contents
    return "\n".join(getattr(p, "text", None) or "" for p in contents)


def test_timely_ad_copy_sees_the_moment_and_keeps_the_verdict():
    client = _client(_ad_body(False))
    result = evaluate_ad_copy(
        {**AD_COPY, "uses_trend_state": True},
        CAMPAIGN,
        EvalConfig(),
        client=client,
        trend_situation=SITUATION,
        sources=SOURCES,
    )
    prompt = _prompt(client)
    assert "Timely: yes" in prompt
    assert "<CURRENT_MOMENT>" in prompt and "Phase: upcoming" in prompt
    assert result.timely is True
    gate = next(g for g in result.score.gates if g.gate == GATE)
    assert gate.passed is False and not result.score.passed


def test_evergreen_ad_copy_gets_no_moment_and_a_code_pass():
    client = _client(_ad_body(False))
    result = evaluate_ad_copy(
        AD_COPY,
        CAMPAIGN,
        EvalConfig(),
        client=client,
        trend_situation=SITUATION,
        sources=SOURCES,
    )
    prompt = _prompt(client)
    assert "Timely: no" in prompt
    assert "<CURRENT_MOMENT>" not in prompt and "Phase: upcoming" not in prompt
    assert result.timely is False
    gate = next(g for g in result.score.gates if g.gate == GATE)
    assert gate.passed is True and gate.note == NO_TIMELY_NOTE


def test_flagged_copy_without_a_timely_moment_is_not_timely():
    client = _client(_ad_body(False))
    result = evaluate_ad_copy(
        {**AD_COPY, "uses_trend_state": True},
        CAMPAIGN,
        EvalConfig(),
        client=client,
        trend_situation={**SITUATION, "timely_ok": False},
    )
    assert "Timely: no" in _prompt(client)
    gate = next(g for g in result.score.gates if g.gate == GATE)
    assert gate.passed and gate.note == NO_TIMELY_NOTE


@pytest.mark.parametrize(
    ("concept", "paired"),
    [({**CONCEPT, "uses_trend_state": True}, False), (CONCEPT, True)],
    ids=["concept-flag", "paired-copy-flag"],
)
def test_timely_visual_sees_the_moment(concept, paired):
    client = _client(_vis_body(False))
    result = evaluate_visual_concept(
        concept,
        CAMPAIGN,
        EvalConfig(),
        client=client,
        trend_situation=SITUATION,
        sources=SOURCES,
        paired_copy_timely=paired,
    )
    prompt = _prompt(client)
    assert "Timely: yes" in prompt and "<CURRENT_MOMENT>" in prompt
    assert result.timely is True
    gate = next(g for g in result.score.gates if g.gate == GATE)
    assert gate.passed is False


def test_concurrent_eval_pairs_visuals_with_flagged_copies():
    """The pairing comes from paired_ad_copies (all final copies), so a visual
    stays timely even when its copy was reused from the early evaluation."""

    def generate(**kwargs):
        system = kwargs["config"].system_instruction
        body = _ad_body(True) if system == eval_prompts.AD_COPY_EVAL_SYSTEM else None
        if body is None:
            text = kwargs["contents"]
            name = "Rivalry" if "Concept Name: Rivalry" in text else "Other"
            body = {**_vis_body(True), "concept_name": name}
        return MagicMock(text=json.dumps(body))

    client = MagicMock()
    client.models.generate_content.side_effect = generate
    timely_copy = {**AD_COPY, "uses_trend_state": True}
    other = {**CONCEPT, "ad_copy_id": 4, "concept_name": "Other"}
    _, visuals = evaluate_all_concurrently(
        [],
        [CONCEPT, other],
        CAMPAIGN,
        EvalConfig(),
        client,
        trend_situation=SITUATION,
        sources=SOURCES,
        paired_ad_copies=[timely_copy, {**AD_COPY, "original_id": 4}],
    )
    by_name = {v.concept_name: v for v in visuals}
    assert by_name["Rivalry"].timely is True
    assert by_name["Other"].timely is False
    gate = next(g for g in by_name["Other"].score.gates if g.gate == GATE)
    assert gate.note == NO_TIMELY_NOTE


def _state(**extra) -> dict:
    return {
        "brand": "ACME",
        "target_product": "Rocket Skates",
        "target_audience": "Texas fans",
        "key_selling_points": "fast",
        "target_search_trends": "texas vs oklahoma",
        "ad_copy_critique": {"ad_copies": [{**AD_COPY, "uses_trend_state": True}]},
        **extra,
    }


def test_fingerprint_changes_when_the_moment_changes():
    base = ev_agent.ad_copy_eval_fingerprint(_state(trend_situation=SITUATION))
    assert base == ev_agent.ad_copy_eval_fingerprint(_state(trend_situation=SITUATION))
    later = {**SITUATION, "phase": "just_happened", "outcome": "loss"}
    assert base != ev_agent.ad_copy_eval_fingerprint(_state(trend_situation=later))
    assert base != ev_agent.ad_copy_eval_fingerprint(_state())


def test_agent_passes_the_moment_and_records_trend_valid_until(monkeypatch):
    seen: dict = {}

    def fake(*args, **kwargs):
        seen.update(kwargs)
        return [], []

    monkeypatch.setattr(ev_agent, "evaluate_all_concurrently", fake)
    ctx = FakeToolContext(
        _state(
            trend_situation=SITUATION,
            sources=SOURCES,
            final_visual_concepts={"visual_concepts": [CONCEPT]},
        )
    )
    ev_agent.evaluate_all_creatives(ctx)
    assert seen["trend_situation"] == SITUATION and seen["sources"] == SOURCES
    assert seen["paired_ad_copies"] == [{**AD_COPY, "uses_trend_state": True}]
    report = ctx.state["creative_evaluation_report"]
    assert report["trend_valid_until"] == SITUATION["valid_until"]

    ctx = FakeToolContext(_state(trend_situation={**SITUATION, "timely_ok": False}))
    ev_agent.evaluate_all_creatives(ctx)
    assert ctx.state["creative_evaluation_report"]["trend_valid_until"] == ""


def test_early_eval_passes_the_moment(monkeypatch):
    seen: dict = {}

    def fake(*args, **kwargs):
        seen.update(kwargs)
        return [], []

    monkeypatch.setattr(ev_agent, "evaluate_all_concurrently", fake)
    ctx = FakeToolContext(_state(trend_situation=SITUATION, sources=SOURCES))
    ev_agent.evaluate_ad_copies_only(ctx)
    assert seen["trend_situation"] == SITUATION and seen["sources"] == SOURCES


def test_standalone_report_records_trend_valid_until(monkeypatch):
    import creative_eval.evaluate as ev

    monkeypatch.setattr(ev, "_get_client", lambda cfg: MagicMock())
    monkeypatch.setattr(ev, "evaluate_all_concurrently", lambda *a, **k: ([], []))
    ctx = {"brand": "B", "target_product": "P", "target_search_trend": "t"}
    report = ev.evaluate_creatives(ctx, [], [], EvalConfig(), trend_situation=SITUATION)
    assert report.trend_valid_until == SITUATION["valid_until"]
    assert ev.evaluate_creatives(ctx, [], [], EvalConfig()).trend_valid_until == ""


def test_old_reports_parse_with_defaults():
    score = {
        "overall_score": 0.8,
        "passed": True,
        "verdicts": [],
        "strengths": [],
        "improvements": [],
    }
    report = CreativeEvaluationReport.model_validate(
        {
            "brand": "B",
            "target_product": "P",
            "target_search_trend": "t",
            "ad_copy_evaluations": [
                {"original_id": 1, "headline": "h", "tone_style": "t", "score": score}
            ],
            "visual_concept_evaluations": [
                {"ad_copy_id": 1, "concept_name": "c", "score": score}
            ],
            "summary": {
                "total_ad_copies": 1,
                "ad_copies_passed": 1,
                "avg_ad_copy_score": 0.8,
                "total_visual_concepts": 1,
                "visual_concepts_passed": 1,
                "avg_visual_score": 0.8,
                "overall_pass_rate": 1.0,
                "weakest_dimensions": [],
            },
        }
    )
    assert report.trend_valid_until == ""
    assert report.ad_copy_evaluations[0].timely is False
    assert isinstance(report.visual_concept_evaluations[0], VisualConceptEvaluation)
    assert report.visual_concept_evaluations[0].timely is False
