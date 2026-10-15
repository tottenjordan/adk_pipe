"""Image generation tool: the gemini image-model call with quota-paced backoff.

Split out of ``tools.py``; the genai client is now created lazily so importing
this module has no side effects.

Post-render image QA (``image_qa``, on unless ``IMAGE_QA_ENABLED=false``): each
render gets one vision check (pipelined: image N is inspected while image N+1
renders; renders themselves stay sequential); a failing image is re-rendered at most
``IMAGE_QA_MAX_RERENDERS`` times (and at most ``IMAGE_QA_MAX_RERENDERS_PER_RUN``
across the whole call) with a quote-free correction appended to the prompt,
the better attempt is kept (critical failures — unsafe, third-party logo —
weigh first), and only that one is uploaded. Per-concept results land in
``state["generated_images"]``; unresolved failures in
``state["image_qa__issues"]``; concepts whose check errored (fail-open) in
``state["image_qa__unavailable"]`` (not a quality issue, so not surfaced as a
degradation warning).

Every render prompt (re-renders too) ends with ``no_other_logos_line`` — an
unrequested third-party logo was the top re-render cause, so it is no longer
gated on the ``unwanted_logo`` rating-strictness flag. Opt-in rating learning
(``rating_strictness``) flags are passed to ``image_qa.inspect_image`` (``product_not_visible`` /
``trend_unclear`` ask for a prominent product / motif).
"""

import asyncio
import functools
import hashlib
import json
import logging
import random
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from google import genai
from google.adk.tools import ToolContext
from google.genai import errors as genai_errors
from google.genai import types

from agent_common import genai_retry
from agent_common.locations import MODEL_LOCATION

from . import image_qa
from .concept_guard import enforce_person_casting, enforce_timely_cap, parse_concepts
from .config import config
from .copy_gate import parse_copies
from .gcs_tools import (
    _download_blob,
    _save_to_gcs,
    artifact_key_for,  # noqa: F401 (re-exported for tests)
    unique_artifact_keys,
)
from .person_render import (
    PERSON_ROLE_INSTRUCTION,
    person_block_reason,
    person_consent_id,
    person_image_config,
    person_reference_available,
    person_reference_uri,
)
from .rating_signals import strictness_flags
from .references import (
    MAX_REFERENCE_IMAGES,
    REFERENCE_MAX_BYTES,
    REFERENCE_ROLES,
    UnsafeReferenceURL,
    reference_mime_for,
    reference_roles_summary,
    resolve_references,
)
from .trend_situation import timely_allowed

if TYPE_CHECKING:
    from .render_concept import RenderReferences, RenderResult

__all__ = [
    "MAX_REFERENCE_IMAGES",
    "REFERENCE_IGNORE_TEXT_LINE",
    "REFERENCE_ROLES",
    "UnsafeReferenceURL",
    "generate_image",
    "reference_roles_summary",
    "resolve_references",
]


@functools.cache
def _get_genai_client() -> genai.Client:
    """Get a configured genai client for the image model (cached, no import-time side effect).

    The image-gen model (``config.image_gen_model``, gemini-nano-banana-2.1) is served only
    from ``global`` — hence MODEL_LOCATION, not config.LOCATION (which is the
    injected regional value inside a deployed Agent Engine).

    Carries the shared per-request timeout (MILLISECONDS) so a hung render can't
    stall the run; the resulting ``httpx`` timeout is retried by
    ``_generate_image_with_backoff`` (this client has no genai retry_options).
    """
    return genai.Client(
        vertexai=True,
        project=config.PROJECT_ID,
        location=MODEL_LOCATION,
        http_options=types.HttpOptions(timeout=genai_retry.model_request_timeout_ms()),
    )


# The image model is quota-limited on the `global` endpoint (project-wide,
# shared). gemini-3.1-flash-image was capped at ~2 RPM; re-measured 2026-10-13,
# gemini-nano-banana-2.1 @ global took 24/24 calls incl. 6 concurrent and
# 12/min sustained without a 429 (the quota API still lists 2/min; likely
# shared capacity, so 429/503 stay possible — hence this backoff and a small
# IMAGE_RENDER_CONCURRENCY). This direct genai call is NOT wrapped by
# ADK's workflow RetryConfig (that only retries Agent *model* calls, not tool
# functions). A concurrent burst can still trip 503 UNAVAILABLE / 429
# RESOURCE_EXHAUSTED, so we retry here with exponential backoff + jitter to pace
# under quota. See docs/notes/ambient-agents-vs-cloud-functions.md.
_IMAGE_GEN_MAX_ATTEMPTS = 5
_IMAGE_GEN_BASE_DELAY_SECS = 20.0
_IMAGE_GEN_MAX_DELAY_SECS = 90.0


def _is_retryable_genai_error(exc: Exception) -> bool:
    """True for transient/quota-paced genai errors: 5xx, 408, 429, client
    timeouts and connection failures (google-genai's own retry treats the same
    ones as transient)."""
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(exc, genai_errors.ServerError):  # 5xx incl. 503 UNAVAILABLE
        return True
    if isinstance(exc, genai_errors.ClientError) and getattr(exc, "code", None) in (
        408,
        429,
    ):
        return True
    return False


async def _generate_image_with_backoff(**kwargs):
    """Invoke the image model, retrying transient 503/429 with backoff + jitter.

    Non-retryable errors and the final attempt propagate unchanged so the caller
    (and ADK) still see genuine failures.
    """
    for attempt in range(_IMAGE_GEN_MAX_ATTEMPTS):
        try:
            # The sync genai call blocks for the whole render; run it off the
            # event loop so concurrent sessions keep progressing.
            return await asyncio.to_thread(
                _get_genai_client().models.generate_content, **kwargs
            )
        except Exception as exc:
            if (
                not _is_retryable_genai_error(exc)
                or attempt == _IMAGE_GEN_MAX_ATTEMPTS - 1
            ):
                raise
            delay = min(
                _IMAGE_GEN_MAX_DELAY_SECS, _IMAGE_GEN_BASE_DELAY_SECS * 2**attempt
            )
            delay += random.uniform(0, delay * 0.25)  # jitter to de-sync workers
            logging.warning(
                f"Image gen transient error "
                f"(attempt {attempt + 1}/{_IMAGE_GEN_MAX_ATTEMPTS}): {exc}. "
                f"Retrying in {delay:.1f}s"
            )
            await asyncio.sleep(delay)


def valid_aspect_ratio_override(raw: object) -> str:
    """The user's ``visual_aspect_ratio`` when it is an allowed ratio, else ``""``
    (an invalid value is logged and ignored, so each concept keeps its own)."""
    override = (raw if isinstance(raw, str) else "").strip()
    if override and override not in config.image_aspect_ratios_allowed:
        logging.warning(
            f"visual_aspect_ratio override '{override}' not in "
            f"allowed set {config.image_aspect_ratios_allowed}; ignoring override."
        )
        return ""
    if override:
        logging.info(f"Applying user aspect-ratio override: {override}")
    return override


def _resolve_aspect_ratio(
    entry: dict,
    override: str,
    allowed: tuple[str, ...],
    default: str,
) -> str:
    """Resolve one concept's aspect ratio — pure, no SDK/state access.

    Precedence: a valid state-level ``override`` (the user's
    ``visual_aspect_ratio``) wins for every concept; otherwise the per-concept
    ``entry["aspect_ratio"]``; otherwise the configured ``default``. Any value
    outside ``allowed`` is ignored at its level and falls through.
    """
    if override and override in allowed:
        return override
    candidate = entry.get("aspect_ratio") or default
    if candidate not in allowed:
        return default
    return candidate


# How each reference image should be used, phrased for the image model (live
# probe on gemini-nano-banana-2.1: multiple references work — the product was
# reproduced faithfully while a style reference guided the palette).
_REFERENCE_ROLE_INSTRUCTIONS = {
    "product": (
        "Reproduce this exact product: keep its shape, colour, label and "
        "proportions, and place it naturally in the new scene."
    ),
    "logo": (
        "This is the brand logo: place it small, legible and undistorted; never "
        "redraw, recolour or stretch it."
    ),
    "style": (
        "Match only its palette, texture and lighting, not its content, subject "
        "or layout."
    ),
    # Only attached to concepts that cast the user's consented person.
    "person": PERSON_ROLE_INSTRUCTION,
}

# The live probe showed the model copying a reference image's baked-in headline
# into the output, so every reference block ends with this line (once).
REFERENCE_IGNORE_TEXT_LINE = (
    "Ignore any text, captions or watermarks that appear in the reference images."
)


def no_other_logos_line(brand: str) -> str:
    """The line every render prompt ends with (and person-swap edits under
    ``unwanted_logo`` rating strictness)."""
    brand = " ".join((brand or "").split())
    if brand:
        return f"No logos, brand marks or trademarks except those of {brand}."
    return "No logos, brand marks or trademarks."


def _reference_prompt(
    prompt_text: str, roles: list[str], missing_roles: list[str] | tuple = ()
) -> str:
    """The concept prompt plus a numbered reference block — pure.

    ``roles`` are the roles of the reference Parts attached after the prompt,
    in order ("Reference image 1 (product): ..."). ``missing_roles`` are roles
    whose reference failed to fetch; each one not still covered by an attached
    reference gets an "unavailable" line so the model doesn't take another
    reference as that role. No roles → ``prompt_text`` unchanged (text-only).
    """
    if not roles:
        return prompt_text
    lines = [
        f"Reference image {i} ({role}): {_REFERENCE_ROLE_INSTRUCTIONS[role]}"
        for i, role in enumerate(roles, start=1)
    ]
    for role in dict.fromkeys(missing_roles):
        if role not in roles:
            lines.append(
                f"The {role} reference image is unavailable; do not imitate any "
                f"other reference as the {role}."
            )
    lines.append(REFERENCE_IGNORE_TEXT_LINE)
    return prompt_text + "\n\n" + "\n".join(lines)


def _final_image_part(parts):
    """The part carrying the FINAL rendered image, or None.

    Thinking image models (gemini-nano-banana-2.1) return an intermediate
    "thought" image part (``part.thought=True``) before the final one, so the
    first inline image is a draft. Prefer the last non-thought image part; fall
    back to the last image part if every one is flagged as a thought.
    """
    images = [p for p in parts if p.inline_data is not None and p.inline_data.data]
    finals = [p for p in images if not getattr(p, "thought", False)]
    if finals:
        return finals[-1]
    return images[-1] if images else None


def _base_image_config(aspect_ratio: str) -> types.ImageConfig:
    return types.ImageConfig(aspect_ratio=aspect_ratio, image_size=config.image_size)


async def _request_image(contents, image_config: types.ImageConfig):
    """One quota-paced image-model call → the raw response."""
    return await _generate_image_with_backoff(
        model=config.image_gen_model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"], image_config=image_config
        ),
    )


def _image_from_response(response) -> tuple[bytes, str] | None:
    # Gemini image models return the image as inline data on a content part,
    # unlike Imagen's generate_images (which returns response.generated_images).
    candidates = response.candidates or []
    if candidates and candidates[0].content and candidates[0].content.parts:
        part = _final_image_part(candidates[0].content.parts)
        if part is not None:
            return part.inline_data.data, part.inline_data.mime_type or "image/png"
    return None


async def _render_image(
    contents, aspect_ratio: str, image_config: types.ImageConfig | None = None
) -> tuple[bytes, str] | None:
    """Render one image → ``(bytes, mime)`` of the final image part, or None.

    Goes through ``_generate_image_with_backoff`` (quota-paced). A response
    with no image part is logged and yields None (that concept is skipped).
    ``image_config`` overrides the default ``ImageConfig`` (aspect ratio +
    ``config.image_size``).
    """
    response = await _request_image(
        contents, image_config or _base_image_config(aspect_ratio)
    )
    rendered = _image_from_response(response)
    if rendered is None:
        logging.error(f"Error with image generation response: {str(response)}")
    return rendered


async def _render_person_image(
    contents, aspect_ratio: str
) -> tuple[tuple[bytes, str] | None, str | None]:
    """Render a concept that casts the person reference, with
    ``person_generation=ALLOW_ADULT`` → ``(image, None)``, or ``(None, reason)``
    when a safety filter (or the request itself) rejected it
    (``person_render.person_block_reason``; a non-retryable 4xx becomes
    ``request_rejected:<code>``). The caller then renders without the person.
    The response isn't logged (it would echo the request)."""
    try:
        response = await _request_image(
            contents, person_image_config(_base_image_config(aspect_ratio))
        )
    except genai_errors.ClientError as exc:
        if _is_retryable_genai_error(exc):
            raise
        return None, f"request_rejected:{getattr(exc, 'code', '') or 'error'}"
    reason = person_block_reason(response)
    if reason is not None:
        return None, reason
    rendered = _image_from_response(response)
    return (rendered, None) if rendered is not None else (None, "no_image")


def _fetch_person_photo(uri: str) -> types.Part | None:
    """The person photo (a ``gs://…/person-refs/…`` object) as a Part, or None.

    Unlike ``references.fetch_reference_image`` it never logs the URI, and only ``gs://``
    objects under ``person-refs/`` in the run's bucket (when configured) are
    fetched."""
    without_scheme = uri.removeprefix("gs://")
    bucket, _, obj = without_scheme.partition("/")
    configured = (config.GCS_BUCKET_NAME or "").strip()
    if configured and bucket != configured:
        logging.warning("Person reference is not in the run's bucket; not cast")
        return None
    try:
        data = _download_blob(bucket, obj)
        if not data or len(data) > REFERENCE_MAX_BYTES:
            raise ValueError("empty or too large")
        return types.Part.from_bytes(data=data, mime_type=reference_mime_for(obj))
    except Exception as exc:
        logging.warning(
            "Person reference photo unavailable (%s); not cast", type(exc).__name__
        )
        return None


PERSON_REJECTED_NOTE = (
    "{name}: person photo rejected by the safety filter; rendered without the person"
)
PERSON_UNAVAILABLE_NOTE = (
    "{name}: person photo unavailable; rendered without the person"
)
PERSON_NO_CONSENT_NOTE = (
    "Person reference has no consent id; rendered without the person"
)


async def _store_image(
    tool_context: ToolContext,
    image_bytes: bytes,
    mime_type: str,
    artifact_key: str,
    consent_id: str | None = None,
    *,
    before_upload: Callable[[], None] | None = None,
) -> str | None:
    """Upload to GCS + save the ADK artifact → the gs:// URI, or None.

    ``before_upload`` runs in the upload's worker thread right before
    ``_save_to_gcs`` (``_upload_all`` uses it to start uploads in order).

    A per-image GCS failure is logged and returns None so one bad upload
    doesn't abort the whole batch (_save_to_gcs raises on failure — it never
    returns an error dict). The blocking upload runs off the event loop.
    A cast render carries blob metadata ``consent_id`` (the consent revoke
    deletes only objects that carry it) and is not saved as an ADK artifact.
    """
    extra: dict[str, Any] = (
        {"metadata": {"consent_id": consent_id}} if consent_id else {}
    )
    if mime_type and mime_type != "image/png":
        # Only a non-default type is passed: _save_to_gcs defaults to image/png,
        # so PNG uploads keep their historical call shape.
        extra["content_type"] = mime_type

    def upload() -> str:
        if before_upload is not None:
            before_upload()
        return _save_to_gcs(
            tool_context=tool_context,
            image_bytes=image_bytes,
            filename=artifact_key,
            **extra,
        )

    try:
        img_gcs_uri = await asyncio.to_thread(upload)
    except Exception as gcs_exc:
        logging.error(
            f"GCS upload failed for '{artifact_key}', skipping image: {gcs_exc}"
        )
        return None
    if consent_id:
        # A cast render lives only in GCS, which the consent revoke cascade can
        # delete; an ADK artifact copy (the api's artifact service) it couldn't.
        # Nothing reads these artifacts back; the results page uses the GCS image.
        logging.info(f"Saved cast image '{artifact_key}' to GCS only (no artifact)")
        return img_gcs_uri
    try:
        await tool_context.save_artifact(
            filename=artifact_key,
            artifact=types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
        )
    except Exception as artifact_exc:
        # Best effort: nothing reads image artifacts back (the results page,
        # gallery and judge use the GCS image), so a failed copy must not abort
        # the batch after earlier (possibly cast) uploads are already in GCS.
        logging.warning(
            f"Artifact save failed for '{artifact_key}'; keeping the GCS image "
            f"({type(artifact_exc).__name__})"
        )
        return img_gcs_uri
    logging.info(f"Saved image artifact, '{artifact_key}', to '{img_gcs_uri}'")
    return img_gcs_uri


def _qa_record(
    result: image_qa.ImageQAResult,
    entry: dict,
    product: str,
    compared_to_original: bool = False,
    *,
    safer_angle_active: bool = False,
) -> dict:
    """The stored verdict: the model's fields + the rule's passed/failures. The
    composition fields are stored only for a check against the original (a
    personalised variant edit), the cast-only person fields only for a cast
    concept and ``public_figure_likeness`` only with a safer angle, so other
    renders keep their record shape."""
    failures = image_qa.qa_failures(result, entry, target_product=product)
    exclude: set[str] = set()
    if not compared_to_original:
        exclude.update(image_qa.COMPOSITION_FIELDS)
    if not image_qa.is_cast(entry):
        exclude.update(image_qa.CAST_ONLY_FIELDS)
    if not safer_angle_active:
        exclude.update(image_qa.SAFER_ANGLE_FIELDS)
    return {
        **result.model_dump(exclude=exclude or None),
        "passed": not failures,
        "failures": failures,
    }


def _severity(rules: list[str]) -> tuple[int, int]:
    """(critical failures, total failures) — compared lexicographically."""
    return sum(r in image_qa.CRITICAL_RULES for r in rules), len(rules)


def _keep_retry(kept_rules: list[str], retry_rules: list[str]) -> bool:
    """Whether the re-render replaces the kept attempt — pure.

    Never keeps a retry that introduces unsafe content; otherwise the lower
    (critical, total) failure count wins and ties go to the latest attempt.
    """
    unsafe = "unsafe content"
    if unsafe in retry_rules and unsafe not in kept_rules:
        return False
    return _severity(retry_rules) <= _severity(kept_rules)


@dataclass
class _RerenderBudget:
    """QA re-renders left for the whole generate_image call (shared)."""

    remaining: int


def timely_record_valid_until(state: Any) -> str:
    """The moment's ``valid_until`` when the run may mark timely concepts.

    Non-empty only when ``timely_allowed`` (a timely validated moment and the
    slot on) and ``trend_situation.valid_until`` is a non-empty string; ``""``
    otherwise, so no record gets the timely keys.
    """
    if not timely_allowed(state, config.max_timely_concepts):
        return ""
    valid_until = (state.get("trend_situation") or {}).get("valid_until")
    return valid_until.strip() if isinstance(valid_until, str) else ""


def copies_by_original_id(ad_copies: Any) -> dict[str, Mapping[str, Any]]:
    """The ``ad_copy_critique`` copies keyed by ``str(original_id)`` (first wins)."""
    by_id: dict[str, Mapping[str, Any]] = {}
    for ad in parse_copies(ad_copies):
        if ad.get("original_id") is not None:
            by_id.setdefault(str(ad.get("original_id")), ad)
    return by_id


def is_timely_concept(
    concept: Mapping[str, Any], copies_by_id: Mapping[str, Mapping[str, Any]]
) -> bool:
    """True when the concept, or its paired copy (``ad_copy_id`` ==
    ``original_id``, compared as strings), has ``uses_trend_state is True``."""
    if concept.get("uses_trend_state") is True:
        return True
    copy_id = concept.get("ad_copy_id")
    if copy_id is None:
        return False
    paired = copies_by_id.get(str(copy_id))
    return paired is not None and paired.get("uses_trend_state") is True


def render_fingerprint(final_visual_concepts: Any, person_reference: Any) -> str:
    """The ``_images_generated`` marker: a short sha256 over each concept's name,
    image prompt and cast flag (concept order) plus the person reference's URI
    and consent id. Never empty, so the marker stays truthy for
    ``images_ready`` / ``RetryUntilKeyNode``; the URI is hashed, never stored."""
    payload = {
        "concepts": [
            [
                str(c.get("concept_name") or ""),
                str(c.get("image_generation_prompt") or ""),
                c.get("casts_person_reference") is True,
            ]
            for c in parse_concepts(final_visual_concepts)
        ],
        "person": [
            person_reference_uri(person_reference),
            person_consent_id(person_reference),
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return f"render-v1:{digest[:16]}"


@dataclass
class RenderPlan:
    """What ``generate_image`` renders, decided before any image-model call.

    ``concepts`` are the concepts after the render-time casting and timely
    guards, with their ``aspect_ratios`` and de-duplicated ``artifact_keys``
    (same order); ``references`` / ``person_part`` are fetched once for every
    concept; ``person_uri`` is "" unless a usable, consented person reference
    exists (never logged); ``person_notes`` collects the person run notes.
    """

    fingerprint: str
    gcs_folder: str
    gcs_subdir: str
    concepts: list[dict]
    aspect_ratios: list[str]
    artifact_keys: list[str]
    references: "RenderReferences"
    person_part: types.Part | None
    person_uri: str
    consent_id: str
    person_notes: list[str]
    brand: str
    product: str
    strictness: tuple[str, ...]
    safer_angle_active: bool
    timely_valid_until: str
    copies_by_id: dict[str, Mapping[str, Any]]


def _person_reference(state: Any, notes: list[str]) -> tuple[str, str]:
    """``(uri, consent_id)`` of the usable person reference, else ``("", "")``.

    Only a gs:// URI under person-refs/ with a consent id is used (consent-
    checked by the api at kick-off; adk web / direct engine sessions skip that
    check, so without a consent id nothing is cast and no render lacks the
    revoke metadata — ``PERSON_NO_CONSENT_NOTE`` goes to ``notes``). Never logs
    the URI."""
    person_ref = state.get("person_reference")
    person_uri = person_reference_uri(person_ref)
    if person_ref and not person_uri:
        logging.warning(
            "Person reference ignored: not a gs:// photo under person-refs/"
        )
    if person_uri and not person_reference_available(person_ref):
        logging.warning("Person reference ignored: no consent id")
        notes.append(PERSON_NO_CONSENT_NOTE)
        return "", ""
    return person_uri, (person_consent_id(person_ref) if person_uri else "")


def _guarded_concepts(state: Any, *, person_available: bool) -> list[dict]:
    """The concepts to render, with the casting guard and the timely cap
    re-applied (a checkpoint-3 edit, or any path that bypassed the after-agent
    guard, must not cast without a reference or render more timely concepts
    than MAX_TIMELY_CONCEPTS). parse_concepts never raises: a missing / null /
    malformed value (e.g. a failed concept stage) renders nothing."""
    concepts, cast_warnings = enforce_person_casting(
        [
            c
            for c in parse_concepts(state.get("final_visual_concepts"))
            if isinstance(c, dict)
        ],
        available=person_available,
        max_cast=config.max_cast_concepts,
        safe_styles=config.person_safe_styles,
    )
    for warning in cast_warnings:
        logging.warning(f"casting guard (render): {warning}")
    concepts, timely_warnings = enforce_timely_cap(
        concepts,
        timely_ok=timely_allowed(state, config.max_timely_concepts),
        max_timely=config.max_timely_concepts,
    )
    for warning in timely_warnings:
        logging.warning(f"timely guard (render): {warning}")
    return concepts


async def _plan_renders(state: Any, *, fingerprint: str) -> RenderPlan:
    """Resolve everything the renders share: references (fetched once,
    concurrently; none → text-only), the aspect ratios (a valid
    ``visual_aspect_ratio`` pins every concept, else each keeps its own), the
    guarded concepts, the person photo (fetched once, only when a concept casts
    it), the artifact keys and the timely record inputs."""
    # Imported here: render_concept builds on this module's helpers.
    from .render_concept import fetch_person_photo, fetch_references

    gcs_folder = state["gcs_folder"]
    gcs_subdir = state["agent_output_dir"]
    references = await fetch_references(resolve_references(state))
    override = valid_aspect_ratio_override(state.get("visual_aspect_ratio"))
    person_notes: list[str] = []
    person_uri, consent_id = _person_reference(state, person_notes)
    concepts = _guarded_concepts(state, person_available=bool(person_uri))
    any_cast = any(c.get("casts_person_reference") is True for c in concepts)
    timely_valid_until = timely_record_valid_until(state)
    return RenderPlan(
        fingerprint=fingerprint,
        gcs_folder=gcs_folder,
        gcs_subdir=gcs_subdir,
        concepts=concepts,
        # .get() inside keeps concepts that only carry image_generation_prompt
        # working (see test_tools_retry).
        aspect_ratios=[
            _resolve_aspect_ratio(
                c,
                override,
                config.image_aspect_ratios_allowed,
                config.image_aspect_ratio_default,
            )
            for c in concepts
        ],
        artifact_keys=unique_artifact_keys(
            [str(c.get("concept_name") or "") for c in concepts]
        ),
        references=references,
        person_part=await fetch_person_photo(person_uri) if any_cast else None,
        person_uri=person_uri,
        consent_id=consent_id,
        person_notes=person_notes,
        brand=state.get("brand") or "",
        product=state.get("target_product") or "",
        # Opt-in rating learning: the run's check-backed fail-reason flags.
        strictness=tuple(strictness_flags(state.get("rating_strictness"))),
        # A sensitive trend's safer angle: image QA also checks for a real
        # public figure's likeness (critical).
        safer_angle_active=state.get("safer_angle_active") == "yes",
        timely_valid_until=timely_valid_until,
        copies_by_id=(
            copies_by_original_id(state.get("ad_copy_critique"))
            if timely_valid_until
            else {}
        ),
    )


# Longest exception text kept in a "render failed" issue.
_RENDER_FAILURE_MAX_CHARS = 200


async def _contained(
    entry: dict, render: Awaitable["RenderResult"]
) -> tuple["RenderResult", Exception | None]:
    """``(result, None)``, or for a render that raised ``(a no-image result
    whose qa_issue is "<concept>: render failed: <Type>: <message>", exc)``."""
    from .render_concept import RenderResult

    try:
        return await render, None
    except Exception as exc:
        name = str(entry.get("concept_name") or "")
        logging.warning(f"Render failed for '{name}'; skipping it: {exc!r}")
        detail = f"{type(exc).__name__}: {exc}"[:_RENDER_FAILURE_MAX_CHARS]
        issue = f"{name}: render failed: {detail}"
        return RenderResult(None, "", 1, None, issue, False, None, entry), exc


async def _run_pipelined(plan: RenderPlan) -> list["RenderResult"]:
    """One ``render_concept`` task per concept → their results, in concept order.

    At most ``IMAGE_RENDER_CONCURRENCY`` (n) image-model calls are in flight
    (the image quota) behind one semaphore, re-renders included. First renders
    are staggered by concept: concept i's first render waits for concept
    i-n's (so the first n start at once and each returned render lets one more
    concept in), and each image's QA (+ any re-render) overlaps the other
    renders. asyncio.Semaphore wakes waiters FIFO, so a re-render queued while
    all slots are busy runs as soon as one frees, ahead of later first renders.
    n=1 is the old strictly sequential pipeline (concept i waits for concept
    i-1). The per-run re-render budget is claimed in concept order.

    A concept whose render raises (after ``_generate_image_with_backoff``'s
    retries) becomes a no-image result with a "render failed" issue, so the
    other concepts still render and upload; only when every concept raised does
    the first error propagate (the node's INFRA_RETRY then retries the call).
    On cancellation the in-flight tasks are stopped and every task's outcome
    retrieved.
    """
    from .render_concept import RenderPacing, render_concept

    n = config.image_render_concurrency
    render_slots = asyncio.Semaphore(n)
    budget = _RerenderBudget(config.image_qa_max_rerenders_per_run)
    tasks: list[asyncio.Task] = []
    rendered: list[asyncio.Event] = []
    previous_done: asyncio.Event | None = None
    try:
        for i, (entry, aspect_ratio) in enumerate(
            zip(plan.concepts, plan.aspect_ratios, strict=True)
        ):
            rendered_event, done = asyncio.Event(), asyncio.Event()
            pacing = RenderPacing(
                lock=render_slots,
                start_after=rendered[i - n] if i >= n else None,
                rendered=rendered_event,
                claim_after=previous_done,
                claims_done=done,
            )
            tasks.append(
                asyncio.create_task(
                    _contained(
                        entry,
                        render_concept(
                            entry,
                            aspect_ratio=aspect_ratio,
                            references=plan.references,
                            person=plan.person_part,
                            strictness=plan.strictness,
                            safer_angle_active=plan.safer_angle_active,
                            qa=config.image_qa_enabled,
                            brand=plan.brand,
                            target_product=plan.product,
                            budget=budget,
                            pacing=pacing,
                        ),
                    )
                )
            )
            rendered.append(rendered_event)
            previous_done = done
        outcomes = await asyncio.gather(*tasks)
        failures = [exc for _, exc in outcomes if exc is not None]
        if failures and len(failures) == len(outcomes):
            raise failures[0]
        return [result for result, _ in outcomes]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _image_record(
    plan: RenderPlan, result: "RenderResult", artifact_key: str, gcs_uri: str
) -> dict:
    """One ``generated_images`` record. Only runs with a person reference carry
    ``cast`` (+ ``consent_id`` when cast; the judge's person gate and the UI
    read ``cast is True``), and only timely concepts carry ``timely`` /
    ``valid_until``, so every other record keeps the pre-timely shape."""
    record = {
        "gcs_uri": gcs_uri,
        "artifact_key": artifact_key,
        "attempts": result.attempts,
        "qa": result.qa,
    }
    if plan.person_uri:
        record["cast"] = result.cast
        if result.cast:
            record["consent_id"] = plan.consent_id
    if plan.timely_valid_until and is_timely_concept(result.concept, plan.copies_by_id):
        record["timely"] = True
        record["valid_until"] = plan.timely_valid_until
    return record


# Kept images uploaded at once (GCS writes are independent; small bound).
UPLOAD_CONCURRENCY = 4
# Safety bound on an upload waiting for the previous one to start.
_UPLOAD_ORDER_WAIT_SECS = 30.0


async def _upload_all(
    tool_context: ToolContext, jobs: list[tuple[bytes, str, str, str | None]]
) -> list[str | None | BaseException]:
    """Upload ``(image_bytes, mime, artifact_key, consent_id)`` jobs
    concurrently (at most ``UPLOAD_CONCURRENCY`` in flight) → per job, in job
    order, the gs:// URI, None (upload failed, logged) or the exception.

    Uploads start in job order, so the ``_save_to_gcs`` calls keep concept order
    while their I/O overlaps: the semaphore (FIFO, acquired with no await
    before it) submits the worker threads in job order, and each thread waits
    until the previous job's thread has entered ``_save_to_gcs`` (a
    ``threading.Event``; the executor queue is FIFO, so the previous job is
    always already running and the wait can't starve it)."""
    slots = asyncio.Semaphore(UPLOAD_CONCURRENCY)
    started = [threading.Event() for _ in jobs]

    async def one(i: int, job: tuple[bytes, str, str, str | None]) -> str | None:
        image_bytes, mime, artifact_key, consent_id = job

        def entered() -> None:  # in the worker thread, right before the upload
            if i:
                started[i - 1].wait(_UPLOAD_ORDER_WAIT_SECS)
            started[i].set()

        try:
            async with slots:
                return await _store_image(
                    tool_context,
                    image_bytes,
                    mime,
                    artifact_key,
                    consent_id=consent_id,
                    before_upload=entered,
                )
        finally:
            started[i].set()  # never leave the next upload waiting

    return await asyncio.gather(
        *(one(i, job) for i, job in enumerate(jobs)), return_exceptions=True
    )


async def _persist_results(
    plan: RenderPlan, results: list["RenderResult"], tool_context: ToolContext
) -> list[str]:
    """Upload the kept images (concurrently, ``_upload_all``) and write the
    run's state → the saved artifact keys; records keep concept order.

    Every finished upload reaches ``generated_images`` in a ``finally``, even
    when a later step raised: a cast render in GCS must stay visible to
    ``cast_renders`` and the consent revoke cascade (an INFRA_RETRY re-run
    overwrites it). The idempotency marker is written only on success.
    """
    state = tool_context.state
    saved_keys: list[str] = []
    generated_images: dict[str, dict] = {}
    qa_issues: list[str] = []
    qa_unavailable: list[str] = []
    person_rejected: dict[str, str] = {}
    kept: list[tuple[RenderResult, str]] = []
    for result, artifact_key in zip(results, plan.artifact_keys, strict=True):
        name = str(result.concept.get("concept_name") or "")
        if result.rejected_reason is not None:
            person_rejected[name] = result.rejected_reason
        if result.qa_unavailable:
            qa_unavailable.append(result.concept["concept_name"])
        if result.qa_issue:
            qa_issues.append(result.qa_issue)
        if result.image_bytes is not None:
            kept.append((result, artifact_key))
    failure: BaseException | None = None
    try:
        uploaded = await _upload_all(
            tool_context,
            [
                (
                    result.image_bytes or b"",
                    result.mime,
                    artifact_key,
                    plan.consent_id if plan.person_uri and result.cast else None,
                )
                for result, artifact_key in kept
            ],
        )
        for (result, artifact_key), gcs_uri in zip(kept, uploaded, strict=True):
            if isinstance(gcs_uri, BaseException):
                failure = failure or gcs_uri
                continue
            if gcs_uri is None:
                continue
            saved_keys.append(artifact_key)
            generated_images[result.concept["concept_name"]] = _image_record(
                plan, result, artifact_key, gcs_uri
            )
        if failure is not None:
            raise failure
    finally:
        if generated_images:
            state["generated_images"] = generated_images

    # Mark as done so subsequent calls are idempotent
    state["_images_generated"] = plan.fingerprint
    state["_generated_artifact_keys"] = saved_keys
    state["generated_images"] = generated_images
    if qa_issues:
        # Generic `<key>__issues` marker → collect_degradation_warnings.
        state["image_qa__issues"] = qa_issues
    if qa_unavailable:
        # Deliberately NOT a `__issues` key: a failed check is not a quality
        # issue, so collect_degradation_warnings does not surface it.
        state["image_qa__unavailable"] = qa_unavailable
    person_notes = list(plan.person_notes)
    if person_rejected:
        # {concept: reason} for the results page, plus a run note (generic
        # `<key>__issues` marker → collect_degradation_warnings).
        state["person_reference_rejected"] = person_rejected
        person_notes.extend(
            (
                PERSON_UNAVAILABLE_NOTE
                if reason == "photo_unavailable"
                else PERSON_REJECTED_NOTE
            ).format(name=name)
            for name, reason in person_rejected.items()
        )
    if person_notes:
        state["person_reference__issues"] = person_notes
    return saved_keys


async def generate_image(
    tool_context: ToolContext,
):
    """Render one image per concept in the 'final_visual_concepts' state key and save them as artifacts. Takes no arguments; call it exactly once."""
    # Renders with config.image_gen_model; returns a dict with 'status' and a
    # 'message' listing the saved artifact keys.
    state = tool_context.state
    # Idempotency guard: skip if these concepts were already rendered. The
    # marker is the render fingerprint, so a re-run of the visual stage (new
    # concepts, or a new person reference) renders again instead of returning
    # stale images; a legacy boolean True (sessions written before the
    # fingerprint) still skips.
    fingerprint = render_fingerprint(
        state.get("final_visual_concepts"), state.get("person_reference")
    )
    marker = state.get("_images_generated")
    if marker is True or (isinstance(marker, str) and marker == fingerprint):
        existing_keys = state.get("_generated_artifact_keys", [])
        return {
            "status": "success",
            "message": f"Images already generated: {existing_keys}",
        }

    plan = await _plan_renders(state, fingerprint=fingerprint)
    try:
        results = await _run_pipelined(plan)
        saved_keys = await _persist_results(plan, results, tool_context)
    except Exception as e:
        # Propagate so ADK 2.0 RetryConfig can retry transient infra failures.
        logging.exception(f"No images generated. {e}")
        raise
    return {
        "status": "success",
        "message": f"Saved img artifacts: {saved_keys} to `gs://{config.GCS_BUCKET_NAME}/{plan.gcs_folder}/{plan.gcs_subdir}`",
    }
