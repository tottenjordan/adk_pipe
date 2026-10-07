"""Image generation tool: the gemini image-model call with quota-paced backoff.

Split out of ``tools.py``; the genai client is now created lazily so importing
this module has no side effects.
"""

import asyncio
import functools
import logging
import random
import urllib.request
from urllib.parse import urlparse

import httpx
from google import genai
from google.adk.tools import ToolContext
from google.genai import errors as genai_errors
from google.genai import types

from agent_common import genai_retry
from agent_common.locations import MODEL_LOCATION

from .config import config
from .gcs_tools import _download_blob, _save_to_gcs, artifact_key_for
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
    "generate_image",
    "reference_roles_summary",
    "resolve_references",
]

# Fetch timeout for an http(s) reference image (stdlib urllib, no new dep).
_REFERENCE_FETCH_TIMEOUT_SECS = 20

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


def _fetch_reference_image(uri: str) -> types.Part | None:
    """Fetch a product/brand reference image as a genai Part, or None on failure.

    Supports ``gs://bucket/object`` (via the GCS client) and ``http(s)://`` URLs
    (via stdlib urllib). Any failure is logged and swallowed so image generation
    always falls back to the text-only path rather than aborting the run.
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
            mime = _reference_mime_for(obj)
        elif uri.startswith("http://") or uri.startswith("https://"):
            with urllib.request.urlopen(
                uri, timeout=_REFERENCE_FETCH_TIMEOUT_SECS
            ) as resp:
                data = resp.read()
            mime = _reference_mime_for(urlparse(uri).path)
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
            return _get_genai_client().models.generate_content(**kwargs)
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
}

# The live probe showed the model copying a reference image's baked-in headline
# into the output, so every reference block ends with this line (once).
REFERENCE_IGNORE_TEXT_LINE = (
    "Ignore any text, captions or watermarks that appear in the reference images."
)


def _reference_prompt(prompt_text: str, roles: list[str]) -> str:
    """The concept prompt plus a numbered reference block — pure.

    ``roles`` are the roles of the reference Parts attached after the prompt,
    in order ("Reference image 1 (product): ..."). No roles → ``prompt_text``
    unchanged (the text-only path).
    """
    if not roles:
        return prompt_text
    lines = [
        f"Reference image {i} ({role}): {_REFERENCE_ROLE_INSTRUCTIONS[role]}"
        for i, role in enumerate(roles, start=1)
    ]
    lines.append(REFERENCE_IGNORE_TEXT_LINE)
    return prompt_text + "\n\n" + "\n".join(lines)


async def _fetch_references(
    refs: list[tuple[str, str]],
) -> list[tuple[str, types.Part]]:
    """Fetch every reference concurrently (off the event loop) → (role, Part).

    A failed fetch (``_fetch_reference_image`` returns None and logs a warning)
    drops only that reference; the rest keep their order.
    """
    parts = await asyncio.gather(
        *(asyncio.to_thread(_fetch_reference_image, uri) for uri, _ in refs)
    )
    fetched = []
    for (uri, role), part in zip(refs, parts, strict=True):
        if part is None:
            logging.warning(f"Skipping {role} reference image '{uri}' (fetch failed)")
            continue
        logging.info(f"Using {role} reference image: {uri}")
        fetched.append((role, part))
    return fetched


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

    # Optional reference images (product / logo / style; `reference_images` plus
    # the legacy single `reference_image_uri`), applied to every concept. All
    # fetched ONCE, concurrently, before the loop; none fetched → text-only.
    references = await _fetch_references(resolve_references(tool_context.state))
    reference_roles = [role for role, _ in references]
    reference_parts = [part for _, part in references]

    # Optional user-supplied deterministic aspect-ratio override. When set to a
    # valid value it pins EVERY concept to that ratio; when empty/invalid, each
    # concept keeps its own LLM-chosen ratio (preserving diversity). Read once.
    aspect_ratio_override = (
        tool_context.state.get("visual_aspect_ratio") or ""
    ).strip()
    if aspect_ratio_override and (
        aspect_ratio_override not in config.image_aspect_ratios_allowed
    ):
        logging.warning(
            f"visual_aspect_ratio override '{aspect_ratio_override}' not in "
            f"allowed set {config.image_aspect_ratios_allowed}; ignoring override."
        )
        aspect_ratio_override = ""
    elif aspect_ratio_override:
        logging.info(f"Applying user aspect-ratio override: {aspect_ratio_override}")

    artifact_keys_list = []
    for entry in final_visual_concepts_list:
        try:
            # Per-concept aspect ratio, unless a valid state override pins all
            # concepts. .get() keeps concepts that only carry
            # image_generation_prompt working (see test_tools_retry).
            aspect_ratio = _resolve_aspect_ratio(
                entry,
                aspect_ratio_override,
                config.image_aspect_ratios_allowed,
                config.image_aspect_ratio_default,
            )

            prompt_text = entry["image_generation_prompt"]
            if reference_parts:
                contents = [
                    _reference_prompt(prompt_text, reference_roles),
                    *reference_parts,
                ]
            else:
                contents = prompt_text
            response = await _generate_image_with_backoff(
                model=config.image_gen_model,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(
                        aspect_ratio=aspect_ratio,
                        image_size=config.image_size,
                    ),
                ),
            )

            # Gemini image models return the image as inline data on a content part,
            # unlike Imagen's generate_images (which returns response.generated_images).
            image_bytes = None
            image_mime_type = "image/png"
            candidates = response.candidates or []
            if candidates and candidates[0].content and candidates[0].content.parts:
                part = _final_image_part(candidates[0].content.parts)
                if part is not None:
                    image_bytes = part.inline_data.data
                    image_mime_type = part.inline_data.mime_type or image_mime_type

            if image_bytes is not None:
                # define artifact key
                artifact_key = artifact_key_for(entry["concept_name"])

                # save img to Cloud Storage (blocking upload — off the event loop).
                # A per-image save failure is logged and skipped so one bad upload
                # doesn't abort the whole batch (_save_to_gcs raises on failure —
                # it never returns an error dict).
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
                    continue

                # save ADK artifact
                img_artifact = types.Part.from_bytes(
                    data=image_bytes, mime_type=image_mime_type
                )
                await tool_context.save_artifact(
                    filename=artifact_key, artifact=img_artifact
                )
                logging.info(
                    f"Saved image artifact, '{artifact_key}', to '{img_gcs_uri}'"
                )
                artifact_keys_list.append(artifact_key)

            else:
                logging.error(f"Error with image generation response: {str(response)}")

        except Exception as e:
            # Propagate so ADK 2.0 RetryConfig can retry transient infra failures.
            logging.exception(f"No images generated. {e}")
            raise

    # Mark as done so subsequent calls are idempotent
    tool_context.state["_images_generated"] = True
    tool_context.state["_generated_artifact_keys"] = artifact_keys_list

    return {
        "status": "success",
        "message": f"Saved img artifacts: {artifact_keys_list} to `gs://{config.GCS_BUCKET_NAME}/{gcs_folder}/{gcs_subdir}`",
    }
