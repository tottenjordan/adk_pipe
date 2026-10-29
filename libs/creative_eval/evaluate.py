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

The public entry points live here; the gate rules are in
:mod:`creative_eval.gates`, the judge call and prompt builders in
:mod:`creative_eval.judge`, and the report assembly in
:mod:`creative_eval.report`.
"""

import logging
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from google import genai

from . import prompts
from .config import EvalConfig
from .dimensions import AD_COPY_GATES
from .gates import (
    _score_visual,
    _trend_given,
    creative_is_timely,
    paired_copy_timely,
    score_from_judge,
    timely_copy_ids,
)

# Re-exports ("X as X") kept for importers outside creative_eval/ only
# (experiments/grader_temperature): _get_client (also used here),
# image_mime_type, safer_angle_active, safer_angle_reason.
from .gates import safer_angle_active as safer_angle_active  # re-export
from .gates import safer_angle_reason as safer_angle_reason  # re-export
from .judge import (
    _build_ad_copy_request,
    _build_visual_request,
    _call_judge,
    _get_client,
    _gs_uri,
    _judge_with_image_fallbacks,
    _moment_section,
    _redact,
    person_photo_uri,
    rendered_image_uri,
)
from .judge import image_mime_type as image_mime_type  # re-export
from .report import _failed_score, build_report
from .schemas import (
    AdCopyEvaluation,
    AdCopyJudgeOutput,
    CreativeEvaluationReport,
    CreativeScore,
    VisualConceptEvaluation,
    VisualJudgeOutput,
)

logger = logging.getLogger(__name__)


def _int_id(value: Any, fallback: int) -> int:
    """Coerce ``value`` to ``int`` when well-formed (rejecting ``bool``), else ``fallback``."""
    if isinstance(value, bool) or value is None:
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def evaluate_ad_copy(
    ad_copy: dict,
    campaign_context: dict,
    config: EvalConfig,
    client: genai.Client | None = None,
    brief: Mapping[str, Any] | None = None,
    trend_situation: Mapping[str, Any] | None = None,
    sources: Mapping[str, Any] | None = None,
    safer_angle: bool = False,
    sensitive_reason: str = "",
) -> AdCopyEvaluation:
    """Evaluate a single ad copy using Gemini-as-judge.

    Args:
        ad_copy: Dict with FinalAdCopy fields.
        campaign_context: Dict with brand, target_product, target_audience,
                          key_selling_points, target_search_trend.
        config: Evaluation configuration.
        client: Optional pre-created Gemini client.
        brief: The parsed creative brief (``brief.parse_brief``), or None.
        trend_situation: The run's validated ``trend_situation``, or None. A
                         copy flagged ``uses_trend_state`` under a timely
                         moment is judged against it (``timely_claim_supported``).
        sources: ``state["sources"]`` (the moment's cited source URLs).
        safer_angle: A safer-angle run (:func:`safer_angle_active`): the judge
                     checks ``safer_angle_respected``.
        sensitive_reason: The sensitive event never to reference (judge context).

    Returns:
        AdCopyEvaluation with per-dimension scores and binary gates.
    """
    if client is None:
        client = _get_client(config)

    timely = creative_is_timely(ad_copy, trend_situation)
    user_prompt = _build_ad_copy_request(
        ad_copy,
        campaign_context,
        brief=brief,
        timely=timely,
        moment_section=_moment_section(timely, trend_situation, sources),
        safer_angle=safer_angle,
        sensitive_reason=sensitive_reason,
    )
    try:
        judged = _call_judge(
            client, config, user_prompt, prompts.AD_COPY_EVAL_SYSTEM, AdCopyJudgeOutput
        )
        score = score_from_judge(
            judged,
            AD_COPY_GATES,
            config.passing_threshold,
            expected_dimensions=config.ad_copy_dimensions,
            brief_used=brief is not None,
            trend_given=_trend_given(campaign_context),
            timely=timely,
            safer_angle=safer_angle,
        )
    except Exception as e:
        logger.error(
            f"Failed to evaluate ad copy {ad_copy.get('original_id', '?')}: {e}"
        )
        score = _failed_score()  # a zero-score evaluation on failure
    # Join keys and labels come from the input copy, never the judge's echo.
    return AdCopyEvaluation(
        original_id=_int_id(ad_copy.get("original_id"), 0),
        headline=str(ad_copy.get("headline") or ""),
        tone_style=str(ad_copy.get("tone_style") or ""),
        angle_id=str(ad_copy.get("angle_id") or ""),
        score=score,
        timely=timely,
    )


def evaluate_visual_concept(
    visual_concept: dict,
    campaign_context: dict,
    config: EvalConfig,
    client: genai.Client | None = None,
    brief: Mapping[str, Any] | None = None,
    image: Mapping[str, Any] | None = None,
    person_photo: str = "",
    trend_situation: Mapping[str, Any] | None = None,
    sources: Mapping[str, Any] | None = None,
    paired_copy_timely: bool = False,
    safer_angle: bool = False,
    sensitive_reason: str = "",
) -> VisualConceptEvaluation:
    """Evaluate a single visual concept using Gemini-as-judge.

    With a rendered image (``image["gcs_uri"]``, a ``generated_images``
    record) the judge sees the pixels as a ``gs://`` Part — Vertex fetches it
    server-side as the project's Vertex AI service agent (project
    permissions), not with the caller's credentials — and judges the image,
    using the prompt only for intent; the record's image-QA verdict is passed
    as a hint line. Without one it judges the image generation prompt and the
    result records ``image_judged=False``.

    For a cast concept (``image["cast"] is True``) with a rendered image and a
    ``person_photo`` (:func:`person_photo_uri`), the consented person's
    reference photo is attached after the render as ground truth for their
    own appearance; an unreadable photo is retried without it.

    Args:
        visual_concept: Dict with VisualConceptFinal fields.
        campaign_context: Dict with brand, target_product, target_audience,
                          key_selling_points, target_search_trend.
        config: Evaluation configuration.
        client: Optional pre-created Gemini client.
        brief: The parsed creative brief (``brief.parse_brief``), or None.
        image: The concept's ``generated_images`` record, or None.
        person_photo: The run's person photo ``gs://`` URI (only used for a
                      cast concept), or "".
        trend_situation: The run's validated ``trend_situation``, or None. A
                         concept flagged ``uses_trend_state`` (or paired with a
                         flagged copy: ``paired_copy_timely``) under a timely
                         moment is judged against it.
        sources: ``state["sources"]`` (the moment's cited source URLs).
        paired_copy_timely: The paired ad copy is flagged ``uses_trend_state``.
        safer_angle: A safer-angle run (:func:`safer_angle_active`): the judge
                     checks ``safer_angle_respected``.
        sensitive_reason: The sensitive event never to reference (judge context).

    Returns:
        VisualConceptEvaluation with per-dimension scores and binary gates.
    """
    if client is None:
        client = _get_client(config)

    name = visual_concept.get("concept_name", "?")
    image_uri = rendered_image_uri(image)
    if _gs_uri(image) and not image_uri:
        logger.warning(
            "Rendered image for %r is outside the run's bucket; judging its prompt",
            name,
        )
    # The render record decides (a blocked person render falls back to an
    # uncast image even when the concept asked to cast).
    person_cast = isinstance(image, Mapping) and image.get("cast") is True
    photo = person_photo if person_cast and image_uri else ""
    timely = creative_is_timely(
        visual_concept, trend_situation, paired_copy_timely=paired_copy_timely
    )
    moment_section = _moment_section(timely, trend_situation, sources)

    def evaluation(
        score: CreativeScore, image_judged: bool = False
    ) -> VisualConceptEvaluation:
        # Join keys come from the input concept, never the judge's echo; the
        # tags are recorded by code (brand history reads them).
        return VisualConceptEvaluation(
            ad_copy_id=_int_id(visual_concept.get("ad_copy_id"), 0),
            concept_name=str(visual_concept.get("concept_name") or ""),
            score=score,
            image_judged=image_judged,
            visual_style=str(visual_concept.get("visual_style") or ""),
            angle_id=str(visual_concept.get("angle_id") or ""),
            timely=timely,
        )

    def judge(uri: str, photo_uri: str) -> VisualConceptEvaluation:
        """One judge call: with the rendered image at ``uri`` (plus the cast
        person's reference photo at ``photo_uri``), or prompt-only."""
        contents = _build_visual_request(
            visual_concept,
            campaign_context,
            brief=brief,
            qa=(image or {}).get("qa"),
            uri=uri,
            photo_uri=photo_uri,
            person_cast=person_cast,
            timely=timely,
            moment_section=moment_section,
            safer_angle=safer_angle,
            sensitive_reason=sensitive_reason,
        )
        judged = _call_judge(
            client,
            config,
            contents,
            prompts.VISUAL_CONCEPT_EVAL_SYSTEM,
            VisualJudgeOutput,
        )
        score = _score_visual(
            judged,
            config,
            brief_used=brief is not None,
            trend_given=_trend_given(campaign_context),
            person_cast=person_cast,
            timely=timely,
            safer_angle=safer_angle,
        )
        return evaluation(score, image_judged=bool(uri))

    if not image_uri:
        logger.info("No rendered image for visual concept %r; judging its prompt", name)
    try:
        return _judge_with_image_fallbacks(judge, name, image_uri, photo)
    except Exception as e:
        logger.error(
            "Failed to evaluate visual concept %s: %s", name, _redact(str(e), photo)
        )
        return evaluation(_failed_score())


def evaluate_all_concurrently(
    ad_copies: list[dict],
    visual_concepts: list[dict],
    campaign_context: dict,
    config: EvalConfig,
    client: genai.Client | None = None,
    *,
    brief: Mapping[str, Any] | None = None,
    generated_images: Mapping[str, Any] | None = None,
    person_reference: Any = None,
    trend_situation: Mapping[str, Any] | None = None,
    sources: Mapping[str, Any] | None = None,
    paired_ad_copies: Sequence[Any] | None = None,
    safer_angle: bool = False,
    sensitive_reason: str = "",
) -> tuple[list[AdCopyEvaluation], list[VisualConceptEvaluation]]:
    """Evaluate all ad copies and visual concepts concurrently, preserving order.

    Each creative is judged by an independent Gemini call, so running them in a
    thread pool collapses eval wall-clock from ~N*28s (sequential) to roughly a
    single call's latency. Results are returned in the same order as the inputs.
    Individual failures are already handled inside evaluate_ad_copy /
    evaluate_visual_concept (they return a zero-score evaluation), so a single
    bad creative can't sink the whole batch. Each visual concept gets its own
    ``generated_images[concept_name]`` record (None when it has no image);
    cast concepts also get the run's ``person_reference`` photo. Timely
    creatives (:func:`creative_is_timely`) are judged against
    ``trend_situation``; a visual concept pairs with its copy among
    ``paired_ad_copies`` (default ``ad_copies``; pass the full list when only
    some copies are judged here). On a safer-angle run (``safer_angle``)
    every creative is judged for ``safer_angle_respected``, with the
    ``sensitive_reason`` in its brief block.
    """
    total = len(ad_copies) + len(visual_concepts)
    if total == 0:
        return [], []

    if client is None:
        # Own the client for this batch: closed (google-genai ``with`` support)
        # only after the pool below has finished every judge call.
        with _get_client(config) as owned:
            return evaluate_all_concurrently(
                ad_copies,
                visual_concepts,
                campaign_context,
                config,
                owned,
                brief=brief,
                generated_images=generated_images,
                person_reference=person_reference,
                trend_situation=trend_situation,
                sources=sources,
                paired_ad_copies=paired_ad_copies,
                safer_angle=safer_angle,
                sensitive_reason=sensitive_reason,
            )

    images = generated_images if isinstance(generated_images, Mapping) else {}
    photo = person_photo_uri(person_reference)
    copy_ids = timely_copy_ids(
        ad_copies if paired_ad_copies is None else paired_ad_copies
    )
    max_workers = config.workers_for(len(ad_copies), len(visual_concepts))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        ad_futures = [
            executor.submit(
                evaluate_ad_copy,
                ac,
                campaign_context,
                config,
                client,
                brief=brief,
                trend_situation=trend_situation,
                sources=sources,
                safer_angle=safer_angle,
                sensitive_reason=sensitive_reason,
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
                image=images.get(vc.get("concept_name", "")),
                person_photo=photo,
                trend_situation=trend_situation,
                sources=sources,
                paired_copy_timely=paired_copy_timely(vc, copy_ids),
                safer_angle=safer_angle,
                sensitive_reason=sensitive_reason,
            )
            for vc in visual_concepts
        ]
        # .result() preserves submission order and re-raises unexpected errors
        ad_evals = [f.result() for f in ad_futures]
        visual_evals = [f.result() for f in vis_futures]

    return ad_evals, visual_evals


def evaluate_creatives(
    campaign_context: dict,
    ad_copies: list[dict],
    visual_concepts: list[dict],
    config: EvalConfig | None = None,
    *,
    brief: Mapping[str, Any] | None = None,
    generated_images: Mapping[str, Any] | None = None,
    person_reference: Any = None,
    trend_situation: Mapping[str, Any] | None = None,
    sources: Mapping[str, Any] | None = None,
    safer_angle: bool = False,
    sensitive_reason: str = "",
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
        generated_images: ``{concept_name: {gcs_uri, qa, ...}}`` — the rendered
               images the visual judge sees (prompt-only without one).
        person_reference: The run's ``{uri, consent_id}`` person reference;
               its photo is attached to cast concepts' judge calls.
        trend_situation: The run's validated moment; timely creatives are
               judged against it (``timely_claim_supported``).
        sources: ``state["sources"]`` (the moment's cited source URLs).
        safer_angle: A safer-angle run: every creative is judged for
               ``safer_angle_respected`` (else passed with "no safer angle").
        sensitive_reason: The sensitive event never to reference.

    Returns:
        CreativeEvaluationReport with per-creative and aggregate scores.
    """
    if config is None:
        config = EvalConfig()

    logger.info(
        f"Evaluating {len(ad_copies)} ad copies and {len(visual_concepts)} visual "
        f"concepts concurrently (max {config.workers_for(len(ad_copies), len(visual_concepts))} workers)..."
    )

    # The sync genai client holds an httpx connection pool: close it once the
    # batch is done (evaluate_all_concurrently returns only after its pool has
    # joined every judge call, so nothing uses the client past this block).
    with _get_client(config) as client:
        ad_evals, visual_evals = evaluate_all_concurrently(
            ad_copies,
            visual_concepts,
            campaign_context,
            config,
            client,
            brief=brief,
            generated_images=generated_images,
            person_reference=person_reference,
            trend_situation=trend_situation,
            sources=sources,
            safer_angle=safer_angle,
            sensitive_reason=sensitive_reason,
        )

    report = build_report(
        campaign_context,
        ad_evals,
        visual_evals,
        config,
        brief_used=brief is not None,
        generated_images=generated_images,
        trend_situation=trend_situation,
    )
    summary = report.summary

    logger.info(
        f"Evaluation complete: {summary.ad_copies_passed}/{summary.total_ad_copies} ad copies passed, "
        f"{summary.visual_concepts_passed}/{summary.total_visual_concepts} visual concepts passed. "
        f"Overall pass rate: {summary.overall_pass_rate:.1%}"
    )

    return report
