"""Tests for creative_eval's binary compliance gates (research F11).

Gates are the trustworthy part of the judge's verdict (binary compliance
against concrete rules); the 1-10 dimension scores stay advisory quality
signals. ``passed`` = ``overall_score >= threshold`` AND ``gates_passed``.
"""

from creative_eval.schemas import (
    AdCopyEvaluation,
    CreativeEvaluationReport,
    CreativeScore,
    EvalVerdict,
    GateResult,
    VisualConceptEvaluation,
)

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
