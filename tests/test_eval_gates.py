"""Tests for creative_eval's binary compliance gates (research F11).

Gates are the trustworthy part of the judge's verdict (binary compliance
against concrete rules); the 1-10 dimension scores stay advisory quality
signals. ``passed`` = ``overall_score >= threshold`` AND ``gates_passed``.
"""

import json
from unittest.mock import MagicMock

import pytest

from creative_eval import prompts as eval_prompts
from creative_eval.brief import (
    NO_BRIEF_BLOCK,
    angle_line,
    format_brief_for_judge,
    parse_brief,
)
from creative_eval.config import EvalConfig
from creative_eval.dimensions import (
    AD_COPY_GATES,
    ADVISORY_GATES,
    BRIEF_GATES,
    VISUAL_GATES,
)
from creative_eval.schemas import (
    AdCopyEvaluation,
    CreativeEvaluationReport,
    CreativeScore,
    EvalVerdict,
    GateResult,
    VisualConceptEvaluation,
)
from tests._fakes import FakeToolContext

OLD_REPORT = {
    "brand": "B",
    "target_product": "P",
    "target_search_trend": "t",
    "ad_copy_evaluations": [
        {
            "original_id": 1,
            "headline": "H",
            "tone_style": "T",
            "score": {
                "overall_score": 0.8,
                "passed": True,
                "verdicts": [],
                "strengths": [],
                "improvements": [],
            },
        }
    ],
    "visual_concept_evaluations": [],
    "summary": {
        "total_ad_copies": 1,
        "ad_copies_passed": 1,
        "avg_ad_copy_score": 0.8,
        "total_visual_concepts": 0,
        "visual_concepts_passed": 0,
        "avg_visual_score": 0.0,
        "overall_pass_rate": 1.0,
        "weakest_dimensions": [],
    },
}


def _verdicts(score: int, n: int = 6) -> list[EvalVerdict]:
    return [
        EvalVerdict(
            dimension=f"d{i}",
            score=score,
            verdict="pass" if score >= 7 else "fail",
            rationale="r",
        )
        for i in range(n)
    ]


# --- Task 7.1: schema --------------------------------------------------------


class TestSchema:
    def test_old_report_still_parses_with_defaults(self):
        report = CreativeEvaluationReport.model_validate(OLD_REPORT)
        score = report.ad_copy_evaluations[0].score
        assert score.gates == []
        assert score.gates_passed is True
        assert report.passing_threshold == 0.7
        assert report.brief_used is False
        assert report.summary.gates_pass_rate is None

    def test_gate_result_fields(self):
        gate = GateResult(gate="product_named", passed=False, note="no product")
        assert gate.model_dump() == {
            "gate": "product_named",
            "passed": False,
            "note": "no product",
            "advisory": False,
        }

    def test_visual_eval_image_judged_defaults_false(self):
        ev = VisualConceptEvaluation(
            ad_copy_id=1,
            concept_name="c",
            score=CreativeScore(
                overall_score=0.5,
                passed=False,
                verdicts=[],
                strengths=[],
                improvements=[],
            ),
        )
        assert ev.image_judged is False


class TestPassedRule:
    def test_high_score_with_failed_gate_does_not_pass(self):
        from creative_eval.evaluate import _score_from_verdicts

        gates = [
            GateResult(gate="product_named", passed=True),
            GateResult(gate="mandatories_met", passed=False, note="no CTA"),
        ]
        result = _score_from_verdicts(_verdicts(9), threshold=0.7, gates=gates)
        assert result.overall_score == 0.9
        assert result.gates_passed is False
        assert result.passed is False
        assert result.gates == gates

    def test_failed_advisory_gate_does_not_block(self):
        from creative_eval.evaluate import _score_from_verdicts

        gates = [
            GateResult(gate="product_visible", passed=True),
            GateResult(gate="brand_cue_present", passed=False, advisory=True),
        ]
        result = _score_from_verdicts(_verdicts(8), threshold=0.7, gates=gates)
        assert result.gates_passed is True
        assert result.passed is True

    def test_gates_pass_but_low_score_fails(self):
        from creative_eval.evaluate import _score_from_verdicts

        gates = [GateResult(gate="product_named", passed=True)]
        result = _score_from_verdicts(_verdicts(5), threshold=0.7, gates=gates)
        assert result.gates_passed is True
        assert result.passed is False

    def test_no_gates_keeps_score_only_rule(self):
        from creative_eval.evaluate import _score_from_verdicts

        result = _score_from_verdicts(_verdicts(8), threshold=0.7)
        assert result.gates == [] and result.gates_passed is True
        assert result.passed is True


def _ad_eval(passed: bool, gates_passed: bool) -> AdCopyEvaluation:
    return AdCopyEvaluation(
        original_id=1,
        headline="h",
        tone_style="t",
        score=CreativeScore(
            overall_score=0.8,
            passed=passed,
            verdicts=[],
            strengths=[],
            improvements=[],
            gates=[GateResult(gate="product_named", passed=gates_passed)],
            gates_passed=gates_passed,
        ),
    )


class TestSummaryGatesPassRate:
    def test_gates_pass_rate_counts_creatives_with_all_gates_passed(self):
        from creative_eval.evaluate import _build_summary

        summary = _build_summary(
            [_ad_eval(True, True), _ad_eval(False, False), _ad_eval(True, True)],
            [],
        )
        assert summary.gates_pass_rate == round(2 / 3, 3)

    def test_gates_pass_rate_none_without_creatives(self):
        from creative_eval.evaluate import _build_summary

        assert _build_summary([], []).gates_pass_rate is None


# --- Task 7.2: brief block, gates, prompts -------------------------------------

BRIEF = {
    "single_minded_proposition": "Rocket Skates make every chase {winnable}.",
    "reasons_to_believe": [
        {"claim": "Top speed 90 mph", "source_id": "src-1"},
        {"claim": "Lab-tested brakes", "source_id": "brief"},
    ],
    "brand": {
        "tone_of_voice": "wry, deadpan",
        "distinctive_assets": ["the red ACME crate", "ACME stencil logo"],
        "do_not": ["never mock the coyote"],
    },
    "trend_bridge": {
        "fit_score": 4,
        "fit_mode": "direct",
        "bridge": "Skate speed meets the roadrunner sprint meme.",
        "motifs": ["dust cloud"],
        "risks": [],
    },
    "mandatories": ["name Rocket Skates", "free shipping"],
    "avoid": ["anvils"],
    "angles": [
        {"angle_id": "A1", "name": "Finally fast", "tension": "t", "route": "Win."},
    ],
}
CAMPAIGN = {
    "brand": "Acme",
    "target_product": "Rocket Skates",
    "target_audience": "Coyotes",
    "key_selling_points": "fast",
    "target_search_trend": "roadrunner",
}
AD_COPY = {
    "original_id": 3,
    "angle_id": "A1",
    "headline": "Beep beep, who?",
    "body_text": "Rocket Skates.",
    "tone_style": "Deadpan",
    "trend_connection": "c",
    "audience_appeal_rationale": "a",
    "social_caption": "Zoom.",
    "call_to_action": "Order now",
    "detailed_performance_rationale": "p",
}
CONCEPT = {
    "ad_copy_id": 3,
    "concept_name": "Dust",
    "visual_style": "Watercolor",
    "aspect_ratio": "9:16",
    "trend": "roadrunner",
    "trend_reference": "r",
    "markets_product": "m",
    "audience_appeal": "a",
    "selection_rationale": "s",
    "headline": "Beep beep, who?",
    "social_caption": "Zoom.",
    "call_to_action": "Order now",
    "concept_summary": "sum",
    "image_generation_prompt": "Watercolor: Rocket Skates in a dust cloud.",
    "trend_motif": "dust cloud",
    "brand_cue": "the red ACME crate",
    "angle_id": "A1",
}


class TestBriefParsing:
    def test_parse_dict_json_and_garbage(self):
        assert parse_brief(BRIEF) is BRIEF
        assert parse_brief(json.dumps(BRIEF)) == BRIEF
        for bad in (None, "", "not json", "[]", {}, 3):
            assert parse_brief(bad) is None

    def test_block_carries_every_gate_input(self):
        block = format_brief_for_judge(BRIEF)
        for text in (
            "Rocket Skates make every chase {winnable}.",  # braces kept verbatim
            "- Top speed 90 mph",
            "- Lab-tested brakes",
            "- name Rocket Skates",
            "- free shipping",
            "- anvils",
            "- never mock the coyote",
            "fit 4/5 (direct)",
            "Skate speed meets the roadrunner sprint meme.",
            "wry, deadpan",
            "the red ACME crate; ACME stencil logo",
        ):
            assert text in block

    def test_block_without_brief_and_malformed_fields(self):
        assert format_brief_for_judge(None) == NO_BRIEF_BLOCK
        block = format_brief_for_judge({"mandatories": "x", "brand": "y"})
        assert "Single-minded proposition: (none)" in block
        assert "fit unknown (unknown)" in block

    def test_angle_line(self):
        assert angle_line(BRIEF, "A1") == "A1 — Finally fast: Win."
        assert angle_line(BRIEF, "A9") == "A9"
        assert angle_line(None, "") == "(none)"

    def test_brief_braces_survive_prompt_format(self):
        text = eval_prompts.AD_COPY_EVAL_USER.format(
            **CAMPAIGN,
            **AD_COPY,
            angle="A1",
            brief_block=format_brief_for_judge(BRIEF),
        )
        assert "chase {winnable}." in text


class TestPromptsListGates:
    def test_ad_copy_prompt_names_every_gate(self):
        for gate in AD_COPY_GATES:
            assert f"**{gate}**" in eval_prompts.AD_COPY_EVAL_USER

    def test_visual_prompt_names_every_gate(self):
        for gate in VISUAL_GATES:
            assert f"**{gate}**" in eval_prompts.VISUAL_CONCEPT_EVAL_USER

    def test_gate_sets(self):
        assert ADVISORY_GATES == {"brand_cue_present"}
        assert BRIEF_GATES <= set(AD_COPY_GATES) | set(VISUAL_GATES)
        assert "product_named" not in BRIEF_GATES


class TestNormalizeGates:
    def test_orders_drops_unknown_and_fails_missing(self):
        from creative_eval.evaluate import normalize_gates

        raw = [
            GateResult(gate="avoid_respected", passed=True, note="  clean  "),
            GateResult(gate="made_up", passed=False),
            GateResult(gate=" Product_Named ", passed=True, note="named"),
            GateResult(gate="product_named", passed=False, note="dup ignored"),
        ]
        gates = normalize_gates(raw, AD_COPY_GATES, brief_used=True)
        assert [g.gate for g in gates] == list(AD_COPY_GATES)
        by = {g.gate: g for g in gates}
        assert by["product_named"].passed and by["product_named"].note == "named"
        assert by["avoid_respected"].note == "clean"
        assert not by["mandatories_met"].passed
        assert by["mandatories_met"].note == "not reported by the judge"

    def test_no_brief_passes_brief_gates_with_note(self):
        from creative_eval.evaluate import normalize_gates

        raw = [GateResult(gate=g, passed=False) for g in AD_COPY_GATES]
        by = {g.gate: g for g in normalize_gates(raw, AD_COPY_GATES, brief_used=False)}
        for name in BRIEF_GATES & set(AD_COPY_GATES):
            assert by[name].passed and by[name].note == "no brief"
        assert not by["product_named"].passed  # not brief-dependent

    def test_advisory_is_set_by_code(self):
        from creative_eval.evaluate import normalize_gates

        raw = [GateResult(gate=g, passed=False, advisory=True) for g in VISUAL_GATES]
        gates = normalize_gates(raw, VISUAL_GATES, brief_used=True)
        assert {g.gate for g in gates if g.advisory} == {"brand_cue_present"}


def _judge_json(kind: str, gates: dict[str, bool], score: int = 9) -> str:
    """A judge response; it (wrongly) claims passed/gates_passed True."""
    body = {
        "score": {
            "overall_score": 0.1,
            "passed": True,
            "gates_passed": True,
            "verdicts": [
                {"dimension": "d", "score": score, "verdict": "pass", "rationale": "r"}
            ],
            "strengths": ["s"],
            "improvements": [],
            "gates": [
                {"gate": g, "passed": p, "note": f"{g} note"} for g, p in gates.items()
            ],
        }
    }
    if kind == "ad":
        body |= {"original_id": 3, "headline": "Beep beep, who?", "tone_style": "D"}
    else:
        body |= {"ad_copy_id": 3, "concept_name": "Dust"}
    return json.dumps(body)


def _client(text: str) -> MagicMock:
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text=text)
    return client


def _prompt_text(client: MagicMock) -> str:
    contents = client.models.generate_content.call_args.kwargs["contents"]
    if isinstance(contents, str):
        return contents
    return "\n".join(getattr(p, "text", None) or "" for p in contents)


class TestEvaluateAdCopyGates:
    def test_prompt_has_brief_and_angle_and_gates_drive_passed(self):
        from creative_eval.evaluate import evaluate_ad_copy

        gates = dict.fromkeys(AD_COPY_GATES, True) | {"mandatories_met": False}
        client = _client(_judge_json("ad", gates))
        result = evaluate_ad_copy(
            AD_COPY, CAMPAIGN, EvalConfig(), client=client, brief=BRIEF
        )
        text = _prompt_text(client)
        assert "<CREATIVE_BRIEF>" in text and "- free shipping" in text
        assert "Creative Angle: A1 — Finally fast: Win." in text
        assert result.score.overall_score == 0.9
        assert result.score.gates_passed is False
        assert result.score.passed is False
        assert [g.gate for g in result.score.gates] == list(AD_COPY_GATES)

    def test_all_gates_pass(self):
        from creative_eval.evaluate import evaluate_ad_copy

        client = _client(_judge_json("ad", dict.fromkeys(AD_COPY_GATES, True)))
        result = evaluate_ad_copy(
            AD_COPY, CAMPAIGN, EvalConfig(), client=client, brief=BRIEF
        )
        assert result.score.gates_passed and result.score.passed

    def test_no_brief_says_so_and_skips_brief_gates(self):
        from creative_eval.evaluate import evaluate_ad_copy

        gates = dict.fromkeys(AD_COPY_GATES, False) | {"product_named": True}
        client = _client(_judge_json("ad", gates))
        result = evaluate_ad_copy(AD_COPY, CAMPAIGN, EvalConfig(), client=client)
        assert NO_BRIEF_BLOCK in _prompt_text(client)
        assert result.score.gates_passed and result.score.passed

    def test_judge_failure_is_unverified(self):
        from creative_eval.evaluate import evaluate_ad_copy

        client = MagicMock()
        client.models.generate_content.side_effect = RuntimeError("offline")
        result = evaluate_ad_copy(AD_COPY, CAMPAIGN, EvalConfig(), client=client)
        assert result.score.gates == [] and result.score.gates_passed is False
        assert result.score.improvements == ["evaluation_failed"]


class TestEvaluateVisualGates:
    def test_advisory_brand_cue_does_not_block(self):
        from creative_eval.evaluate import evaluate_visual_concept

        gates = dict.fromkeys(VISUAL_GATES, True) | {"brand_cue_present": False}
        client = _client(_judge_json("visual", gates))
        result = evaluate_visual_concept(
            CONCEPT, CAMPAIGN, EvalConfig(), client=client, brief=BRIEF
        )
        text = _prompt_text(client)
        assert "Trend Motif: dust cloud" in text
        assert "Brand Cue: the red ACME crate" in text
        assert result.score.gates_passed and result.score.passed
        cue = next(g for g in result.score.gates if g.gate == "brand_cue_present")
        assert cue.advisory and not cue.passed

    def test_failed_product_visible_blocks(self):
        from creative_eval.evaluate import evaluate_visual_concept

        gates = dict.fromkeys(VISUAL_GATES, True) | {"product_visible": False}
        client = _client(_judge_json("visual", gates))
        result = evaluate_visual_concept(
            CONCEPT, CAMPAIGN, EvalConfig(), client=client, brief=BRIEF
        )
        assert not result.score.gates_passed and not result.score.passed

    def test_concept_without_motif_or_cue_formats(self):
        from creative_eval.evaluate import evaluate_visual_concept

        concept = {
            k: v for k, v in CONCEPT.items() if k not in {"trend_motif", "brand_cue"}
        }
        client = _client(_judge_json("visual", dict.fromkeys(VISUAL_GATES, True)))
        evaluate_visual_concept(concept, CAMPAIGN, EvalConfig(), client=client)
        assert "Brand Cue: (none)" in _prompt_text(client)


class TestAgentToolBrief:
    def _run(self, monkeypatch, extra_state):
        import creative_eval.agent as ev_agent

        seen = {}

        def fake_concurrent(ads, vis, ctx, config, client=None, *, brief=None, **kw):
            seen["brief"] = brief
            return [_ad_eval(False, False)], []

        monkeypatch.setattr(ev_agent, "evaluate_all_concurrently", fake_concurrent)
        state = {
            "brand": "Acme",
            "target_product": "Rocket Skates",
            "target_search_trends": "roadrunner",
            "ad_copy_critique": {"ad_copies": [AD_COPY]},
            "final_visual_concepts": {"visual_concepts": []},
            **extra_state,
        }
        ctx = FakeToolContext(state)
        result = ev_agent.evaluate_all_creatives(ctx)
        return seen, result, ctx.state["creative_evaluation_report"]

    def test_brief_from_state_json_string(self, monkeypatch):
        seen, result, report = self._run(
            monkeypatch, {"creative_brief": json.dumps(BRIEF)}
        )
        assert seen["brief"] == BRIEF
        assert report["brief_used"] is True
        assert report["passing_threshold"] == 0.7
        assert result["failed_creatives"][0]["failed_gates"] == ["product_named"]

    def test_no_brief(self, monkeypatch):
        seen, _, report = self._run(monkeypatch, {})
        assert seen["brief"] is None and report["brief_used"] is False


def test_evaluate_creatives_records_brief_and_threshold(monkeypatch):
    import creative_eval.evaluate as ev

    monkeypatch.setattr(ev, "_get_client", lambda cfg: MagicMock())
    monkeypatch.setattr(ev, "evaluate_all_concurrently", lambda *a, **k: ([], []))
    report = ev.evaluate_creatives(
        CAMPAIGN, [], [], EvalConfig(passing_threshold=0.8), brief=BRIEF
    )
    assert report.brief_used is True and report.passing_threshold == 0.8


@pytest.mark.parametrize("advisory", [False, True])
def test_finalize_summary_names_failed_checks(advisory):
    from creative_agent.finalize import finalize_summary

    gate = {"gate": "product_named", "passed": False, "advisory": advisory}
    report = {
        "summary": {"total_ad_copies": 1, "overall_pass_rate": 0.0},
        "ad_copy_evaluations": [
            {
                "headline": "Zoom",
                "score": {"overall_score": 0.9, "passed": False, "gates": [gate]},
            }
        ],
    }
    out = finalize_summary({"creative_evaluation_report": report})
    assert "Did not pass: 'Zoom' (ad copy, 0.90" in out
    assert ("failed checks: Product named" in out) is not advisory
