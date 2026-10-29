"""Run changelog & Gemini Enterprise A2A handoff endpoints.

Routes (both user-scoped under ``/runs/{app_name}/{user_id}/{session_id}/...``
and gated by ``UserAuthzMiddleware``):

- ``GET /runs/{app_name}/{user_id}/{session_id}/changelog``: returns deterministic
  provenance, source counts, quality-gate revision rounds, degradation warnings,
  and chronological milestone entries derived from ``session.state`` and
  ``session.events``.
- ``POST /runs/{app_name}/{user_id}/{session_id}/claim-handoff``: verifies a
  signed HMAC handoff token for an ``a2a-service`` session and clones its state,
  event log, and session-scoped ADK artifacts into a new session owned by the
  authenticated Studio user (idempotent per ``(caller_user, app_name, session_id)``).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import threading
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from google.adk.errors.session_not_found_error import SessionNotFoundError
from google.adk.events import Event, EventActions
from pydantic import BaseModel, Field

from agent_common.observability import collect_degradation_warnings
from runserver.a2a_mount import A2A_SERVICE_USER, verify_handoff_token
from runserver.async_runs import RUN_STATUS_KEY, RUNSERVER_AUTHOR
from runserver.authz import (
    AuthzMode,
    UserAuthzError,
    authorize_body_user,
    normalize_user_id,
    trusted_user,
)

log = logging.getLogger(__name__)
VALID_ORIGINS = frozenset({"studio", "a2a", "gemini_enterprise"})
_CLAIM_CACHE_MAX = 2048
_HANDOFF_CLAIMED_BY_KEY = "__handoff_claimed_by"
_HANDOFF_CLAIMED_SID_KEY = "__handoff_claimed_session_id"
_CHECKPOINT_LABELS: dict[str, str] = {
    "review_trends": "Reviewed candidate trends",
    "review_research": "Reviewed research & creative brief",
    "review_ad_copies": "Reviewed ad copy",
    "review_visual_concepts": "Reviewed visual concepts",
}

_SESSION_SERVICE: Any = None
_ARTIFACT_SERVICE: Any = None
_AUTHZ_MODE: AuthzMode = AuthzMode.TRUST_CLIENT
_HANDOFF_SECRET_OVERRIDE: str | None = None
_CLAIMED_SESSIONS: OrderedDict[tuple[str, str], tuple[str, str]] = OrderedDict()
_CLAIMED_CACHE_LOCK = threading.Lock()
_CLAIM_LOCKS: weakref.WeakValueDictionary[tuple[int, str, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)
_CLAIM_LOCKS_GUARD = threading.Lock()


def _get_claim_lock(app_name: str, session_id: str) -> asyncio.Lock:
    loop_id = id(asyncio.get_running_loop())
    key = (loop_id, app_name, session_id)
    with _CLAIM_LOCKS_GUARD:
        lock = _CLAIM_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _CLAIM_LOCKS[key] = lock
        return lock


def configure(
    *,
    session_service: Any,
    artifact_service: Any = None,
    authz_mode: AuthzMode = AuthzMode.TRUST_CLIENT,
    handoff_secret: str | None = None,
) -> None:
    """Bind the shared ``session_service``, ``artifact_service``, ``authz_mode``, and optional secret override."""
    global _SESSION_SERVICE, _ARTIFACT_SERVICE, _AUTHZ_MODE, _HANDOFF_SECRET_OVERRIDE
    _SESSION_SERVICE = session_service
    _ARTIFACT_SERVICE = artifact_service
    _AUTHZ_MODE = authz_mode
    _HANDOFF_SECRET_OVERRIDE = handoff_secret
    with _CLAIMED_CACHE_LOCK:
        _CLAIMED_SESSIONS.clear()


def _resolve_handoff_secret() -> str | None:
    if _HANDOFF_SECRET_OVERRIDE is not None:
        return _HANDOFF_SECRET_OVERRIDE.strip() or None
    return (os.environ.get("A2A_HANDOFF_SECRET") or "").strip() or None


def _as_obj(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _safe_nonneg_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _count_sources(value: Any) -> int:
    obj = _as_obj(value)
    if isinstance(obj, (Mapping, list, tuple)):
        return len(obj)
    return 0


def _field(obj: Any, snake: str, camel: str | None = None) -> Any:
    if isinstance(obj, Mapping):
        if snake in obj:
            return obj[snake]
        return obj.get(camel) if camel else None
    return getattr(obj, snake, None)


def _event_timestamp(event: Any) -> float | str | None:
    ts = _field(event, "timestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    if isinstance(ts, str) and ts.strip():
        return ts.strip()
    return None


def _entry(
    kind: str,
    label: str,
    *,
    detail: str | None = None,
    timestamp: float | str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": kind, "label": label}
    if detail:
        item["detail"] = detail
    if timestamp is not None:
        item["timestamp"] = timestamp
    return item


def _summarize_checkpoint_response(name: str, response: Any) -> str | None:
    if not isinstance(response, Mapping):
        return None
    action = (
        response.get("action") or response.get("decision") or response.get("status")
    )
    feedback = response.get("feedback") or response.get("notes")
    parts: list[str] = []
    if isinstance(action, str) and action.strip():
        parts.append(action.strip().replace("_", " ").capitalize())
    elif response.get("approved") is True:
        parts.append("Approved")
    elif response.get("approved") is False:
        parts.append("Requested revision")
    if name == "review_trends":
        picked = response.get("selected_trends") or response.get("trends")
        if isinstance(picked, list) and picked:
            parts.append(f"{len(picked)} trend(s) selected")
    if isinstance(feedback, str) and feedback.strip():
        snippet = " ".join(feedback.split())
        if len(snippet) > 100:
            snippet = snippet[:99].rstrip() + "…"
        parts.append(snippet)
    return " — ".join(parts) if parts else None


def _extract_checkpoint_entries(
    state: Mapping[str, Any], events: Sequence[Any]
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen_brief_edit = False
    seen_copy_review = False
    seen_visual_review = False

    for ev in events:
        ts = _event_timestamp(ev)
        actions = _field(ev, "actions")
        delta = (
            _field(actions, "state_delta", "stateDelta")
            if actions is not None
            else None
        )
        if isinstance(delta, Mapping):
            if delta.get("creative_brief_edited"):
                seen_brief_edit = True
                entries.append(
                    _entry(
                        "checkpoint",
                        "Edited creative brief at review checkpoint",
                        detail="Updated structured creative brief before copywriting",
                        timestamp=ts,
                    )
                )
            elif (
                delta.get("combined_final_cited_report")
                and _field(ev, "author") == "__runserver__"
            ):
                entries.append(
                    _entry(
                        "checkpoint",
                        "Edited research report at review checkpoint",
                        timestamp=ts,
                    )
                )
            notes = delta.get("visual_revision_notes")
            if isinstance(notes, list) and notes:
                seen_visual_review = True
                entries.append(
                    _entry(
                        "checkpoint",
                        "Submitted visual concept edits",
                        detail=f"{len(notes)} revision note(s) applied before rendering",
                        timestamp=ts,
                    )
                )

        content = _field(ev, "content")
        parts = _field(content, "parts") if content is not None else None
        for part in parts or ():
            fn_resp = _field(part, "function_response", "functionResponse")
            if fn_resp is None:
                continue
            name = _field(fn_resp, "name")
            if not isinstance(name, str) or name not in _CHECKPOINT_LABELS:
                continue
            if name == "review_ad_copies":
                seen_copy_review = True
            elif name == "review_visual_concepts":
                seen_visual_review = True
            resp_payload = _field(fn_resp, "response")
            entries.append(
                _entry(
                    "checkpoint",
                    _CHECKPOINT_LABELS[name],
                    detail=_summarize_checkpoint_response(name, resp_payload),
                    timestamp=ts,
                )
            )

    # Fallback to durable state markers when events were not copied (e.g. claimed handoff).
    if state.get("creative_brief_edited") and not seen_brief_edit:
        entries.append(
            _entry(
                "checkpoint",
                "Edited creative brief at review checkpoint",
                detail="Updated structured creative brief before copywriting",
            )
        )
    if state.get("ad_copy_user_revised") and not seen_copy_review:
        entries.append(
            _entry(
                "checkpoint",
                "Revised ad copy from reviewer feedback",
            )
        )
    notes_state = state.get("visual_revision_notes")
    if isinstance(notes_state, list) and notes_state and not seen_visual_review:
        entries.append(
            _entry(
                "checkpoint",
                "Submitted visual concept edits",
                detail=f"{len(notes_state)} revision note(s) applied before rendering",
            )
        )
    return entries


def build_changelog(
    app_name: str,
    session_id: str,
    state: Mapping[str, Any],
    events: Sequence[Any] = (),
) -> dict[str, Any]:
    """Deterministically build a run changelog payload from ``state`` and ``events``."""
    raw_origin = str(state.get("run_origin") or "studio").strip().lower()
    origin = raw_origin if raw_origin in VALID_ORIGINS else "studio"
    handoff_src = state.get("handoff_source_session_id")
    handoff_source_session_id = (
        handoff_src.strip()
        if isinstance(handoff_src, str) and handoff_src.strip()
        else None
    )

    gate_rounds = {
        "brief": _safe_nonneg_int(state.get("brief_revision_rounds_used")),
        "ad_copy": _safe_nonneg_int(state.get("ad_copy_revision_rounds_used")),
        "visual_concept": _safe_nonneg_int(
            state.get("visual_concept_revision_rounds_used")
        ),
    }
    source_counts = {
        "web": _count_sources(state.get("sources")),
        "enterprise": _count_sources(state.get("enterprise_sources")),
    }
    warnings = collect_degradation_warnings(dict(state))

    first_ts = _event_timestamp(events[0]) if events else None
    entries: list[dict[str, Any]] = []

    brand = str(state.get("brand") or "").strip()
    trend = str(
        state.get("target_search_trends") or state.get("target_search_trend") or ""
    ).strip()
    campaign_detail = " — ".join(p for p in (brand, trend) if p) or None

    if origin == "gemini_enterprise":
        detail = (
            f"Handed off from Gemini Enterprise session {handoff_source_session_id}"
            if handoff_source_session_id
            else (campaign_detail or "Initiated from Gemini Enterprise")
        )
        entries.append(
            _entry(
                "origin",
                "Started in Gemini Enterprise",
                detail=detail,
                timestamp=first_ts,
            )
        )
    elif origin == "a2a":
        entries.append(
            _entry(
                "origin",
                "Started via A2A",
                detail=campaign_detail or "Initiated via A2A protocol",
                timestamp=first_ts,
            )
        )
    else:
        entries.append(
            _entry(
                "origin",
                "Started in Trend Trawler Studio",
                detail=campaign_detail,
                timestamp=first_ts,
            )
        )

    if state.get("selected_gtrends"):
        entries.append(
            _entry(
                "trends",
                "Selected campaign search trends",
            )
        )

    has_research = bool(
        state.get("combined_final_cited_report")
        or state.get("creative_brief")
        or source_counts["web"]
        or source_counts["enterprise"]
    )
    if has_research:
        src_parts = [f"{source_counts['web']} web source(s)"]
        if source_counts["enterprise"]:
            src_parts.append(f"{source_counts['enterprise']} enterprise source(s)")
        if gate_rounds["brief"] > 0:
            src_parts.append(f"brief gate revised {gate_rounds['brief']}x")
        entries.append(
            _entry(
                "research",
                "Synthesized research & creative brief",
                detail=", ".join(src_parts),
            )
        )

    entries.extend(_extract_checkpoint_entries(state, events))

    ad_copy_obj = _as_obj(state.get("ad_copy_critique"))
    ad_copies = (
        ad_copy_obj.get("ad_copies") if isinstance(ad_copy_obj, Mapping) else None
    )
    if isinstance(ad_copies, list) and ad_copies:
        copy_detail = (
            f"{len(ad_copies)} final copies ({gate_rounds['ad_copy']} gate revision(s))"
            if gate_rounds["ad_copy"] > 0
            else f"{len(ad_copies)} final copies passed copy gate"
        )
        entries.append(
            _entry("gate", "Finalized & gated ad copies", detail=copy_detail)
        )

    vc_obj = _as_obj(state.get("final_visual_concepts"))
    visual_concepts = (
        vc_obj.get("visual_concepts") if isinstance(vc_obj, Mapping) else None
    )
    if isinstance(visual_concepts, list) and visual_concepts:
        vc_detail = (
            f"{len(visual_concepts)} concepts ({gate_rounds['visual_concept']} gate revision(s))"
            if gate_rounds["visual_concept"] > 0
            else f"{len(visual_concepts)} concepts passed concept gate"
        )
        entries.append(
            _entry("gate", "Finalized & gated visual concepts", detail=vc_detail)
        )

    gen_images = _as_obj(state.get("generated_images"))
    if isinstance(gen_images, Mapping) and gen_images:
        rerendered: list[str] = []
        total_rerenders = 0
        for concept_name, rec in gen_images.items():
            if isinstance(rec, Mapping):
                attempts = _safe_nonneg_int(rec.get("attempts") or 1)
                if attempts > 1:
                    rerendered.append(str(concept_name))
                    total_rerenders += attempts - 1
        if total_rerenders > 0:
            entries.append(
                _entry(
                    "image_qa",
                    "Rendered ad images with QA re-renders",
                    detail=(
                        f"{total_rerenders} QA re-render(s) across "
                        f"{', '.join(rerendered)}"
                    ),
                )
            )
        else:
            entries.append(
                _entry(
                    "image_qa",
                    "Rendered ad images",
                    detail=f"{len(gen_images)} image(s) passed first-pass QA",
                )
            )

    eval_report = _as_obj(state.get("creative_evaluation_report"))
    if isinstance(eval_report, Mapping) and eval_report:
        summary = eval_report.get("summary")
        pass_rate = (
            summary.get("overall_pass_rate")
            if isinstance(summary, Mapping)
            else eval_report.get("overall_pass_rate")
        )
        detail = None
        if isinstance(pass_rate, (int, float)) and not isinstance(pass_rate, bool):
            detail = f"Judge pass rate: {round(float(pass_rate) * 100)}%"
        entries.append(
            _entry("evaluation", "Evaluated creative portfolio", detail=detail)
        )
    elif state.get("eval_report_gcs_uri"):
        entries.append(_entry("evaluation", "Evaluated creative portfolio"))

    return {
        "session_id": session_id,
        "app_name": app_name,
        "origin": origin,
        "handoff_source_session_id": handoff_source_session_id,
        "gate_rounds": gate_rounds,
        "source_counts": source_counts,
        "warnings": warnings,
        "entries": entries,
    }


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


async def _replay_handoff_events(
    new_session: Any,
    source_events: Sequence[Any],
    claimed_state: Mapping[str, Any],
    source_session_id: str,
    *,
    app_name: str,
) -> None:
    """Replay non-partial ``source_events`` onto ``new_session`` with fresh event IDs.

    Preserves ``long_running_tool_ids``, ``function_call.id``, ``invocation_id``,
    and ``actions.agent_state`` so paused interactive checkpoints can be viewed
    and resumed in Studio on ``/run/<new_sid>``. Appends a final ``__runserver__``
    state-sync event so ``claimed_state`` (including ``run_origin='gemini_enterprise'``,
    ``handoff_source_session_id``, and ``ui_app``) remains authoritative.
    """
    non_partial = [ev for ev in source_events if not getattr(ev, "partial", False)]
    if not non_partial:
        return

    has_status_marker = False
    has_error_event = False
    for ev in non_partial:
        if isinstance(ev, Event):
            cloned = ev.model_copy(deep=True, update={"id": str(uuid.uuid4())})
        elif isinstance(ev, Mapping):
            cloned = Event.model_validate(
                {**copy.deepcopy(dict(ev)), "id": str(uuid.uuid4())}
            )
        else:
            continue
        if not cloned.invocation_id:
            cloned.invocation_id = RUNSERVER_AUTHOR
        delta = cloned.actions.state_delta if cloned.actions is not None else None
        if isinstance(delta, dict):
            delta.pop(_HANDOFF_CLAIMED_BY_KEY, None)
            delta.pop(_HANDOFF_CLAIMED_SID_KEY, None)
            if (
                cloned.author == RUNSERVER_AUTHOR
                and not delta
                and cloned.content is None
            ):
                continue
            if "run_origin" in delta:
                delta["run_origin"] = "gemini_enterprise"
            if "ui_app" in delta:
                delta["ui_app"] = app_name
            if delta.get(RUN_STATUS_KEY):
                has_status_marker = True
        if cloned.error_code or cloned.error_message:
            has_error_event = True
        await _SESSION_SERVICE.append_event(new_session, cloned)

    current_state = getattr(new_session, "state", None) or {}
    handoff_delta: dict[str, Any] = {
        "run_origin": "gemini_enterprise",
        "handoff_source_session_id": source_session_id,
        "ui_app": app_name,
    }
    for key, val in claimed_state.items():
        if not str(key).startswith("temp:") and current_state.get(key) != val:
            handoff_delta[key] = copy.deepcopy(val)
    if not has_status_marker and not has_error_event:
        handoff_delta[RUN_STATUS_KEY] = "done"

    await _SESSION_SERVICE.append_event(
        new_session,
        Event(
            author=RUNSERVER_AUTHOR,
            invocation_id=RUNSERVER_AUTHOR,
            actions=EventActions(state_delta=handoff_delta),
        ),
    )


async def _copy_session_artifacts(
    *,
    app_name: str,
    source_session_id: str,
    target_user_id: str,
    target_session_id: str,
) -> None:
    """Best-effort copy of session-scoped ADK artifacts (skipping ``user:`` keys)."""
    if _ARTIFACT_SERVICE is None:
        return
    try:
        keys = await _ARTIFACT_SERVICE.list_artifact_keys(
            app_name=app_name,
            user_id=A2A_SERVICE_USER,
            session_id=source_session_id,
        )
    except Exception:
        log.warning(
            "claim-handoff: failed to list artifacts for %s/%s",
            app_name,
            source_session_id,
            exc_info=True,
        )
        return
    for key in keys or ():
        if not isinstance(key, str) or not key or key.startswith("user:"):
            continue
        try:
            part = await _ARTIFACT_SERVICE.load_artifact(
                app_name=app_name,
                user_id=A2A_SERVICE_USER,
                session_id=source_session_id,
                filename=key,
            )
            if part is None:
                continue
            await _ARTIFACT_SERVICE.save_artifact(
                app_name=app_name,
                user_id=target_user_id,
                session_id=target_session_id,
                filename=key,
                artifact=part,
            )
        except Exception:
            log.warning(
                "claim-handoff: failed to copy artifact %r from %s/%s to %s/%s",
                key,
                app_name,
                source_session_id,
                target_user_id,
                target_session_id,
                exc_info=True,
            )


class _ClaimHandoffBody(BaseModel):
    handoff: str = Field(..., min_length=1, max_length=2048)


router = APIRouter()


@router.get("/runs/{app_name}/{user_id}/{session_id}/changelog")
async def http_get_run_changelog(
    app_name: str, user_id: str, session_id: str, request: Request
) -> dict[str, Any]:
    caller_user = _authorize_caller(request, user_id)
    session = await _get_session_or_none(app_name, caller_user, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return build_changelog(
        app_name=app_name,
        session_id=session_id,
        state=dict(session.state or {}),
        events=list(getattr(session, "events", None) or ()),
    )


@router.post("/runs/{app_name}/{user_id}/{session_id}/claim-handoff")
async def http_claim_handoff(
    app_name: str,
    user_id: str,
    session_id: str,
    body: _ClaimHandoffBody,
    request: Request,
) -> dict[str, Any]:
    caller_user = _authorize_caller(request, user_id)
    secret = _resolve_handoff_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="A2A handoff is not configured")
    if not verify_handoff_token(
        body.handoff,
        app_name=app_name,
        session_id=session_id,
        secret=secret,
    ):
        raise HTTPException(status_code=403, detail="Invalid or expired handoff token")

    claim_key = (app_name, session_id)
    claim_lock = _get_claim_lock(app_name, session_id)
    async with claim_lock:
        with _CLAIMED_CACHE_LOCK:
            cached = _CLAIMED_SESSIONS.get(claim_key)
        if cached is not None:
            owner_user, cached_sid = cached
            if owner_user != caller_user:
                raise HTTPException(
                    status_code=403, detail="Handoff token has already been claimed"
                )
            existing_claimed = await _get_session_or_none(
                app_name, caller_user, cached_sid
            )
            if existing_claimed is not None:
                with _CLAIMED_CACHE_LOCK:
                    if claim_key in _CLAIMED_SESSIONS:
                        _CLAIMED_SESSIONS.move_to_end(claim_key)
                return {
                    "session_id": str(existing_claimed.id),
                    "app_name": app_name,
                    "already_owned": False,
                }

        source_session = await _get_session_or_none(
            app_name, A2A_SERVICE_USER, session_id
        )
        if source_session is None:
            owned_session = await _get_session_or_none(
                app_name, caller_user, session_id
            )
            if owned_session is not None:
                return {
                    "session_id": session_id,
                    "app_name": app_name,
                    "already_owned": True,
                }
            raise HTTPException(
                status_code=404, detail="Handoff source session not found"
            )

        src_state = dict(source_session.state or {})
        durable_owner = src_state.get(_HANDOFF_CLAIMED_BY_KEY)
        durable_sid = src_state.get(_HANDOFF_CLAIMED_SID_KEY)
        if isinstance(durable_owner, str) and durable_owner:
            if durable_owner != caller_user:
                if isinstance(durable_sid, str) and durable_sid:
                    with _CLAIMED_CACHE_LOCK:
                        _CLAIMED_SESSIONS[claim_key] = (durable_owner, durable_sid)
                        while len(_CLAIMED_SESSIONS) > _CLAIM_CACHE_MAX:
                            _CLAIMED_SESSIONS.popitem(last=False)
                raise HTTPException(
                    status_code=403, detail="Handoff token has already been claimed"
                )
            if isinstance(durable_sid, str) and durable_sid:
                existing_durable = await _get_session_or_none(
                    app_name, caller_user, durable_sid
                )
                if existing_durable is not None:
                    resolved_sid = str(existing_durable.id)
                    with _CLAIMED_CACHE_LOCK:
                        _CLAIMED_SESSIONS[claim_key] = (caller_user, resolved_sid)
                        _CLAIMED_SESSIONS.move_to_end(claim_key)
                        while len(_CLAIMED_SESSIONS) > _CLAIM_CACHE_MAX:
                            _CLAIMED_SESSIONS.popitem(last=False)
                    return {
                        "session_id": resolved_sid,
                        "app_name": app_name,
                        "already_owned": False,
                    }

        source_events = list(getattr(source_session, "events", None) or ())
        claimed_state = copy.deepcopy(src_state)
        claimed_state.pop(_HANDOFF_CLAIMED_BY_KEY, None)
        claimed_state.pop(_HANDOFF_CLAIMED_SID_KEY, None)
        claimed_state["run_origin"] = "gemini_enterprise"
        claimed_state["handoff_source_session_id"] = session_id
        claimed_state["ui_app"] = app_name

        new_session = await _SESSION_SERVICE.create_session(
            app_name=app_name,
            user_id=caller_user,
            state=claimed_state,
            session_id=None,
        )
        new_sid = str(new_session.id)
        await _replay_handoff_events(
            new_session,
            source_events,
            claimed_state,
            session_id,
            app_name=app_name,
        )
        await _copy_session_artifacts(
            app_name=app_name,
            source_session_id=session_id,
            target_user_id=caller_user,
            target_session_id=new_sid,
        )
        try:
            await _SESSION_SERVICE.append_event(
                source_session,
                Event(
                    author=RUNSERVER_AUTHOR,
                    invocation_id=RUNSERVER_AUTHOR,
                    actions=EventActions(
                        state_delta={
                            _HANDOFF_CLAIMED_BY_KEY: caller_user,
                            _HANDOFF_CLAIMED_SID_KEY: new_sid,
                        }
                    ),
                ),
            )
        except Exception:
            log.warning(
                "claim-handoff: failed to persist durable claim marker on %s/%s",
                app_name,
                session_id,
                exc_info=True,
            )

        with _CLAIMED_CACHE_LOCK:
            _CLAIMED_SESSIONS[claim_key] = (caller_user, new_sid)
            _CLAIMED_SESSIONS.move_to_end(claim_key)
            while len(_CLAIMED_SESSIONS) > _CLAIM_CACHE_MAX:
                _CLAIMED_SESSIONS.popitem(last=False)
        return {
            "session_id": new_sid,
            "app_name": app_name,
            "already_owned": False,
        }
