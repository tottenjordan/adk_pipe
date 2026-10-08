"""ADK agent integration for creative evaluation.

Provides an evaluation agent that can be plugged into the creative_agent
pipeline as an additional step after visual generation.

Usage:
    from creative_eval.agent import creative_eval_agent

    # Add to root_agent tools:
    tools=[..., AgentTool(agent=creative_eval_agent)]
"""

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any

from google.adk.agents import Agent

from agent_common import (
    build_gemini,
    collect_degradation_warnings,
    log_empty_turn_finish_reason,
)

from .brief import parse_brief
from .config import EvalConfig
from .evaluate import (
    _build_summary,
    evaluate_all_concurrently,
    judge_warnings,
)
from .schemas import AdCopyEvaluation, CreativeEvaluationReport, CreativeScore

logger = logging.getLogger(__name__)

_config = EvalConfig()

# Ad-copy evaluations judged early (creative_agent's evaluate_ad_copies_node,
# concurrently with the visual stage): {"fingerprint": str, "evaluations":
# [AdCopyEvaluation dict, ...]}. evaluate_all_creatives reuses them only when
# the fingerprint still matches the current ad copies / brief / campaign.
AD_COPY_PARTIAL_KEY = "ad_copy_evaluations_partial"


def _failed_gates(score: CreativeScore) -> list[str]:
    """Names of the failed non-advisory gates (the ones that block ``passed``)."""
    return [g.gate for g in score.gates if not g.passed and not g.advisory]


def _campaign_context(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "brand": state.get("brand", ""),
        "target_product": state.get("target_product", ""),
        "target_audience": state.get("target_audience", ""),
        "key_selling_points": state.get("key_selling_points", ""),
        "target_search_trend": state.get("target_search_trends", ""),
    }


def _state_items(state: Mapping[str, Any], key: str, list_key: str) -> list[dict]:
    """The ``list_key`` items of state ``key`` (a dict or its JSON), as dicts."""
    raw = state.get(key, {})
    if isinstance(raw, str):
        raw = json.loads(raw)
    items = raw.get(list_key, []) if isinstance(raw, dict) else []
    return [json.loads(i) if isinstance(i, str) else i for i in items]


def ad_copy_eval_fingerprint(state: Mapping[str, Any]) -> str:
    """Hash of everything an ad-copy judgement depends on (ad copies, brief, campaign).

    An early ad-copy evaluation is reused by :func:`evaluate_all_creatives`
    only while this still matches, so a re-run with changed copies re-judges.
    """
    payload = {
        "campaign": _campaign_context(state),
        "ad_copies": _state_items(state, "ad_copy_critique", "ad_copies"),
        "brief": parse_brief(state.get("creative_brief")),
        "judge": [_config.eval_model, _config.passing_threshold],
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def evaluate_ad_copies_only(tool_context) -> dict:
    """Judge only the final ad copies and store them in ``ad_copy_evaluations_partial``.

    The early half of the evaluation: creative_agent runs it while the visual
    stage renders, so finalize only has the visual judge calls left. Same
    judge, inputs and per-copy failure handling as :func:`evaluate_all_creatives`.
    No ad copies → ``{"status": "skipped"}`` and nothing stored.
    """
    state = tool_context.state
    ad_copies = _state_items(state, "ad_copy_critique", "ad_copies")
    if not ad_copies:
        return {"status": "skipped", "message": "No ad copies to evaluate yet."}
    logger.info("Evaluating %d ad copies early (before rendering)", len(ad_copies))
    ad_evals, _ = evaluate_all_concurrently(
        ad_copies,
        [],
        _campaign_context(state),
        _config,
        brief=parse_brief(state.get("creative_brief")),
    )
    state[AD_COPY_PARTIAL_KEY] = {
        "fingerprint": ad_copy_eval_fingerprint(state),
        "evaluations": [e.model_dump() for e in ad_evals],
    }
    return {"status": "success", "total_ad_copies": len(ad_evals)}


def _reusable_ad_evals(
    state: Mapping[str, Any], n_ad_copies: int
) -> list[AdCopyEvaluation | None]:
    """Per ad copy: its early evaluation to reuse, or ``None`` to judge it now.

    All ``None`` without a partial, on a fingerprint / count mismatch or a
    malformed partial. A failed early judge call (zero-score placeholder) is
    ``None`` too, so finalize retries just that copy.
    """
    fresh: list[AdCopyEvaluation | None] = [None] * n_ad_copies
    partial = state.get(AD_COPY_PARTIAL_KEY)
    if not isinstance(partial, Mapping) or not n_ad_copies:
        return fresh
    raw = partial.get("evaluations")
    if not isinstance(raw, list) or len(raw) != n_ad_copies:
        return fresh
    if partial.get("fingerprint") != ad_copy_eval_fingerprint(state):
        logger.info("Early ad-copy evaluation is stale; re-judging the ad copies")
        return fresh
    try:
        evals = [AdCopyEvaluation.model_validate(e) for e in raw]
    except ValueError:
        logger.warning("Malformed early ad-copy evaluation; re-judging the ad copies")
        return fresh
    return [None if "evaluation_failed" in e.score.improvements else e for e in evals]


def evaluate_all_creatives(tool_context) -> dict:
    """Evaluate all finalized ad copies and visual concepts in session state.

    Reads from state keys:
      - ad_copy_critique (FinalAdCopyList JSON)
      - final_visual_concepts (VisualConceptFinalList JSON)
      - brand, target_product, target_audience, key_selling_points, target_search_trends
      - creative_brief (CreativeBrief dict/JSON; optional — the judge's gate contract)
      - generated_images ({concept_name: {gcs_uri, qa, ...}}; optional — the
        rendered images the visual judge sees)

    Writes to state key:
      - creative_evaluation_report (CreativeEvaluationReport JSON)

    Returns:
        Summary dict with pass rates, weakest dimensions, and a compact
        ``failed_creatives`` list (type, id, name, overall_score, top-2
        improvement dimensions, failed gate names) for each creative that did
        not pass (score below the threshold or a failed gate).
    """
    state = tool_context.state
    campaign_context = _campaign_context(state)
    ad_copies = _state_items(state, "ad_copy_critique", "ad_copies")
    visual_concepts = _state_items(state, "final_visual_concepts", "visual_concepts")

    if not ad_copies and not visual_concepts:
        return {
            "status": "error",
            "message": "No ad copies or visual concepts found in session state to evaluate.",
        }

    # Ad copies already judged while the visuals rendered (creative_agent's
    # evaluate_ad_copies_node) are reused; only the rest are judged now.
    reused = _reusable_ad_evals(state, len(ad_copies))
    to_judge = [ac for ac, e in zip(ad_copies, reused, strict=True) if e is None]

    logger.info(
        f"Evaluating {len(to_judge)} ad copies ({len(ad_copies) - len(to_judge)} "
        f"reused from the early evaluation) and {len(visual_concepts)} visual "
        f"concepts concurrently (max {_config.workers_for(len(to_judge), len(visual_concepts))} workers)..."
    )

    brief = parse_brief(state.get("creative_brief"))
    generated_images = state.get("generated_images") or {}

    # Score every creative in parallel — each is an independent judge call, so
    # this collapses eval wall-clock from ~N*28s to roughly one call's latency.
    judged_ads, visual_evals = evaluate_all_concurrently(
        to_judge,
        visual_concepts,
        campaign_context,
        _config,
        brief=brief,
        generated_images=generated_images,
    )
    if any(e is not None for e in reused):
        judged_iter = iter(judged_ads)
        ad_evals = [e if e is not None else next(judged_iter) for e in reused]
    else:
        ad_evals = judged_ads

    summary = _build_summary(ad_evals, visual_evals)

    # Surface any research producers that exhausted their retries (RetryUntilKeyNode
    # markers) as structured, consumable degradation notes on the report.
    warnings = collect_degradation_warnings(state) + judge_warnings(
        ad_evals, visual_evals, generated_images
    )

    report = CreativeEvaluationReport(
        brand=campaign_context["brand"],
        target_product=campaign_context["target_product"],
        target_search_trend=campaign_context["target_search_trend"],
        ad_copy_evaluations=ad_evals,
        visual_concept_evaluations=visual_evals,
        summary=summary,
        warnings=warnings,
        judge_model=_config.eval_model,
        passing_threshold=_config.passing_threshold,
        brief_used=brief is not None,
    )

    # Store in session state
    state["creative_evaluation_report"] = report.model_dump()

    # Compact per-creative list of failures so the agent can say which failed and
    # why (improvements are the lowest-scoring failed dimensions, top 2).
    failed_creatives = [
        {
            "type": "ad_copy",
            "id": e.original_id,
            "name": e.headline,
            "overall_score": e.score.overall_score,
            "improvements": e.score.improvements[:2],
            "failed_gates": _failed_gates(e.score),
        }
        for e in ad_evals
        if not e.score.passed
    ] + [
        {
            "type": "visual_concept",
            "id": e.ad_copy_id,
            "name": e.concept_name,
            "overall_score": e.score.overall_score,
            "improvements": e.score.improvements[:2],
            "failed_gates": _failed_gates(e.score),
        }
        for e in visual_evals
        if not e.score.passed
    ]

    return {
        "status": "success",
        "total_ad_copies": summary.total_ad_copies,
        "ad_copies_passed": summary.ad_copies_passed,
        "avg_ad_copy_score": summary.avg_ad_copy_score,
        "total_visual_concepts": summary.total_visual_concepts,
        "visual_concepts_passed": summary.visual_concepts_passed,
        "avg_visual_score": summary.avg_visual_score,
        "overall_pass_rate": summary.overall_pass_rate,
        "weakest_dimensions": summary.weakest_dimensions,
        "failed_creatives": failed_creatives,
    }


# ADK Agent that wraps the evaluation tool
creative_eval_agent = Agent(
    model=build_gemini(_config.eval_model),
    name="creative_eval_agent",
    include_contents="none",
    description="Evaluate the quality of generated ad copies and visual concepts using LLM-as-judge scoring.",
    instruction="""Role: You are a Creative Quality Evaluation Specialist.

    Your task is to evaluate all finalized ad copies and visual concepts using the `evaluate_all_creatives` tool.

    <INSTRUCTIONS>
    1. Call the `evaluate_all_creatives` tool to score all creatives in the session.
    2. Report the results: overall pass rate, average scores, and weakest dimensions.
    3. Highlight any creatives listed in `failed_creatives` (score < 0.7 or a failed compliance check) and explain why, using their `failed_gates` (failed checks) and `improvements` (the weakest failed dimensions).
    </INSTRUCTIONS>

    Call the tool now and report the results.
    """,
    tools=[evaluate_all_creatives],
    after_model_callback=log_empty_turn_finish_reason,
)
