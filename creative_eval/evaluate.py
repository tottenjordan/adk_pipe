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

import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from google import genai

from agent_common import genai_retry
from agent_common.genai_retry import build_genai_http_retry

from . import prompts
from .brief import angle_line, format_brief_for_judge
from .config import EvalConfig
from .dimensions import AD_COPY_GATES, ADVISORY_GATES, BRIEF_GATES, VISUAL_GATES
from .schemas import (
    AdCopyEvaluation,
    CreativeEvaluationReport,
    CreativeScore,
    EvaluationSummary,
    EvalVerdict,
    GateResult,
    VisualConceptEvaluation,
)

logger = logging.getLogger(__name__)


def _get_client(config: EvalConfig) -> genai.Client:
    """Create a Gemini client with HTTP-layer retry for transient 429/503.

    Each creative is judged by an independent, concurrent call, so a fan-out that
    briefly outpaces the shared per-minute Vertex quota would otherwise 429 and
    (via evaluate_ad_copy/evaluate_visual_concept's except) degrade that creative
    to a zero score — sinking the report for a transient error. Retrying at the
    HTTP layer lets those calls self-heal instead. See agent_common.genai_retry.

    The shared per-request timeout (MILLISECONDS) bounds a hung judge call; this
    sync client uses httpx, whose ``ReadTimeout`` genai's retry_options retries.
    """
    return genai.Client(
        vertexai=True,
        project=config.project_id,
        location=config.location,
        http_options=genai.types.HttpOptions(
            retry_options=build_genai_http_retry(),
            timeout=genai_retry.model_request_timeout_ms(),
        ),
    )


NO_BRIEF_NOTE = "no brief"
NOT_REPORTED_NOTE = "not reported by the judge"


def normalize_gates(
    raw: list[GateResult], expected: tuple[str, ...], *, brief_used: bool
) -> list[GateResult]:
    """The judge's gates, reduced to exactly ``expected`` in order (pure).

    Unknown names are dropped and duplicates keep the first; a gate the judge
    left out fails (note "not reported by the judge") — a compliance check is
    never assumed. Without a brief the brief-dependent gates pass with note
    "no brief". ``advisory`` is set from ``ADVISORY_GATES``, never the judge.
    """
    reported: dict[str, GateResult] = {}
    for gate in raw:
        reported.setdefault(gate.gate.strip().lower(), gate)
    out = []
    for name in expected:
        advisory = name in ADVISORY_GATES
        if name in BRIEF_GATES and not brief_used:
            out.append(GateResult(gate=name, passed=True, note=NO_BRIEF_NOTE))
        elif name in reported:
            gate = reported[name]
            out.append(
                GateResult(
                    gate=name,
                    passed=gate.passed,
                    note=" ".join(gate.note.split()),
                    advisory=advisory,
                )
            )
        else:
            logger.warning("judge did not report gate %r", name)
            out.append(
                GateResult(
                    gate=name, passed=False, note=NOT_REPORTED_NOTE, advisory=advisory
                )
            )
    return out


def _apply_recomputed_score(
    score: CreativeScore,
    expected_gates: tuple[str, ...],
    threshold: float,
    *,
    brief_used: bool,
) -> None:
    """Overwrite the judge's aggregates with code-computed ones (in place).

    The model's strengths/improvements text is kept; overall_score, gates,
    gates_passed and passed are recomputed from its verdicts and gates.
    """
    gates = normalize_gates(score.gates, expected_gates, brief_used=brief_used)
    recomputed = _score_from_verdicts(score.verdicts, threshold, gates)
    score.overall_score = recomputed.overall_score
    score.passed = recomputed.passed
    score.gates = recomputed.gates
    score.gates_passed = recomputed.gates_passed


def _failed_score() -> CreativeScore:
    """The zero score of a failed judge call — nothing was verified."""
    return CreativeScore(
        overall_score=0.0,
        passed=False,
        verdicts=[],
        strengths=[],
        improvements=["evaluation_failed"],
        gates=[],
        gates_passed=False,
    )


def gates_passed(gates: list[GateResult]) -> bool:
    """True when every non-advisory gate passed (vacuously True with none).

    Computed in code — the judge's own aggregate is never trusted.
    """
    return all(g.passed for g in gates if not g.advisory)


def _score_from_verdicts(
    verdicts: list[EvalVerdict],
    threshold: float,
    gates: list[GateResult] | None = None,
) -> CreativeScore:
    """Compute an aggregate CreativeScore from individual verdicts and gates.

    ``passed`` = mean score (0-1) >= ``threshold`` AND ``gates_passed``.
    """
    gates = list(gates or [])
    gates_ok = gates_passed(gates)
    if not verdicts:
        return CreativeScore(
            overall_score=0.0,
            passed=False,
            verdicts=[],
            strengths=[],
            improvements=[],
            gates=gates,
            gates_passed=gates_ok,
        )

    avg_score = sum(v.score for v in verdicts) / (len(verdicts) * 10)
    passed = avg_score >= threshold and gates_ok

    strengths = [
        v.dimension
        for v in sorted(verdicts, key=lambda v: v.score, reverse=True)
        if v.verdict == "pass"
    ][:3]

    improvements = [
        v.dimension
        for v in sorted(verdicts, key=lambda v: v.score)
        if v.verdict == "fail"
    ][:3]

    return CreativeScore(
        overall_score=round(avg_score, 3),
        passed=passed,
        verdicts=verdicts,
        strengths=strengths,
        improvements=improvements,
        gates=gates,
        gates_passed=gates_ok,
    )


def evaluate_ad_copy(
    ad_copy: dict,
    campaign_context: dict,
    config: EvalConfig,
    client: genai.Client | None = None,
    brief: Mapping[str, Any] | None = None,
) -> AdCopyEvaluation:
    """Evaluate a single ad copy using Gemini-as-judge.

    Args:
        ad_copy: Dict with FinalAdCopy fields.
        campaign_context: Dict with brand, target_product, target_audience,
                          key_selling_points, target_search_trend.
        config: Evaluation configuration.
        client: Optional pre-created Gemini client.
        brief: The parsed creative brief (``brief.parse_brief``), or None.

    Returns:
        AdCopyEvaluation with per-dimension scores and binary gates.
    """
    if client is None:
        client = _get_client(config)

    # Build the prompt
    user_prompt = prompts.AD_COPY_EVAL_USER.format(
        **campaign_context,
        **ad_copy,
        angle=angle_line(brief, ad_copy.get("angle_id") or ""),
        brief_block=format_brief_for_judge(brief),
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

        result = AdCopyEvaluation.model_validate_json(response.text or "")

        # Recompute overall_score / gates_passed / passed in code for consistency
        _apply_recomputed_score(
            result.score,
            AD_COPY_GATES,
            config.passing_threshold,
            brief_used=brief is not None,
        )
        return result

    except Exception as e:
        logger.error(
            f"Failed to evaluate ad copy {ad_copy.get('original_id', '?')}: {e}"
        )
        # Return a zero-score evaluation on failure
        return AdCopyEvaluation(
            original_id=ad_copy.get("original_id", 0),
            headline=ad_copy.get("headline", ""),
            tone_style=ad_copy.get("tone_style", ""),
            score=_failed_score(),
        )


def evaluate_visual_concept(
    visual_concept: dict,
    campaign_context: dict,
    config: EvalConfig,
    client: genai.Client | None = None,
    brief: Mapping[str, Any] | None = None,
) -> VisualConceptEvaluation:
    """Evaluate a single visual concept using Gemini-as-judge.

    Args:
        visual_concept: Dict with VisualConceptFinal fields.
        campaign_context: Dict with brand, target_product, target_audience,
                          key_selling_points, target_search_trend.
        config: Evaluation configuration.
        client: Optional pre-created Gemini client.
        brief: The parsed creative brief (``brief.parse_brief``), or None.

    Returns:
        VisualConceptEvaluation with per-dimension scores and binary gates.
    """
    if client is None:
        client = _get_client(config)

    user_prompt = prompts.VISUAL_CONCEPT_EVAL_USER.format(
        **campaign_context,
        **{
            **visual_concept,
            "aspect_ratio": visual_concept.get("aspect_ratio") or "unspecified",
            "trend_motif": visual_concept.get("trend_motif") or "(none)",
            "brand_cue": visual_concept.get("brand_cue") or "(none)",
        },
        brief_block=format_brief_for_judge(brief),
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

        result = VisualConceptEvaluation.model_validate_json(response.text or "")

        _apply_recomputed_score(
            result.score,
            VISUAL_GATES,
            config.passing_threshold,
            brief_used=brief is not None,
        )
        return result

    except Exception as e:
        logger.error(
            f"Failed to evaluate visual concept {visual_concept.get('concept_name', '?')}: {e}"
        )
        return VisualConceptEvaluation(
            ad_copy_id=visual_concept.get("ad_copy_id", 0),
            concept_name=visual_concept.get("concept_name", ""),
            score=_failed_score(),
        )


def evaluate_all_concurrently(
    ad_copies: list[dict],
    visual_concepts: list[dict],
    campaign_context: dict,
    config: EvalConfig,
    client: genai.Client | None = None,
    *,
    brief: Mapping[str, Any] | None = None,
) -> tuple[list[AdCopyEvaluation], list[VisualConceptEvaluation]]:
    """Evaluate all ad copies and visual concepts concurrently, preserving order.

    Each creative is judged by an independent Gemini call, so running them in a
    thread pool collapses eval wall-clock from ~N*28s (sequential) to roughly a
    single call's latency. Results are returned in the same order as the inputs.
    Individual failures are already handled inside evaluate_ad_copy /
    evaluate_visual_concept (they return a zero-score evaluation), so a single
    bad creative can't sink the whole batch.
    """
    if client is None:
        client = _get_client(config)

    total = len(ad_copies) + len(visual_concepts)
    if total == 0:
        return [], []

    max_workers = max(1, min(config.max_eval_workers, total))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        ad_futures = [
            executor.submit(
                evaluate_ad_copy, ac, campaign_context, config, client, brief=brief
            )
            for ac in ad_copies
        ]
        vis_futures = [
            executor.submit(
                evaluate_visual_concept,
                vc,
                campaign_context,
                config,
                client,
                brief=brief,
            )
            for vc in visual_concepts
        ]
        # .result() preserves submission order and re-raises unexpected errors
        ad_evals = [f.result() for f in ad_futures]
        visual_evals = [f.result() for f in vis_futures]

    return ad_evals, visual_evals


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

    gates_ok = sum(1 for e in [*ad_evals, *visual_evals] if e.score.gates_passed)

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
        gates_pass_rate=round(gates_ok / total, 3) if total > 0 else None,
    )


def evaluate_creatives(
    campaign_context: dict,
    ad_copies: list[dict],
    visual_concepts: list[dict],
    config: EvalConfig | None = None,
    *,
    brief: Mapping[str, Any] | None = None,
) -> CreativeEvaluationReport:
    """Evaluate all creatives from a single agent run.

    Args:
        campaign_context: Dict with keys: brand, target_product, target_audience,
                          key_selling_points, target_search_trend.
        ad_copies: List of FinalAdCopy dicts.
        visual_concepts: List of VisualConceptFinal dicts.
        config: Optional EvalConfig (uses defaults if None).
        brief: The parsed creative brief (``brief.parse_brief``), or None —
               then the brief-dependent gates pass with note "no brief".

    Returns:
        CreativeEvaluationReport with per-creative and aggregate scores.
    """
    if config is None:
        config = EvalConfig()

    client = _get_client(config)

    logger.info(
        f"Evaluating {len(ad_copies)} ad copies and {len(visual_concepts)} visual "
        f"concepts concurrently (max {config.max_eval_workers} workers)..."
    )

    ad_evals, visual_evals = evaluate_all_concurrently(
        ad_copies, visual_concepts, campaign_context, config, client, brief=brief
    )

    summary = _build_summary(ad_evals, visual_evals)

    report = CreativeEvaluationReport(
        brand=campaign_context["brand"],
        target_product=campaign_context["target_product"],
        target_search_trend=campaign_context["target_search_trend"],
        ad_copy_evaluations=ad_evals,
        visual_concept_evaluations=visual_evals,
        summary=summary,
        judge_model=config.eval_model,
        passing_threshold=config.passing_threshold,
        brief_used=brief is not None,
    )

    logger.info(
        f"Evaluation complete: {summary.ad_copies_passed}/{summary.total_ad_copies} ad copies passed, "
        f"{summary.visual_concepts_passed}/{summary.total_visual_concepts} visual concepts passed. "
        f"Overall pass rate: {summary.overall_pass_rate:.1%}"
    )

    return report
