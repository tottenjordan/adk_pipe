"""Streaming ``.zip`` Delivery Kit export endpoint.

Route (user-scoped under ``/runs/{app_name}/{user_id}/{session_id}/delivery-kit``
and gated by ``UserAuthzMiddleware``):

- ``GET /runs/{app_name}/{user_id}/{session_id}/delivery-kit``: assembles a
  structured campaign handoff archive off the event loop into a
  ``SpooledTemporaryFile`` and streams it back as ``application/zip``.

Security & reliability invariants:
- Enforces P3 per-user session ownership (401/403 in ``ENFORCE``, 404 on missing
  or foreign sessions).
- Strict GCS bucket confinement: only ``gs://{allowed_bucket}/...`` object paths
  free of ``..`` / ``.`` traversal segments are ever read. External buckets and
  ``http(s)://`` URIs are rejected and recorded in ``manifest.json``
  (``skipped_files``) with zero network or cross-bucket reads.
- Enforces ``MAX_ARCHIVE_BYTES`` (150 MB uncompressed payload ceiling), checking
  ``blob.size`` prior to download and charging a running byte counter for every
  archived entry (raising HTTP 413 if exceeded).
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import json
import logging
import os
import re
import tempfile
import zipfile
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from google.adk.errors.session_not_found_error import SessionNotFoundError

from agent_common.clients import get_gcs_client
from runserver.authz import (
    AuthzMode,
    UserAuthzError,
    authorize_body_user,
    normalize_user_id,
    trusted_user,
)

log = logging.getLogger(__name__)

MAX_ARCHIVE_BYTES = 150 * 1024 * 1024  # 150 MiB ceiling
_SPOOL_MEMORY_BYTES = 16 * 1024 * 1024  # 16 MiB before spilling to disk
_STREAM_CHUNK_BYTES = 64 * 1024  # 64 KiB response chunks

_SESSION_SERVICE: Any = None
_ARTIFACT_SERVICE: Any = None
_AUTHZ_MODE: AuthzMode = AuthzMode.TRUST_CLIENT
_GCS_BUCKET_OVERRIDE: str | None = None
_GCS_CLIENT_FACTORY: Callable[[], Any] | None = None


def configure(
    *,
    session_service: Any,
    artifact_service: Any = None,
    authz_mode: AuthzMode = AuthzMode.TRUST_CLIENT,
    gcs_bucket: str | None = None,
    gcs_client_factory: Callable[[], Any] | None = None,
) -> None:
    """Bind shared ``session_service``, ``artifact_service``, and ``authz_mode``."""
    global _SESSION_SERVICE, _ARTIFACT_SERVICE, _AUTHZ_MODE
    global _GCS_BUCKET_OVERRIDE, _GCS_CLIENT_FACTORY
    _SESSION_SERVICE = session_service
    _ARTIFACT_SERVICE = artifact_service
    _AUTHZ_MODE = authz_mode
    _GCS_BUCKET_OVERRIDE = gcs_bucket
    _GCS_CLIENT_FACTORY = gcs_client_factory


class _ArchiveSizeLimitExceeded(Exception):
    """Raised when uncompressed archive payload exceeds ``MAX_ARCHIVE_BYTES``."""


class _ArchiveBudget:
    """Running uncompressed byte budget guard."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.total_bytes = 0

    def check_can_add(self, num_bytes: int) -> None:
        if num_bytes < 0 or self.total_bytes + num_bytes > self.max_bytes:
            raise _ArchiveSizeLimitExceeded(
                f"Archive payload exceeds {self.max_bytes} bytes limit"
            )

    def charge(self, num_bytes: int) -> None:
        self.check_can_add(num_bytes)
        self.total_bytes += num_bytes


_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_NUMERIC_LITERAL_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_CSV_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def slugify_filename(raw: Any, *, fallback: str, max_len: int = 48) -> str:
    """Sanitize untrusted text into a safe lowercase ASCII filename slug."""
    ascii_text = str(raw or "").encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_NON_ALNUM_RE.sub("-", ascii_text).strip("-")
    if not slug:
        slug = fallback
    return slug[:max_len].rstrip("-") or fallback


def sanitize_csv_cell(value: Any) -> str:
    """Neutralize spreadsheet formula-injection prefixes (OWASP CSV mitigation)."""
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    stripped = text.lstrip(" ")
    if (
        text.startswith(_CSV_DANGEROUS_PREFIXES)
        or (stripped and stripped.startswith(("=", "+", "-", "@")))
    ) and not _NUMERIC_LITERAL_RE.fullmatch(stripped):
        return f"'{text}"
    return text


def parse_confined_gcs_uri(
    uri: Any, allowed_bucket: str | None
) -> tuple[tuple[str, str] | None, str | None]:
    """Validate that ``uri`` is strictly confined to ``gs://{allowed_bucket}/...``.

    Returns ``((bucket, blob_path), None)`` when safe, or ``(None, reason)`` when
    blocked. Never performs network calls.
    """
    if not isinstance(uri, str) or not uri.strip():
        return None, "empty_uri"
    candidate = uri.strip()
    if not candidate.startswith("gs://"):
        return None, "non_gcs_scheme"

    without_scheme = candidate[5:]
    bucket, sep, blob_path = without_scheme.partition("/")
    if not sep or not bucket or not blob_path:
        return None, "malformed_gcs_uri"

    allowed = (allowed_bucket or "").strip().removeprefix("gs://").strip("/")
    if not allowed:
        return None, "gcs_bucket_unconfigured"
    if bucket != allowed:
        return None, "cross_bucket_blocked"

    if "\\" in blob_path or "\x00" in blob_path or blob_path.startswith("/"):
        return None, "invalid_blob_path"
    segments = blob_path.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        return None, "invalid_blob_path"

    return (bucket, blob_path), None


def _resolve_allowed_bucket() -> str:
    if _GCS_BUCKET_OVERRIDE is not None:
        return _GCS_BUCKET_OVERRIDE.strip()
    env_bucket = (os.environ.get("GOOGLE_CLOUD_STORAGE_BUCKET") or "").strip()
    if env_bucket:
        return env_bucket
    try:
        from creative_agent.config import config

        return str(config.GCS_BUCKET_NAME or "").strip()
    except Exception:
        return ""


def _resolve_gcs_client() -> Any:
    if _GCS_CLIENT_FACTORY is not None:
        return _GCS_CLIENT_FACTORY()
    return get_gcs_client()


def _authorize_caller(request: Request, path_user: str) -> str:
    claimed = path_user
    if _AUTHZ_MODE is AuthzMode.ENFORCE:
        try:
            claimed = normalize_user_id(path_user)
        except ValueError:
            claimed = path_user.strip().lower()
    try:
        return authorize_body_user(_AUTHZ_MODE, trusted_user(request), claimed)
    except UserAuthzError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


async def _get_session_or_none(
    app_name: str, user_id: str, session_id: str
) -> Any | None:
    if _SESSION_SERVICE is None:
        raise HTTPException(status_code=503, detail="Session service is not configured")
    try:
        return await _SESSION_SERVICE.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
    except (SessionNotFoundError, ValueError):
        return None


def _as_json_obj(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _extract_part_bytes(part: Any) -> bytes | None:
    if part is None:
        return None
    inline = getattr(part, "inline_data", None)
    if inline is not None:
        data = getattr(inline, "data", None)
        if isinstance(data, (bytes, bytearray)) and data:
            return bytes(data)
    if isinstance(part, Mapping):
        inline_dict = part.get("inline_data") or part.get("inlineData")
        if isinstance(inline_dict, Mapping):
            raw = inline_dict.get("data")
            if isinstance(raw, (bytes, bytearray)) and raw:
                return bytes(raw)
    return None


async def _preload_adk_artifacts(
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    filenames: set[str],
    budget: _ArchiveBudget,
) -> dict[str, bytes]:
    """Load requested session-scoped ADK artifacts into memory (budget-checked)."""
    if _ARTIFACT_SERVICE is None or not filenames:
        return {}
    loaded: dict[str, bytes] = {}
    for fname in sorted(filenames):
        if not fname or fname.startswith("user:"):
            continue
        try:
            part = await _ARTIFACT_SERVICE.load_artifact(
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
                filename=fname,
            )
        except Exception:
            continue
        data = _extract_part_bytes(part)
        if data is not None:
            budget.charge(len(data))
            loaded[fname] = data
    return loaded


def _is_not_found_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code == 404 or type(exc).__name__ == "NotFound":
        return True
    return "404" in str(exc) or "not found" in str(exc).lower()


def _read_confined_gcs_blob(
    uri: str,
    *,
    archive_path: str,
    allowed_bucket: str,
    budget: _ArchiveBudget,
    skipped_files: list[dict[str, str]],
) -> bytes | None:
    """Validate ``uri`` against ``allowed_bucket``, enforce byte cap, and download."""
    parsed, err_reason = parse_confined_gcs_uri(uri, allowed_bucket)
    if parsed is None:
        skipped_files.append(
            {
                "archive_path": archive_path,
                "uri": str(uri),
                "reason": err_reason or "invalid_uri",
            }
        )
        return None

    bucket_name, blob_path = parsed
    try:
        client = _resolve_gcs_client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        size = getattr(blob, "size", None)
        if size is None and hasattr(blob, "reload"):
            try:
                blob.reload()
                size = getattr(blob, "size", None)
            except Exception as reload_exc:
                if _is_not_found_error(reload_exc):
                    skipped_files.append(
                        {
                            "archive_path": archive_path,
                            "uri": uri,
                            "reason": "not_found",
                        }
                    )
                    return None
        if isinstance(size, int) and not isinstance(size, bool):
            budget.check_can_add(size)

        data = blob.download_as_bytes()
    except _ArchiveSizeLimitExceeded:
        raise
    except Exception as exc:
        reason = "not_found" if _is_not_found_error(exc) else "gcs_read_failed"
        log.warning("Delivery kit skipped %s (%s): %s", uri, reason, exc)
        skipped_files.append(
            {"archive_path": archive_path, "uri": uri, "reason": reason}
        )
        return None

    if not isinstance(data, (bytes, bytearray)):
        skipped_files.append(
            {"archive_path": archive_path, "uri": uri, "reason": "empty_blob"}
        )
        return None
    raw_bytes = bytes(data)
    budget.charge(len(raw_bytes))
    return raw_bytes


def _build_ad_copies_csv(ad_copies: list[Mapping[str, Any]]) -> bytes:
    columns = (
        "original_id",
        "headline",
        "body_text",
        "social_caption",
        "call_to_action",
        "tone_style",
        "angle_id",
    )
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for copy_item in ad_copies:
        writer.writerow([sanitize_csv_cell(copy_item.get(col)) for col in columns])
    return buf.getvalue().encode("utf-8")


def _extract_video_records(raw_videos: Any) -> list[tuple[str, str, str]]:
    """Extract ``(cut_name, uri, artifact_key)`` triples from ``state['generated_videos']``."""
    obj = _as_json_obj(raw_videos)
    entries: list[tuple[str, str, str]] = []
    if isinstance(obj, Mapping):
        for name, item in obj.items():
            if isinstance(item, str) and item.strip():
                entries.append((str(name), item.strip(), ""))
            elif isinstance(item, Mapping):
                raw_uri = item.get("gcs_uri") or item.get("uri")
                uri = raw_uri.strip() if isinstance(raw_uri, str) else ""
                raw_akey = item.get("artifact_key")
                akey = raw_akey.strip() if isinstance(raw_akey, str) else ""
                if uri or akey:
                    entries.append((str(name), uri, akey))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            if isinstance(item, str) and item.strip():
                entries.append((f"video-{idx + 1}", item.strip(), ""))
            elif isinstance(item, Mapping):
                name = str(
                    item.get("name")
                    or item.get("cut")
                    or item.get("concept_name")
                    or f"video-{idx + 1}"
                )
                raw_uri = item.get("gcs_uri") or item.get("uri")
                uri = raw_uri.strip() if isinstance(raw_uri, str) else ""
                raw_akey = item.get("artifact_key")
                akey = raw_akey.strip() if isinstance(raw_akey, str) else ""
                if uri or akey:
                    entries.append((name, uri, akey))
    return entries


def _extract_video_entries(raw_videos: Any) -> list[tuple[str, str]]:
    """Extract ``(cut_name, uri)`` pairs from ``state['generated_videos']``."""
    return [
        (name, uri) for name, uri, _akey in _extract_video_records(raw_videos) if uri
    ]


def _unique_slug(base_slug: str, used: set[str]) -> str:
    if base_slug not in used:
        used.add(base_slug)
        return base_slug
    seq = 2
    while f"{base_slug}-{seq}" in used:
        seq += 1
    candidate = f"{base_slug}-{seq}"
    used.add(candidate)
    return candidate


def _build_zip_sync(
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    state: Mapping[str, Any],
    preloaded_artifacts: Mapping[str, bytes],
    allowed_bucket: str,
    max_bytes: int,
) -> tempfile.SpooledTemporaryFile[bytes]:
    """Assemble the complete Delivery Kit ``.zip`` inside a worker thread."""
    from creative_agent.gcs_tools import artifact_key_for  # lazy import

    budget = _ArchiveBudget(max_bytes=max_bytes)
    included_files: list[str] = []
    skipped_files: list[dict[str, str]] = []

    spool: tempfile.SpooledTemporaryFile[bytes] = tempfile.SpooledTemporaryFile(
        max_size=_SPOOL_MEMORY_BYTES, mode="w+b"
    )
    try:
        with zipfile.ZipFile(spool, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:

            def write_entry(
                arcname: str, payload: bytes, *, charge: bool = True
            ) -> None:
                if charge:
                    budget.charge(len(payload))
                zf.writestr(arcname, payload)
                included_files.append(arcname)

            # 1. Creative Brief (JSON + Markdown)
            brief_obj = _as_json_obj(state.get("creative_brief"))
            if isinstance(brief_obj, Mapping) and brief_obj:
                write_entry(
                    "brief/creative_brief.json",
                    json.dumps(brief_obj, indent=2, ensure_ascii=False).encode("utf-8"),
                )
            brief_md = state.get("creative_brief_md")
            if isinstance(brief_md, str) and brief_md.strip():
                write_entry("brief/creative_brief.md", brief_md.encode("utf-8"))

            # 2. Research Report (Markdown + PDF)
            report_md = state.get("final_report_with_citations") or state.get(
                "combined_final_cited_report"
            )
            if isinstance(report_md, str) and report_md.strip():
                write_entry("research/research_report.md", report_md.encode("utf-8"))

            pdf_uri = state.get("research_report_gcs_uri")
            pdf_bytes: bytes | None = None
            if isinstance(pdf_uri, str) and pdf_uri.strip():
                pdf_bytes = _read_confined_gcs_blob(
                    pdf_uri.strip(),
                    archive_path="research/research_report.pdf",
                    allowed_bucket=allowed_bucket,
                    budget=budget,
                    skipped_files=skipped_files,
                )
                if pdf_bytes is not None:
                    write_entry("research/research_report.pdf", pdf_bytes, charge=False)
            elif "research_report_with_citations.pdf" in preloaded_artifacts:
                write_entry(
                    "research/research_report.pdf",
                    preloaded_artifacts["research_report_with_citations.pdf"],
                )

            # 3. Ad Copies (JSON + CSV)
            copies_env = _as_json_obj(state.get("ad_copy_critique"))
            copies_raw = (
                copies_env.get("ad_copies")
                if isinstance(copies_env, Mapping)
                else copies_env
            )
            ad_copies = (
                [c for c in copies_raw if isinstance(c, Mapping)]
                if isinstance(copies_raw, list)
                else []
            )
            if ad_copies:
                write_entry(
                    "copy/ad_copies.json",
                    json.dumps(ad_copies, indent=2, ensure_ascii=False).encode("utf-8"),
                )
                write_entry("copy/ad_copies.csv", _build_ad_copies_csv(ad_copies))

            # 4. Visual Concepts (PNG + per-concept metadata JSON)
            visuals_env = _as_json_obj(state.get("final_visual_concepts"))
            visuals_raw = (
                visuals_env.get("visual_concepts")
                if isinstance(visuals_env, Mapping)
                else visuals_env
            )
            visual_concepts = (
                [vc for vc in visuals_raw if isinstance(vc, Mapping)]
                if isinstance(visuals_raw, list)
                else []
            )
            gen_images_obj = _as_json_obj(state.get("generated_images"))
            gen_images = gen_images_obj if isinstance(gen_images_obj, Mapping) else {}
            used_visual_slugs: set[str] = set()
            variation_lineage: list[dict[str, Any]] = []

            gcs_folder = str(state.get("gcs_folder") or "").strip()
            gcs_subdir = str(state.get("agent_output_dir") or "").strip()
            gen_keys_raw = state.get("_generated_artifact_keys")
            gen_keys_set = (
                {str(k) for k in gen_keys_raw if isinstance(k, str)}
                if isinstance(gen_keys_raw, list)
                else set()
            )

            for idx, vc in enumerate(visual_concepts):
                cname = str(vc.get("concept_name") or f"concept-{idx + 1}")
                slug = _unique_slug(
                    slugify_filename(cname, fallback=f"concept-{idx + 1}", max_len=64),
                    used_visual_slugs,
                )
                img_rec = gen_images.get(cname)
                img_rec_map = img_rec if isinstance(img_rec, Mapping) else {}
                akey = str(img_rec_map.get("artifact_key") or artifact_key_for(cname))

                if vc.get("variation_of"):
                    variation_lineage.append(
                        {
                            "index": idx,
                            "concept_name": cname,
                            "slug": slug,
                            "variation_of": vc.get("variation_of"),
                            "variation_base_index": vc.get("variation_base_index"),
                            "variation_axis": vc.get("variation_axis"),
                            "variation_value": vc.get("variation_value"),
                        }
                    )

                meta_payload = {
                    **dict(vc),
                    "index": idx,
                    "slug": slug,
                    "artifact_key": akey,
                    "qa": img_rec_map.get("qa"),
                    "attempts": img_rec_map.get("attempts"),
                }
                write_entry(
                    f"visuals/{slug}.json",
                    json.dumps(meta_payload, indent=2, ensure_ascii=False).encode(
                        "utf-8"
                    ),
                )

                png_arcname = f"visuals/{slug}.png"
                explicit_uri = img_rec_map.get("gcs_uri")
                if akey in preloaded_artifacts:
                    # If an explicit untrusted URI was placed in state alongside no artifact,
                    # confinement handles it below; ADK session artifacts are already owned.
                    write_entry(png_arcname, preloaded_artifacts[akey])
                elif isinstance(explicit_uri, str) and explicit_uri.strip():
                    img_bytes = _read_confined_gcs_blob(
                        explicit_uri.strip(),
                        archive_path=png_arcname,
                        allowed_bucket=allowed_bucket,
                        budget=budget,
                        skipped_files=skipped_files,
                    )
                    if img_bytes is not None:
                        write_entry(png_arcname, img_bytes, charge=False)
                elif (
                    allowed_bucket
                    and gcs_folder
                    and gcs_subdir
                    and (not gen_keys_set or akey in gen_keys_set)
                ):
                    derived_uri = (
                        f"gs://{allowed_bucket}/{gcs_folder}/{gcs_subdir}/{akey}"
                    )
                    img_bytes = _read_confined_gcs_blob(
                        derived_uri,
                        archive_path=png_arcname,
                        allowed_bucket=allowed_bucket,
                        budget=budget,
                        skipped_files=skipped_files,
                    )
                    if img_bytes is not None:
                        write_entry(png_arcname, img_bytes, charge=False)

            # 5. Optional Videos (forward-compatible generated_videos URIs)
            used_video_slugs: set[str] = set()
            for idx, (cut_name, video_uri, video_akey) in enumerate(
                _extract_video_records(state.get("generated_videos"))
            ):
                cut_slug = _unique_slug(
                    slugify_filename(cut_name, fallback=f"video-{idx + 1}", max_len=64),
                    used_video_slugs,
                )
                mp4_arcname = f"videos/{cut_slug}.mp4"
                if video_akey and video_akey in preloaded_artifacts:
                    write_entry(mp4_arcname, preloaded_artifacts[video_akey])
                elif video_uri:
                    vid_bytes = _read_confined_gcs_blob(
                        video_uri,
                        archive_path=mp4_arcname,
                        allowed_bucket=allowed_bucket,
                        budget=budget,
                        skipped_files=skipped_files,
                    )
                    if vid_bytes is not None:
                        write_entry(mp4_arcname, vid_bytes, charge=False)

            # 6. Evaluation Report JSON
            eval_arcname = "evaluation/creative_evaluation_report.json"
            eval_obj = _as_json_obj(state.get("creative_evaluation_report"))
            eval_uri = state.get("eval_report_gcs_uri")
            if isinstance(eval_obj, Mapping) and eval_obj:
                write_entry(
                    eval_arcname,
                    json.dumps(eval_obj, indent=2, ensure_ascii=False).encode("utf-8"),
                )
            elif isinstance(eval_uri, str) and eval_uri.strip():
                eval_bytes = _read_confined_gcs_blob(
                    eval_uri.strip(),
                    archive_path=eval_arcname,
                    allowed_bucket=allowed_bucket,
                    budget=budget,
                    skipped_files=skipped_files,
                )
                if eval_bytes is not None:
                    write_entry(eval_arcname, eval_bytes, charge=False)

            # 7. Standalone HTML Gallery
            gallery_uri = state.get("creative_gallery_gcs_uri")
            if isinstance(gallery_uri, str) and gallery_uri.strip():
                gallery_bytes = _read_confined_gcs_blob(
                    gallery_uri.strip(),
                    archive_path="gallery/index.html",
                    allowed_bucket=allowed_bucket,
                    budget=budget,
                    skipped_files=skipped_files,
                )
                if gallery_bytes is not None:
                    write_entry("gallery/index.html", gallery_bytes, charge=False)

            # 8. Top-level manifest.json (written last so file inventory is exact)
            manifest = {
                "schema_version": "1.0",
                "session_id": session_id,
                "app_name": app_name,
                "user_id": user_id,
                "exported_at": dt.datetime.now(dt.UTC).isoformat(),
                "campaign": {
                    "brand": str(state.get("brand") or ""),
                    "target_product": str(state.get("target_product") or ""),
                    "target_audience": str(state.get("target_audience") or ""),
                    "key_selling_points": str(state.get("key_selling_points") or ""),
                    "target_search_trends": str(
                        state.get("target_search_trends") or ""
                    ),
                },
                "counts": {
                    "ad_copies": len(ad_copies),
                    "visual_concepts": len(visual_concepts),
                    "variations": len(variation_lineage),
                },
                "variation_lineage": variation_lineage,
                "included_files": [*included_files, "manifest.json"],
                "skipped_files": skipped_files,
                "total_payload_bytes_before_manifest": budget.total_bytes,
            }
            write_entry(
                "manifest.json",
                json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"),
            )

        spool.seek(0)
        return spool
    except BaseException:
        spool.close()
        raise


def _iter_spooled_file(
    spool: tempfile.SpooledTemporaryFile[bytes],
) -> Iterator[bytes]:
    try:
        while True:
            chunk = spool.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            yield chunk
    finally:
        spool.close()


router = APIRouter()


@router.get("/runs/{app_name}/{user_id}/{session_id}/delivery-kit")
async def download_delivery_kit(
    app_name: str,
    user_id: str,
    session_id: str,
    request: Request,
) -> StreamingResponse:
    """Export all available creative deliverables for ``session_id`` as a ``.zip``."""
    caller_user = _authorize_caller(request, user_id)
    session = await _get_session_or_none(app_name, caller_user, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    state: dict[str, Any] = dict(getattr(session, "state", None) or {})
    from creative_agent.gcs_tools import artifact_key_for  # lazy import

    # Collect candidate ADK artifact filenames to preload before entering worker thread.
    wanted_artifacts: set[str] = {"research_report_with_citations.pdf"}
    visuals_env = _as_json_obj(state.get("final_visual_concepts"))
    visuals_raw = (
        visuals_env.get("visual_concepts")
        if isinstance(visuals_env, Mapping)
        else visuals_env
    )
    gen_images_obj = _as_json_obj(state.get("generated_images"))
    gen_images = gen_images_obj if isinstance(gen_images_obj, Mapping) else {}
    if isinstance(visuals_raw, list):
        for vc in visuals_raw:
            if isinstance(vc, Mapping) and vc.get("concept_name"):
                cname = str(vc["concept_name"])
                rec = gen_images.get(cname)
                akey = (
                    rec.get("artifact_key") if isinstance(rec, Mapping) else None
                ) or artifact_key_for(cname)
                wanted_artifacts.add(str(akey))
    for _cut_name, _video_uri, video_akey in _extract_video_records(
        state.get("generated_videos")
    ):
        if video_akey:
            wanted_artifacts.add(video_akey)

    max_bytes = MAX_ARCHIVE_BYTES
    preload_budget = _ArchiveBudget(max_bytes=max_bytes)
    try:
        preloaded = await _preload_adk_artifacts(
            app_name=app_name,
            user_id=caller_user,
            session_id=session_id,
            filenames=wanted_artifacts,
            budget=preload_budget,
        )
        allowed_bucket = _resolve_allowed_bucket()
        spool = await asyncio.to_thread(
            _build_zip_sync,
            app_name=app_name,
            user_id=caller_user,
            session_id=session_id,
            state=state,
            preloaded_artifacts=preloaded,
            allowed_bucket=allowed_bucket,
            max_bytes=max_bytes,
        )
    except _ArchiveSizeLimitExceeded as exc:
        raise HTTPException(
            status_code=413,
            detail="Delivery kit archive exceeds the 150 MB size limit",
        ) from exc

    brand_slug = slugify_filename(
        state.get("brand") or app_name, fallback="campaign", max_len=40
    )
    session_short = slugify_filename(session_id[:12], fallback="session", max_len=16)
    filename = f"trend-trawler-{brand_slug}-{session_short}.zip"

    return StreamingResponse(
        _iter_spooled_file(spool),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
