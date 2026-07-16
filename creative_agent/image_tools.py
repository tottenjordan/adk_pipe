"""Image generation tool: the gemini image-model call with quota-paced backoff.

Split out of ``tools.py``; the genai client is now created lazily so importing
this module has no side effects.
"""

import asyncio
import string
import random
import logging

from google import genai
from google.genai import types
from google.genai import errors as genai_errors
from google.adk.tools import ToolContext

from agent_common.locations import MODEL_LOCATION
from .config import config
from .gcs_tools import _save_to_gcs


# Create a translation table to map punctuation characters to None (removal)
REMOVE_PUNCTUATION = str.maketrans("", "", string.punctuation)


def _get_genai_client() -> genai.Client:
    """Get a configured genai client for the image model (lazy, no import-time side effect).

    The image-gen model (gemini-3.1-flash-image) is a gemini-3.x model served only
    from ``global`` — hence MODEL_LOCATION, not config.LOCATION (which is the
    injected regional value inside a deployed Agent Engine).
    """
    return genai.Client(
        vertexai=True,
        project=config.PROJECT_ID,
        location=MODEL_LOCATION,
    )


# The image model (gemini-3.1-flash-image) is capped at ~2 RPM on the `global`
# endpoint (project-wide, shared), and this direct genai call is NOT wrapped by
# ADK's workflow RetryConfig (that only retries Agent *model* calls, not tool
# functions). A concurrent burst reliably trips 503 UNAVAILABLE / 429
# RESOURCE_EXHAUSTED, so we retry here with exponential backoff + jitter to pace
# under quota. See docs/notes/ambient-agents-vs-cloud-functions.md.
_IMAGE_GEN_MAX_ATTEMPTS = 5
_IMAGE_GEN_BASE_DELAY_SECS = 20.0
_IMAGE_GEN_MAX_DELAY_SECS = 90.0


def _is_retryable_genai_error(exc: Exception) -> bool:
    """True for transient/quota-paced genai errors: 5xx (ServerError) and 429."""
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


async def generate_image(
    tool_context: ToolContext,
):
    f"""Generates an image based on the prompt for {config.image_gen_model}

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: Status and the artifact_key of the generated image.
    """
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

    artifact_keys_list = []
    for entry in final_visual_concepts_list:
        try:
            response = await _generate_image_with_backoff(
                model=config.image_gen_model,
                contents=entry["image_generation_prompt"],
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                ),
            )

            # Gemini image models return the image as inline data on a content part,
            # unlike Imagen's generate_images (which returns response.generated_images).
            image_bytes = None
            image_mime_type = "image/png"
            candidates = response.candidates or []
            if candidates and candidates[0].content and candidates[0].content.parts:
                for part in candidates[0].content.parts:
                    if part.inline_data is not None and part.inline_data.data:
                        image_bytes = part.inline_data.data
                        image_mime_type = part.inline_data.mime_type or image_mime_type
                        break

            if image_bytes is not None:
                # define artifact key
                ARTIFACT_NAME = (
                    entry["concept_name"]
                    .translate(REMOVE_PUNCTUATION)
                    .replace(" ", "_")
                )
                artifact_key = f"{ARTIFACT_NAME}.png"

                # save img to Cloud Storage
                img_gcs_uri = _save_to_gcs(
                    tool_context=tool_context,
                    image_bytes=image_bytes,
                    filename=artifact_key,
                )
                if (
                    isinstance(img_gcs_uri, dict)
                    and img_gcs_uri.get("status") == "error"
                ):
                    logging.error(
                        f"GCS upload failed for '{artifact_key}': {img_gcs_uri.get('message')}"
                    )

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
