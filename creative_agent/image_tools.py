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

Opt-in rating learning (``rating_strictness``): ``unwanted_logo`` adds
``no_other_logos_line`` to every render prompt (re-renders too), and the flags
are passed to ``image_qa.inspect_image`` (``product_not_visible`` /
``trend_unclear`` ask for a prominent product / motif).
"""

import asyncio
import functools
import ipaddress
import logging
import random
import socket
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from google import genai
from google.adk.tools import ToolContext
from google.genai import errors as genai_errors
from google.genai import types

from agent_common import genai_retry
from agent_common.locations import MODEL_LOCATION

from . import image_qa
from .concept_guard import enforce_person_casting
from .config import config
from .gcs_tools import _download_blob, _save_to_gcs, artifact_key_for
from .person_render import (
    PERSON_ROLE_INSTRUCTION,
    person_block_reason,
    person_consent_id,
    person_image_config,
    person_reference_uri,
)
from .rating_signals import strictness_flags
from .references import (
    MAX_REFERENCE_IMAGES,
    REFERENCE_ROLES,
    reference_roles_summary,
    resolve_references,
)

__all__ = [
    "MAX_REFERENCE_IMAGES",
    "REFERENCE_IGNORE_TEXT_LINE",
    "REFERENCE_ROLES",
    "UnsafeReferenceURL",
    "generate_image",
    "reference_roles_summary",
    "resolve_references",
]

# Fetch timeout for an http(s) reference image (stdlib urllib, no new dep).
_REFERENCE_FETCH_TIMEOUT_SECS = 20

# Largest reference image accepted (http(s) body or gs:// object).
_REFERENCE_MAX_BYTES = 10 * 1024 * 1024

# Map a reference-image extension to a mime type (default image/png).
_REFERENCE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _reference_mime_for(path: str) -> str:
    """Best-effort mime type from a reference-image path/URL extension."""
    lower = path.lower()
    for ext, mime in _REFERENCE_MIME_BY_EXT.items():
        if lower.endswith(ext):
            return mime
    return "image/png"


class UnsafeReferenceURL(ValueError):
    """An http(s) reference URL that must not be fetched (SSRF guard)."""


def _is_public_ip(raw: str) -> bool:
    """False for loopback/private/link-local/reserved/multicast/unspecified."""
    ip = ipaddress.ip_address(raw.split("%", 1)[0])  # drop an IPv6 zone id
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_reference_url(url: str) -> None:
    """Raise UnsafeReferenceURL unless ``url`` is http(s) to a public host.

    Every address the hostname resolves to must be public (a mixed answer is
    refused). Note: urllib re-resolves on connect, so this does not pin the
    address (DNS-rebinding window); it blocks the plain metadata/private-host
    cases a user-supplied URL could otherwise reach.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeReferenceURL(f"not an http(s) URL with a host: '{url}'")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not infos:
        raise UnsafeReferenceURL(f"'{parsed.hostname}' did not resolve")
    for info in infos:
        address = str(info[4][0])
        if not _is_public_ip(address):
            raise UnsafeReferenceURL(
                f"'{parsed.hostname}' resolves to non-public address {address}"
            )


class _RevalidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow at most 3 redirects, re-validating every hop's URL."""

    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_reference_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _reference_opener() -> urllib.request.OpenerDirector:
    """A urllib opener whose redirect handler re-validates each hop."""
    return urllib.request.build_opener(_RevalidatingRedirectHandler)


def _fetch_http_reference(uri: str) -> tuple[bytes, str]:
    """(bytes, mime) of a public http(s) image ≤ the size cap, else raise."""
    _validate_reference_url(uri)
    with _reference_opener().open(uri, timeout=_REFERENCE_FETCH_TIMEOUT_SECS) as resp:
        mime = resp.headers.get_content_type()
        if not mime.startswith("image/"):
            raise ValueError(f"Content-Type {mime!r} is not an image")
        data = resp.read(_REFERENCE_MAX_BYTES + 1)
    if len(data) > _REFERENCE_MAX_BYTES:
        raise ValueError(f"larger than {_REFERENCE_MAX_BYTES} bytes")
    return data, mime


def _fetch_reference_image(uri: str) -> types.Part | None:
    """Fetch a product/brand reference image as a genai Part, or None on failure.

    Supports ``gs://bucket/object`` (via the GCS client) and ``http(s)://`` URLs
    (via stdlib urllib, hardened: public hosts only, ≤ 3 re-validated
    redirects, an ``image/*`` Content-Type). Both are capped at
    ``_REFERENCE_MAX_BYTES``. Any failure is logged and swallowed so image
    generation always falls back to the text-only path rather than aborting.
    """
    uri = (uri or "").strip()
    if not uri:
        return None
    try:
        if uri.startswith("gs://"):
            without_scheme = uri[len("gs://") :]
            bucket, _, obj = without_scheme.partition("/")
            if not bucket or not obj:
                logging.warning(f"Malformed gs:// reference image URI: '{uri}'")
                return None
            data = _download_blob(bucket, obj)
            if len(data) > _REFERENCE_MAX_BYTES:
                raise ValueError(f"larger than {_REFERENCE_MAX_BYTES} bytes")
            mime = _reference_mime_for(obj)
        elif uri.startswith("http://") or uri.startswith("https://"):
            data, mime = _fetch_http_reference(uri)
        else:
            logging.warning(
                f"Unsupported reference image URI scheme (want gs:// or http(s)://): '{uri}'"
            )
            return None
        return types.Part.from_bytes(data=data, mime_type=mime)
    except Exception as exc:
        logging.warning(
            f"Failed to fetch reference image '{uri}'; "
            f"falling back to text-only prompt: {exc}"
        )
        return None


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


# The image model (gemini-3.1-flash-image was capped at ~2 RPM; re-measure for
# gemini-nano-banana-2.1) is quota-limited on the `global`
# endpoint (project-wide, shared), and this direct genai call is NOT wrapped by
# ADK's workflow RetryConfig (that only retries Agent *model* calls, not tool
# functions). A concurrent burst reliably trips 503 UNAVAILABLE / 429
# RESOURCE_EXHAUSTED, so we retry here with exponential backoff + jitter to pace
# under quota. See docs/notes/ambient-agents-vs-cloud-functions.md.
_IMAGE_GEN_MAX_ATTEMPTS = 5
_IMAGE_GEN_BASE_DELAY_SECS = 20.0
_IMAGE_GEN_MAX_DELAY_SECS = 90.0


def _is_retryable_genai_error(exc: Exception) -> bool:
    """True for transient/quota-paced genai errors: 5xx, 429, client timeouts."""
    if isinstance(exc, httpx.TimeoutException):  # per-request timeout hit
        return True
    if isinstance(exc, genai_errors.ServerError):  # 5xx incl. 503 UNAVAILABLE
        return True
    if isinstance(exc, genai_errors.ClientError) and getattr(exc, "code", None) == 429:
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
    """The render-prompt line added under ``unwanted_logo`` rating strictness."""
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


async def _fetch_references(
    refs: list[tuple[str, str]],
) -> tuple[list[tuple[str, types.Part]], list[str]]:
    """Fetch every reference concurrently (off the event loop).

    Returns ``(fetched, missing_roles)``: the ``(role, Part)`` pairs that
    fetched, in order, and the roles whose fetch failed
    (``_fetch_reference_image`` returned None and logged a warning). A failed
    fetch drops only that reference.
    """
    parts = await asyncio.gather(
        *(asyncio.to_thread(_fetch_reference_image, uri) for uri, _ in refs)
    )
    fetched = []
    missing = []
    for (uri, role), part in zip(refs, parts, strict=True):
        if part is None:
            logging.warning(f"Skipping {role} reference image '{uri}' (fetch failed)")
            missing.append(role)
            continue
        logging.info(f"Using {role} reference image: {uri}")
        fetched.append((role, part))
    return fetched, missing


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

    Unlike ``_fetch_reference_image`` it never logs the URI, and only ``gs://``
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
        if not data or len(data) > _REFERENCE_MAX_BYTES:
            raise ValueError("empty or too large")
        return types.Part.from_bytes(data=data, mime_type=_reference_mime_for(obj))
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


async def _store_image(
    tool_context: ToolContext, image_bytes: bytes, mime_type: str, artifact_key: str
) -> str | None:
    """Upload to GCS + save the ADK artifact → the gs:// URI, or None.

    A per-image GCS failure is logged and returns None so one bad upload
    doesn't abort the whole batch (_save_to_gcs raises on failure — it never
    returns an error dict). The blocking upload runs off the event loop.
    """
    try:
        img_gcs_uri = await asyncio.to_thread(
            _save_to_gcs,
            tool_context=tool_context,
            image_bytes=image_bytes,
            filename=artifact_key,
        )
    except Exception as gcs_exc:
        logging.error(
            f"GCS upload failed for '{artifact_key}', skipping image: {gcs_exc}"
        )
        return None
    await tool_context.save_artifact(
        filename=artifact_key,
        artifact=types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
    )
    logging.info(f"Saved image artifact, '{artifact_key}', to '{img_gcs_uri}'")
    return img_gcs_uri


async def _inspect(
    rendered: tuple[bytes, str],
    entry: dict,
    brand: str,
    product: str,
    has_logo_reference: bool = False,
    strictness: Sequence[str] = (),
    person_image: tuple[bytes, str] | None = None,
) -> image_qa.ImageQAResult | None:
    """One QA call off the event loop → the verdict, or None (fail-open).
    ``person_image`` (cast concepts only) adds the likeness comparison."""
    image_bytes, mime = rendered
    extra = {"person_image": person_image} if person_image is not None else {}
    try:
        return await asyncio.to_thread(
            image_qa.inspect_image,
            image_bytes,
            mime,
            entry,
            brand=brand,
            target_product=product,
            client=image_qa._get_qa_client(),
            model=config.image_qa_model,
            has_logo_reference=has_logo_reference,
            strictness=strictness,
            **extra,
        )
    except Exception as exc:
        logging.warning(
            f"Image QA unavailable for '{entry.get('concept_name')}': {exc}"
        )
        return None


def _qa_record(result: image_qa.ImageQAResult, entry: dict, product: str) -> dict:
    """The stored verdict: the model's fields + the rule's passed/failures."""
    failures = image_qa.qa_failures(result, entry, target_product=product)
    return {**result.model_dump(), "passed": not failures, "failures": failures}


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


BUDGET_REACHED = "re-render budget reached"


async def _inspect_and_rerender(
    entry: dict,
    rendered: tuple[bytes, str],
    prompt_text: str,
    aspect_ratio: str,
    contents_for,
    brand: str,
    product: str,
    budget: _RerenderBudget,
    has_logo_reference: bool = False,
    *,
    render=None,
    claim_after: asyncio.Event | None = None,
    claims_done: asyncio.Event | None = None,
    strictness: Sequence[str] = (),
    person_image: tuple[bytes, str] | None = None,
) -> tuple[tuple[bytes, str], int, dict | None, str | None]:
    """Inspect a render; re-render (bounded) while it fails; keep the best.

    Returns ``(kept_image, attempts, qa_record, issue)``. Each re-render
    appends ``image_qa.correction_text`` (quote-free, "no new text") to the
    prompt (same references / aspect ratio) and is inspected again;
    ``_keep_retry`` decides which attempt is kept. Re-renders are bounded per
    image (``config.image_qa_max_rerenders``) and per run (``budget``, spent
    here). ``issue`` is the ``image_qa__issues`` entry: "<concept>:
    <failures>" when the kept image still fails (suffixed "(re-render budget
    reached)" when the run cap stopped a re-render), else None. ``qa_record``
    is None when the first check errored (fail-open; the render is kept and
    the caller records the concept as unavailable). A re-render or re-check
    that errors stops the loop and keeps the best inspected image so far.

    Pipelining (``generate_image``): ``render`` is the serialized render
    callable (default :func:`_render_image`); the budget is claimed in concept
    order — this concept decides only after the previous one set
    ``claim_after`` — and ``claims_done`` is set once this concept will claim
    no more (always set on exit, so a failure never blocks later concepts).
    """
    render = render or _render_image

    async def my_turn() -> None:
        # Wait until every earlier concept has made its budget claims, so the
        # per-run cap goes to concepts in concept order (as when sequential).
        if claim_after is not None:
            await claim_after.wait()

    try:
        name = entry.get("concept_name", "")
        result = await _inspect(
            rendered,
            entry,
            brand,
            product,
            has_logo_reference,
            strictness,
            person_image,
        )
        if result is None:
            await my_turn()
            return rendered, 1, None, None
        attempts = 1
        kept, kept_result = rendered, result
        kept_rules = image_qa.qa_failed_rules(result, entry, target_product=product)
        budget_reached = False
        max_rerenders = config.image_qa_max_rerenders
        for round_ in range(max_rerenders):
            if not kept_rules:
                break
            await my_turn()
            if budget.remaining <= 0:
                budget_reached = True
                break
            budget.remaining -= 1
            if round_ == max_rerenders - 1 and claims_done is not None:
                claims_done.set()  # last possible claim made: let the next decide
            logging.warning(
                f"Image QA failed for '{name}' ({kept_rules}); re-rendering"
            )
            attempts += 1
            correction = image_qa.correction_text(
                kept_result, entry, target_product=product
            )
            try:
                retry = await render(
                    contents_for(prompt_text + "\n\n" + correction), aspect_ratio
                )
            except Exception as exc:
                logging.warning(
                    f"Re-render failed for '{name}'; keeping previous: {exc}"
                )
                break
            if retry is None:
                break
            retry_result = await _inspect(
                retry,
                entry,
                brand,
                product,
                has_logo_reference,
                strictness,
                person_image,
            )
            if retry_result is None:
                break
            retry_rules = image_qa.qa_failed_rules(
                retry_result, entry, target_product=product
            )
            if _keep_retry(kept_rules, retry_rules):
                kept, kept_result, kept_rules = retry, retry_result, retry_rules
        await my_turn()
        record = _qa_record(kept_result, entry, product)
        issue = None
        if record["failures"]:
            issue = f"{name}: {'; '.join(record['failures'])}"
            if budget_reached:
                issue += f" ({BUDGET_REACHED})"
        return kept, attempts, record, issue
    finally:
        if claims_done is not None:
            claims_done.set()


async def generate_image(
    tool_context: ToolContext,
):
    """Render one image per concept in the 'final_visual_concepts' state key and save them as artifacts. Takes no arguments; call it exactly once."""
    # Renders with config.image_gen_model; returns a dict with 'status' and a
    # 'message' listing the saved artifact keys.
    # Idempotency guard: skip if images were already generated
    if tool_context.state.get("_images_generated"):
        existing_keys = tool_context.state.get("_generated_artifact_keys", [])
        return {
            "status": "success",
            "message": f"Images already generated: {existing_keys}",
        }

    # get constants
    gcs_folder = tool_context.state["gcs_folder"]
    gcs_subdir = tool_context.state["agent_output_dir"]

    # get artifact details
    final_visual_concepts_dict = tool_context.state.get("final_visual_concepts")
    final_visual_concepts_list = final_visual_concepts_dict["visual_concepts"]

    # Imported here: render_concept builds on this module's helpers.
    from .render_concept import (
        RenderPacing,
        fetch_person_photo,
        fetch_references,
        render_concept,
    )

    # Optional reference images (product / logo / style; `reference_images` plus
    # the legacy single `reference_image_uri`), applied to every concept. All
    # fetched ONCE, concurrently, before the loop; none fetched → text-only.
    references = await fetch_references(resolve_references(tool_context.state))

    # Optional user-supplied deterministic aspect-ratio override. When set to a
    # valid value it pins EVERY concept to that ratio; when empty/invalid, each
    # concept keeps its own LLM-chosen ratio (preserving diversity). Read once.
    aspect_ratio_override = valid_aspect_ratio_override(
        tool_context.state.get("visual_aspect_ratio")
    )

    brand = tool_context.state.get("brand") or ""
    # Opt-in rating learning: the run's check-backed fail-reason flags.
    strictness = strictness_flags(tool_context.state.get("rating_strictness"))

    # Optional consented person reference (consent-checked by the api at
    # kick-off). Only a gs:// URI under person-refs/ is used; the casting guard
    # is re-applied here (a checkpoint-3 edit may have bypassed it), and the
    # photo is fetched once, only when a concept casts it. Never log the URI.
    person_ref = tool_context.state.get("person_reference")
    person_uri = person_reference_uri(person_ref)
    if person_ref and not person_uri:
        logging.warning(
            "Person reference ignored: not a gs:// photo under person-refs/"
        )
    final_visual_concepts_list, cast_warnings = enforce_person_casting(
        [c for c in final_visual_concepts_list if isinstance(c, dict)],
        available=bool(person_uri),
        max_cast=config.max_cast_concepts,
        safe_styles=config.person_safe_styles,
    )
    for warning in cast_warnings:
        logging.warning(f"casting guard (render): {warning}")
    any_cast = any(
        c.get("casts_person_reference") is True for c in final_visual_concepts_list
    )
    person_part = await fetch_person_photo(person_uri) if any_cast else None
    person_rejected: dict[str, str] = {}

    product = tool_context.state.get("target_product") or ""
    artifact_keys_list = []
    generated_images: dict[str, dict] = {}
    qa_issues: list[str] = []
    qa_unavailable: list[str] = []
    budget = _RerenderBudget(config.image_qa_max_rerenders_per_run)

    # Pipelined: one render_concept task per concept. Renders stay strictly
    # sequential (the image quota) behind one lock and in concept order (each
    # concept's first render waits for the previous one's), but each image's QA
    # (+ any re-render) runs while the next concept renders. asyncio.Lock is
    # FIFO and a re-render queues on it while the next render is in flight, so
    # it runs right after that render, ahead of the following first render. The
    # per-run budget is claimed in concept order. Uploads happen afterwards, in
    # concept order, for the kept image only.
    render_lock = asyncio.Lock()
    tasks: list[asyncio.Task] = []
    previous_rendered: asyncio.Event | None = None
    previous_done: asyncio.Event | None = None
    try:
        for entry in final_visual_concepts_list:
            # Per-concept aspect ratio, unless a valid state override pins all
            # concepts. .get() keeps concepts that only carry
            # image_generation_prompt working (see test_tools_retry).
            aspect_ratio = _resolve_aspect_ratio(
                entry,
                aspect_ratio_override,
                config.image_aspect_ratios_allowed,
                config.image_aspect_ratio_default,
            )
            rendered_event, done = asyncio.Event(), asyncio.Event()
            tasks.append(
                asyncio.create_task(
                    render_concept(
                        entry,
                        aspect_ratio=aspect_ratio,
                        references=references,
                        person=person_part,
                        strictness=strictness,
                        qa=config.image_qa_enabled,
                        brand=brand,
                        target_product=product,
                        budget=budget,
                        pacing=RenderPacing(
                            lock=render_lock,
                            start_after=previous_rendered,
                            rendered=rendered_event,
                            claim_after=previous_done,
                            claims_done=done,
                        ),
                    )
                )
            )
            previous_rendered, previous_done = rendered_event, done

        # A first-render failure aborts the batch at once (later concepts are
        # still waiting for their turn); nothing is uploaded.
        if tasks:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in tasks:
            if task.done() and not task.cancelled():
                failure = task.exception()
                if failure is not None:
                    raise failure
        results = [await task for task in tasks]

        for result in results:
            entry = result.concept
            name = str(entry.get("concept_name") or "")
            if result.rejected_reason is not None:
                person_rejected[name] = result.rejected_reason
            if result.qa_unavailable:
                qa_unavailable.append(entry["concept_name"])
            if result.qa_issue:
                qa_issues.append(result.qa_issue)
            if result.image_bytes is None:
                continue
            artifact_key = artifact_key_for(entry["concept_name"])
            img_gcs_uri = await _store_image(
                tool_context, result.image_bytes, result.mime, artifact_key
            )
            if img_gcs_uri is None:
                continue
            artifact_keys_list.append(artifact_key)
            record = {
                "gcs_uri": img_gcs_uri,
                "artifact_key": artifact_key,
                "attempts": result.attempts,
                "qa": result.qa,
            }
            if person_uri:
                # Only runs with a person reference carry the cast flag; the
                # judge's person gate and the UI read `cast is True`.
                record["cast"] = result.cast
                if result.cast:
                    record["consent_id"] = person_consent_id(person_ref)
            generated_images[entry["concept_name"]] = record
    except Exception as e:
        # Propagate so ADK 2.0 RetryConfig can retry transient infra failures.
        logging.exception(f"No images generated. {e}")
        raise
    finally:
        # On failure/cancellation stop the in-flight render / QA tasks (and
        # retrieve every task's outcome); on success they are all done already.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # Mark as done so subsequent calls are idempotent
    tool_context.state["_images_generated"] = True
    tool_context.state["_generated_artifact_keys"] = artifact_keys_list
    tool_context.state["generated_images"] = generated_images
    if qa_issues:
        # Generic `<key>__issues` marker → collect_degradation_warnings.
        tool_context.state["image_qa__issues"] = qa_issues
    if qa_unavailable:
        # Deliberately NOT a `__issues` key: a failed check is not a quality
        # issue, so collect_degradation_warnings does not surface it.
        tool_context.state["image_qa__unavailable"] = qa_unavailable
    if person_rejected:
        # {concept: reason} for the results page, plus a run note (generic
        # `<key>__issues` marker → collect_degradation_warnings).
        tool_context.state["person_reference_rejected"] = person_rejected
        tool_context.state["person_reference__issues"] = [
            (
                PERSON_UNAVAILABLE_NOTE
                if reason == "photo_unavailable"
                else PERSON_REJECTED_NOTE
            ).format(name=name)
            for name, reason in person_rejected.items()
        ]

    return {
        "status": "success",
        "message": f"Saved img artifacts: {artifact_keys_list} to `gs://{config.GCS_BUCKET_NAME}/{gcs_folder}/{gcs_subdir}`",
    }
