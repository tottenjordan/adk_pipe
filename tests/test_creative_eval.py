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


# =====================================================================
# Tests for evaluate_all_creatives (agent tool input/output contract)
# =====================================================================

# --- Sample fixtures ---

SAMPLE_CAMPAIGN_STATE = {
    "brand": "PRS Guitars",
    "target_product": "SE CE24",
    "target_audience": "Millennial jam band guitarists",
    "key_selling_points": "Versatile 85/15 S pickups",
    "target_search_trends": "tswift engaged",
}

SAMPLE_AD_COPIES = {
    "ad_copies": [
        {
            "original_id": 1,
            "headline": "She Said Yes, You Said Solo",
            "body_text": "While the world celebrates love, your PRS SE CE24 celebrates tone.",
            "tone_style": "Humorous",
            "trend_connection": "Riffs on engagement trend",
            "audience_appeal_rationale": "Speaks to gear-obsessed musicians",
            "social_caption": "Commitment issues? Not with this guitar.",
            "call_to_action": "Find your perfect match",
            "detailed_performance_rationale": "Humor + product focus = engagement",
        },
        {
            "original_id": 2,
            "headline": "Every Love Story Deserves a Soundtrack",
            "body_text": "The SE CE24 delivers the tone for every moment.",
            "tone_style": "Aspirational",
            "trend_connection": "Universal love theme from trend",
            "audience_appeal_rationale": "Emotional resonance with musicians",
            "social_caption": "Write the soundtrack to your story.",
            "call_to_action": "Start your story",
            "detailed_performance_rationale": "Aspirational tone drives brand affinity",
        },
    ]
}

SAMPLE_VISUAL_CONCEPTS = {
    "visual_concepts": [
        {
            "ad_copy_id": 1,
            "concept_name": "The Proposal Riff",
            "trend": "tswift engaged",
            "trend_reference": "Visual pun on guitar proposal",
            "markets_product": "Guitar as hero shot in ring box",
            "audience_appeal": "Surreal humor for meme-native audience",
            "selection_rationale": "High stopping power + product focus",
            "headline": "She Said Yes, You Said Solo",
            "social_caption": "The only proposal that matters.",
            "call_to_action": "Find your perfect match",
            "concept_summary": "Guitar in a ring box under dramatic lighting",
            "image_generation_prompt": "A PRS SE CE24 guitar resting in a velvet ring box...",
        },
    ]
}


class _FakeToolContext:
    """Minimal mock of ADK ToolContext for testing evaluate_all_creatives."""

    def __init__(self, state: dict):
        self.state = dict(state)


class TestEvaluateAllCreativesInputs:
    """Test that evaluate_all_creatives correctly reads inputs from session state."""

    def test_returns_error_when_no_creatives_in_state(self):
        from creative_eval.agent import evaluate_all_creatives

        ctx = _FakeToolContext({**SAMPLE_CAMPAIGN_STATE})
        result = evaluate_all_creatives(ctx)
        assert result["status"] == "error"
        assert "No ad copies or visual concepts" in result["message"]

    def test_returns_error_with_empty_lists(self):
        from creative_eval.agent import evaluate_all_creatives

        ctx = _FakeToolContext({
            **SAMPLE_CAMPAIGN_STATE,
            "ad_copy_critique": {"ad_copies": []},
            "final_visual_concepts": {"visual_concepts": []},
        })
        result = evaluate_all_creatives(ctx)
        assert result["status"] == "error"

    def test_parses_string_state_values(self):
        """State values may arrive as JSON strings — verify they're handled."""
        import json
        from creative_eval.agent import evaluate_all_creatives
        from unittest.mock import patch, MagicMock

        ctx = _FakeToolContext({
            **SAMPLE_CAMPAIGN_STATE,
            "ad_copy_critique": json.dumps(SAMPLE_AD_COPIES),
            "final_visual_concepts": json.dumps(SAMPLE_VISUAL_CONCEPTS),
        })

        # Mock the actual LLM calls to avoid hitting the API
        mock_ad_eval = MagicMock()
        mock_vis_eval = MagicMock()
        with patch("creative_eval.agent.evaluate_ad_copy", mock_ad_eval), \
             patch("creative_eval.agent.evaluate_visual_concept", mock_vis_eval), \
             patch("creative_eval.agent._build_summary") as mock_summary:

            # Setup return values
            from creative_eval.schemas import (
                AdCopyEvaluation, VisualConceptEvaluation, CreativeScore, EvaluationSummary,
            )
            mock_score = CreativeScore(
                overall_score=0.8, passed=True, verdicts=[], strengths=[], improvements=[],
            )
            mock_ad_eval.return_value = AdCopyEvaluation(
                original_id=1, headline="Test", tone_style="Humorous", score=mock_score,
            )
            mock_vis_eval.return_value = VisualConceptEvaluation(
                ad_copy_id=1, concept_name="Test", score=mock_score,
            )
            mock_summary.return_value = EvaluationSummary(
                total_ad_copies=2, ad_copies_passed=2, avg_ad_copy_score=0.8,
                total_visual_concepts=1, visual_concepts_passed=1, avg_visual_score=0.8,
                overall_pass_rate=1.0, weakest_dimensions=[],
            )

            result = evaluate_all_creatives(ctx)

        assert result["status"] == "success"
        # Should have been called once per ad copy (2) and once per visual concept (1)
        assert mock_ad_eval.call_count == 2
        assert mock_vis_eval.call_count == 1

    def test_extracts_campaign_context_keys(self):
        """Verify the campaign context dict passed to evaluators has the right keys."""
        import json
        from creative_eval.agent import evaluate_all_creatives
        from unittest.mock import patch, MagicMock

        ctx = _FakeToolContext({
            **SAMPLE_CAMPAIGN_STATE,
            "ad_copy_critique": SAMPLE_AD_COPIES,
            "final_visual_concepts": {"visual_concepts": []},
        })

        captured_contexts = []

        def capture_ad_eval(ad_copy, campaign_context, config):
            captured_contexts.append(campaign_context)
            from creative_eval.schemas import AdCopyEvaluation, CreativeScore
            return AdCopyEvaluation(
                original_id=1, headline="T", tone_style="H",
                score=CreativeScore(
                    overall_score=0.8, passed=True, verdicts=[], strengths=[], improvements=[],
                ),
            )

        with patch("creative_eval.agent.evaluate_ad_copy", side_effect=capture_ad_eval), \
             patch("creative_eval.agent._build_summary") as mock_summary:
            from creative_eval.schemas import EvaluationSummary
            mock_summary.return_value = EvaluationSummary(
                total_ad_copies=2, ad_copies_passed=2, avg_ad_copy_score=0.8,
                total_visual_concepts=0, visual_concepts_passed=0, avg_visual_score=0.0,
                overall_pass_rate=1.0, weakest_dimensions=[],
            )
            evaluate_all_creatives(ctx)

        assert len(captured_contexts) == 2
        cc = captured_contexts[0]
        assert cc["brand"] == "PRS Guitars"
        assert cc["target_product"] == "SE CE24"
        assert cc["target_audience"] == "Millennial jam band guitarists"
        assert cc["key_selling_points"] == "Versatile 85/15 S pickups"
        assert cc["target_search_trend"] == "tswift engaged"


class TestEvaluateAllCreativesOutputs:
    """Test that evaluate_all_creatives produces correct outputs."""

    def _run_with_mocks(self, ad_scores, vis_scores, threshold=0.7):
        """Helper: run evaluate_all_creatives with mocked evaluators returning given scores."""
        from creative_eval.agent import evaluate_all_creatives
        from creative_eval.schemas import (
            AdCopyEvaluation, VisualConceptEvaluation, CreativeScore,
            EvalVerdict, EvaluationSummary,
        )
        from unittest.mock import patch

        ad_copies = [
            {**SAMPLE_AD_COPIES["ad_copies"][0], "original_id": i}
            for i in range(len(ad_scores))
        ]
        vis_concepts = [
            {**SAMPLE_VISUAL_CONCEPTS["visual_concepts"][0], "ad_copy_id": i}
            for i in range(len(vis_scores))
        ]

        ctx = _FakeToolContext({
            **SAMPLE_CAMPAIGN_STATE,
            "ad_copy_critique": {"ad_copies": ad_copies},
            "final_visual_concepts": {"visual_concepts": vis_concepts},
        })

        ad_iter = iter(ad_scores)
        vis_iter = iter(vis_scores)

        def mock_ad_eval(ad_copy, campaign_context, config):
            score = next(ad_iter)
            return AdCopyEvaluation(
                original_id=ad_copy.get("original_id", 0),
                headline=ad_copy.get("headline", ""),
                tone_style=ad_copy.get("tone_style", ""),
                score=CreativeScore(
                    overall_score=score,
                    passed=score >= threshold,
                    verdicts=[
                        EvalVerdict(dimension="d1", score=int(score * 10),
                                    verdict="pass" if score >= threshold else "fail",
                                    rationale="test"),
                    ],
                    strengths=["d1"] if score >= threshold else [],
                    improvements=[] if score >= threshold else ["d1"],
                ),
            )

        def mock_vis_eval(vc, campaign_context, config):
            score = next(vis_iter)
            return VisualConceptEvaluation(
                ad_copy_id=vc.get("ad_copy_id", 0),
                concept_name=vc.get("concept_name", ""),
                score=CreativeScore(
                    overall_score=score,
                    passed=score >= threshold,
                    verdicts=[
                        EvalVerdict(dimension="d1", score=int(score * 10),
                                    verdict="pass" if score >= threshold else "fail",
                                    rationale="test"),
                    ],
                    strengths=["d1"] if score >= threshold else [],
                    improvements=[] if score >= threshold else ["d1"],
                ),
            )

        with patch("creative_eval.agent.evaluate_ad_copy", side_effect=mock_ad_eval), \
             patch("creative_eval.agent.evaluate_visual_concept", side_effect=mock_vis_eval):
            result = evaluate_all_creatives(ctx)

        return result, ctx

    def test_success_result_keys(self):
        result, _ = self._run_with_mocks(ad_scores=[0.8, 0.9], vis_scores=[0.75])
        assert result["status"] == "success"
        expected_keys = {
            "status", "total_ad_copies", "ad_copies_passed", "avg_ad_copy_score",
            "total_visual_concepts", "visual_concepts_passed", "avg_visual_score",
            "overall_pass_rate", "weakest_dimensions",
        }
        assert set(result.keys()) == expected_keys

    def test_counts_match_inputs(self):
        result, _ = self._run_with_mocks(ad_scores=[0.8, 0.9, 0.6], vis_scores=[0.75, 0.5])
        assert result["total_ad_copies"] == 3
        assert result["total_visual_concepts"] == 2

    def test_pass_counts_correct(self):
        result, _ = self._run_with_mocks(
            ad_scores=[0.8, 0.5, 0.9],   # 2 pass, 1 fail
            vis_scores=[0.75, 0.3],        # 1 pass, 1 fail
        )
        assert result["ad_copies_passed"] == 2
        assert result["visual_concepts_passed"] == 1

    def test_report_stored_in_session_state(self):
        _, ctx = self._run_with_mocks(ad_scores=[0.8], vis_scores=[0.75])
        report = ctx.state.get("creative_evaluation_report")
        assert report is not None
        assert isinstance(report, dict)

    def test_stored_report_has_correct_structure(self):
        _, ctx = self._run_with_mocks(ad_scores=[0.8, 0.9], vis_scores=[0.75])
        report = ctx.state["creative_evaluation_report"]

        # Top-level keys
        assert report["brand"] == "PRS Guitars"
        assert report["target_product"] == "SE CE24"
        assert report["target_search_trend"] == "tswift engaged"

        # Evaluation lists
        assert len(report["ad_copy_evaluations"]) == 2
        assert len(report["visual_concept_evaluations"]) == 1

        # Summary
        summary = report["summary"]
        assert summary["total_ad_copies"] == 2
        assert summary["total_visual_concepts"] == 1
        assert 0.0 <= summary["overall_pass_rate"] <= 1.0

    def test_stored_report_validates_as_schema(self):
        """The dict stored in state should round-trip through the Pydantic schema."""
        from creative_eval.schemas import CreativeEvaluationReport

        _, ctx = self._run_with_mocks(ad_scores=[0.8], vis_scores=[0.75])
        report_dict = ctx.state["creative_evaluation_report"]
        restored = CreativeEvaluationReport.model_validate(report_dict)
        assert restored.brand == "PRS Guitars"

    def test_all_fail_produces_zero_pass_rate(self):
        result, _ = self._run_with_mocks(ad_scores=[0.3, 0.4], vis_scores=[0.2])
        assert result["ad_copies_passed"] == 0
        assert result["visual_concepts_passed"] == 0
        assert result["overall_pass_rate"] == 0.0

    def test_all_pass_produces_full_pass_rate(self):
        result, _ = self._run_with_mocks(ad_scores=[0.8, 0.9], vis_scores=[0.85])
        assert result["ad_copies_passed"] == 2
        assert result["visual_concepts_passed"] == 1
        assert result["overall_pass_rate"] == 1.0

    def test_weakest_dimensions_populated(self):
        result, _ = self._run_with_mocks(ad_scores=[0.5], vis_scores=[0.4])
        assert isinstance(result["weakest_dimensions"], list)
