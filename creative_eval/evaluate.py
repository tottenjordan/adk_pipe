"""Standalone creative evaluation pipeline (no ADK dependency).

Usage:
    from creative_eval.evaluate import evaluate_creatives
    from creative_eval.config import EvalConfig

    report = evaluate_creatives(
        campaign_context={...},
        ad_copies=[...],          # list of FinalAdCopy dicts
        visual_concepts=[...],    # list of VisualConceptFinal dicts
        config=EvalConfig(),
    )
"""

import json
import logging
from google import genai

from .config import EvalConfig
from .schemas import (
    AdCopyEvaluation,
    VisualConceptEvaluation,
    CreativeEvaluationReport,
    CreativeScore,
    EvalVerdict,
    EvaluationSummary,
)
from . import prompts

logger = logging.getLogger(__name__)


def _get_client(config: EvalConfig) -> genai.Client:
    """Create a Gemini client."""
    return genai.Client(
        vertexai=True,
        project=config.project_id,
        location=config.location,
    )


def _score_from_verdicts(
    verdicts: list[EvalVerdict], threshold: float
) -> CreativeScore:
    """Compute an aggregate CreativeScore from individual verdicts."""
    if not verdicts:
        return CreativeScore(
            overall_score=0.0,
            passed=False,
            verdicts=[],
            strengths=[],
            improvements=[],
        )

    avg_score = sum(v.score for v in verdicts) / (len(verdicts) * 10)
    passed = avg_score >= threshold

    strengths = [
        v.dimension for v in sorted(verdicts, key=lambda v: v.score, reverse=True)
        if v.verdict == "pass"
    ][:3]

    improvements = [
        v.dimension for v in sorted(verdicts, key=lambda v: v.score)
        if v.verdict == "fail"
    ][:3]

    return CreativeScore(
        overall_score=round(avg_score, 3),
        passed=passed,
        verdicts=verdicts,
        strengths=strengths,
        improvements=improvements,
    )


def evaluate_ad_copy(
    ad_copy: dict,
    campaign_context: dict,
    config: EvalConfig,
    client: genai.Client | None = None,
) -> AdCopyEvaluation:
    """Evaluate a single ad copy using Gemini-as-judge.

    Args:
        ad_copy: Dict with FinalAdCopy fields.
        campaign_context: Dict with brand, target_product, target_audience,
                          key_selling_points, target_search_trend.
        config: Evaluation configuration.
        client: Optional pre-created Gemini client.

    Returns:
        AdCopyEvaluation with per-dimension scores.
    """
    if client is None:
        client = _get_client(config)

    # Build the prompt
    user_prompt = prompts.AD_COPY_EVAL_USER.format(
        **campaign_context,
        **ad_copy,
    )

    try:
        response = client.models.generate_content(
            model=config.eval_model,
            contents=user_prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=prompts.AD_COPY_EVAL_SYSTEM,
                response_mime_type="application/json",
                response_schema=AdCopyEvaluation,
                temperature=0.3,
            ),
        )

        result = AdCopyEvaluation.model_validate_json(response.text)

        # Recompute overall_score from verdicts for consistency
        recomputed = _score_from_verdicts(result.score.verdicts, config.passing_threshold)
        result.score.overall_score = recomputed.overall_score
        result.score.passed = recomputed.passed

        return result

    except Exception as e:
        logger.error(f"Failed to evaluate ad copy {ad_copy.get('original_id', '?')}: {e}")
        # Return a zero-score evaluation on failure
        return AdCopyEvaluation(
            original_id=ad_copy.get("original_id", 0),
            headline=ad_copy.get("headline", ""),
            tone_style=ad_copy.get("tone_style", ""),
            score=CreativeScore(
                overall_score=0.0,
                passed=False,
                verdicts=[],
                strengths=[],
                improvements=["evaluation_failed"],
            ),
        )


def evaluate_visual_concept(
    visual_concept: dict,
    campaign_context: dict,
    config: EvalConfig,
    client: genai.Client | None = None,
) -> VisualConceptEvaluation:
    """Evaluate a single visual concept using Gemini-as-judge.

    Args:
        visual_concept: Dict with VisualConceptFinal fields.
        campaign_context: Dict with brand, target_product, target_audience,
                          key_selling_points, target_search_trend.
        config: Evaluation configuration.
        client: Optional pre-created Gemini client.

    Returns:
        VisualConceptEvaluation with per-dimension scores.
    """
    if client is None:
        client = _get_client(config)

    user_prompt = prompts.VISUAL_CONCEPT_EVAL_USER.format(
        **campaign_context,
        **visual_concept,
    )

    try:
        response = client.models.generate_content(
            model=config.eval_model,
            contents=user_prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=prompts.VISUAL_CONCEPT_EVAL_SYSTEM,
                response_mime_type="application/json",
                response_schema=VisualConceptEvaluation,
                temperature=0.3,
            ),
        )

        result = VisualConceptEvaluation.model_validate_json(response.text)

        recomputed = _score_from_verdicts(result.score.verdicts, config.passing_threshold)
        result.score.overall_score = recomputed.overall_score
        result.score.passed = recomputed.passed

        return result

    except Exception as e:
        logger.error(f"Failed to evaluate visual concept {visual_concept.get('concept_name', '?')}: {e}")
        return VisualConceptEvaluation(
            ad_copy_id=visual_concept.get("ad_copy_id", 0),
            concept_name=visual_concept.get("concept_name", ""),
            score=CreativeScore(
                overall_score=0.0,
                passed=False,
                verdicts=[],
                strengths=[],
                improvements=["evaluation_failed"],
            ),
        )


def _build_summary(
    ad_evals: list[AdCopyEvaluation],
    visual_evals: list[VisualConceptEvaluation],
) -> EvaluationSummary:
    """Build aggregate statistics from individual evaluations."""
    ad_scores = [e.score.overall_score for e in ad_evals]
    vis_scores = [e.score.overall_score for e in visual_evals]

    total = len(ad_evals) + len(visual_evals)
    passed = sum(1 for e in ad_evals if e.score.passed) + sum(1 for e in visual_evals if e.score.passed)

    # Find weakest dimensions across all verdicts
    dim_scores: dict[str, list[int]] = {}
    for eval_item in [*ad_evals, *visual_evals]:
        for v in eval_item.score.verdicts:
            dim_scores.setdefault(v.dimension, []).append(v.score)

    dim_avgs = {dim: sum(s) / len(s) for dim, s in dim_scores.items()}
    weakest = sorted(dim_avgs, key=dim_avgs.get)[:3]

    return EvaluationSummary(
        total_ad_copies=len(ad_evals),
        ad_copies_passed=sum(1 for e in ad_evals if e.score.passed),
        avg_ad_copy_score=round(sum(ad_scores) / len(ad_scores), 3) if ad_scores else 0.0,
        total_visual_concepts=len(visual_evals),
        visual_concepts_passed=sum(1 for e in visual_evals if e.score.passed),
        avg_visual_score=round(sum(vis_scores) / len(vis_scores), 3) if vis_scores else 0.0,
        overall_pass_rate=round(passed / total, 3) if total > 0 else 0.0,
        weakest_dimensions=weakest,
    )


def evaluate_creatives(
    campaign_context: dict,
    ad_copies: list[dict],
    visual_concepts: list[dict],
    config: EvalConfig | None = None,
) -> CreativeEvaluationReport:
    """Evaluate all creatives from a single agent run.

    Args:
        campaign_context: Dict with keys: brand, target_product, target_audience,
                          key_selling_points, target_search_trend.
        ad_copies: List of FinalAdCopy dicts.
        visual_concepts: List of VisualConceptFinal dicts.
        config: Optional EvalConfig (uses defaults if None).

    Returns:
        CreativeEvaluationReport with per-creative and aggregate scores.
    """
    if config is None:
        config = EvalConfig()

    client = _get_client(config)

    logger.info(f"Evaluating {len(ad_copies)} ad copies and {len(visual_concepts)} visual concepts...")

    ad_evals = []
    for i, ac in enumerate(ad_copies):
        logger.info(f"  Evaluating ad copy {i + 1}/{len(ad_copies)}: {ac.get('headline', '?')}")
        ad_evals.append(evaluate_ad_copy(ac, campaign_context, config, client))

    visual_evals = []
    for i, vc in enumerate(visual_concepts):
        logger.info(f"  Evaluating visual concept {i + 1}/{len(visual_concepts)}: {vc.get('concept_name', '?')}")
        visual_evals.append(evaluate_visual_concept(vc, campaign_context, config, client))

    summary = _build_summary(ad_evals, visual_evals)

    report = CreativeEvaluationReport(
        brand=campaign_context["brand"],
        target_product=campaign_context["target_product"],
        target_search_trend=campaign_context["target_search_trend"],
        ad_copy_evaluations=ad_evals,
        visual_concept_evaluations=visual_evals,
        summary=summary,
    )

    logger.info(
        f"Evaluation complete: {summary.ad_copies_passed}/{summary.total_ad_copies} ad copies passed, "
        f"{summary.visual_concepts_passed}/{summary.total_visual_concepts} visual concepts passed. "
        f"Overall pass rate: {summary.overall_pass_rate:.1%}"
    )

    return report
