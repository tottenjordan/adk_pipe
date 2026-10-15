"""Creative evaluation entry points for the creative_agent pipeline.

Plain functions on a ``tool_context``-like object (anything with ``.state``),
called directly by creative_agent's ``finalize_pipeline`` nodes
(``creative_agent/finalize.py``): :func:`evaluate_ad_copies_only` (early
ad-copy judging) and :func:`evaluate_all_creatives` (the full report).
"""

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any

from agent_common import collect_degradation_warnings

from .brief import parse_brief
from .config import EvalConfig
from .evaluate import (
    _is_failed,
    build_report,
    evaluate_all_concurrently,
    run_trend_situation,
    safer_angle_active,
    safer_angle_reason,
)
from .schemas import AdCopyEvaluation, CreativeScore, VisualConceptEvaluation

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
    """Hash of everything an ad-copy judgement depends on.

    Ad copies, brief, campaign, the validated ``trend_situation`` and the
    ``trend_sensitive_hint`` that turns it off (a timely copy is judged
    against it, :func:`creative_eval.evaluate.run_trend_situation`), the
    safer-angle state (``safer_angle_active``,
    ``safer_angle_sensitive_reason``, ``safer_angle_named_copies``; the brief's
    ``trend_bridge.safer_angle`` is in the brief) and the judge. An early ad-copy evaluation is
    reused by :func:`evaluate_all_creatives` only while this still matches, so
    a re-run with changed copies or a changed moment re-judges.
    """
    payload = {
        "campaign": _campaign_context(state),
        "ad_copies": _state_items(state, "ad_copy_critique", "ad_copies"),
        "brief": parse_brief(state.get("creative_brief")),
        "trend_situation": state.get("trend_situation"),
        "trend_sensitive_hint": state.get("trend_sensitive_hint"),
        "safer_angle": [
            state.get("safer_angle_active"),
            state.get("safer_angle_sensitive_reason"),
            state.get("safer_angle_named_copies"),
        ],
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
        trend_situation=run_trend_situation(state),
        sources=state.get("sources"),
        safer_angle=safer_angle_active(state),
        sensitive_reason=safer_angle_reason(state),
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
    return [None if _is_failed(e) else e for e in evals]


def learning_record(applied: Any) -> tuple[bool, list[str]]:
    """``(learning_used, learning_flags)`` from ``rating_signals_applied`` (pure).

    Only an applied record (``applied is True``) counts. The flags name what
    actually changed the run: ``guidance`` (a non-empty note), ``styles``
    (families excluded or preferred) and each strictness flag, in order and
    deduplicated. Malformed values degrade to ``(False, [])``; never raises.
    """
    if not isinstance(applied, Mapping) or applied.get("applied") is not True:
        return False, []

    def _strings(value: Any) -> list[str]:
        if not isinstance(value, list | tuple):
            return []
        return [v for v in value if isinstance(v, str) and v.strip()]

    flags: list[str] = []
    if str(applied.get("signals") or "").strip():
        flags.append("guidance")
    if _strings(applied.get("styles_excluded")) or _strings(
        applied.get("styles_preferred")
    ):
        flags.append("styles")
    flags += _strings(applied.get("strictness"))
    return True, list(dict.fromkeys(flags))


def evaluate_all_creatives(tool_context) -> dict:
    """Evaluate all finalized ad copies and visual concepts in session state.

    Reads from state keys:
      - ad_copy_critique (FinalAdCopyList JSON)
      - final_visual_concepts (VisualConceptFinalList JSON)
      - brand, target_product, target_audience, key_selling_points, target_search_trends
      - creative_brief (CreativeBrief dict/JSON; optional — the judge's gate contract)
      - generated_images ({concept_name: {gcs_uri, qa, ...}}; optional — the
        rendered images the visual judge sees)
      - person_reference ({uri, consent_id}; optional — the consented person's
        photo, attached to cast concepts' judge calls as ground truth)
      - trend_situation + sources (optional — the validated current moment a
        timely creative's claim is judged against, with its source URLs)
      - safer_angle_active ("yes" on a safer-angle run) +
        safer_angle_sensitive_reason (optional — every creative is judged for
        safer_angle_respected; the brief's trend_bridge.safer_angle and the
        reason reach the judge's brief block)

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

    ad_evals, visual_evals = _judge_run(
        state, campaign_context, ad_copies, visual_concepts
    )
    learning_used, learning_flags = learning_record(state.get("rating_signals_applied"))
    report = build_report(
        campaign_context,
        ad_evals,
        visual_evals,
        _config,
        brief_used=parse_brief(state.get("creative_brief")) is not None,
        generated_images=state.get("generated_images") or {},
        trend_situation=run_trend_situation(state),
        # Research producers that exhausted their retries (RetryUntilKeyNode
        # markers) and other residual-issue markers, as report notes.
        run_warnings=collect_degradation_warnings(state),
        learning_used=learning_used,
        learning_flags=learning_flags,
    )
    state["creative_evaluation_report"] = report.model_dump()

    summary = report.summary
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
        "failed_creatives": _failed_creatives(ad_evals, visual_evals),
    }


def _judge_run(
    state: Mapping[str, Any],
    campaign_context: dict[str, Any],
    ad_copies: list[dict],
    visual_concepts: list[dict],
) -> tuple[list[AdCopyEvaluation], list[VisualConceptEvaluation]]:
    """Judge the run's creatives, reusing the early ad-copy evaluations.

    Ad copies already judged while the visuals rendered (creative_agent's
    evaluate_ad_copies_node) are reused; only the rest are judged now, in one
    concurrent batch with every visual concept. Order follows the inputs.
    """
    reused = _reusable_ad_evals(state, len(ad_copies))
    to_judge = [ac for ac, e in zip(ad_copies, reused, strict=True) if e is None]

    logger.info(
        f"Evaluating {len(to_judge)} ad copies ({len(ad_copies) - len(to_judge)} "
        f"reused from the early evaluation) and {len(visual_concepts)} visual "
        f"concepts concurrently (max {_config.workers_for(len(to_judge), len(visual_concepts))} workers)..."
    )

    # Score every creative in parallel — each is an independent judge call, so
    # this collapses eval wall-clock from ~N*28s to roughly one call's latency.
    judged_ads, visual_evals = evaluate_all_concurrently(
        to_judge,
        visual_concepts,
        campaign_context,
        _config,
        brief=parse_brief(state.get("creative_brief")),
        generated_images=state.get("generated_images") or {},
        person_reference=state.get("person_reference"),
        trend_situation=run_trend_situation(state),
        sources=state.get("sources"),
        # Visuals pair with every final copy, also the ones reused above.
        paired_ad_copies=ad_copies,
        safer_angle=safer_angle_active(state),
        sensitive_reason=safer_angle_reason(state),
    )
    if not any(e is not None for e in reused):
        return judged_ads, visual_evals
    judged_iter = iter(judged_ads)
    return [e if e is not None else next(judged_iter) for e in reused], visual_evals


def _failed_creatives(
    ad_evals: list[AdCopyEvaluation], visual_evals: list[VisualConceptEvaluation]
) -> list[dict[str, Any]]:
    """Compact per-creative failures, so the agent can say which failed and why.

    ``improvements`` are the judge's own top-2 suggested improvements (free
    text from its score), ``failed_gates`` the failed blocking gates.
    """
    rows: list[tuple[str, Any, str, AdCopyEvaluation | VisualConceptEvaluation]] = [
        *(("ad_copy", e.original_id, e.headline, e) for e in ad_evals),
        *(("visual_concept", e.ad_copy_id, e.concept_name, e) for e in visual_evals),
    ]
    return [
        {
            "type": kind,
            "id": creative_id,
            "name": name,
            "overall_score": e.score.overall_score,
            "improvements": e.score.improvements[:2],
            "failed_gates": _failed_gates(e.score),
        }
        for kind, creative_id, name, e in rows
        if not e.score.passed
    ]
