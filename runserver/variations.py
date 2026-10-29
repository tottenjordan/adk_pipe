"""Controlled single-axis creative variation endpoint.

Route (user-scoped under ``/runs/{app_name}/{user_id}/{session_id}/variations``
and gated by ``UserAuthzMiddleware``):

- ``POST /runs/{app_name}/{user_id}/{session_id}/variations``: validates user
  ownership, enforces a per-session ``asyncio.Lock`` and a 16-concept ceiling,
  generates 1–3 single-axis variations via ``creative_agent.variations``, saves
  rendered PNGs to ADK ``artifact_service`` (and GCS when configured), appends
  new judge scores + recomputed summary to ``creative_evaluation_report``, and
  persists the merged state delta as an ADK ``Event`` so downstream features
  (Results Proof Room, ``/experiments`` contextual-bandit arm snapshotting,
  ``/changelog``, and ``/delivery-kit``) see the new arms immediately.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import logging
import threading
import weakref
from collections.abc import Callable, Mapping
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from google.adk.errors.session_not_found_error import SessionNotFoundError
from google.adk.events import Event, EventActions
from google.genai import errors as genai_errors
from pydantic import BaseModel, ConfigDict, Field, field_validator

from runserver import ratings
from runserver.async_runs import RUN_STATUS_KEY, RUNSERVER_AUTHOR, _is_live
from runserver.authz import (
    AuthzMode,
    UserAuthzError,
    authorize_body_user,
    normalize_user_id,
    trusted_user,
)

log = logging.getLogger(__name__)

MAX_CONCEPTS_PER_SESSION = 16
MAX_TARGET_VALUE_CHARS = 100
CREATIVE_APPS = frozenset({"creative_agent", "interactive_creative"})

VariationAxis = Literal[
    "locale_market",
    "audience_persona",
    "visual_style",
    "sibling_sku",
    "cta_hook",
    "composition_mode",
]

_SESSION_SERVICE: Any = None
_ARTIFACT_SERVICE: Any = None
_AUTHZ_MODE: AuthzMode = AuthzMode.TRUST_CLIENT
_VARIATION_RUNNER: Callable[..., Any] | None = None

_VARIATION_LOCKS: weakref.WeakValueDictionary[tuple[int, str, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)
_VARIATION_LOCKS_GUARD = threading.Lock()


def _get_variation_lock(app_name: str, session_id: str) -> asyncio.Lock:
    loop_id = id(asyncio.get_running_loop())
    key = (loop_id, app_name, session_id)
    with _VARIATION_LOCKS_GUARD:
        lock = _VARIATION_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _VARIATION_LOCKS[key] = lock
        return lock


def configure(
    *,
    session_service: Any,
    artifact_service: Any = None,
    authz_mode: AuthzMode = AuthzMode.TRUST_CLIENT,
    variation_runner: Callable[..., Any] | None = None,
) -> None:
    """Bind shared ``session_service``, ``artifact_service``, and ``authz_mode``."""
    global _SESSION_SERVICE, _ARTIFACT_SERVICE, _AUTHZ_MODE, _VARIATION_RUNNER
    _SESSION_SERVICE = session_service
    _ARTIFACT_SERVICE = artifact_service
    _AUTHZ_MODE = authz_mode
    _VARIATION_RUNNER = variation_runner


class GenerateVariationsRequest(BaseModel):
    """Strict request schema for ``POST /runs/{app}/{user}/{session}/variations``."""

    model_config = ConfigDict(extra="forbid")

    base_concept_index: int = Field(ge=0)
    axis: VariationAxis
    count: int = Field(default=2, ge=1, le=3)
    target_values: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("target_values")
    @classmethod
    def _validate_target_values(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in values:
            if not isinstance(raw, str):
                raise ValueError("Each target_value must be a string")
            if any(ord(ch) < 32 for ch in raw):
                raise ValueError("target_values must not contain control characters")
            trimmed = raw.strip()
            if len(trimmed) > MAX_TARGET_VALUE_CHARS:
                raise ValueError(
                    f"Each target_value must be at most {MAX_TARGET_VALUE_CHARS} characters"
                )
            if trimmed:
                cleaned.append(trimmed)
        return cleaned


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


def _parse_list_envelope(raw: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if isinstance(raw, Mapping):
        raw = raw.get(key)
    if not isinstance(raw, list):
        return []
    return [copy.deepcopy(dict(item)) for item in raw if isinstance(item, Mapping)]


def _resolve_variation_runner() -> Callable[..., Any]:
    if _VARIATION_RUNNER is not None:
        return _VARIATION_RUNNER
    from creative_agent.variations import generate_variations_for_session

    return generate_variations_for_session


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, genai_errors.APIError) and getattr(exc, "code", None) == 504:
        return True
    name_lower = type(exc).__name__.lower()
    return "timed out" in name_lower or "timeout" in name_lower


def _is_rate_limit_error(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    status = str(getattr(exc, "status", "") or "").upper()
    if isinstance(exc, genai_errors.APIError) and (
        code == 429 or "429" in status or "RESOURCE_EXHAUSTED" in status
    ):
        return True
    return code == 429 or "RESOURCE_EXHAUSTED" in status


router = APIRouter()


@router.post("/runs/{app_name}/{user_id}/{session_id}/variations")
async def create_session_variations(
    app_name: str,
    user_id: str,
    session_id: str,
    body: GenerateVariationsRequest,
    request: Request,
) -> dict[str, Any]:
    """Generate 1–3 single-axis controlled variations for a completed creative session."""
    caller_user = _authorize_caller(request, user_id)
    if app_name not in CREATIVE_APPS:
        raise HTTPException(
            status_code=409,
            detail=f"Controlled variations are not supported for app '{app_name}'",
        )

    lock = _get_variation_lock(app_name, session_id)
    async with lock:
        session = await _get_session_or_none(app_name, caller_user, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        state: dict[str, Any] = dict(getattr(session, "state", None) or {})
        if state.get(RUN_STATUS_KEY) == "running" or _is_live(
            (app_name, caller_user, session_id)
        ):
            raise HTTPException(
                status_code=409,
                detail="Cannot generate variations while the creative run is active",
            )

        existing_concepts = _parse_list_envelope(
            state.get("final_visual_concepts"), "visual_concepts"
        )
        if not existing_concepts:
            raise HTTPException(
                status_code=400,
                detail="Session does not contain finalized visual concepts",
            )
        if body.base_concept_index >= len(existing_concepts):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"base_concept_index {body.base_concept_index} is out of range "
                    f"(session has {len(existing_concepts)} visual concept(s))"
                ),
            )
        if len(existing_concepts) + body.count > MAX_CONCEPTS_PER_SESSION:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Adding {body.count} variation(s) would exceed the per-session "
                    f"limit of {MAX_CONCEPTS_PER_SESSION} visual concepts "
                    f"(currently {len(existing_concepts)})"
                ),
            )

        existing_copies = _parse_list_envelope(
            state.get("ad_copy_critique"), "ad_copies"
        )
        created_at = dt.datetime.now(dt.UTC).isoformat()
        runner_fn = _resolve_variation_runner()

        try:
            result = await runner_fn(
                app_name=app_name,
                user_id=caller_user,
                session_id=session_id,
                state=state,
                base_concept_index=body.base_concept_index,
                axis=body.axis,
                count=body.count,
                target_values=body.target_values,
                artifact_service=_ARTIFACT_SERVICE,
                created_at=created_at,
            )
        except HTTPException:
            raise
        except TimeoutError as exc:
            log.warning(
                "variations timed out for session=%r axis=%r: %s",
                session_id,
                body.axis,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=504,
                detail="Variation generation timed out; please retry.",
            ) from exc
        except Exception as exc:
            if _is_timeout_error(exc):
                log.warning(
                    "variations timed out for session=%r axis=%r: %s",
                    session_id,
                    body.axis,
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=504,
                    detail="Variation generation timed out; please retry.",
                ) from exc
            if _is_rate_limit_error(exc):
                log.warning(
                    "variations rate-limited for session=%r axis=%r: %s",
                    session_id,
                    body.axis,
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=429,
                    detail="Variation generation rate-limited upstream; please retry shortly.",
                ) from exc
            log.warning(
                "variations failed upstream for session=%r axis=%r: %s",
                session_id,
                body.axis,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=502,
                detail="Variation generation failed upstream; please retry.",
            ) from exc

        from creative_agent.gcs_tools import artifact_key_for  # lazy import

        updated_concepts = [*existing_concepts, *result.new_concepts]
        updated_copies = [*existing_copies, *result.new_copies]

        existing_images_raw = state.get("generated_images")
        existing_images = (
            copy.deepcopy(dict(existing_images_raw))
            if isinstance(existing_images_raw, Mapping)
            else {}
        )
        merged_images = {**existing_images, **result.new_generated_images}

        # Keep _generated_artifact_keys synchronized so experiments.snapshot_arms
        # resolves imageUri for both base creatives and newly minted variation arms.
        prior_keys_raw = state.get("_generated_artifact_keys")
        prior_keys = (
            [str(k) for k in prior_keys_raw if isinstance(k, str) and k]
            if isinstance(prior_keys_raw, list)
            else []
        )
        if not prior_keys and existing_images:
            for item in existing_concepts:
                cname = str(item.get("concept_name") or "")
                if cname and cname in existing_images:
                    rec = existing_images[cname]
                    akey = (
                        rec.get("artifact_key") if isinstance(rec, Mapping) else None
                    ) or artifact_key_for(cname)
                    prior_keys.append(str(akey))
        merged_artifact_keys = list(
            dict.fromkeys([*prior_keys, *result.new_artifact_keys])
        )

        prior_variations_raw = state.get("creative_variations")
        prior_variations = (
            [
                copy.deepcopy(dict(v))
                for v in prior_variations_raw
                if isinstance(v, Mapping)
            ]
            if isinstance(prior_variations_raw, list)
            else []
        )
        merged_variations = [*prior_variations, *result.variation_records]

        state_delta: dict[str, Any] = {
            "final_visual_concepts": {"visual_concepts": updated_concepts},
            "generated_images": merged_images,
            "_generated_artifact_keys": merged_artifact_keys,
            "creative_evaluation_report": result.updated_report,
            "creative_variations": merged_variations,
        }
        if updated_copies:
            state_delta["ad_copy_critique"] = {"ad_copies": updated_copies}
        minted_eval_uri = getattr(result, "eval_report_gcs_uri", None)
        if isinstance(minted_eval_uri, str) and minted_eval_uri:
            state_delta["eval_report_gcs_uri"] = minted_eval_uri
        minted_gallery_uri = getattr(result, "creative_gallery_gcs_uri", None)
        if isinstance(minted_gallery_uri, str) and minted_gallery_uri:
            state_delta["creative_gallery_gcs_uri"] = minted_gallery_uri

        await _SESSION_SERVICE.append_event(
            session,
            Event(
                author=RUNSERVER_AUTHOR,
                invocation_id=RUNSERVER_AUTHOR,
                actions=EventActions(state_delta=state_delta),
            ),
        )

        merged_state = {**state, **state_delta}
        for uri_candidate in {
            state.get("eval_report_gcs_uri"),
            state_delta.get("eval_report_gcs_uri"),
            merged_state.get("eval_report_gcs_uri"),
        }:
            if isinstance(uri_candidate, str) and uri_candidate:
                ratings.invalidate_report_cache(uri_candidate)
        added_indices = list(
            range(
                len(existing_concepts),
                len(existing_concepts) + len(result.new_concepts),
            )
        )
        return {
            "session_id": session_id,
            "app_name": app_name,
            "axis": body.axis,
            "base_concept_index": body.base_concept_index,
            "added_indices": added_indices,
            "variations": result.new_concepts,
            "new_ad_copies": result.new_copies,
            "generated_images": result.new_generated_images,
            "creative_evaluation_report": result.updated_report,
            "state": merged_state,
        }
