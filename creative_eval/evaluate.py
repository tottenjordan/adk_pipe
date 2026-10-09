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
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from google import genai
from google.genai import errors as genai_errors

from agent_common import genai_retry
from agent_common.genai_retry import build_genai_http_retry

from . import prompts
from .brief import angle_line, format_brief_for_judge
from .config import EvalConfig
from .dimensions import (
    AD_COPY_GATES,
    ADVISORY_GATES,
    BRIEF_GATES,
    JUDGE_VERSION,
    NO_GATES_GATE,
    VISUAL_GATES,
)
from .schemas import (
    AdCopyEvaluation,
    AdCopyJudgeOutput,
    CreativeEvaluationReport,
    CreativeScore,
    EvaluationSummary,
    EvalVerdict,
    GateResult,
    GateResultIn,
    JudgeScore,
    VisualConceptEvaluation,
    VisualJudgeOutput,
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
NOT_REPORTED_NOTE = "not checked (the judge did not report it)"
NO_GATES_NOTE = "judge returned no gates"


def normalize_gates(
    raw: Sequence[GateResultIn | GateResult],
    expected: tuple[str, ...],
    *,
    brief_used: bool,
) -> list[GateResult]:
    """The judge's gates, reduced to exactly ``expected`` in order (pure).

    Names are matched leniently (case, spaces and hyphens ignored); unknown
    names are dropped and duplicates keep the first. When NONE of the
    expected gates was reported the judge skipped the checks: the result is a
    single failed ``NO_GATES_GATE`` gate (note "judge returned no gates"), so
    the creative cannot pass unverified. A partial omission stays
    conservative — the missing gate passes with a "not checked" note (counted
    into a report warning by :func:`unreported_gates_warning`). Without a
    brief the brief-dependent gates pass with note "no brief". ``advisory``
    is set from ``ADVISORY_GATES``, never the judge.
    """
    reported: dict[str, GateResultIn | GateResult] = {}
    for gate in raw:
        key = "_".join(gate.gate.strip().lower().replace("-", " ").split())
        reported.setdefault(key, gate)
    if not any(name in reported for name in expected):
        logger.warning("judge reported none of the expected gates %s", expected)
        return [GateResult(gate=NO_GATES_GATE, passed=False, note=NO_GATES_NOTE)]
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
                    gate=name, passed=True, note=NOT_REPORTED_NOTE, advisory=advisory
                )
            )
    return out


def score_from_judge(
    judged: JudgeScore,
    expected_gates: tuple[str, ...],
    threshold: float,
    *,
    brief_used: bool,
) -> CreativeScore:
    """Map the judge's raw score to the report's ``CreativeScore`` (pure).

    The model's strengths/improvements text is kept; overall_score, gates,
    gates_passed and passed are computed here from its verdicts and gates
    (the judge schema does not even carry them).
    """
    gates = normalize_gates(judged.gates, expected_gates, brief_used=brief_used)
    score = _score_from_verdicts(judged.verdicts, threshold, gates)
    score.strengths = list(judged.strengths)
    score.improvements = list(judged.improvements)
    return score


def _is_failed(evaluation: AdCopyEvaluation | VisualConceptEvaluation) -> bool:
    """True for the zero-score placeholder of a failed judge call."""
    return "evaluation_failed" in evaluation.score.improvements


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


_IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def image_mime_type(gcs_uri: str) -> str:
    """The image MIME type from the URI's extension (default ``image/png``)."""
    path = gcs_uri.lower()
    for ext, mime in _IMAGE_MIME_BY_EXT.items():
        if path.endswith(ext):
            return mime
    return "image/png"


def image_qa_hint(qa: Any) -> str:
    """One line summarising the image-QA verdict (``generated_images[…].qa``).

    A hint for the judge, never ground truth. ``None`` (QA disabled or
    unavailable) → "not run".
    """
    if not isinstance(qa, Mapping):
        return "Automated image check: not run"
    if qa.get("passed"):
        return "Automated image check: passed"
    failures = "; ".join(" ".join(str(f).split()) for f in qa.get("failures") or [])
    return "Automated image check: failed" + (f" — {failures}" if failures else "")


def _rendered_image_uri(image: Any) -> str:
    """The record's ``gs://`` URI, or "" when there is no usable rendered image."""
    uri = image.get("gcs_uri") if isinstance(image, Mapping) else None
    return uri if isinstance(uri, str) and uri.startswith("gs://") else ""


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

    angle_id = str(ad_copy.get("angle_id") or "")
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
                response_schema=AdCopyJudgeOutput,
                temperature=0.3,
            ),
        )

        judged = AdCopyJudgeOutput.model_validate_json(response.text or "")
        return AdCopyEvaluation(
            original_id=judged.original_id,
            headline=judged.headline,
            tone_style=judged.tone_style,
            angle_id=angle_id,
            score=score_from_judge(
                judged.score,
                AD_COPY_GATES,
                config.passing_threshold,
                brief_used=brief is not None,
            ),
        )

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
            angle_id=angle_id,
        )


def evaluate_visual_concept(
    visual_concept: dict,
    campaign_context: dict,
    config: EvalConfig,
    client: genai.Client | None = None,
    brief: Mapping[str, Any] | None = None,
    image: Mapping[str, Any] | None = None,
) -> VisualConceptEvaluation:
    """Evaluate a single visual concept using Gemini-as-judge.

    With a rendered image (``image["gcs_uri"]``, a ``generated_images``
    record) the judge sees the pixels as a ``gs://`` Part — Vertex fetches it
    server-side as the project's Vertex AI service agent (project
    permissions), not with the caller's credentials — and judges the image, using the prompt only for
    intent; the record's image-QA verdict is passed as a hint line. Without
    one it judges the image generation prompt and the result records
    ``image_judged=False``.

    Args:
        visual_concept: Dict with VisualConceptFinal fields.
        campaign_context: Dict with brand, target_product, target_audience,
                          key_selling_points, target_search_trend.
        config: Evaluation configuration.
        client: Optional pre-created Gemini client.
        brief: The parsed creative brief (``brief.parse_brief``), or None.
        image: The concept's ``generated_images`` record, or None.

    Returns:
        VisualConceptEvaluation with per-dimension scores and binary gates.
    """
    if client is None:
        client = _get_client(config)

    name = visual_concept.get("concept_name", "?")
    image_uri = _rendered_image_uri(image)
    # Recorded on the evaluation by code (brand history reads them).
    tags = {
        "visual_style": str(visual_concept.get("visual_style") or ""),
        "angle_id": str(visual_concept.get("angle_id") or ""),
    }

    def judge(uri: str) -> VisualConceptEvaluation:
        """One judge call: with the rendered image at ``uri``, or prompt-only."""
        image_section = (
            prompts.VISUAL_IMAGE_ATTACHED.format(
                qa_hint=image_qa_hint((image or {}).get("qa"))
            )
            if uri
            else prompts.VISUAL_IMAGE_MISSING
        )
        user_prompt = prompts.VISUAL_CONCEPT_EVAL_USER.format(
            **campaign_context,
            **{
                **visual_concept,
                "aspect_ratio": visual_concept.get("aspect_ratio") or "unspecified",
                "trend_motif": visual_concept.get("trend_motif") or "(none)",
                "brand_cue": visual_concept.get("brand_cue") or "(none)",
            },
            brief_block=format_brief_for_judge(brief),
            image_section=image_section,
        )
        contents: genai.types.ContentListUnionDict = user_prompt
        if uri:
            contents = [
                genai.types.Part.from_uri(file_uri=uri, mime_type=image_mime_type(uri)),
                genai.types.Part.from_text(text=user_prompt),
            ]
        response = client.models.generate_content(
            model=config.eval_model,
            contents=contents,
            config=genai.types.GenerateContentConfig(
                system_instruction=prompts.VISUAL_CONCEPT_EVAL_SYSTEM,
                response_mime_type="application/json",
                response_schema=VisualJudgeOutput,
                temperature=0.3,
            ),
        )
        judged = VisualJudgeOutput.model_validate_json(response.text or "")
        return VisualConceptEvaluation(
            ad_copy_id=judged.ad_copy_id,
            concept_name=judged.concept_name,
            score=score_from_judge(
                judged.score,
                VISUAL_GATES,
                config.passing_threshold,
                brief_used=brief is not None,
            ),
            image_judged=bool(uri),
            **tags,
        )

    if not image_uri:
        logger.info("No rendered image for visual concept %r; judging its prompt", name)
    try:
        try:
            return judge(image_uri)
        except genai_errors.ClientError as e:
            # An image-related 4xx (unreadable / missing object, unsupported
            # file — see is_image_fallback_error) must not zero the creative:
            # fail soft to a prompt-only verdict (image_judged=False). Any
            # other client error (quota, a bad request unrelated to the
            # image) propagates to the normal judge-failure path.
            if not image_uri or not is_image_fallback_error(e):
                raise
            logger.warning(
                "Judge could not use the rendered image %s for %r (%s); "
                "judging the prompt instead",
                image_uri,
                name,
                e,
            )
            return judge("")
    except Exception as e:
        logger.error(f"Failed to evaluate visual concept {name}: {e}")
        return VisualConceptEvaluation(
            ad_copy_id=visual_concept.get("ad_copy_id", 0),
            concept_name=visual_concept.get("concept_name", ""),
            score=_failed_score(),
            **tags,
        )


_IMAGE_ERROR_HINTS = ("image", "uri", "file", "permission")


def is_image_fallback_error(error: genai_errors.ClientError) -> bool:
    """True when a judge 4xx is about the attached image (pure).

    403/404 always (the object is unreadable or missing); a 400 only when its
    message mentions the image / URI / file / permission. Everything else
    (429 quota, 401, an unrelated 400) is a normal judge failure.
    """
    if error.code in (403, 404):
        return True
    if error.code != 400:
        return False
    message = f"{error.message or ''} {error.status or ''}".lower()
    return any(hint in message for hint in _IMAGE_ERROR_HINTS)


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
        and _rendered_image_uri(images.get(e.concept_name))
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


def unreported_gates_warning(
    ad_evals: list[AdCopyEvaluation],
    visual_evals: list[VisualConceptEvaluation],
) -> list[str]:
    """The report ``warnings`` entry counting gates the judge left out ([] if none).

    Such gates pass as "not checked" (see :func:`normalize_gates`); the
    warning keeps that leniency visible.
    """
    count = sum(
        g.note == NOT_REPORTED_NOTE
        for e in [*ad_evals, *visual_evals]
        for g in e.score.gates
    )
    if not count:
        return []
    noun = "check" if count == 1 else "checks"
    return [f"{count} {noun} not reported by the judge (passed as not checked)"]


def judge_warnings(
    ad_evals: list[AdCopyEvaluation],
    visual_evals: list[VisualConceptEvaluation],
    generated_images: Mapping[str, Any] | None,
) -> list[str]:
    """Every judge-side report warning (image fallback + unreported gates)."""
    return image_fallback_warning(
        image_fallback_concepts(visual_evals, generated_images)
    ) + unreported_gates_warning(ad_evals, visual_evals)


def evaluate_all_concurrently(
    ad_copies: list[dict],
    visual_concepts: list[dict],
    campaign_context: dict,
    config: EvalConfig,
    client: genai.Client | None = None,
    *,
    brief: Mapping[str, Any] | None = None,
    generated_images: Mapping[str, Any] | None = None,
) -> tuple[list[AdCopyEvaluation], list[VisualConceptEvaluation]]:
    """Evaluate all ad copies and visual concepts concurrently, preserving order.

    Each creative is judged by an independent Gemini call, so running them in a
    thread pool collapses eval wall-clock from ~N*28s (sequential) to roughly a
    single call's latency. Results are returned in the same order as the inputs.
    Individual failures are already handled inside evaluate_ad_copy /
    evaluate_visual_concept (they return a zero-score evaluation), so a single
    bad creative can't sink the whole batch. Each visual concept gets its own
    ``generated_images[concept_name]`` record (None when it has no image).
    """
    if client is None:
        client = _get_client(config)

    total = len(ad_copies) + len(visual_concepts)
    if total == 0:
        return [], []

    images = generated_images if isinstance(generated_images, Mapping) else {}
    max_workers = config.workers_for(len(ad_copies), len(visual_concepts))
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
                image=images.get(vc.get("concept_name", "")),
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


def evaluate_creatives(
    campaign_context: dict,
    ad_copies: list[dict],
    visual_concepts: list[dict],
    config: EvalConfig | None = None,
    *,
    brief: Mapping[str, Any] | None = None,
    generated_images: Mapping[str, Any] | None = None,
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

    Returns:
        CreativeEvaluationReport with per-creative and aggregate scores.
    """
    if config is None:
        config = EvalConfig()

    client = _get_client(config)

    logger.info(
        f"Evaluating {len(ad_copies)} ad copies and {len(visual_concepts)} visual "
        f"concepts concurrently (max {config.workers_for(len(ad_copies), len(visual_concepts))} workers)..."
    )

    ad_evals, visual_evals = evaluate_all_concurrently(
        ad_copies,
        visual_concepts,
        campaign_context,
        config,
        client,
        brief=brief,
        generated_images=generated_images,
    )

    summary = _build_summary(ad_evals, visual_evals)

    report = CreativeEvaluationReport(
        brand=campaign_context["brand"],
        target_product=campaign_context["target_product"],
        target_search_trend=campaign_context["target_search_trend"],
        ad_copy_evaluations=ad_evals,
        visual_concept_evaluations=visual_evals,
        summary=summary,
        warnings=judge_warnings(ad_evals, visual_evals, generated_images),
        judge_model=config.eval_model,
        judge_version=JUDGE_VERSION,
        passing_threshold=config.passing_threshold,
        brief_used=brief is not None,
    )

    logger.info(
        f"Evaluation complete: {summary.ad_copies_passed}/{summary.total_ad_copies} ad copies passed, "
        f"{summary.visual_concepts_passed}/{summary.total_visual_concepts} visual concepts passed. "
        f"Overall pass rate: {summary.overall_pass_rate:.1%}"
    )

    return report
