"""The judge call: genai client, prompt/request builders and image fallbacks.

Every interpolated value is fenced (:func:`fence`); the rendered image and the
cast person's photo are attached as ``gs://`` Parts only from the run's bucket
(:func:`rendered_image_uri`, :func:`person_photo_uri`), and an image-related
4xx degrades to a prompt-only verdict (:func:`_judge_with_image_fallbacks`).
"""

import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from google import genai
from google.genai import errors as genai_errors

from agent_common import genai_retry
from agent_common.genai_retry import build_genai_http_retry

from . import prompts
from .brief import angle_line, format_brief_for_judge
from .config import EvalConfig
from .gates import moment_is_timely
from .schemas import (
    AdCopyJudgeOutput,
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
    lines = [prompts.MOMENT_HEADER]
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
    return prompts.MOMENT_SECTION.format(block=fence(block)) if block else ""


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
    "composition_mode",
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
    "composition_mode": "product_in_scene",
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


def _brief_block(
    brief: Mapping[str, Any] | None, safer_angle: bool, sensitive_reason: str
) -> str:
    """The fenced ``<CREATIVE_BRIEF>`` block both judges read."""
    return fence(
        format_brief_for_judge(
            brief, safer_angle=safer_angle, sensitive_reason=sensitive_reason
        )
    )


def _yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def _call_judge(
    client: genai.Client,
    config: EvalConfig,
    contents: genai.types.ContentListUnionDict,
    system: str,
    schema: type[AdCopyJudgeOutput] | type[VisualJudgeOutput],
) -> JudgeScore:
    """One structured judge call → the judge's raw :class:`JudgeScore`.

    Raises on a transport error or an unparseable response (the callers turn
    that into a failed-judge placeholder).
    """
    response = client.models.generate_content(
        model=config.eval_model,
        contents=contents,
        config=genai.types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=config.judge_temperature,
        ),
    )
    return schema.model_validate_json(response.text or "").score


def _build_ad_copy_request(
    ad_copy: Mapping[str, Any],
    campaign_context: Mapping[str, Any],
    *,
    brief: Mapping[str, Any] | None,
    timely: bool,
    moment_section: str,
    safer_angle: bool,
    sensitive_reason: str,
) -> str:
    """The ad-copy judge's user prompt (every interpolated value fenced)."""
    return prompts.AD_COPY_EVAL_USER.format(
        **_prompt_fields(campaign_context, CAMPAIGN_PROMPT_FIELDS),
        **_prompt_fields(ad_copy, AD_COPY_PROMPT_FIELDS),
        angle=fence(angle_line(brief, ad_copy.get("angle_id") or "")),
        brief_block=_brief_block(brief, safer_angle, sensitive_reason),
        timely=_yes_no(timely),
        safer_angle=_yes_no(safer_angle),
        moment_section=moment_section,
    )


def _build_visual_request(
    visual_concept: Mapping[str, Any],
    campaign_context: Mapping[str, Any],
    *,
    brief: Mapping[str, Any] | None,
    qa: Any,
    uri: str,
    photo_uri: str,
    person_cast: bool,
    timely: bool,
    moment_section: str,
    safer_angle: bool,
    sensitive_reason: str,
) -> genai.types.ContentListUnionDict:
    """The visual judge's contents (pure).

    With the rendered image at ``uri``: ``[image, (person label, person
    photo,) user prompt]``, the person photo only with ``photo_uri``;
    prompt-only (``uri`` "") the user prompt alone. ``qa`` is the render
    record's image-QA verdict (a hint line).
    """
    image_section = (
        prompts.VISUAL_IMAGE_ATTACHED.format(qa_hint=fence(image_qa_hint(qa)))
        if uri
        else prompts.VISUAL_IMAGE_MISSING
    )
    if uri and photo_uri:
        image_section += "\n" + prompts.VISUAL_PERSON_PHOTO_ATTACHED
    user_prompt = prompts.VISUAL_CONCEPT_EVAL_USER.format(
        **_prompt_fields(campaign_context, CAMPAIGN_PROMPT_FIELDS),
        **_prompt_fields(visual_concept, VISUAL_PROMPT_FIELDS, _VISUAL_FIELD_DEFAULTS),
        person_cast=_yes_no(person_cast),
        timely=_yes_no(timely),
        safer_angle=_yes_no(safer_angle),
        brief_block=_brief_block(brief, safer_angle, sensitive_reason),
        image_section=image_section,
        moment_section=moment_section,
    )
    if not uri:
        return user_prompt
    contents: list[genai.types.PartUnionDict] = [
        genai.types.Part.from_uri(file_uri=uri, mime_type=image_mime_type(uri))
    ]
    if photo_uri:
        contents += [
            genai.types.Part.from_text(text=prompts.PERSON_PHOTO_LABEL),
            genai.types.Part.from_uri(
                file_uri=photo_uri, mime_type=image_mime_type(photo_uri)
            ),
        ]
    contents.append(genai.types.Part.from_text(text=user_prompt))
    return contents


def _judge_with_image_fallbacks(
    judge: Callable[[str, str], VisualConceptEvaluation],
    name: Any,
    image_uri: str,
    photo: str,
) -> VisualConceptEvaluation:
    """Run ``judge(uri, photo_uri)``, degrading on an image-related 4xx.

    An unreadable person photo is retried without it; an unreadable rendered
    image falls back to a prompt-only verdict (``image_judged=False``). Any
    other error propagates (:func:`is_image_fallback_error`).
    """
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
        return judge(image_uri, "")
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
        return judge("", "")


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
