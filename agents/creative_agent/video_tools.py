"""Phase 3 video production engine: Omni Flash 1.1 / Veo 3.1, TTS/music, and FFmpeg conform."""

from __future__ import annotations

import array
import asyncio
import base64
import contextlib
import io
import logging
import math
import os
import shutil
import tempfile
import wave
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import imageio_ffmpeg
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from agent_common.clients import get_gcs_client
from agent_common.genai_retry import (
    build_genai_http_retry,
    model_request_timeout_ms,
)
from agent_common.locations import MODEL_LOCATION
from agent_common.sanitize import scrub_lone_surrogates

from . import references, video_qa
from .config import config, is_video_production_enabled, parse_video_gen_backend
from .gcs_tools import artifact_key_for
from .references import resolve_references
from .schemas import VideoCutFormat

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS: float = 3.0
_MAX_POLL_ATTEMPTS: int = 120
_FFMPEG_TIMEOUT_SECONDS: float = 180.0
_AUDIO_SAMPLE_RATE: int = 24000
_FALLBACK_HTTP_CODES = frozenset({403, 404, 500, 501, 502, 503, 504})
_PENDING_INTERACTION_STATUSES = frozenset(
    {"in_progress", "queued", "pending", "running", "processing"}
)
_FAILED_INTERACTION_STATUSES = frozenset(
    {"failed", "cancelled", "budget_exceeded", "incomplete"}
)
_VIDEO_REFERENCE_ROLES = frozenset({"product", "cast"})
_VIDEO_OUTPUT_MAX_BYTES: int = (
    100 * 1024 * 1024
)  # 100 MiB ceiling for model media outputs

_video_genai_client: genai.Client | None = None
_get_gcs_client = get_gcs_client


@dataclass(frozen=True)
class CutDefinition:
    """Target deliverables for multi-cut commercial conform."""

    cut: VideoCutFormat
    label: str
    width: int
    height: int
    aspect_ratio: str
    max_duration_seconds: int


CUT_DEFINITIONS: tuple[CutDefinition, ...] = (
    CutDefinition(
        cut="hero_30s_16x9",
        label="30s Hero (16:9)",
        width=1280,
        height=720,
        aspect_ratio="16:9",
        max_duration_seconds=30,
    ),
    CutDefinition(
        cut="social_15s_9x16",
        label="15s Social (9:16)",
        width=720,
        height=1280,
        aspect_ratio="9:16",
        max_duration_seconds=15,
    ),
    CutDefinition(
        cut="bumper_6s_9x16",
        label="6s Bumper (9:16)",
        width=720,
        height=1280,
        aspect_ratio="9:16",
        max_duration_seconds=6,
    ),
)


@dataclass(frozen=True)
class ReferenceImage:
    """Resolved reference image bytes + role for `<IMAGE_REF_N>` shot anchoring."""

    role: str
    data: bytes
    mime_type: str = "image/png"


@dataclass
class RenderedShot:
    """Rendered `.mp4` clip and QA metadata for a single storyboard shot."""

    shot_number: int
    duration_seconds: float
    mp4_bytes: bytes
    backend: str
    interaction_id: str | None
    qa_verdict: video_qa.VideoQAVerdict | None
    retakes_used: int = 0
    voiceover_line: str = ""
    include_in_15s: bool = False
    include_in_6s: bool = False


def _get_video_genai_client() -> genai.Client:
    """Lazy Vertex GenAI client @ `global` for video, TTS, and music generation."""
    global _video_genai_client
    if _video_genai_client is None:
        _video_genai_client = genai.Client(
            vertexai=True,
            project=config.PROJECT_ID,
            location=MODEL_LOCATION,
            http_options=types.HttpOptions(
                retry_options=build_genai_http_retry(),
                timeout=model_request_timeout_ms(),
            ),
        )
    return _video_genai_client


async def _async_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def resolve_ffmpeg_binary() -> str:
    """Resolve system `ffmpeg` when installed, else `imageio-ffmpeg`'s static binary."""
    return shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()


def _decode_media_bytes(raw: Any) -> bytes | None:
    """Decode raw bytes or a base64-encoded string returned by GenAI media APIs."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray, memoryview)):
        data = bytes(raw)
        # Some SDK wrappers return base64 ASCII stored inside a `bytes` buffer.
        if (
            data
            and not data.startswith(b"\x00\x00\x00")
            and not data.startswith(b"RIFF")
            and len(data) % 4 == 0
        ):
            try:
                decoded = base64.b64decode(data, validate=True)
                if decoded.startswith(b"RIFF") or b"ftyp" in decoded[:32]:
                    return decoded
            except Exception:
                pass
        return data if data else None
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        try:
            decoded = base64.b64decode(stripped)
            return decoded if decoded else None
        except Exception:
            return None
    return None


def _download_gcs_uri(
    gcs_uri: str,
    *,
    allowed_bucket: str | None = None,
    max_bytes: int | None = None,
) -> bytes:
    """Download bytes from a `gs://bucket/blob` URI via the shared GCS client.

    Enforces path-traversal hygiene (`\\x00`, `\\`, empty/`"."`/`".."` segments),
    optional `allowed_bucket` confinement, and optional `max_bytes` size cap
    (checked on `blob.size` before downloading when metadata is available, and
    on the downloaded buffer length afterwards).
    """
    if "\x00" in gcs_uri or "\\" in gcs_uri:
        raise ValueError(f"Unsafe characters in gs:// URI: {gcs_uri!r}")
    if not gcs_uri.startswith("gs://"):
        raise ValueError(f"Expected gs:// URI, got {gcs_uri!r}")
    rest = gcs_uri[5:]
    bucket_name, sep, blob_name = rest.partition("/")
    if not sep or not bucket_name or not blob_name:
        raise ValueError(f"Malformed gs:// URI: {gcs_uri!r}")
    segments = blob_name.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise ValueError(f"Unsafe path segment in gs:// URI: {gcs_uri!r}")
    if allowed_bucket is not None:
        normalized_allowed = allowed_bucket.strip().removeprefix("gs://").strip("/")
        if not normalized_allowed or bucket_name != normalized_allowed:
            raise ValueError(
                f"Cross-bucket GCS URI rejected: {bucket_name!r} != {normalized_allowed!r}"
            )
    storage_client = _get_gcs_client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    if max_bytes is not None:
        size = getattr(blob, "size", None)
        reload_fn = getattr(blob, "reload", None)
        if size is None and callable(reload_fn):
            with contextlib.suppress(AttributeError, NotImplementedError, TypeError):
                reload_fn()
            size = getattr(blob, "size", None)
        if isinstance(size, int) and not isinstance(size, bool) and size > max_bytes:
            raise ValueError(
                f"GCS blob {gcs_uri!r} exceeds max_bytes ({size} > {max_bytes})"
            )
    data = blob.download_as_bytes()
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError(
            f"GCS blob {gcs_uri!r} exceeds max_bytes ({len(data)} > {max_bytes})"
        )
    return data


def _upload_video_bytes_to_gcs(
    mp4_bytes: bytes,
    *,
    bucket_name: str,
    blob_name: str,
) -> str:
    """Upload conformed `.mp4` bytes to GCS and return the `gs://` URI."""
    storage_client = _get_gcs_client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(mp4_bytes, content_type="video/mp4")
    return f"gs://{bucket_name}/{blob_name}"


# ---------------------------------------------------------------------------
# Keyframe & Role-Gated Reference Preparation
# ---------------------------------------------------------------------------


def _match_anchor_concept(
    anchor_name: str,
    concepts: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    target = anchor_name.strip().lower()
    for concept in concepts:
        cname = str(concept.get("concept_name") or "").strip()
        if cname.lower() == target:
            return concept
    return concepts[0] if concepts else {}


def _lookup_generated_image_record(
    concept_name: str,
    generated_images: Mapping[str, Any],
) -> dict[str, Any]:
    if concept_name in generated_images and isinstance(
        generated_images[concept_name], Mapping
    ):
        return dict(generated_images[concept_name])
    lower = concept_name.strip().lower()
    for key, val in generated_images.items():
        if str(key).strip().lower() == lower and isinstance(val, Mapping):
            return dict(val)
    return {}


async def _load_keyframe_bytes(
    ctx: Any,
    concept_name: str,
    generated_images: Mapping[str, Any],
) -> tuple[bytes | None, str]:
    """Load Phase 2 keyframe PNG/JPEG bytes from ADK artifacts or GCS."""
    rec = _lookup_generated_image_record(concept_name, generated_images)
    artifact_key = str(rec.get("artifact_key") or "").strip()
    if not artifact_key and concept_name:
        artifact_key = artifact_key_for(concept_name)

    load_fn = getattr(ctx, "load_artifact", None)
    if callable(load_fn) and artifact_key:
        try:
            loaded = load_fn(filename=artifact_key)
            part = await loaded if asyncio.iscoroutine(loaded) else loaded
            inline = getattr(part, "inline_data", None)
            raw_data = getattr(inline, "data", None) if inline is not None else None
            decoded = _decode_media_bytes(raw_data)
            if decoded:
                mime = str(getattr(inline, "mime_type", "") or "image/png")
                return decoded, mime
        except Exception as exc:
            logger.debug(
                "ADK load_artifact(%r) missed; trying GCS: %s", artifact_key, exc
            )

    gcs_uri = str(rec.get("gcs_uri") or "").strip()
    allowed_bucket = str(config.GCS_BUCKET_NAME or "").strip()
    if gcs_uri.startswith("gs://") and allowed_bucket:
        try:
            data = await asyncio.to_thread(
                _download_gcs_uri,
                gcs_uri,
                allowed_bucket=allowed_bucket,
                max_bytes=references.REFERENCE_MAX_BYTES,
            )
            if data:
                return data, references.reference_mime_for(gcs_uri)
        except Exception as exc:
            logger.warning("Failed to download keyframe %s from GCS: %s", gcs_uri, exc)

    return None, "image/png"


def select_video_references_for_concept(
    fetched_references: Sequence[ReferenceImage],
    composition_mode: str | None,
) -> list[ReferenceImage]:
    """Filter reference images for a shot, respecting `composition_mode` role gating.

    Strictly omits `"cast"` references for `product_hero` and `graphic_lockup`,
    and restricts video `<IMAGE_REF_N>` bindings to `"product"` and `"cast"` roles.
    """
    paired = [(ref.role, ref) for ref in fetched_references]
    filtered, _ = references.filter_references_for_composition(
        paired,
        [],
        composition_mode,
    )
    return [ref for role, ref in filtered if role in _VIDEO_REFERENCE_ROLES]


def build_omniflash_shot_inputs(
    shot: Mapping[str, Any],
    *,
    concept: Mapping[str, Any],
    keyframe_bytes: bytes | None,
    keyframe_mime: str,
    references: Sequence[ReferenceImage],
    brand: str,
    target_product: str,
    reference_description: str,
) -> tuple[list[dict[str, Any]], str]:
    """Construct multimodal `input` items for Omni Flash 1.1 and a text prompt."""
    items: list[dict[str, Any]] = []
    anchor_lines: list[str] = []

    if keyframe_bytes:
        items.append({"type": "text", "text": "<FIRST_FRAME>"})
        items.append(
            {
                "type": "image",
                "data": base64.b64encode(keyframe_bytes).decode("ascii"),
                "mime_type": keyframe_mime or "image/png",
            }
        )
        anchor_lines.append(
            "- Anchor the opening frame and scene continuity on <FIRST_FRAME>."
        )

    for idx, ref in enumerate(references):
        tag = f"<IMAGE_REF_{idx}>"
        items.append({"type": "text", "text": f"{tag} ({ref.role} reference)"})
        items.append(
            {
                "type": "image",
                "data": base64.b64encode(ref.data).decode("ascii"),
                "mime_type": ref.mime_type or "image/png",
            }
        )
        anchor_lines.append(f"- Preserve exact {ref.role} visual identity from {tag}.")

    duration = int(shot.get("duration_seconds") or 6)
    camera_motion = str(shot.get("camera_motion") or "").strip()
    visual_action = str(shot.get("visual_action_prompt") or "").strip()
    super_text = str(shot.get("on_screen_super") or "").strip()
    audio_cue = str(shot.get("audio_cue") or "").strip()
    visual_style = str(concept.get("visual_style") or "cinematic commercial").strip()
    comp_mode = str(concept.get("composition_mode") or "product_in_scene").strip()

    comp_rule = ""
    if comp_mode == "product_hero":
        comp_rule = "Product-only hero framing: zero people, faces, or human hands."
    elif comp_mode == "graphic_lockup":
        comp_rule = "Graphic design lockup framing: zero human models."

    super_rule = (
        f'Clean typographic on-screen super reading verbatim: "{super_text}".'
        if super_text
        else "Do not render any overlaid text, subtitles, or watermarks."
    )
    ref_desc_line = (
        f"Product visual specification: {reference_description.strip()}\n"
        if reference_description.strip()
        else ""
    )
    anchors_block = ("\n".join(anchor_lines) + "\n") if anchor_lines else ""

    prompt_text = (
        f"Commercial video shot ({duration}s, 16:9, style: {visual_style}) for "
        f"{brand or 'the brand'} featuring {target_product or 'the product'}.\n"
        f"{anchors_block}"
        f"{ref_desc_line}"
        f"Camera motion: {camera_motion}\n"
        f"Visual action: {visual_action}\n"
        f"{f'Framing rule: {comp_rule}\n' if comp_rule else ''}"
        f"Text overlay rule: {super_rule}\n"
        f"Audio direction: {audio_cue or 'Subtle clean ambient foley only'}; "
        "no spoken dialogue or lip movement (studio voiceover is mixed in post)."
    )
    items.append({"type": "text", "text": prompt_text})
    return items, prompt_text


# ---------------------------------------------------------------------------
# Omni Flash 1.1 & Veo 3.1 Shot Renderers
# ---------------------------------------------------------------------------


def _should_fallback_to_veo(exc: BaseException) -> bool:
    """Return True when an Omni Flash error warrants automatic Veo 3.1 fallback."""
    if isinstance(exc, genai_errors.ServerError):
        return True
    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        if code in _FALLBACK_HTTP_CODES:
            return True
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code in _FALLBACK_HTTP_CODES:
            return True
    if isinstance(exc, (NotImplementedError, AttributeError)):
        return True
    return False


def _extract_interaction_media_bytes(
    interaction: Any,
    *,
    media_kind: str,
) -> bytes | None:
    """Extract video or audio bytes from an `Interaction` object or dict."""
    direct_attr = f"output_{media_kind}"
    direct = (
        interaction.get(direct_attr)
        if isinstance(interaction, Mapping)
        else getattr(interaction, direct_attr, None)
    )
    candidates: list[Any] = []
    if direct is not None:
        candidates.append(direct)

    outputs = (
        interaction.get("outputs")
        if isinstance(interaction, Mapping)
        else getattr(interaction, "outputs", None)
    )
    if isinstance(outputs, Sequence) and not isinstance(outputs, (str, bytes)):
        for item in outputs:
            item_type = (
                item.get("type")
                if isinstance(item, Mapping)
                else getattr(item, "type", None)
            )
            item_mime = str(
                (
                    item.get("mime_type")
                    if isinstance(item, Mapping)
                    else getattr(item, "mime_type", None)
                )
                or ""
            )
            if item_type == media_kind or item_mime.startswith(f"{media_kind}/"):
                candidates.append(item)

    steps = (
        interaction.get("steps")
        if isinstance(interaction, Mapping)
        else getattr(interaction, "steps", None)
    )
    if isinstance(steps, Sequence) and not isinstance(steps, (str, bytes)):
        for step in steps:
            step_content = (
                step.get("content")
                if isinstance(step, Mapping)
                else getattr(step, "content", None)
            )
            items = (
                step_content
                if isinstance(step_content, Sequence)
                and not isinstance(step_content, (str, bytes))
                else [step_content]
                if step_content is not None
                else []
            )
            for item in items:
                item_type = (
                    item.get("type")
                    if isinstance(item, Mapping)
                    else getattr(item, "type", None)
                )
                if item_type == media_kind:
                    candidates.append(item)

    for candidate in candidates:
        raw_data = (
            candidate.get("data")
            if isinstance(candidate, Mapping)
            else getattr(candidate, "data", None)
        )
        decoded = _decode_media_bytes(raw_data)
        if decoded:
            return decoded
        uri = str(
            (
                candidate.get("uri")
                if isinstance(candidate, Mapping)
                else getattr(candidate, "uri", None)
            )
            or ""
        ).strip()
        if uri.startswith("gs://"):
            return _download_gcs_uri(uri, max_bytes=_VIDEO_OUTPUT_MAX_BYTES)

    return None


async def _poll_interaction(client: Any, interaction: Any) -> Any:
    """Poll `client.interactions.get(id)` off-loop while status is pending/in_progress."""
    current = interaction
    for _ in range(_MAX_POLL_ATTEMPTS):
        status = str(
            (
                current.get("status")
                if isinstance(current, Mapping)
                else getattr(current, "status", None)
            )
            or "completed"
        ).lower()
        if status not in _PENDING_INTERACTION_STATUSES:
            if status in _FAILED_INTERACTION_STATUSES:
                err_detail = (
                    current.get("errors")
                    if isinstance(current, Mapping)
                    else getattr(current, "errors", None)
                )
                raise RuntimeError(
                    f"Interaction ended with status={status!r}: {err_detail}"
                )
            return current

        interaction_id = str(
            (
                current.get("id")
                if isinstance(current, Mapping)
                else getattr(current, "id", None)
            )
            or ""
        ).strip()
        if not interaction_id:
            raise RuntimeError("Pending interaction returned no id for polling.")
        await _async_sleep(_POLL_INTERVAL_SECONDS)
        current = await asyncio.to_thread(client.interactions.get, interaction_id)

    raise TimeoutError("Timed out polling Omni Flash interaction completion.")


async def _render_shot_omniflash(
    client: Any,
    *,
    input_items: list[dict[str, Any]],
    duration_seconds: int,
    previous_interaction_id: str | None = None,
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
) -> tuple[bytes, str | None]:
    """Render a shot via Omni Flash 1.1 (`client.interactions.create`, `store=True`)."""
    create_kwargs: dict[str, Any] = {
        "model": config.omniflash_video_model,
        "input": input_items,
        "store": True,
        "response_format": {
            "type": "video",
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "duration": f"{int(duration_seconds)}s",
        },
    }
    if previous_interaction_id:
        create_kwargs["previous_interaction_id"] = previous_interaction_id

    interaction = await asyncio.to_thread(
        lambda: client.interactions.create(**create_kwargs)
    )
    completed = await _poll_interaction(client, interaction)
    mp4_bytes = await asyncio.to_thread(
        _extract_interaction_media_bytes, completed, media_kind="video"
    )
    if not mp4_bytes:
        raise RuntimeError(
            "Omni Flash interaction completed without output video bytes."
        )
    interaction_id = (
        str(
            (
                completed.get("id")
                if isinstance(completed, Mapping)
                else getattr(completed, "id", None)
            )
            or ""
        ).strip()
        or None
    )
    return mp4_bytes, interaction_id


async def _render_shot_veo(
    client: Any,
    *,
    prompt: str,
    duration_seconds: int,
    keyframe_bytes: bytes | None = None,
    keyframe_mime: str = "image/png",
    aspect_ratio: str = "16:9",
) -> tuple[bytes, None]:
    """Render a shot via Veo 3.1 (`client.models.generate_videos` + `operations.get`)."""
    image_arg = (
        types.Image(image_bytes=keyframe_bytes, mime_type=keyframe_mime or "image/png")
        if keyframe_bytes
        else None
    )
    source = types.GenerateVideosSource(prompt=prompt, image=image_arg)
    veo_config = types.GenerateVideosConfig(
        aspect_ratio=aspect_ratio,
        duration_seconds=int(duration_seconds),
        number_of_videos=1,
    )
    operation = await asyncio.to_thread(
        lambda: client.models.generate_videos(
            model=config.veo_video_model,
            source=source,
            config=veo_config,
        )
    )
    for _ in range(_MAX_POLL_ATTEMPTS):
        if getattr(operation, "done", False):
            break
        await _async_sleep(_POLL_INTERVAL_SECONDS)
        operation = await asyncio.to_thread(client.operations.get, operation)
    else:
        raise TimeoutError("Timed out polling Veo 3.1 video generation operation.")

    op_error = getattr(operation, "error", None)
    if op_error:
        raise RuntimeError(f"Veo 3.1 video operation failed: {op_error}")

    response = getattr(operation, "response", None) or getattr(
        operation, "result", None
    )
    gen_videos = getattr(response, "generated_videos", None) if response else None
    if not gen_videos:
        raise RuntimeError("Veo 3.1 completed without generated_videos.")

    video_obj = getattr(gen_videos[0], "video", None)
    if video_obj is None:
        raise RuntimeError("Veo 3.1 GeneratedVideo entry has no video payload.")

    raw_bytes = getattr(video_obj, "video_bytes", None)
    decoded = _decode_media_bytes(raw_bytes)
    if decoded:
        return decoded, None

    uri = str(getattr(video_obj, "uri", "") or "").strip()
    if uri.startswith("gs://"):
        downloaded = await asyncio.to_thread(
            _download_gcs_uri, uri, max_bytes=_VIDEO_OUTPUT_MAX_BYTES
        )
        if downloaded:
            return downloaded, None

    raise RuntimeError("Veo 3.1 video payload had neither video_bytes nor gs:// uri.")


async def _render_shot_with_backend_policy(
    client: Any,
    *,
    requested_backend: str,
    active_backend: str,
    input_items: list[dict[str, Any]],
    prompt_text: str,
    duration_seconds: int,
    keyframe_bytes: bytes | None,
    keyframe_mime: str,
    previous_interaction_id: str | None = None,
) -> tuple[bytes, str | None, str]:
    """Render one shot according to `VIDEO_GEN_BACKEND` (`auto` | `omniflash` | `veo`).

    Returns ``(mp4_bytes, interaction_id, resolved_active_backend)``.
    """
    if active_backend == "veo":
        mp4_bytes, _ = await _render_shot_veo(
            client,
            prompt=prompt_text,
            duration_seconds=duration_seconds,
            keyframe_bytes=keyframe_bytes,
            keyframe_mime=keyframe_mime,
        )
        return mp4_bytes, None, "veo"

    try:
        mp4_bytes, interaction_id = await _render_shot_omniflash(
            client,
            input_items=input_items,
            duration_seconds=duration_seconds,
            previous_interaction_id=previous_interaction_id,
        )
        return mp4_bytes, interaction_id, "omniflash"
    except Exception as exc:
        if requested_backend == "auto" and _should_fallback_to_veo(exc):
            logger.warning(
                "Omni Flash 1.1 failed (%s); falling back automatically to Veo 3.1 (%s).",
                exc,
                config.veo_video_model,
            )
            mp4_bytes, _ = await _render_shot_veo(
                client,
                prompt=prompt_text,
                duration_seconds=duration_seconds,
                keyframe_bytes=keyframe_bytes,
                keyframe_mime=keyframe_mime,
            )
            return mp4_bytes, None, "veo"
        raise


async def render_and_qa_shot(
    client: Any,
    *,
    shot: Mapping[str, Any],
    concept: Mapping[str, Any],
    keyframe_bytes: bytes | None,
    keyframe_mime: str,
    references: Sequence[ReferenceImage],
    requested_backend: str,
    active_backend: str,
    brand: str,
    target_product: str,
    avoid_terms: Sequence[str],
    reference_description: str,
    max_retakes: int,
) -> tuple[RenderedShot, str]:
    """Render a shot, run multimodal QA, and execute bounded retakes if needed."""
    shot_num = int(shot.get("shot_number") or 1)
    duration = int(shot.get("duration_seconds") or 6)
    input_items, prompt_text = build_omniflash_shot_inputs(
        shot,
        concept=concept,
        keyframe_bytes=keyframe_bytes,
        keyframe_mime=keyframe_mime,
        references=references,
        brand=brand,
        target_product=target_product,
        reference_description=reference_description,
    )

    (
        best_bytes,
        best_interaction_id,
        active_backend,
    ) = await _render_shot_with_backend_policy(
        client,
        requested_backend=requested_backend,
        active_backend=active_backend,
        input_items=input_items,
        prompt_text=prompt_text,
        duration_seconds=duration,
        keyframe_bytes=keyframe_bytes,
        keyframe_mime=keyframe_mime,
    )
    best_backend = active_backend

    best_verdict = await asyncio.to_thread(
        video_qa.inspect_video_shot,
        best_bytes,
        shot,
        brand=brand,
        target_product=target_product,
        avoid_terms=avoid_terms,
        reference_description=reference_description,
        client=client,
    )

    retakes_used = 0
    if best_verdict is not None and not video_qa.qa_passed(best_verdict):
        for _ in range(max(0, min(2, int(max_retakes)))):
            retakes_used += 1
            retake_diff = video_qa.build_omniflash_retake_prompt(best_verdict, shot)
            veo_repair = video_qa.build_veo_repair_prompt(prompt_text, best_verdict)
            try:
                if active_backend == "omniflash" and best_interaction_id:
                    (
                        cand_bytes,
                        cand_id,
                        active_backend,
                    ) = await _render_shot_with_backend_policy(
                        client,
                        requested_backend=requested_backend,
                        active_backend=active_backend,
                        input_items=[{"type": "text", "text": retake_diff}],
                        prompt_text=veo_repair,
                        duration_seconds=duration,
                        keyframe_bytes=keyframe_bytes,
                        keyframe_mime=keyframe_mime,
                        previous_interaction_id=best_interaction_id,
                    )
                    cand_backend = active_backend
                else:
                    cand_bytes, cand_id = await _render_shot_veo(
                        client,
                        prompt=veo_repair,
                        duration_seconds=duration,
                        keyframe_bytes=keyframe_bytes,
                        keyframe_mime=keyframe_mime,
                    )
                    cand_backend = "veo"
            except Exception as exc:
                logger.warning(
                    "Shot %s retake %s failed (keeping prior clip): %s",
                    shot_num,
                    retakes_used,
                    exc,
                )
                break

            cand_verdict = await asyncio.to_thread(
                video_qa.inspect_video_shot,
                cand_bytes,
                shot,
                brand=brand,
                target_product=target_product,
                avoid_terms=avoid_terms,
                reference_description=reference_description,
                client=client,
            )
            best_score = video_qa.qa_score(best_verdict)
            cand_score = (
                video_qa.qa_score(cand_verdict)
                if cand_verdict is not None
                else best_score
            )
            if cand_verdict is not None and cand_score > best_score:
                best_bytes = cand_bytes
                best_interaction_id = cand_id
                best_verdict = cand_verdict
                best_backend = cand_backend

            if video_qa.qa_passed(best_verdict):
                break

    rendered = RenderedShot(
        shot_number=shot_num,
        duration_seconds=float(duration),
        mp4_bytes=best_bytes,
        backend=best_backend,
        interaction_id=best_interaction_id,
        qa_verdict=best_verdict,
        retakes_used=retakes_used,
        voiceover_line=str(shot.get("voiceover_line") or "").strip(),
        include_in_15s=bool(shot.get("include_in_15s")),
        include_in_6s=bool(shot.get("include_in_6s")),
    )
    return rendered, active_backend


# ---------------------------------------------------------------------------
# Vertex-Compatible TTS Voiceover & Fail-Soft Instrumental Music
# (NEVER calls client.aio.live.music, which is unsupported on Vertex AI)
# ---------------------------------------------------------------------------


def wrap_pcm_as_wav(
    pcm_or_wav: bytes,
    *,
    sample_rate: int = _AUDIO_SAMPLE_RATE,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """Wrap raw 16-bit signed little-endian PCM bytes in a RIFF `.wav` container."""
    if pcm_or_wav.startswith(b"RIFF") and b"WAVE" in pcm_or_wav[:16]:
        return pcm_or_wav
    frame_size = max(1, channels * sample_width)
    remainder = len(pcm_or_wav) % frame_size
    aligned = (
        pcm_or_wav if remainder == 0 else pcm_or_wav[: len(pcm_or_wav) - remainder]
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(aligned)
    return buf.getvalue()


def _normalize_wav_to_duration_pcm(
    audio_bytes: bytes,
    *,
    duration_seconds: float,
    target_rate: int = _AUDIO_SAMPLE_RATE,
) -> bytes:
    """Normalize WAV or raw 24kHz 16-bit mono PCM to exact `duration_seconds` length."""
    target_samples = max(1, int(round(duration_seconds * target_rate)))
    target_byte_len = target_samples * 2

    wav_bytes = wrap_pcm_as_wav(audio_bytes, sample_rate=target_rate)
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    except wave.Error:
        frames = audio_bytes
        n_channels = 1
        sampwidth = 2
        framerate = target_rate

    if sampwidth != 2 or not frames:
        raw_pcm = frames[:target_byte_len]
        return raw_pcm.ljust(target_byte_len, b"\x00")

    even_len = len(frames) - (len(frames) % 2)
    samples = array.array("h")
    samples.frombytes(frames[:even_len])

    if n_channels > 1:
        mono = array.array(
            "h",
            (
                int(sum(samples[i : i + n_channels]) / n_channels)
                for i in range(0, len(samples) - n_channels + 1, n_channels)
            ),
        )
    else:
        mono = samples

    if framerate > 0 and framerate != target_rate and len(mono) > 1:
        out_count = max(1, int(round(len(mono) * target_rate / framerate)))
        ratio = (len(mono) - 1) / max(1, out_count - 1)
        resampled = array.array(
            "h",
            (
                mono[min(len(mono) - 1, int(round(idx * ratio)))]
                for idx in range(out_count)
            ),
        )
        mono = resampled

    raw_bytes = mono.tobytes()
    if len(raw_bytes) >= target_byte_len:
        return raw_bytes[:target_byte_len]
    return raw_bytes + (b"\x00" * (target_byte_len - len(raw_bytes)))


def _extract_generate_content_audio_bytes(response: Any) -> bytes | None:
    """Extract inline audio bytes from a `generate_content(response_modalities=['AUDIO'])` response."""
    candidates = getattr(response, "candidates", None) or []
    for cand in candidates:
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline is not None:
                data = _decode_media_bytes(getattr(inline, "data", None))
                if data:
                    return data
    return None


def synthesize_voiceover_clip_sync(
    voiceover_line: str,
    *,
    duration_seconds: float,
    voiceover_style: str = "",
    voice_name: str = "Kore",
    client: Any = None,
) -> bytes | None:
    """Synthesize a single shot's voiceover line to exact-duration 24kHz mono PCM.

    Fail-soft: returns ``None`` on empty input or TTS error so video conform
    never fails when a TTS preview model is unavailable.
    """
    clean_line = scrub_lone_surrogates(voiceover_line or "").strip()
    if not clean_line:
        return None

    tts_client = client if client is not None else _get_video_genai_client()
    style_prefix = (
        f"Speak in a {voiceover_style.strip()} commercial voiceover tone: "
        if voiceover_style.strip()
        else "Speak clearly as a polished commercial narrator: "
    )

    try:
        response = tts_client.models.generate_content(
            model=config.tts_model,
            contents=f"{style_prefix}{clean_line}",
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=voice_name
                        )
                    )
                ),
            ),
        )
        audio_bytes = _extract_generate_content_audio_bytes(response)
        if not audio_bytes:
            logger.warning("TTS returned no audio bytes for line %r", clean_line[:60])
            return None
        return _normalize_wav_to_duration_pcm(
            audio_bytes, duration_seconds=duration_seconds
        )
    except Exception as exc:
        logger.warning("TTS synthesis failed (fail-soft to silent VO): %s", exc)
        return None


def build_cut_voiceover_wav(
    shots: Sequence[RenderedShot],
    shot_pcm_map: Mapping[int, bytes | None],
) -> tuple[bytes | None, bool]:
    """Assemble a cut-aligned RIFF `.wav` voiceover track from per-shot PCM buffers.

    Returns ``(wav_bytes, has_spoken_audio)``.
    """
    if not shots:
        return None, False

    has_spoken = False
    pcm_chunks: list[bytes] = []
    for shot in shots:
        target_bytes = (
            max(1, int(round(shot.duration_seconds * _AUDIO_SAMPLE_RATE))) * 2
        )
        clip_pcm = shot_pcm_map.get(shot.shot_number)
        if clip_pcm:
            has_spoken = True
            if len(clip_pcm) >= target_bytes:
                pcm_chunks.append(clip_pcm[:target_bytes])
            else:
                pcm_chunks.append(clip_pcm + b"\x00" * (target_bytes - len(clip_pcm)))
        else:
            pcm_chunks.append(b"\x00" * target_bytes)

    if not has_spoken:
        return None, False
    return wrap_pcm_as_wav(b"".join(pcm_chunks), sample_rate=_AUDIO_SAMPLE_RATE), True


def generate_fallback_ambient_wav(
    duration_seconds: float,
    *,
    sample_rate: int = _AUDIO_SAMPLE_RATE,
) -> bytes:
    """Synthesize a clean, low-volume ambient instrumental chord pad `.wav` in pure Python.

    Used when Vertex `music_model` (`lyria-002`) is unavailable so multi-cut
    FFmpeg sidechain ducking and loudness normalization always have a valid WAV stem.
    """
    total_samples = max(1, int(round(max(0.5, duration_seconds) * sample_rate)))
    fade_in = max(1, min(int(0.4 * sample_rate), total_samples // 4))
    fade_out = max(1, min(int(0.6 * sample_rate), total_samples // 4))

    # Warm Cmaj7 instrumental pad frequencies (Hz) with gentle harmonic weights.
    tones = (
        (130.81, 0.34),  # C3
        (196.00, 0.26),  # G3
        (246.94, 0.18),  # B3
        (261.63, 0.14),  # C4
        (329.63, 0.08),  # E4
    )
    two_pi_over_sr = (2.0 * math.pi) / sample_rate
    peak_amp = 4200.0  # ~ -18 dBFS headroom before ducking/loudnorm

    samples = array.array("h", [0]) * total_samples
    for idx in range(total_samples):
        val = 0.0
        for freq, weight in tones:
            val += weight * math.sin(two_pi_over_sr * freq * idx)
        # Subtle slow breathing LFO (0.25 Hz) so the pad feels musical, not static.
        lfo = 0.88 + 0.12 * math.sin(two_pi_over_sr * 0.25 * idx)
        env = 1.0
        if idx < fade_in:
            env = idx / fade_in
        elif idx > total_samples - fade_out:
            env = (total_samples - idx) / fade_out
        samples[idx] = int(peak_amp * val * lfo * env)

    return wrap_pcm_as_wav(samples.tobytes(), sample_rate=sample_rate)


async def synthesize_music_wav(
    music_direction: str,
    *,
    duration_seconds: float = 30.0,
    brand: str = "",
    client: Any = None,
) -> tuple[bytes, bool]:
    """Synthesize an instrumental music bed via Vertex GenAI, degrading fail-soft.

    Returns ``(wav_bytes, used_model)``. Never calls ``client.aio.live.music``
    (which raises ``NotImplementedError`` on ``vertexai=True``).
    """
    music_client = client if client is not None else _get_video_genai_client()
    direction = scrub_lone_surrogates(music_direction or "").strip()
    prompt = (
        f"Instrumental commercial background music ({int(round(duration_seconds))}s), "
        f"strictly instrumental with no vocals or lyrics, for {brand or 'a modern brand'}. "
        f"Direction: {direction or 'uplifting modern cinematic pulse, warm synths and light percussion'}."
    )

    try:
        interaction = await asyncio.to_thread(
            lambda: music_client.interactions.create(
                model=config.music_model,
                input=prompt,
                response_format={
                    "type": "audio",
                    "mime_type": "audio/wav",
                },
            )
        )
        completed = await _poll_interaction(music_client, interaction)
        raw_audio = await asyncio.to_thread(
            _extract_interaction_media_bytes, completed, media_kind="audio"
        )
        if raw_audio:
            return wrap_pcm_as_wav(raw_audio, sample_rate=_AUDIO_SAMPLE_RATE), True
    except Exception as exc:
        logger.warning(
            "Instrumental music model %r unavailable (fail-soft to ambient pad): %s",
            config.music_model,
            exc,
        )

    fallback_wav = await asyncio.to_thread(
        generate_fallback_ambient_wav, duration_seconds
    )
    return fallback_wav, False


# ---------------------------------------------------------------------------
# Deterministic FFmpeg Multi-Cut Conform (30s 16:9, 15s 9:16, 6s 9:16)
# ---------------------------------------------------------------------------


def select_shots_for_cut(
    cut: VideoCutFormat,
    shots: Sequence[RenderedShot],
) -> list[RenderedShot]:
    """Select ordered shots for `hero_30s_16x9`, `social_15s_9x16`, or `bumper_6s_9x16`."""
    if not shots:
        return []
    if cut == "hero_30s_16x9":
        return list(shots)
    if cut == "social_15s_9x16":
        picked = [s for s in shots if s.include_in_15s]
        if picked:
            return picked
        # Graceful fallback if storyboard omitted 15s flags after fail-soft.
        acc: list[RenderedShot] = []
        running = 0.0
        for shot in shots:
            if running + shot.duration_seconds <= 16.0 or len(acc) < 2:
                acc.append(shot)
                running += shot.duration_seconds
            if running >= 14.0:
                break
        return acc or list(shots[:2])
    if cut == "bumper_6s_9x16":
        picked = [s for s in shots if s.include_in_6s]
        if picked:
            return picked
        six_sec = [s for s in shots if int(round(s.duration_seconds)) == 6]
        return [six_sec[0]] if six_sec else [shots[0]]
    return list(shots)


async def conform_cut_with_ffmpeg(
    shot_mp4_list: Sequence[bytes],
    *,
    width: int,
    height: int,
    max_duration_seconds: float,
    voiceover_wav: bytes | None = None,
    music_wav: bytes | None = None,
    ffmpeg_bin: str | None = None,
    timeout_seconds: float = _FFMPEG_TIMEOUT_SECONDS,
) -> bytes:
    """Mux, crop/scale, sidechain-duck (-14 dB), and EBU R128 loudness-normalize one cut.

    Executes ``asyncio.create_subprocess_exec`` with an explicit argument list
    (never ``shell=True``) inside an isolated ``tempfile.TemporaryDirectory()``.
    """
    if not shot_mp4_list:
        raise ValueError("conform_cut_with_ffmpeg requires at least one shot MP4.")

    exe = ffmpeg_bin or resolve_ffmpeg_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        input_args: list[str] = []
        v_filters: list[str] = []
        n_shots = len(shot_mp4_list)

        for idx, clip_bytes in enumerate(shot_mp4_list):
            clip_path = os.path.join(tmpdir, f"shot_{idx:02d}.mp4")
            with open(clip_path, "wb") as f:
                f.write(clip_bytes)
            input_args.extend(["-i", clip_path])
            v_filters.append(
                f"[{idx}:v]scale={int(width)}:{int(height)}:"
                f"force_original_aspect_ratio=increase,"
                f"crop={int(width)}:{int(height)},setsar=1,fps=24,format=yuv420p[v{idx}]"
            )

        concat_inputs = "".join(f"[v{idx}]" for idx in range(n_shots))
        v_filters.append(f"{concat_inputs}concat=n={n_shots}:v=1:a=0[vcat]")

        next_input_idx = n_shots
        vo_idx: int | None = None
        music_idx: int | None = None

        if voiceover_wav:
            vo_path = os.path.join(tmpdir, "voiceover.wav")
            with open(vo_path, "wb") as f:
                f.write(voiceover_wav)
            input_args.extend(["-i", vo_path])
            vo_idx = next_input_idx
            next_input_idx += 1

        if music_wav:
            music_path = os.path.join(tmpdir, "music.wav")
            with open(music_path, "wb") as f:
                f.write(music_wav)
            input_args.extend(["-stream_loop", "-1", "-i", music_path])
            music_idx = next_input_idx
            next_input_idx += 1

        if vo_idx is not None and music_idx is not None:
            # Sidechain-duck instrumental music ~14 dB beneath voiceover, then EBU R128 loudnorm.
            a_filter = (
                f"[{vo_idx}:a]aresample=48000,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                "apad,asplit=2[vo_sc][vo_mix];"
                f"[{music_idx}:a]aresample=48000,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                "volume=0.45[music_pre];"
                "[music_pre][vo_sc]sidechaincompress="
                "threshold=0.015:ratio=12:attack=15:release=250:level_sc=1[music_ducked];"
                "[vo_mix][music_ducked]amix=inputs=2:duration=first:"
                "dropout_transition=0:normalize=0,"
                "loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
            )
        elif vo_idx is not None:
            a_filter = (
                f"[{vo_idx}:a]aresample=48000,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                "loudnorm=I=-14:TP=-1.5:LRA=11,apad[aout]"
            )
        elif music_idx is not None:
            a_filter = (
                f"[{music_idx}:a]aresample=48000,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                "volume=0.5,loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
            )
        else:
            input_args.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=48000",
                ]
            )
            null_idx = next_input_idx
            a_filter = (
                f"[{null_idx}:a]aformat=sample_fmts=fltp:channel_layouts=stereo[aout]"
            )

        filter_complex = ";".join(v_filters) + ";" + a_filter
        out_path = os.path.join(tmpdir, "conformed.mp4")

        cmd = [
            exe,
            "-y",
            *input_args,
            "-filter_complex",
            filter_complex,
            "-map",
            "[vcat]",
            "-map",
            "[aout]",
            "-t",
            f"{max(0.1, float(max_duration_seconds)):.3f}",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            out_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except TimeoutError:
            raise TimeoutError(
                f"FFmpeg conform timed out after {timeout_seconds}s."
            ) from None
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

        if proc.returncode != 0:
            err_tail = stderr_bytes.decode("utf-8", errors="replace")[-1200:]
            raise RuntimeError(
                f"FFmpeg conform failed (exit {proc.returncode}): {err_tail}"
            )

        with open(out_path, "rb") as f:
            mp4_out = f.read()
        if not mp4_out:
            raise RuntimeError("FFmpeg conform produced an empty output file.")
        return mp4_out


async def conform_video_cuts(
    rendered_shots: Sequence[RenderedShot],
    *,
    shot_pcm_map: Mapping[int, bytes | None],
    music_wav: bytes | None,
    ffmpeg_bin: str | None = None,
    issues_out: list[str] | None = None,
) -> dict[VideoCutFormat, tuple[bytes, list[RenderedShot], float]]:
    """Conform all three commercial cuts (`hero_30s_16x9`, `social_15s_9x16`, `bumper_6s_9x16`).

    Fail-soft per cut: if FFmpeg fails or times out on one cut, logs a warning,
    records the error in `issues_out` (when provided), and continues conforming
    the remaining cuts so surviving deliverables still ship.
    """
    results: dict[VideoCutFormat, tuple[bytes, list[RenderedShot], float]] = {}
    for cut_def in CUT_DEFINITIONS:
        selected = select_shots_for_cut(cut_def.cut, rendered_shots)
        if not selected:
            continue
        raw_sum = sum(s.duration_seconds for s in selected)
        effective_duration = (
            min(float(cut_def.max_duration_seconds), raw_sum)
            if raw_sum > 0
            else float(cut_def.max_duration_seconds)
        )
        vo_wav, _ = build_cut_voiceover_wav(selected, shot_pcm_map)
        try:
            mp4_bytes = await conform_cut_with_ffmpeg(
                [s.mp4_bytes for s in selected],
                width=cut_def.width,
                height=cut_def.height,
                max_duration_seconds=effective_duration,
                voiceover_wav=vo_wav,
                music_wav=music_wav,
                ffmpeg_bin=ffmpeg_bin,
            )
            results[cut_def.cut] = (mp4_bytes, selected, effective_duration)
        except Exception as exc:
            logger.warning("FFmpeg conform failed for %s: %s", cut_def.cut, exc)
            if issues_out is not None:
                issues_out.append(
                    f"{cut_def.cut} FFmpeg conform failed: {type(exc).__name__}: {exc}"
                )
    return results


# ---------------------------------------------------------------------------
# End-to-End Orchestrator for `video_render_pipeline`
# ---------------------------------------------------------------------------


async def produce_video_reel(ctx: Any) -> dict[str, Any]:
    """Render storyboard shots, QA/retake, synthesize audio, conform 3 cuts, and persist."""
    if not is_video_production_enabled(ctx.state.get("video_production_enabled")):
        return {"status": "skipped", "reason": "video_production_enabled is False"}

    if ctx.state.get("_videos_generated") and ctx.state.get("generated_videos"):
        return {
            "status": "cached",
            "cuts": list(ctx.state["generated_videos"].keys()),
        }

    storyboard = video_qa.parse_storyboard(ctx.state.get("video_storyboard"))
    raw_shots = storyboard.get("shots") if storyboard else None
    if not storyboard or not isinstance(raw_shots, list) or not raw_shots:
        msg = "No valid video_storyboard found in session state; skipping video render."
        ctx.state["generated_videos__issues"] = [msg]
        return {"status": "skipped", "reason": msg}

    client = _get_video_genai_client()
    requested_backend = parse_video_gen_backend(config.video_gen_backend)
    active_backend = "veo" if requested_backend == "veo" else "omniflash"

    brand = str(ctx.state.get("brand") or "").strip()
    target_product = str(ctx.state.get("target_product") or "").strip()
    reference_description = str(
        ctx.state.get("reference_product_description") or ""
    ).strip()
    avoid_terms = video_qa.extract_avoid_terms(ctx.state)
    concepts = video_qa.extract_visual_concepts(ctx.state)
    raw_generated_images = ctx.state.get("generated_images")
    generated_images = (
        dict(raw_generated_images) if isinstance(raw_generated_images, Mapping) else {}
    )

    raw_refs = resolve_references(ctx.state)
    fetched_refs: list[ReferenceImage] = []
    if raw_refs:
        try:
            fetched_parts, _missing = await references.fetch_references(raw_refs)
            for role, part in fetched_parts:
                inline = getattr(part, "inline_data", None)
                raw_data = getattr(inline, "data", None) if inline is not None else None
                decoded_ref = _decode_media_bytes(raw_data)
                if decoded_ref:
                    mime = (
                        getattr(inline, "mime_type", None)
                        if inline is not None
                        else None
                    ) or "image/png"
                    fetched_refs.append(
                        ReferenceImage(role=role, data=decoded_ref, mime_type=mime)
                    )
        except Exception as exc:
            logger.warning(
                "Reference fetch for video reel failed (continuing): %s", exc
            )

    rendered_shots: list[RenderedShot] = []
    shot_errors: list[str] = []
    qa_issues: list[str] = []

    for idx, raw_shot in enumerate(raw_shots, start=1):
        shot = dict(raw_shot) if isinstance(raw_shot, Mapping) else {}
        if not shot:
            continue
        shot_num = int(shot.get("shot_number") or idx)
        anchor_name = str(shot.get("anchor_concept_name") or "").strip()
        concept = _match_anchor_concept(anchor_name, concepts)
        resolved_concept_name = str(concept.get("concept_name") or anchor_name).strip()

        keyframe_bytes, keyframe_mime = await _load_keyframe_bytes(
            ctx, resolved_concept_name, generated_images
        )
        role_refs = select_video_references_for_concept(
            fetched_refs,
            str(concept.get("composition_mode") or "product_in_scene"),
        )

        try:
            rendered, active_backend = await render_and_qa_shot(
                client,
                shot=shot,
                concept=concept,
                keyframe_bytes=keyframe_bytes,
                keyframe_mime=keyframe_mime,
                references=role_refs,
                requested_backend=requested_backend,
                active_backend=active_backend,
                brand=brand,
                target_product=target_product,
                avoid_terms=avoid_terms,
                reference_description=reference_description,
                max_retakes=config.video_qa_max_retakes,
            )
            rendered_shots.append(rendered)
            if rendered.qa_verdict is not None and not video_qa.qa_passed(
                rendered.qa_verdict
            ):
                failed = ", ".join(video_qa.failed_checks(rendered.qa_verdict))
                detail = (rendered.qa_verdict.defect_instruction or "").strip()
                suffix = f" ({detail})" if detail else ""
                qa_issues.append(f"Shot {shot_num}: failed {failed}{suffix}")
        except Exception as exc:
            logger.warning("Shot %s video render failed: %s", shot_num, exc)
            shot_errors.append(f"Shot {shot_num}: {type(exc).__name__}: {exc}")

    if qa_issues:
        ctx.state["video_qa__issues"] = qa_issues
    else:
        ctx.state["video_qa__issues"] = None

    if not rendered_shots:
        ctx.state["_videos_generated"] = False
        ctx.state["generated_videos__issues"] = shot_errors or [
            "All video shots failed to render."
        ]
        return {"status": "error", "issues": ctx.state["generated_videos__issues"]}

    # Synthesize per-shot voiceover clips (off-loop, fail-soft) and instrumental music bed.
    voiceover_style = str(storyboard.get("voiceover_style") or "").strip()
    shot_pcm_map: dict[int, bytes | None] = {}
    for shot_item in rendered_shots:
        if shot_item.voiceover_line:
            pcm = await asyncio.to_thread(
                synthesize_voiceover_clip_sync,
                shot_item.voiceover_line,
                duration_seconds=shot_item.duration_seconds,
                voiceover_style=voiceover_style,
                client=client,
            )
            shot_pcm_map[shot_item.shot_number] = pcm
        else:
            shot_pcm_map[shot_item.shot_number] = None

    total_hero_dur = max(6.0, sum(s.duration_seconds for s in rendered_shots))
    music_wav, _ = await synthesize_music_wav(
        str(storyboard.get("music_direction") or ""),
        duration_seconds=total_hero_dur,
        brand=brand,
        client=client,
    )

    conform_issues: list[str] = []
    conformed = await conform_video_cuts(
        rendered_shots,
        shot_pcm_map=shot_pcm_map,
        music_wav=music_wav,
        issues_out=conform_issues,
    )

    gcs_bucket = str(config.GCS_BUCKET_NAME or "").strip()
    gcs_folder = str(ctx.state.get("gcs_folder") or "").strip()
    gcs_subdir = str(ctx.state.get("agent_output_dir") or "").strip()
    save_artifact_fn = getattr(ctx, "save_artifact", None)

    generated_videos: dict[str, dict[str, Any]] = {}
    upload_issues: list[str] = [*shot_errors, *conform_issues]

    for cut_def in CUT_DEFINITIONS:
        if cut_def.cut not in conformed:
            continue
        mp4_bytes, cut_shots, effective_dur = conformed[cut_def.cut]
        artifact_key = f"video_{cut_def.cut}.mp4"

        if callable(save_artifact_fn):
            try:
                part = types.Part.from_bytes(data=mp4_bytes, mime_type="video/mp4")
                res = save_artifact_fn(filename=artifact_key, artifact=part)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                logger.warning("ADK save_artifact(%r) failed: %s", artifact_key, exc)

        gcs_uri = ""
        if gcs_bucket and gcs_folder and gcs_subdir:
            blob_name = f"{gcs_folder}/{gcs_subdir}/videos/{cut_def.cut}.mp4"
            try:
                gcs_uri = await asyncio.to_thread(
                    _upload_video_bytes_to_gcs,
                    mp4_bytes,
                    bucket_name=gcs_bucket,
                    blob_name=blob_name,
                )
            except Exception as exc:
                logger.warning("GCS upload failed for %s: %s", blob_name, exc)
                upload_issues.append(
                    f"{cut_def.cut} GCS upload failed: {type(exc).__name__}: {exc}"
                )

        passed_count = sum(
            1
            for s in cut_shots
            if s.qa_verdict is None or video_qa.qa_passed(s.qa_verdict)
        )
        cut_qa_passed = passed_count == len(cut_shots)
        cut_backend = (
            "veo"
            if any(s.backend == "veo" for s in cut_shots)
            else cut_shots[0].backend
        )
        transcript = " ".join(
            s.voiceover_line for s in cut_shots if s.voiceover_line
        ).strip()

        generated_videos[cut_def.cut] = {
            "cut": cut_def.cut,
            "label": cut_def.label,
            "gcs_uri": gcs_uri,
            "artifact_key": artifact_key,
            "duration_seconds": int(round(effective_dur)),
            "aspect_ratio": cut_def.aspect_ratio,
            "backend": cut_backend,
            "shot_numbers": [s.shot_number for s in cut_shots],
            "voiceover_transcript": transcript,
            "qa_passed": cut_qa_passed,
            "qa_summary": f"{passed_count}/{len(cut_shots)} shots passed multimodal QA",
        }

    if not generated_videos:
        ctx.state["generated_videos"] = None
        ctx.state["_videos_generated"] = False
        ctx.state["generated_videos__issues"] = upload_issues or [
            "All video cuts failed to conform."
        ]
        return {"status": "error", "issues": ctx.state["generated_videos__issues"]}

    ctx.state["generated_videos"] = generated_videos
    ctx.state["_videos_generated"] = True
    if upload_issues:
        ctx.state["generated_videos__issues"] = upload_issues
    else:
        ctx.state["generated_videos__issues"] = None

    return {
        "status": "success",
        "cuts": list(generated_videos.keys()),
    }
