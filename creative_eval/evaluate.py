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
import os
import re
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
    PERSON_GATES,
    SAFER_ANGLE_GATES,
    TIMELY_GATES,
    TREND_GATES,
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
# An advisory gate the judge left out: recorded as passed (it never blocks).
NOT_REPORTED_NOTE = "not checked (the judge did not report it)"
# A blocking gate the judge had to decide but left out: failed (unverified).
NOT_CHECKED_NOTE = "not checked"
NO_GATES_NOTE = "judge returned no gates"
NO_PERSON_CAST_NOTE = "no person cast"
# Note on a TREND_GATES gate when the run has no target trend.
NO_TREND_NOTE = "no trend"
# Note on a TIMELY_GATES gate for a creative that does not reference a
# validated timely moment.
NO_TIMELY_NOTE = "not timely"
# Note on a SAFER_ANGLE_GATES gate on a run without a safer angle.
NO_SAFER_ANGLE_NOTE = "no safer angle"


def safer_angle_active(state: Mapping[str, Any]) -> bool:
    """True on a safer-angle run: ``state["safer_angle_active"] == "yes"`` (pure).

    The user deliberately picked a sensitive trend and opted in to a
    respectful, positive angle on its subject; anything else is inactive.
    """
    return state.get("safer_angle_active") == "yes"


def safer_angle_reason(state: Mapping[str, Any]) -> str:
    """The run's ``safer_angle_sensitive_reason`` ("" unless a safer-angle run)."""
    if not safer_angle_active(state):
        return ""
    reason = state.get("safer_angle_sensitive_reason")
    return reason.strip() if isinstance(reason, str) else ""


def normalize_gates(
    raw: Sequence[GateResultIn | GateResult],
    expected: tuple[str, ...],
    *,
    brief_used: bool,
    person_cast: bool = False,
    trend_given: bool = True,
    timely: bool = False,
    safer_angle: bool = False,
) -> list[GateResult]:
    """The judge's gates, reduced to exactly ``expected`` in order (pure).

    Names are matched leniently (case, spaces and hyphens ignored); unknown
    names are dropped and duplicates keep the first. Only the gates the judge
    HAD to decide count as reported: blocking (not ``ADVISORY_GATES``) and not
    decided in code (below). When NONE of those was reported the judge
    skipped the checks: the result is a single failed ``NO_GATES_GATE`` gate
    (note "judge returned no gates"), so the creative cannot pass unverified
    (an advisory or code-decided gate alone never counts). A partial omission
    of such a gate fails it as "not checked" (unverified, counted into a
    report warning by :func:`unreported_gates_warning`); an omitted advisory
    gate passes with a "not checked (the judge did not report it)" note. Without a
    brief the brief-dependent gates pass with note "no brief", and unless
    ``person_cast`` the ``PERSON_GATES`` pass with note "no person cast";
    without a target trend (``trend_given`` False) the ``TREND_GATES`` pass
    with note "no trend" (decided in code, never the judge; they need no
    brief, so a missing brief never passes them). Unless ``timely`` (the
    creative references a validated timely moment, :func:`creative_is_timely`)
    the ``TIMELY_GATES`` pass with note "not timely", and unless
    ``safer_angle`` (a safer-angle run, :func:`safer_angle_active`) the
    ``SAFER_ANGLE_GATES`` pass with note "no safer angle". A person gate
    omitted for a cast concept, a timely gate omitted for a timely creative,
    or a safer-angle gate omitted on a safer-angle run, fails as "not checked"
    (unverified) like any other blocking gate. ``advisory`` is set from
    ``ADVISORY_GATES``, never the judge.
    """
    reported: dict[str, GateResultIn | GateResult] = {}
    for gate in raw:
        reported.setdefault(_norm_name(gate.gate), gate)

    def code_note(name: str) -> str | None:
        """The note of a gate decided in code (passed), else None."""
        if name in BRIEF_GATES and not brief_used:
            return NO_BRIEF_NOTE
        if name in PERSON_GATES and not person_cast:
            return NO_PERSON_CAST_NOTE
        if name in TREND_GATES and not trend_given:
            return NO_TREND_NOTE
        if name in TIMELY_GATES and not timely:
            return NO_TIMELY_NOTE
        if name in SAFER_ANGLE_GATES and not safer_angle:
            return NO_SAFER_ANGLE_NOTE
        return None

    required = [
        name
        for name in expected
        if name not in ADVISORY_GATES and code_note(name) is None
    ]
    if required and not any(name in reported for name in required):
        logger.warning("judge reported none of the required gates %s", required)
        return [GateResult(gate=NO_GATES_GATE, passed=False, note=NO_GATES_NOTE)]
    out = []
    for name in expected:
        advisory = name in ADVISORY_GATES
        note = code_note(name)
        if note is not None:
            out.append(GateResult(gate=name, passed=True, note=note))
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
        elif advisory:
            logger.warning("judge did not report advisory gate %r", name)
            out.append(
                GateResult(
                    gate=name, passed=True, note=NOT_REPORTED_NOTE, advisory=True
                )
            )
        else:
            # A gate the judge had to decide: an omission fails (unverified).
            logger.warning("judge did not report required gate %r", name)
            out.append(GateResult(gate=name, passed=False, note=NOT_CHECKED_NOTE))
    return out


def _norm_name(name: str) -> str:
    """A judge-reported gate/dimension name, normalised for lenient matching."""
    return "_".join(name.strip().lower().replace("-", " ").split())


def normalize_verdicts(
    verdicts: Sequence[EvalVerdict], expected: Sequence[str]
) -> tuple[list[EvalVerdict], list[str]]:
    """The judge's verdicts reduced to ``expected`` in order, plus the missing (pure).

    Mirrors :func:`normalize_gates`: names are matched leniently (case,
    spaces and hyphens ignored) and rewritten to the expected name, unknown
    dimensions are dropped and duplicates keep the first. Returns
    ``(verdicts, missing)``; any missing dimension makes the score unverified
    (:func:`score_from_judge` fails ``passed``).
    """
    reported: dict[str, EvalVerdict] = {}
    for verdict in verdicts:
        reported.setdefault(_norm_name(verdict.dimension), verdict)
    kept = [
        reported[name].model_copy(update={"dimension": name})
        for name in expected
        if name in reported
    ]
    missing = [name for name in expected if name not in reported]
    if missing:
        logger.warning("judge did not score dimensions %s", missing)
    return kept, missing


def score_from_judge(
    judged: JudgeScore,
    expected_gates: tuple[str, ...],
    threshold: float,
    *,
    expected_dimensions: Sequence[str],
    brief_used: bool,
    person_cast: bool = False,
    trend_given: bool = True,
    timely: bool = False,
    safer_angle: bool = False,
) -> CreativeScore:
    """Map the judge's raw score to the report's ``CreativeScore`` (pure).

    The model's strengths/improvements text is kept; overall_score, gates,
    gates_passed and passed are computed here from its verdicts reduced to
    ``expected_dimensions`` (:func:`normalize_verdicts`) and its gates
    (:func:`normalize_gates`; the judge schema does not even carry them). A
    dimension the judge did not score is listed in ``unscored_dimensions``
    and the creative does not pass (the mean covers only the scored ones).
    """
    gates = normalize_gates(
        judged.gates,
        expected_gates,
        brief_used=brief_used,
        person_cast=person_cast,
        trend_given=trend_given,
        timely=timely,
        safer_angle=safer_angle,
    )
    verdicts, missing = normalize_verdicts(judged.verdicts, expected_dimensions)
    score = _score_from_verdicts(verdicts, threshold, gates)
    score.strengths = list(judged.strengths)
    score.improvements = list(judged.improvements)
    if missing:
        score.unscored_dimensions = missing
        score.passed = False
    return score


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


def _gs_uri(image: Any) -> str:
    """The record's ``gs://`` URI (any bucket), or "" when it has none."""
    uri = image.get("gcs_uri") if isinstance(image, Mapping) else None
    return uri.strip() if isinstance(uri, str) and uri.startswith("gs://") else ""


def rendered_image_uri(image: Any) -> str:
    """The record's ``gs://`` URI when the judge may attach it, else "".

    Vertex fetches the attached object as the project's Vertex AI service
    agent, so (like the person photo and the ratings report) only an object
    directly under ``gs://$GOOGLE_CLOUD_STORAGE_BUCKET/`` is attached; a URI
    with ``..`` or in another bucket is refused (the judge then judges the
    prompt and the report warns, :func:`foreign_image_concepts`). Without
    ``GOOGLE_CLOUD_STORAGE_BUCKET`` (local runs) any ``gs://`` object is
    accepted, as for :func:`person_photo_uri`.
    """
    uri = _gs_uri(image)
    if not uri or ".." in uri:
        return ""
    bucket = (os.environ.get("GOOGLE_CLOUD_STORAGE_BUCKET") or "").strip()
    if bucket and not uri.startswith(f"gs://{bucket}/"):
        return ""
    return uri


# The engine's person-reference shape (``creative_agent.person_render``
# ``person_reference_uri``; creative_eval never imports creative_agent, so the
# rule is mirrored here and parity-tested).
_PERSON_PHOTO_RE = re.compile(r"^gs://([^/]+)/person-refs/[^/]+/[^/]+$")


def person_photo_uri(person_reference: Any) -> str:
    """The consented person's photo URI from ``state["person_reference"]``.

    ``""`` unless it is a ``{uri, ...}`` mapping whose URI sits under
    ``gs://<bucket>/person-refs/<slug>/`` and, when ``GOOGLE_CLOUD_STORAGE_BUCKET``
    is set, in that bucket (like ``image_tools``' person fetch). Never logged.
    """
    if not isinstance(person_reference, dict):
        return ""
    uri = person_reference.get("uri")
    if not isinstance(uri, str) or ".." in uri:
        return ""
    uri = uri.strip()
    match = _PERSON_PHOTO_RE.match(uri)
    if not match:
        return ""
    bucket = (os.environ.get("GOOGLE_CLOUD_STORAGE_BUCKET") or "").strip()
    if bucket and match.group(1) != bucket:
        return ""
    return uri


def _redact(text: str, secret: str) -> str:
    """``text`` with ``secret`` (a person photo URI) masked, for log lines."""
    return text.replace(secret, "<person photo>") if secret else text


def moment_is_timely(trend_situation: Any) -> bool:
    """True when ``state["trend_situation"]`` is a validated timely moment (pure).

    ``timely_ok is True``: the same rule as the engine's guards
    (``creative_agent.trend_situation.timely_allowed``, mirrored with plain
    dict keys so creative_eval never imports creative_agent; parity-tested
    via :func:`run_is_timely`). A missing ``valid_until`` does not switch the
    check off: a creative the guards treated as timely is always judged for
    ``timely_claim_supported`` (the validator always sets one; the block then
    just shows it empty). Pass the run's moment via :func:`run_trend_situation`.
    """
    return (
        isinstance(trend_situation, Mapping)
        and trend_situation.get("timely_ok") is True
    )


def run_trend_situation(state: Mapping[str, Any]) -> Any:
    """The run's ``trend_situation`` for the judge, or None (pure).

    None when trend_scout flagged the trend sensitive
    (``state["trend_sensitive_hint"] == "yes"``): like the engine's
    ``timely_allowed``, such a run never has a timely moment.
    """
    if state.get("trend_sensitive_hint") == "yes":
        return None
    return state.get("trend_situation")


def run_is_timely(state: Mapping[str, Any]) -> bool:
    """True when the run has a timely moment (:func:`moment_is_timely` of
    :func:`run_trend_situation`); parity-tested against
    ``creative_agent.trend_situation.timely_allowed(state, 1)``."""
    return moment_is_timely(run_trend_situation(state))


def creative_is_timely(
    creative: Any, trend_situation: Any, *, paired_copy_timely: bool = False
) -> bool:
    """True when the judge must check this creative's timely claim (pure).

    The creative's own ``uses_trend_state is True`` (or, for a visual concept,
    its paired ad copy's: ``paired_copy_timely``) AND a validated timely moment
    (:func:`moment_is_timely`).
    """
    flagged = isinstance(creative, Mapping) and creative.get("uses_trend_state") is True
    return (flagged or paired_copy_timely) and moment_is_timely(trend_situation)


def timely_copy_ids(ad_copies: Sequence[Any]) -> frozenset[str]:
    """``original_id`` (as str) of every ad copy flagged ``uses_trend_state``.

    A visual concept pairs with its copy via ``ad_copy_id == original_id``
    (compared as strings, like ``concept_guard``).
    """
    return frozenset(
        str(ac.get("original_id"))
        for ac in ad_copies
        if isinstance(ac, Mapping)
        and ac.get("uses_trend_state") is True
        and ac.get("original_id") is not None
    )


def paired_copy_timely(concept: Any, copy_ids: frozenset[str]) -> bool:
    """True when ``concept``'s paired copy is in :func:`timely_copy_ids` (pure)."""
    if not isinstance(concept, Mapping) or concept.get("ad_copy_id") is None:
        return False
    return str(concept.get("ad_copy_id")) in copy_ids


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def moment_block(trend_situation: Any, sources: Any = None) -> str:
    """The judge's ``Current moment`` block for a timely creative (pure).

    Built from the validated ``trend_situation`` dict keys (phase, event,
    dates, outcome, as_of, valid_until, tone_rule) plus the URLs of its cited
    sources from ``state["sources"]`` (``{id: {"url": ...}}``; ids without a
    URL are left out). "" when the moment is not timely.
    """
    if not moment_is_timely(trend_situation):
        return ""
    ts: Mapping[str, Any] = trend_situation
    rows = [
        ("Phase", ts.get("phase")),
        ("Event", ts.get("event_name")),
        ("Event start", ts.get("event_start")),
        ("Event end", ts.get("event_end")),
        ("Outcome", ts.get("outcome")),
        ("Outcome summary", ts.get("outcome_summary")),
        ("As of", ts.get("as_of")),
        ("Valid until", ts.get("valid_until")),
        ("Tone rule", ts.get("tone_rule")),
    ]
    lines = [
        "Current moment (validated from research; the only facts a timely "
        "claim may state):"
    ]
    lines += [f"{label}: {_one_line(value)}" for label, value in rows]
    by_id = sources if isinstance(sources, Mapping) else {}
    urls = []
    for source_id in ts.get("source_ids") or []:
        record = by_id.get(source_id) if isinstance(source_id, str) else None
        url = record.get("url") if isinstance(record, Mapping) else None
        if isinstance(url, str) and url.strip():
            urls.append(_one_line(url))
    lines.append(f"Sources: {', '.join(urls) if urls else '(none listed)'}")
    return "\n".join(lines)


def _moment_section(timely: bool, trend_situation: Any, sources: Any) -> str:
    """The prompt's ``moment_section``: the moment block for a timely creative."""
    block = moment_block(trend_situation, sources) if timely else ""
    return f"<CURRENT_MOMENT>\n{fence(block)}\n</CURRENT_MOMENT>\n\n" if block else ""


def trend_valid_until(trend_situation: Any) -> str:
    """The report's ``trend_valid_until``: the timely moment's expiry, else ""."""
    if not moment_is_timely(trend_situation):
        return ""
    return str(trend_situation.get("valid_until") or "").strip()


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
            judge_failed=False,
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
        judge_failed=False,
    )


def fence(value: Any) -> str:
    """``value`` as prompt text with its angle brackets neutralised (pure).

    Creative, brief, campaign, research and QA text is untrusted (users edit
    it, the web supplies it), so ``<`` / ``>`` become ``‹`` / ``›``: a value
    can never close the prompt's data tags or open a fake ``<GATES>`` block.
    None → "".
    """
    text = "" if value is None else str(value)
    return text.replace("<", "‹").replace(">", "›")


# The prompt fields filled from the campaign context and from the creative
# itself, each fenced and defaulted to "" (never ``**creative``: an extra key
# would collide with a code-set placeholder, a missing one would KeyError).
CAMPAIGN_PROMPT_FIELDS: tuple[str, ...] = (
    "brand",
    "target_product",
    "target_audience",
    "key_selling_points",
    "target_search_trend",
)
AD_COPY_PROMPT_FIELDS: tuple[str, ...] = (
    "headline",
    "body_text",
    "tone_style",
    "trend_connection",
    "audience_appeal_rationale",
    "social_caption",
    "call_to_action",
    "detailed_performance_rationale",
)
VISUAL_PROMPT_FIELDS: tuple[str, ...] = (
    "concept_name",
    "visual_style",
    "aspect_ratio",
    "trend",
    "trend_reference",
    "trend_motif",
    "brand_cue",
    "markets_product",
    "audience_appeal",
    "selection_rationale",
    "headline",
    "social_caption",
    "call_to_action",
    "concept_summary",
    "image_generation_prompt",
)
# Defaults for empty visual fields (the judge reads "(none)", not a blank).
_VISUAL_FIELD_DEFAULTS = {
    "aspect_ratio": "unspecified",
    "trend_motif": "(none)",
    "brand_cue": "(none)",
}
# Placeholders set by code, never from the creative.
_AD_COPY_RESERVED = frozenset(
    {"angle", "brief_block", "timely", "safer_angle", "moment_section"}
)
_VISUAL_RESERVED = frozenset(
    {
        "brief_block",
        "image_section",
        "person_cast",
        "timely",
        "safer_angle",
        "moment_section",
    }
)


def ad_copy_prompt_reserved_keys() -> set[str]:
    """The ad-copy prompt's code-set placeholders (no creative field may use one)."""
    return set(_AD_COPY_RESERVED)


def visual_prompt_reserved_keys() -> set[str]:
    """The visual prompt's code-set placeholders (no creative field may use one)."""
    return set(_VISUAL_RESERVED)


def _prompt_fields(
    source: Mapping[str, Any],
    names: Sequence[str],
    defaults: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """``{name: fenced value}`` for ``names`` from ``source`` (missing → default/"")."""
    defaults = defaults or {}
    out = {}
    for name in names:
        value = source.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            value = defaults.get(name, "")
        out[name] = fence(value)
    return out


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

    angle_id = str(ad_copy.get("angle_id") or "")
    timely = creative_is_timely(ad_copy, trend_situation)
    # Build the prompt
    # Join keys and labels come from the input copy, never the judge's echo.
    ids = {
        "original_id": ad_copy.get("original_id", 0),
        "headline": str(ad_copy.get("headline") or ""),
        "tone_style": str(ad_copy.get("tone_style") or ""),
    }
    user_prompt = prompts.AD_COPY_EVAL_USER.format(
        **_prompt_fields(campaign_context, CAMPAIGN_PROMPT_FIELDS),
        **_prompt_fields(ad_copy, AD_COPY_PROMPT_FIELDS),
        angle=fence(angle_line(brief, ad_copy.get("angle_id") or "")),
        brief_block=fence(
            format_brief_for_judge(
                brief, safer_angle=safer_angle, sensitive_reason=sensitive_reason
            )
        ),
        timely="yes" if timely else "no",
        safer_angle="yes" if safer_angle else "no",
        moment_section=_moment_section(timely, trend_situation, sources),
    )

    try:
        response = client.models.generate_content(
            model=config.eval_model,
            contents=user_prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=prompts.AD_COPY_EVAL_SYSTEM,
                response_mime_type="application/json",
                response_schema=AdCopyJudgeOutput,
                temperature=config.judge_temperature,
            ),
        )

        judged = AdCopyJudgeOutput.model_validate_json(response.text or "")
        return AdCopyEvaluation(
            **ids,
            angle_id=angle_id,
            score=score_from_judge(
                judged.score,
                AD_COPY_GATES,
                config.passing_threshold,
                expected_dimensions=config.ad_copy_dimensions,
                brief_used=brief is not None,
                trend_given=bool(
                    str(campaign_context.get("target_search_trend") or "").strip()
                ),
                timely=timely,
                safer_angle=safer_angle,
            ),
            timely=timely,
        )

    except Exception as e:
        logger.error(
            f"Failed to evaluate ad copy {ad_copy.get('original_id', '?')}: {e}"
        )
        # Return a zero-score evaluation on failure
        return AdCopyEvaluation(
            **ids,
            score=_failed_score(),
            angle_id=angle_id,
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
    permissions), not with the caller's credentials — and judges the image, using the prompt only for
    intent; the record's image-QA verdict is passed as a hint line. Without
    one it judges the image generation prompt and the result records
    ``image_judged=False``.

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
    # Join keys come from the input concept, never the judge's echo.
    ids = {
        "ad_copy_id": visual_concept.get("ad_copy_id", 0),
        "concept_name": str(visual_concept.get("concept_name") or ""),
    }
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
    # Recorded on the evaluation by code (brand history reads them).
    tags = {
        "visual_style": str(visual_concept.get("visual_style") or ""),
        "angle_id": str(visual_concept.get("angle_id") or ""),
    }

    def judge(uri: str, photo_uri: str = "") -> VisualConceptEvaluation:
        """One judge call: with the rendered image at ``uri`` (plus the cast
        person's reference photo at ``photo_uri``), or prompt-only."""
        image_section = (
            prompts.VISUAL_IMAGE_ATTACHED.format(
                qa_hint=fence(image_qa_hint((image or {}).get("qa")))
            )
            if uri
            else prompts.VISUAL_IMAGE_MISSING
        )
        if uri and photo_uri:
            image_section += "\n" + prompts.VISUAL_PERSON_PHOTO_ATTACHED
        user_prompt = prompts.VISUAL_CONCEPT_EVAL_USER.format(
            **_prompt_fields(campaign_context, CAMPAIGN_PROMPT_FIELDS),
            **_prompt_fields(
                visual_concept, VISUAL_PROMPT_FIELDS, _VISUAL_FIELD_DEFAULTS
            ),
            person_cast="yes" if person_cast else "no",
            timely="yes" if timely else "no",
            safer_angle="yes" if safer_angle else "no",
            brief_block=fence(
                format_brief_for_judge(
                    brief, safer_angle=safer_angle, sensitive_reason=sensitive_reason
                )
            ),
            image_section=image_section,
            moment_section=moment_section,
        )
        contents: genai.types.ContentListUnionDict = user_prompt
        if uri:
            contents = [
                genai.types.Part.from_uri(file_uri=uri, mime_type=image_mime_type(uri)),
            ]
            if photo_uri:
                contents += [
                    genai.types.Part.from_text(text=prompts.PERSON_PHOTO_LABEL),
                    genai.types.Part.from_uri(
                        file_uri=photo_uri, mime_type=image_mime_type(photo_uri)
                    ),
                ]
            contents.append(genai.types.Part.from_text(text=user_prompt))
        response = client.models.generate_content(
            model=config.eval_model,
            contents=contents,
            config=genai.types.GenerateContentConfig(
                system_instruction=prompts.VISUAL_CONCEPT_EVAL_SYSTEM,
                response_mime_type="application/json",
                response_schema=VisualJudgeOutput,
                temperature=config.judge_temperature,
            ),
        )
        judged = VisualJudgeOutput.model_validate_json(response.text or "")
        return VisualConceptEvaluation(
            **ids,
            score=score_from_judge(
                judged.score,
                VISUAL_GATES,
                config.passing_threshold,
                expected_dimensions=config.visual_dimensions,
                brief_used=brief is not None,
                person_cast=person_cast,
                timely=timely,
                safer_angle=safer_angle,
            ),
            image_judged=bool(uri),
            timely=timely,
            **tags,
        )

    if not image_uri:
        logger.info("No rendered image for visual concept %r; judging its prompt", name)
    try:
        try:
            if photo:
                try:
                    return judge(image_uri, photo)
                except genai_errors.ClientError as e:
                    if not is_image_fallback_error(e):
                        raise
                    # The error text may name the photo: log only the code.
                    logger.warning(
                        "Judge could not use the person reference photo for %r "
                        "(HTTP %s); judging the rendered image without it",
                        name,
                        e.code,
                    )
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
        logger.error(
            "Failed to evaluate visual concept %s: %s", name, _redact(str(e), photo)
        )
        return VisualConceptEvaluation(
            **ids,
            score=_failed_score(),
            timely=timely,
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


def _count_line(count: int, kind: str, outcome: str) -> str:
    noun = "check" if count == 1 else "checks"
    return f"{count} {kind}{noun} not reported by the judge ({outcome} as not checked)"


def unreported_gates_warning(
    ad_evals: list[AdCopyEvaluation],
    visual_evals: list[VisualConceptEvaluation],
) -> list[str]:
    """The report ``warnings`` entries counting gates the judge left out ([] if none).

    An omitted advisory gate passes as "not checked" (see
    :func:`normalize_gates`); the first entry keeps that visible. An omitted
    gate the judge had to decide fails as "not checked": person, timely and
    safer-angle gates get their own entries, every other gate one shared
    entry.
    """
    gates = [g for e in [*ad_evals, *visual_evals] for g in e.score.gates]
    out = []
    passed = sum(g.note == NOT_REPORTED_NOTE for g in gates)
    if passed:
        out.append(_count_line(passed, "", "passed"))
    unchecked = [g for g in gates if g.note == NOT_CHECKED_NOTE and not g.passed]
    special = PERSON_GATES | TIMELY_GATES | SAFER_ANGLE_GATES
    other = sum(g.gate not in special for g in unchecked)
    if other:
        out.append(_count_line(other, "", "failed"))
    for kind, names in (
        ("person ", PERSON_GATES),
        ("timely ", TIMELY_GATES),
        ("safer-angle ", SAFER_ANGLE_GATES),
    ):
        count = sum(g.gate in names for g in unchecked)
        if count:
            out.append(_count_line(count, kind, "failed"))
    return out


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
        trend_valid_until=trend_valid_until(trend_situation),
    )

    logger.info(
        f"Evaluation complete: {summary.ad_copies_passed}/{summary.total_ad_copies} ad copies passed, "
        f"{summary.visual_concepts_passed}/{summary.total_visual_concepts} visual concepts passed. "
        f"Overall pass rate: {summary.overall_pass_rate:.1%}"
    )

    return report
