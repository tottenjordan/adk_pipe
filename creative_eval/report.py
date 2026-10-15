"""The evaluation report: summary, ``gates_pass_rate`` and judge warnings (pure).

:func:`build_report` is the one place a :class:`CreativeEvaluationReport` is
assembled; a failed judge call is the :func:`_failed_score` placeholder, read
back by :func:`_is_failed`.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from .config import EvalConfig
from .dimensions import JUDGE_VERSION
from .gates import moment_is_timely, unreported_gates_warning
from .judge import _gs_uri, rendered_image_uri
from .schemas import (
    AdCopyEvaluation,
    CreativeEvaluationReport,
    CreativeScore,
    EvaluationSummary,
    VisualConceptEvaluation,
)

# The pre-``judge_failed`` marker of a failed judge call, still written to
# ``improvements`` for older readers (creative_agent.finalize, experiments).
EVALUATION_FAILED = "evaluation_failed"


def _is_failed(evaluation: AdCopyEvaluation | VisualConceptEvaluation) -> bool:
    """True for the zero-score placeholder of a failed judge call.

    Reads the code-set ``score.judge_failed``; only a report written before
    that field existed (not in the parsed fields) falls back to the old
    ``"evaluation_failed"`` improvements marker, so a judge writing that
    phrase into its improvements is never mistaken for a failed call.
    """
    score = evaluation.score
    if "judge_failed" in score.model_fields_set:
        return score.judge_failed
    return EVALUATION_FAILED in score.improvements


def _failed_score() -> CreativeScore:
    """The zero score of a failed judge call — nothing was verified."""
    return CreativeScore(
        overall_score=0.0,
        passed=False,
        verdicts=[],
        strengths=[],
        improvements=[EVALUATION_FAILED],
        gates=[],
        gates_passed=False,
        judge_failed=True,
    )


def trend_valid_until(trend_situation: Any) -> str:
    """The report's ``trend_valid_until``: the timely moment's expiry, else ""."""
    if not moment_is_timely(trend_situation):
        return ""
    return str(trend_situation.get("valid_until") or "").strip()


def image_fallback_concepts(
    visual_evals: list[VisualConceptEvaluation],
    generated_images: Mapping[str, Any] | None,
) -> list[str]:
    """Concepts that had a rendered image but were judged from the prompt (pure).

    Excludes failed judge calls (they carry their own ``evaluation_failed``).
    """
    images = generated_images if isinstance(generated_images, Mapping) else {}
    return [
        e.concept_name
        for e in visual_evals
        if not e.image_judged
        and rendered_image_uri(images.get(e.concept_name))
        and not _is_failed(e)
    ]


def image_fallback_warning(concepts: list[str]) -> list[str]:
    """The report ``warnings`` entry for :func:`image_fallback_concepts` ([] if none)."""
    if not concepts:
        return []
    return [
        "Visual judge could not read the rendered image and judged the prompt "
        f"instead for: {', '.join(concepts)}"
    ]


def foreign_image_concepts(
    visual_evals: list[VisualConceptEvaluation],
    generated_images: Mapping[str, Any] | None,
) -> list[str]:
    """Concepts whose ``gs://`` image was refused by :func:`rendered_image_uri`
    (outside the run's bucket), so the judge saw only the prompt (pure).
    Excludes failed judge calls."""
    images = generated_images if isinstance(generated_images, Mapping) else {}
    out = []
    for e in visual_evals:
        image = images.get(e.concept_name)
        if _gs_uri(image) and not rendered_image_uri(image) and not _is_failed(e):
            out.append(e.concept_name)
    return out


def foreign_image_warning(concepts: list[str]) -> list[str]:
    """The report ``warnings`` entry for :func:`foreign_image_concepts` ([] if none)."""
    if not concepts:
        return []
    return [
        "Visual judge ignored a rendered image outside the run's bucket and "
        f"judged the prompt instead for: {', '.join(concepts)}"
    ]


def unscored_dimensions_warning(
    ad_evals: list[AdCopyEvaluation],
    visual_evals: list[VisualConceptEvaluation],
) -> list[str]:
    """The report ``warnings`` entry counting creatives with dimensions the
    judge did not score (failed as unverified, :func:`score_from_judge`)."""
    count = sum(bool(e.score.unscored_dimensions) for e in [*ad_evals, *visual_evals])
    if not count:
        return []
    noun = "creative" if count == 1 else "creatives"
    return [f"{count} {noun} missing judge scores (failed as unverified)"]


def judge_warnings(
    ad_evals: list[AdCopyEvaluation],
    visual_evals: list[VisualConceptEvaluation],
    generated_images: Mapping[str, Any] | None,
) -> list[str]:
    """Every judge-side report warning (image fallback, foreign image,
    unreported gates, unscored dimensions)."""
    return (
        image_fallback_warning(image_fallback_concepts(visual_evals, generated_images))
        + foreign_image_warning(foreign_image_concepts(visual_evals, generated_images))
        + unreported_gates_warning(ad_evals, visual_evals)
        + unscored_dimensions_warning(ad_evals, visual_evals)
    )


def _build_summary(
    ad_evals: list[AdCopyEvaluation],
    visual_evals: list[VisualConceptEvaluation],
) -> EvaluationSummary:
    """Build aggregate statistics from individual evaluations."""
    ad_scores = [e.score.overall_score for e in ad_evals]
    vis_scores = [e.score.overall_score for e in visual_evals]

    total = len(ad_evals) + len(visual_evals)
    passed = sum(1 for e in ad_evals if e.score.passed) + sum(
        1 for e in visual_evals if e.score.passed
    )

    # Find weakest dimensions across all verdicts
    dim_scores: dict[str, list[int]] = {}
    for eval_item in [*ad_evals, *visual_evals]:
        for v in eval_item.score.verdicts:
            dim_scores.setdefault(v.dimension, []).append(v.score)

    dim_avgs = {dim: sum(s) / len(s) for dim, s in dim_scores.items()}
    weakest = sorted(dim_avgs, key=lambda d: dim_avgs[d])[:3]

    # Over judged creatives only: a failed judge call verified nothing, so it
    # is neither a gate pass nor a gate failure (see the schema description).
    judged = [e for e in [*ad_evals, *visual_evals] if not _is_failed(e)]
    gates_ok = sum(1 for e in judged if e.score.gates_passed)

    return EvaluationSummary(
        total_ad_copies=len(ad_evals),
        ad_copies_passed=sum(1 for e in ad_evals if e.score.passed),
        avg_ad_copy_score=round(sum(ad_scores) / len(ad_scores), 3)
        if ad_scores
        else 0.0,
        total_visual_concepts=len(visual_evals),
        visual_concepts_passed=sum(1 for e in visual_evals if e.score.passed),
        avg_visual_score=round(sum(vis_scores) / len(vis_scores), 3)
        if vis_scores
        else 0.0,
        overall_pass_rate=round(passed / total, 3) if total > 0 else 0.0,
        weakest_dimensions=weakest,
        gates_pass_rate=round(gates_ok / len(judged), 3) if judged else None,
    )


def build_report(
    campaign_context: Mapping[str, Any],
    ad_evals: list[AdCopyEvaluation],
    visual_evals: list[VisualConceptEvaluation],
    config: EvalConfig,
    *,
    brief_used: bool,
    generated_images: Mapping[str, Any] | None = None,
    trend_situation: Any = None,
    run_warnings: Sequence[str] = (),
    learning_used: bool = False,
    learning_flags: Sequence[str] = (),
) -> CreativeEvaluationReport:
    """The one place a :class:`CreativeEvaluationReport` is assembled (pure).

    Shared by :func:`evaluate_creatives` and
    ``creative_eval.agent.evaluate_all_creatives``, so both entry points stamp
    the same summary, judge metadata and warnings: ``run_warnings`` (the run's
    degradation notes) first, then :func:`judge_warnings`.
    """
    return CreativeEvaluationReport(
        brand=campaign_context.get("brand", ""),
        target_product=campaign_context.get("target_product", ""),
        target_search_trend=campaign_context.get("target_search_trend", ""),
        ad_copy_evaluations=ad_evals,
        visual_concept_evaluations=visual_evals,
        summary=_build_summary(ad_evals, visual_evals),
        warnings=[
            *run_warnings,
            *judge_warnings(ad_evals, visual_evals, generated_images),
        ],
        judge_model=config.eval_model,
        judge_version=JUDGE_VERSION,
        passing_threshold=config.passing_threshold,
        brief_used=brief_used,
        learning_used=learning_used,
        learning_flags=list(learning_flags),
        trend_valid_until=trend_valid_until(trend_situation),
    )
