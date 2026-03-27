"""Tests for the creative evaluation module (schemas, scoring logic, config)."""

import pytest
from pydantic import ValidationError


class TestEvalVerdict:
    def test_valid_pass_verdict(self):
        from creative_eval.schemas import EvalVerdict

        v = EvalVerdict(
            dimension="trend_authenticity",
            score=8,
            verdict="pass",
            rationale="Strong natural connection to the trend.",
        )
        assert v.score == 8
        assert v.verdict == "pass"

    def test_valid_fail_verdict(self):
        from creative_eval.schemas import EvalVerdict

        v = EvalVerdict(
            dimension="copy_quality",
            score=4,
            verdict="fail",
            rationale="Grammar issues and unclear messaging.",
        )
        assert v.verdict == "fail"

    def test_score_bounds(self):
        from creative_eval.schemas import EvalVerdict

        with pytest.raises(ValidationError):
            EvalVerdict(dimension="x", score=0, verdict="fail", rationale="too low")

        with pytest.raises(ValidationError):
            EvalVerdict(dimension="x", score=11, verdict="pass", rationale="too high")

    def test_invalid_verdict_value(self):
        from creative_eval.schemas import EvalVerdict

        with pytest.raises(ValidationError):
            EvalVerdict(dimension="x", score=5, verdict="maybe", rationale="invalid")


class TestCreativeScore:
    def test_valid_score(self):
        from creative_eval.schemas import CreativeScore, EvalVerdict

        verdicts = [
            EvalVerdict(dimension="d1", score=8, verdict="pass", rationale="good"),
            EvalVerdict(dimension="d2", score=5, verdict="fail", rationale="needs work"),
        ]
        score = CreativeScore(
            overall_score=0.65,
            passed=False,
            verdicts=verdicts,
            strengths=["d1"],
            improvements=["d2"],
        )
        assert score.overall_score == 0.65
        assert not score.passed
        assert len(score.verdicts) == 2

    def test_score_bounds(self):
        from creative_eval.schemas import CreativeScore

        with pytest.raises(ValidationError):
            CreativeScore(
                overall_score=1.5,
                passed=True,
                verdicts=[],
                strengths=[],
                improvements=[],
            )


class TestAdCopyEvaluation:
    def test_valid_evaluation(self):
        from creative_eval.schemas import AdCopyEvaluation, CreativeScore

        eval_result = AdCopyEvaluation(
            original_id=1,
            headline="Rock Your World",
            tone_style="Humorous",
            score=CreativeScore(
                overall_score=0.8,
                passed=True,
                verdicts=[],
                strengths=["copy_quality"],
                improvements=[],
            ),
        )
        assert eval_result.original_id == 1
        assert eval_result.score.passed


class TestVisualConceptEvaluation:
    def test_valid_evaluation(self):
        from creative_eval.schemas import VisualConceptEvaluation, CreativeScore

        eval_result = VisualConceptEvaluation(
            ad_copy_id=3,
            concept_name="Sunset Serenade",
            score=CreativeScore(
                overall_score=0.72,
                passed=True,
                verdicts=[],
                strengths=["stopping_power"],
                improvements=[],
            ),
        )
        assert eval_result.concept_name == "Sunset Serenade"


class TestEvaluationSummary:
    def test_valid_summary(self):
        from creative_eval.schemas import EvaluationSummary

        summary = EvaluationSummary(
            total_ad_copies=6,
            ad_copies_passed=5,
            avg_ad_copy_score=0.78,
            total_visual_concepts=6,
            visual_concepts_passed=4,
            avg_visual_score=0.71,
            overall_pass_rate=0.75,
            weakest_dimensions=["prompt_technical_quality", "trend_visual_connection"],
        )
        assert summary.overall_pass_rate == 0.75
        assert len(summary.weakest_dimensions) == 2


class TestCreativeEvaluationReport:
    def test_valid_report(self):
        from creative_eval.schemas import (
            CreativeEvaluationReport,
            AdCopyEvaluation,
            VisualConceptEvaluation,
            CreativeScore,
            EvaluationSummary,
        )

        report = CreativeEvaluationReport(
            brand="PRS Guitars",
            target_product="SE CE24",
            target_search_trend="tswift engaged",
            ad_copy_evaluations=[
                AdCopyEvaluation(
                    original_id=1,
                    headline="Test",
                    tone_style="Humorous",
                    score=CreativeScore(
                        overall_score=0.8,
                        passed=True,
                        verdicts=[],
                        strengths=[],
                        improvements=[],
                    ),
                )
            ],
            visual_concept_evaluations=[
                VisualConceptEvaluation(
                    ad_copy_id=1,
                    concept_name="Test Concept",
                    score=CreativeScore(
                        overall_score=0.65,
                        passed=False,
                        verdicts=[],
                        strengths=[],
                        improvements=["stopping_power"],
                    ),
                )
            ],
            summary=EvaluationSummary(
                total_ad_copies=1,
                ad_copies_passed=1,
                avg_ad_copy_score=0.8,
                total_visual_concepts=1,
                visual_concepts_passed=0,
                avg_visual_score=0.65,
                overall_pass_rate=0.5,
                weakest_dimensions=["stopping_power"],
            ),
        )
        assert report.brand == "PRS Guitars"
        assert report.summary.overall_pass_rate == 0.5

    def test_report_serialization(self):
        from creative_eval.schemas import (
            CreativeEvaluationReport,
            EvaluationSummary,
        )

        report = CreativeEvaluationReport(
            brand="Test",
            target_product="Product",
            target_search_trend="trend",
            ad_copy_evaluations=[],
            visual_concept_evaluations=[],
            summary=EvaluationSummary(
                total_ad_copies=0,
                ad_copies_passed=0,
                avg_ad_copy_score=0.0,
                total_visual_concepts=0,
                visual_concepts_passed=0,
                avg_visual_score=0.0,
                overall_pass_rate=0.0,
                weakest_dimensions=[],
            ),
        )
        # Should serialize to JSON and back without error
        json_str = report.model_dump_json()
        restored = CreativeEvaluationReport.model_validate_json(json_str)
        assert restored.brand == "Test"


class TestScoreFromVerdicts:
    def test_computes_average(self):
        from creative_eval.evaluate import _score_from_verdicts
        from creative_eval.schemas import EvalVerdict

        verdicts = [
            EvalVerdict(dimension="d1", score=8, verdict="pass", rationale="good"),
            EvalVerdict(dimension="d2", score=6, verdict="fail", rationale="ok"),
            EvalVerdict(dimension="d3", score=10, verdict="pass", rationale="great"),
        ]
        result = _score_from_verdicts(verdicts, threshold=0.7)
        # avg = (8+6+10) / 30 = 0.8
        assert result.overall_score == 0.8
        assert result.passed

    def test_empty_verdicts(self):
        from creative_eval.evaluate import _score_from_verdicts

        result = _score_from_verdicts([], threshold=0.7)
        assert result.overall_score == 0.0
        assert not result.passed

    def test_below_threshold(self):
        from creative_eval.evaluate import _score_from_verdicts
        from creative_eval.schemas import EvalVerdict

        verdicts = [
            EvalVerdict(dimension="d1", score=3, verdict="fail", rationale="bad"),
            EvalVerdict(dimension="d2", score=4, verdict="fail", rationale="weak"),
        ]
        result = _score_from_verdicts(verdicts, threshold=0.7)
        # avg = (3+4) / 20 = 0.35
        assert result.overall_score == 0.35
        assert not result.passed

    def test_identifies_strengths_and_improvements(self):
        from creative_eval.evaluate import _score_from_verdicts
        from creative_eval.schemas import EvalVerdict

        verdicts = [
            EvalVerdict(dimension="strong_dim", score=9, verdict="pass", rationale="excellent"),
            EvalVerdict(dimension="weak_dim", score=3, verdict="fail", rationale="poor"),
            EvalVerdict(dimension="ok_dim", score=7, verdict="pass", rationale="decent"),
        ]
        result = _score_from_verdicts(verdicts, threshold=0.7)
        assert "strong_dim" in result.strengths
        assert "weak_dim" in result.improvements


class TestEvalConfig:
    def test_default_config(self):
        from creative_eval.config import EvalConfig

        config = EvalConfig()
        assert config.eval_model == "gemini-2.5-pro"
        assert config.passing_threshold == 0.7
        assert config.max_retries == 3
        assert len(config.ad_copy_dimensions) == 6
        assert len(config.visual_dimensions) == 6

    def test_custom_threshold(self):
        from creative_eval.config import EvalConfig

        config = EvalConfig(passing_threshold=0.8)
        assert config.passing_threshold == 0.8

    def test_custom_model(self):
        from creative_eval.config import EvalConfig

        config = EvalConfig(eval_model="gemini-2.5-flash")
        assert config.eval_model == "gemini-2.5-flash"
