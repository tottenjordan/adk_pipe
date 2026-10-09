"""Async-job run helpers (pure) for the runserver package."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from google.adk.errors.session_not_found_error import SessionNotFoundError
from google.adk.events import Event, EventActions
from google.genai import types
from pydantic import BaseModel, ValidationError

from runserver.authz import (
    AuthzMode,
    UserAuthzError,
    authorize_body_user,
    trusted_user,
)

if TYPE_CHECKING:
    from google.adk.sessions import Session

RUNSERVER_AUTHOR = "__runserver__"
RUN_STATUS_KEY = "__run_status"
RUN_ERROR_KEY = "__run_error"

# Hard ceiling on a single detached run (seconds). A wedged model call would
# otherwise keep the asyncio task — and, with --no-cpu-throttling --min-instances
# 1, the Cloud Run instance — alive indefinitely with no server-side stall
# detection, leaving the run polling 'running' forever. Default 1800s is well
# beyond a normal ~6-8 min run; override via the RUN_MAX_SECONDS env var.
RUN_MAX_SECONDS = int(os.environ.get("RUN_MAX_SECONDS", "1800"))

# Bounded attempts for writing the terminal status marker (see
# _append_terminal_safe). The marker IS the poller contract, so its own write is
# retried a little against a transient session service, then dropped (never raised).
_MARKER_APPEND_ATTEMPTS = 2

# --- Auto-continue after an empty root turn ------------------------------------
#
# A gemini Pro root agent occasionally returns an EMPTY final turn (finish_reason
# STOP, no text, no function call) right after a long NodeTool response. ADK
# treats that as the end of the invocation, so the segment would end ``done``
# with the workflow unfinished (e.g. interactive_creative never calling its next
# checkpoint). A plain re-prompt recovers it, so ``_drive_run`` re-prompts with
# ``AUTO_CONTINUE_MESSAGE`` — bounded by ``MAX_AUTO_CONTINUES`` per detached
# run — when ``should_auto_continue`` says the segment ended on that signature.
AUTO_CONTINUE_MESSAGE = (
    "Continue the WORKFLOW from where it stopped. Do not repeat completed steps; "
    "call the next step now."
)
AUTO_CONTINUES_KEY = "__auto_continues"
_DEFAULT_MAX_AUTO_CONTINUES = 2
_MAX_AUTO_CONTINUES_CEILING = 3

# Per-app state key whose (non-empty) presence means the workflow has finished.
# Creative apps key on `finalize_done`, set by finalize_pipeline's terminal node
# on every path. Not eval_report_gcs_uri (written mid persist_node) nor
# eval_bq_row_uuid (never written when there is no report or the eval BQ write
# failed, so keying on it would re-run finalize, incl. the ~70 s judge).
_COMPLETION_KEYS = {
    "creative_agent": "finalize_done",
    "interactive_creative": "finalize_done",
    "trend_scout": "select_trends_markdown_gcs_uri",
}

# --- Person reference consent check ---------------------------------------------
#
# A creative run may cast the caller's consented person (session state
# ``person_reference = {uri, consent_id}``, seeded by the browser via createSession
# initialState). The api is the consent authority: at kick-off the consent must be
# active, the caller's own, and for the same photo (else 400
# ``person_reference_invalid``); a resume re-checks it is still active (a revoke
# mid-run → 400 ``person_reference_revoked``). Both raise before anything is claimed
# or written. The engines only re-check the URI shape. Photo URIs are never logged.
PERSON_REFERENCE_APPS = frozenset({"creative_agent", "interactive_creative"})
PERSON_REFERENCE_KEY = "person_reference"


class PersonReferenceError(Exception):
    """The session's person reference can't be used (→ HTTP ``status``)."""

    def __init__(self, reason: str, message: str, status: int = 400):
        super().__init__(message)
        self.reason = reason
        self.status = status


async def check_person_reference(
    app_name: str, user_id: str, state: dict | None, *, reason: str
) -> None:
    """Raise ``PersonReferenceError(reason)`` unless the state's person reference
    is empty or names an active consent of ``user_id`` for the same photo. Only
    creative apps are checked; batch (CRF) runs never seed the key."""
    if app_name not in PERSON_REFERENCE_APPS or not state:
        return
    ref = state.get(PERSON_REFERENCE_KEY)
    if not ref:
        return
    invalid = PersonReferenceError(
        reason, "the selected person can't be used: their consent isn't active"
    )
    if not isinstance(ref, dict):
        raise invalid
    uri, consent_id = ref.get("uri"), ref.get("consent_id")
    if not (isinstance(uri, str) and uri and isinstance(consent_id, str)):
        raise invalid
    from runserver import person_refs

    try:
        record = await person_refs.active_consent(user_id, consent_id)
    except Exception as exc:
        logging.exception("person reference: consent lookup failed")
        raise PersonReferenceError(
            "person_reference_unavailable",
            "could not check the person's consent; retry shortly",
            status=503,
        ) from exc
    if record is None or record.get("photo_uri") != uri:
        raise invalid


def _parse_max_auto_continues(raw: str | None) -> int:
    """``RUN_MAX_AUTO_CONTINUES`` → int clamped to 0..3; unset/invalid → 2."""
    try:
        value = int(raw) if raw is not None and raw.strip() else None
    except ValueError:
        value = None
    if value is None:
        return _DEFAULT_MAX_AUTO_CONTINUES
    return max(0, min(_MAX_AUTO_CONTINUES_CEILING, value))


MAX_AUTO_CONTINUES = _parse_max_auto_continues(os.environ.get("RUN_MAX_AUTO_CONTINUES"))


def _field(obj, snake: str, camel: str | None = None):
    """Read a field from an ADK/genai model OR its serialized (camelCase) dict."""
    if isinstance(obj, dict):
        if snake in obj:
            return obj[snake]
        return obj.get(camel) if camel else None
    return getattr(obj, snake, None)


def _event_parts(event) -> list:
    content = _field(event, "content")
    return list(_field(content, "parts") or []) if content is not None else []


def _part_has_text(part) -> bool:
    text = _field(part, "text")
    return bool(text and text.strip()) and not _field(part, "thought")


def _has_unanswered_long_running_call(events) -> bool:
    """True if a long-running function call in ``events`` (its id listed in that
    event's ``long_running_tool_ids``) has no matching function_response in
    ``events`` — i.e. the segment legitimately paused at a checkpoint."""
    pending: set[str] = set()
    answered: set[str] = set()
    for ev in events:
        lr_ids = set(_field(ev, "long_running_tool_ids", "longRunningToolIds") or ())
        for part in _event_parts(ev):
            call = _field(part, "function_call", "functionCall")
            if call is not None and _field(call, "id") in lr_ids:
                pending.add(_field(call, "id"))
            resp = _field(part, "function_response", "functionResponse")
            if resp is not None and _field(resp, "id"):
                answered.add(_field(resp, "id"))
    return bool(pending - answered)


def _is_empty_turn(event) -> bool:
    """The empty-turn signature: no (non-thought) text, no function call, and no
    function response (a tool-output event is not a model turn)."""
    for part in _event_parts(event):
        if _part_has_text(part):
            return False
        if _field(part, "function_call", "functionCall") is not None:
            return False
        if _field(part, "function_response", "functionResponse") is not None:
            return False
    return True


def _is_lifecycle_event(event) -> bool:
    """An agent-lifecycle marker, not a model turn: no content parts, with
    ``actions.end_of_agent`` or ``actions.agent_state`` set (a resumable App
    appends one after the root's last turn). A real empty STOP turn has
    neither flag, so it still counts as empty."""
    if _event_parts(event):
        return False
    actions = _field(event, "actions")
    if actions is None:
        return False
    return bool(_field(actions, "end_of_agent", "endOfAgent")) or (
        _field(actions, "agent_state", "agentState") is not None
    )


def should_auto_continue(
    app_name: str, state: dict, segment_events: list, root_author: str, attempts: int
) -> bool:
    """Whether a segment that completed without error should be re-prompted.

    True only when ALL hold: ``attempts < MAX_AUTO_CONTINUES``; the app's
    completion key (``_COMPLETION_KEYS``) is missing/blank in ``state`` (unknown
    app → False); the segment did not pause at an unanswered long-running call;
    and the segment's last ``root_author`` event is an empty turn. Accepts ADK
    ``Event`` objects or their serialized dicts."""
    if attempts >= MAX_AUTO_CONTINUES or not root_author:
        return False
    completion_key = _COMPLETION_KEYS.get(app_name)
    if completion_key is None:
        return False
    done_value = (state or {}).get(completion_key)
    if done_value and (not isinstance(done_value, str) or done_value.strip()):
        return False
    if _has_unanswered_long_running_call(segment_events):
        return False
    root_events = [
        ev
        for ev in segment_events
        if _field(ev, "author") == root_author and not _is_lifecycle_event(ev)
    ]
    return bool(root_events) and _is_empty_turn(root_events[-1])


def _root_author(runner) -> str:
    """The root agent's name (= its events' ``author``); '' if undeterminable."""
    name = getattr(getattr(runner, "agent", None), "name", None)
    return name if isinstance(name, str) else ""


async def _get_session_or_none(
    session_service, app_name, user_id, session_id
) -> Session | None:
    """``get_session`` that maps ``SessionNotFoundError`` to ``None``.

    ADK session services are typed to return ``None`` for a missing session, but
    the ADK 2.9 changelog moves toward raising ``SessionNotFoundError`` instead
    (2.10's InMemory service still returns None). Normalize so the
    ``is None`` branches below (create-on-kickoff, best-effort no-ops) keep working
    whichever contract the configured service follows."""
    try:
        return await session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
    except SessionNotFoundError:
        return None


def get_root_agent(app_name: str):
    """Map an app_name to its ADK ``App`` (lazy import — builds a genai client).

    Every agent returns its ``App`` (not the bare root agent), so App-level
    ``plugins`` (the opt-in Model Armor screen, ``agent_common/safety.py``) reach
    the Runner. The interactive agents' Apps also carry
    ``ResumabilityConfig(is_resumable=True)``: a ``LongRunningFunctionTool``
    checkpoint only pauses/resumes when the Runner is built from such an App
    (``trend_scout``'s opt-in ``review_trends``; ``interactive_creative``'s three
    review checkpoints). ``creative_agent`` has no checkpoints, so its App is
    non-resumable. The runner factory still branches on the returned type
    (App vs Agent) for robustness."""
    from creative_agent.agent import app as creative_app
    from interactive_creative.agent import app as interactive_app
    from trend_scout.agent import app as scout_app

    agents = {
        "creative_agent": creative_app,
        "trend_scout": scout_app,
        "interactive_creative": interactive_app,
    }
    if app_name not in agents:
        raise KeyError(app_name)
    return agents[app_name]


def build_user_message(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def build_resume_message(
    function_call_id: str, name: str, response: dict
) -> types.Content:
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=function_call_id, name=name, response=response
                )
            )
        ],
    )


def build_terminal_event(
    status: str, error: str | None = None, *, invocation_id: str = RUNSERVER_AUTHOR
) -> Event:
    # ``invocation_id`` MUST be non-empty: VertexAiSessionService.append_event
    # rejects an event with an unset invocation_id (400 INVALID_ARGUMENT). We
    # thread the run's own invocation id through when available (see _drive_run)
    # and fall back to a stable non-empty marker author otherwise.
    delta = {RUN_STATUS_KEY: status}
    if error is not None:
        delta[RUN_ERROR_KEY] = error
    return Event(
        author=RUNSERVER_AUTHOR,
        invocation_id=invocation_id,
        actions=EventActions(state_delta=delta),
    )


def events_since(events, n: int):
    return list(events[n:]) if n and n > 0 else list(events)


def _serialize_event(ev: Event) -> dict:
    """Serialize an ADK Event to the exact camelCase shape the frontend
    ``AgentEvent`` expects (``invocationId``, ``actions.stateDelta``,
    ``longRunningToolIds`` as a list, ``errorCode``/``errorMessage``). This is
    the same by-alias JSON dump ADK's own ``get_session`` REST endpoint emits.
    ``mode="json"`` coerces the ``long_running_tool_ids`` set and any nested
    genai types to JSON-native values."""
    return ev.model_dump(mode="json", by_alias=True, exclude_none=True)


def _derive_status(events) -> tuple[str, str | None]:
    """Derive a run's ``(status, error_message)`` from its full event log.

    A ``__run_status`` terminal marker wins (scan for the LAST one, so a late
    ``done``/``error`` marker is authoritative); its ``__run_error`` supplies
    the message. Absent a marker, an in-pipeline error event (``error_code`` or
    ``error_message`` set — mirroring the frontend ``getEventError``) surfaces
    as ``error`` so model 429s aren't masked. Otherwise ``running``."""
    status = "running"
    error: str | None = None
    for ev in events:
        delta = getattr(getattr(ev, "actions", None), "state_delta", None) or {}
        marker = delta.get(RUN_STATUS_KEY)
        if marker:
            status = marker
            error = delta.get(RUN_ERROR_KEY)
    if status != "running":
        return status, error
    for ev in events:
        if getattr(ev, "error_code", None) or getattr(ev, "error_message", None):
            return "error", getattr(ev, "error_message", None) or getattr(
                ev, "error_code", None
            )
    return "running", None


async def get_run_status(
    *, app_name, user_id, session_id, since, session_service
) -> dict:
    """Poll a run: return its derived status, the events appended since the
    ``since`` cursor (serialized to the frontend ``AgentEvent`` shape), the next
    cursor, the merged session state, and any error message.

    Returns ``{"status": "not_found", ...}`` (not a raise) when the session is
    absent, so the caller/router can map it to a 404 while staying testable.

    ``get_session`` is *typed* to return ``None`` for a missing session, but the
    remote ``VertexAiSessionService`` instead RAISES (400/404) for an unknown or
    not-yet-visible session. Treat that the same as ``None`` — a poll must degrade
    to ``not_found`` (which the client's ``pollRun`` handles as transient) rather
    than surfacing a 500 that would abort the run view."""
    try:
        session = await session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
    except Exception:  # noqa: BLE001 — any lookup failure degrades to not_found (see above)
        logging.debug(
            "get_session failed for %s/%s/%s; treating as not_found",
            app_name,
            user_id,
            session_id,
        )
        session = None
    if session is None:
        return {"status": "not_found", "events": [], "nextCursor": 0, "state": {}}
    status, error = _derive_status(session.events)
    sliced = events_since(session.events, since)
    return {
        "status": status,
        "events": [_serialize_event(ev) for ev in sliced],
        "nextCursor": len(session.events),
        "state": dict(session.state),
        "error": error,
    }


# Hold references to detached run tasks so asyncio's GC can't cancel them before
# they finish (a classic footgun — create_task keeps only a weak reference).
_BACKGROUND_TASKS: set = set()

# Duplicate-run guard: at most ONE active detached run per (app, user, session).
# Without it a double POST (double-clicked Start, a retried resume) starts two
# concurrent Runners interleaving events into one session log.
#
# SINGLE-PROCESS, BEST-EFFORT guard. It is correct for the deployed backend
# because trend-trawler-api runs ONE uvicorn process (backend_entrypoint.sh
# passes no --workers) and all requests for a run normally land on that one
# instance (--min-instances 1). It is NOT a distributed lock: multiple uvicorn
# workers, or Cloud Run scaling out past one instance (max-instances > 1 — the
# service does not currently pin it), would each hold their own registry and
# defeat it. A multi-instance deploy would need a distributed lock (e.g. a
# conditional write on the session / a Firestore lease).
#
# Check + claim is atomic because it happens synchronously (no ``await`` between
# the check and the insert) on a single event loop. The key is claimed with the
# ``_CLAIMING`` sentinel BEFORE the kickoff's own awaits (session create, resume
# state writes), then swapped for the Task; the Task's done-callback releases it
# (only if the stored value is still that Task), and a setup failure before the
# Task exists releases the sentinel.
_RunKey = tuple[str, str, str]
_CLAIMING = object()
_ACTIVE_RUNS: dict[_RunKey, asyncio.Task | object] = {}
# The long-running function-call id an active RESUME segment is answering, kept in
# lockstep with ``_ACTIVE_RUNS`` (set on claim, dropped on the same release). Lets
# a duplicate resume for the SAME call be told apart from one blocked by a
# still-running prior segment — see ``RunAlreadyActive.reason``.
_ACTIVE_RESUME_CALL_IDS: dict[_RunKey, str] = {}

# How long a resume waits for the PREVIOUS segment's task to finish before
# rejecting with RunAlreadyActive. The frontend shows a review panel as soon as it
# polls the long-running function-call event, but the paused segment's task stays
# alive a little longer (remaining Runner events + the terminal-marker append,
# with its bounded retry). A resume in that window waits instead of 409ing — and
# waiting also stops the stale 'done' marker landing AFTER the resume's 'running'
# reset (which would make pollers stop early).
# The wait holds the resume HTTP request open: fine under the api service's 900s
# Cloud Run request timeout and the /api/adk proxy, which sets no timeout of its
# own (Node fetch/undici default: 300s to response headers).
RESUME_PRIOR_SEGMENT_GRACE_SECONDS = 30.0

# ``RunAlreadyActive.reason`` values (surfaced as ``detail.reason`` on the 409):
#   run_active           — a start hit an active run; it is live, just poll it.
#   resume_in_progress   — a duplicate resume for the SAME call; the first one is
#                          already serving the user's response, just poll it.
#   prior_segment_active — the previous segment was still running after the
#                          grace wait; this response was NOT applied, retry it.
REASON_RUN_ACTIVE = "run_active"
REASON_RESUME_IN_PROGRESS = "resume_in_progress"
REASON_PRIOR_SEGMENT_ACTIVE = "prior_segment_active"


class RunAlreadyActive(Exception):
    """A detached run is already active for this (app, user, session)."""

    def __init__(self, key: _RunKey, reason: str = REASON_RUN_ACTIVE):
        self.key = key
        self.reason = reason
        app_name, user_id, session_id = key
        super().__init__(
            f"a run is already active for app={app_name} user={user_id} "
            f"session={session_id}"
        )


def _is_live(key: _RunKey) -> bool:
    """True if ``key`` holds a claim sentinel or a not-yet-done Task. A done Task
    whose release callback hasn't run yet counts as released."""
    held = _ACTIVE_RUNS.get(key)
    if held is None:
        return False
    return not (isinstance(held, asyncio.Task) and held.done())


def _conflict_reason(key: _RunKey, function_call_id: str | None) -> str:
    if function_call_id is None:
        return REASON_RUN_ACTIVE
    if _ACTIVE_RESUME_CALL_IDS.get(key) == function_call_id:
        return REASON_RESUME_IN_PROGRESS
    return REASON_PRIOR_SEGMENT_ACTIVE


def _claim_run(key: _RunKey, function_call_id: str | None = None) -> None:
    """Atomically claim ``key`` (synchronous — never await inside) or raise.

    ``function_call_id`` is the long-running call a resume answers (``None`` for
    a start); it decides the ``RunAlreadyActive.reason`` on conflict."""
    if _is_live(key):
        raise RunAlreadyActive(key, _conflict_reason(key, function_call_id))
    _ACTIVE_RUNS[key] = _CLAIMING
    if function_call_id is None:
        _ACTIVE_RESUME_CALL_IDS.pop(key, None)
    else:
        _ACTIVE_RESUME_CALL_IDS[key] = function_call_id


def _release_claim(key: _RunKey) -> None:
    """Release a sentinel claim whose Task was never spawned (setup failed)."""
    if _ACTIVE_RUNS.get(key) is _CLAIMING:
        del _ACTIVE_RUNS[key]
        _ACTIVE_RESUME_CALL_IDS.pop(key, None)


def _register_run_task(key: _RunKey, task: asyncio.Task) -> None:
    """Swap the claim sentinel for ``task``; hold a strong ref; release on done."""
    _ACTIVE_RUNS[key] = task
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)

    def _release(t: asyncio.Task) -> None:
        if _ACTIVE_RUNS.get(key) is t:
            del _ACTIVE_RUNS[key]
            _ACTIVE_RESUME_CALL_IDS.pop(key, None)

    task.add_done_callback(_release)


async def _await_prior_segment(key: _RunKey) -> None:
    """If a previous segment's Task is still finishing, wait (bounded) for it.

    Only a real Task is awaited; a ``_CLAIMING`` sentinel means a concurrent
    duplicate request is mid-kickoff, which ``_claim_run`` then rejects. The
    prior task is never cancelled (``asyncio.wait`` doesn't cancel on timeout)."""
    prior = _ACTIVE_RUNS.get(key)
    if isinstance(prior, asyncio.Task) and not prior.done():
        await asyncio.wait({prior}, timeout=RESUME_PRIOR_SEGMENT_GRACE_SECONDS)
        # Let the prior task's done-callbacks (key release) run.
        await asyncio.sleep(0)


async def _append_terminal_safe(
    session_service, app_name, user_id, session_id, event
) -> None:
    """Append a terminal status marker, absorbing any failure.

    The terminal marker IS the completion/failure contract pollers read, so a
    marker write that itself fails must NOT escape the detached task — that would
    leave the exception unretrieved and the run polling ``running`` forever,
    violating ``_drive_run``'s never-re-raise guarantee. ``get_session`` +
    ``append_event`` are retried a bounded number of times against the
    (documented-transient) ``VertexAiSessionService``, then logged loudly and
    dropped. A missing session is unrecoverable → log and give up."""
    for attempt in range(1, _MARKER_APPEND_ATTEMPTS + 1):
        try:
            session = await _get_session_or_none(
                session_service, app_name, user_id, session_id
            )
            if session is None:
                logging.error(
                    "cannot append terminal marker: session missing app=%s session=%s",
                    app_name,
                    session_id,
                )
                return
            await session_service.append_event(session, event)
            return
        except Exception:  # noqa: BLE001 — bounded retry; final failure logged, never raised
            logging.exception(
                "terminal marker append failed (attempt %d/%d) app=%s session=%s",
                attempt,
                _MARKER_APPEND_ATTEMPTS,
                app_name,
                session_id,
            )
    logging.error(
        "gave up writing terminal marker after %d attempts app=%s session=%s",
        _MARKER_APPEND_ATTEMPTS,
        app_name,
        session_id,
    )


async def _maybe_record_auto_continue(
    session_service,
    app_name,
    user_id,
    session_id,
    segment_events,
    root_author,
    attempts,
    invocation_id,
) -> bool:
    """Decide whether to auto-continue after a cleanly-completed segment; if so,
    log it and record the cumulative ``__auto_continues`` count in session state.

    Never raises: a failed session read means "don't continue" (the segment
    finishes ``done`` as before); a failed record write is logged and the
    continue still happens (the count is observability, not control)."""
    # Cheap pre-check without state: state can only turn a True into False.
    if not should_auto_continue(app_name, {}, segment_events, root_author, attempts):
        return False
    try:
        session = await _get_session_or_none(
            session_service, app_name, user_id, session_id
        )
    except Exception:  # noqa: BLE001 — can't read state → don't continue
        logging.exception(
            "auto-continue check failed to read session app=%s session=%s",
            app_name,
            session_id,
        )
        return False
    if session is None:
        return False
    state = dict(session.state)
    if not should_auto_continue(app_name, state, segment_events, root_author, attempts):
        return False
    attempt = attempts + 1
    logging.warning(
        "auto-continue after empty root turn: app=%s session=%s attempt=%d",
        app_name,
        session_id,
        attempt,
    )
    prior = state.get(AUTO_CONTINUES_KEY)
    total = (prior if isinstance(prior, int) else 0) + 1
    try:
        await session_service.append_event(
            session,
            Event(
                author=RUNSERVER_AUTHOR,
                invocation_id=invocation_id or RUNSERVER_AUTHOR,
                actions=EventActions(state_delta={AUTO_CONTINUES_KEY: total}),
            ),
        )
    except Exception:  # noqa: BLE001 — record is best-effort; still continue
        logging.exception(
            "failed to record %s app=%s session=%s",
            AUTO_CONTINUES_KEY,
            app_name,
            session_id,
        )
    return True


async def _drive_run(
    runner, session_service, app_name, user_id, session_id, new_message
) -> None:
    """Drive a Runner to completion detached from any request, then append a
    terminal status marker to the session. Never re-raises — the terminal
    ``error`` marker IS the failure contract for pollers (even when the marker
    write itself fails; see ``_append_terminal_safe``). Bounded by
    ``RUN_MAX_SECONDS`` so a wedged run can't hang ``running`` forever.

    If a segment ends on an empty root turn with the workflow unfinished (see
    ``should_auto_continue``), the run is re-prompted with
    ``AUTO_CONTINUE_MESSAGE`` on the same runner/session — still inside this one
    task (so the run stays claimed and pollers keep seeing ``running``) and
    under the same ``RUN_MAX_SECONDS`` budget — before the terminal marker."""
    # Reuse the run's own invocation id on the terminal marker (Vertex requires a
    # non-empty invocation_id); fall back to the marker author if the run emitted
    # no events (e.g. an immediate error).
    invocation_id = RUNSERVER_AUTHOR
    root_author = _root_author(runner)
    try:
        async with asyncio.timeout(RUN_MAX_SECONDS):
            message = new_message
            attempts = 0
            while True:
                segment_events: list[Event] = []
                async for event in runner.run_async(
                    user_id=user_id, session_id=session_id, new_message=message
                ):
                    # Runner persists final events to the session service itself.
                    segment_events.append(event)
                    if getattr(event, "invocation_id", None):
                        invocation_id = event.invocation_id
                if not await _maybe_record_auto_continue(
                    session_service,
                    app_name,
                    user_id,
                    session_id,
                    segment_events,
                    root_author,
                    attempts,
                    invocation_id,
                ):
                    break
                attempts += 1
                message = build_user_message(AUTO_CONTINUE_MESSAGE)
        await _append_terminal_safe(
            session_service,
            app_name,
            user_id,
            session_id,
            build_terminal_event("done", invocation_id=invocation_id),
        )
    except TimeoutError:
        logging.error(
            "detached run timed out after %ss app=%s session=%s",
            RUN_MAX_SECONDS,
            app_name,
            session_id,
        )
        await _append_terminal_safe(
            session_service,
            app_name,
            user_id,
            session_id,
            build_terminal_event(
                "error",
                f"run exceeded {RUN_MAX_SECONDS}s timeout",
                invocation_id=invocation_id,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — terminal marker is the contract; log+persist, never raise
        logging.exception("detached run failed app=%s session=%s", app_name, session_id)
        await _append_terminal_safe(
            session_service,
            app_name,
            user_id,
            session_id,
            build_terminal_event("error", str(exc), invocation_id=invocation_id),
        )


async def start_run(
    *, app_name, user_id, session_id, message, session_service, runner_factory
) -> tuple[dict, asyncio.Task]:
    """Ensure the session exists, spawn a detached task that drives the run to
    completion, and return ``({"runId", "status": "running"}, task)`` without
    awaiting the task. Returning the task lets callers/tests drain it; the HTTP
    handler ignores the second element.

    Raises ``RunAlreadyActive`` (→ HTTP 409) if a run is already active for this
    (app, user, session) in this process — see ``_ACTIVE_RUNS``, and
    ``PersonReferenceError`` (→ HTTP 400/503) before claiming anything when the
    seeded person reference fails the consent check."""
    key = (app_name, user_id, session_id)
    existing = None
    if app_name in PERSON_REFERENCE_APPS:
        existing = await _get_session_or_none(
            session_service, app_name, user_id, session_id
        )
        await check_person_reference(
            app_name,
            user_id,
            existing.state if existing is not None else None,
            reason="person_reference_invalid",
        )
    _claim_run(key)  # synchronous check+claim, before any further await
    try:
        if existing is None:
            # Re-read under the claim: the session may have appeared meanwhile.
            existing = await _get_session_or_none(
                session_service, app_name, user_id, session_id
            )
        if existing is None:
            await session_service.create_session(
                app_name=app_name, user_id=user_id, session_id=session_id, state={}
            )
        runner = runner_factory(app_name)
        task = asyncio.create_task(
            _drive_run(
                runner,
                session_service,
                app_name,
                user_id,
                session_id,
                build_user_message(message),
            )
        )
    except BaseException:
        _release_claim(key)
        raise
    _register_run_task(key, task)
    return {"runId": session_id, "status": "running"}, task


# Per-concept fields a user may directly edit at the checkpoint-3 review. These
# map straight onto VisualConceptFinal fields the renderer/reviser read.
_EDITABLE_CONCEPT_FIELDS = ("image_generation_prompt", "aspect_ratio", "visual_style")


def merge_visual_concept_edits(
    current: dict | None, edits: list | None
) -> tuple[dict, str]:
    """Merge per-concept direct edits into a ``final_visual_concepts`` envelope.

    Pure (no I/O). ``current`` is the ``{"visual_concepts": [...]}`` envelope from
    session state; ``edits`` is a list of ``{index, image_generation_prompt?,
    aspect_ratio?, visual_style?, revision_note?}``. Direct field edits are applied
    by 0-based ``index`` (out-of-range/invalid indices ignored); ``revision_note``
    values are collected into a single human-readable notes string for the LLM
    reviser (``visual_concept_reviser``). Concepts/fields not named are left
    untouched, the envelope shape is preserved, and the input is not mutated.

    Returns ``(merged_envelope, revision_notes)``.
    """
    envelope = dict(current) if isinstance(current, dict) else {}
    concepts = [
        dict(c) if isinstance(c, dict) else c
        for c in (envelope.get("visual_concepts") or [])
    ]

    notes_lines: list[str] = []
    for edit in edits or []:
        if not isinstance(edit, dict):
            continue
        idx = edit.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool):
            continue
        if idx < 0 or idx >= len(concepts):
            continue
        concept = concepts[idx]
        if not isinstance(concept, dict):
            continue
        for field in _EDITABLE_CONCEPT_FIELDS:
            value = edit.get(field)
            if value is not None:
                concept[field] = value
        note = (edit.get("revision_note") or "").strip()
        if note:
            name = concept.get("concept_name")
            label = f"Concept {idx} ({name})" if name else f"Concept {idx}"
            notes_lines.append(f"{label}: {note}")

    envelope["visual_concepts"] = concepts
    return envelope, "\n".join(notes_lines)


RESEARCH_EDIT_FIELD = "combined_final_cited_report"
RESEARCH_EDIT_MAX_CHARS = 200_000


def merge_research_edit(state: dict | None, edits: list | None) -> dict:
    """Pure: turn a checkpoint-1 report edit into a state delta ({} = no-op).

    Writes the raw report (read by the creative prompts) and re-renders
    ``final_report_with_citations`` (read by the PDF tool) with the same
    citation renderer the composer callback uses. Unchanged, blank, oversized
    or non-string values are ignored."""
    # Lazy import: importing anything under ``creative_agent`` runs its package
    # ``__init__`` (which builds the full agent graph + a genai client), and this
    # module must stay importable without agents/GCP creds (see the router note).
    from creative_agent.citations import render_citations

    state = state if isinstance(state, dict) else {}
    for edit in edits or []:
        if not isinstance(edit, dict) or edit.get("field") != RESEARCH_EDIT_FIELD:
            continue
        value = edit.get("value")
        if not isinstance(value, str) or not value.strip():
            return {}
        if len(value) > RESEARCH_EDIT_MAX_CHARS:
            return {}
        if value.strip() == str(state.get(RESEARCH_EDIT_FIELD) or "").strip():
            return {}
        return {
            RESEARCH_EDIT_FIELD: value,
            "final_report_with_citations": render_citations(
                value, state.get("sources") or {}
            ),
            "research_report_edited": True,
        }
    return {}


BRIEF_EDIT_FIELD = "creative_brief"
BRIEF_EDIT_MAX_CHARS = 50_000


class BriefEditError(ValueError):
    """A checkpoint-1 structured-brief edit that fails validation (→ HTTP 400).

    ``errors`` is a list of ``{"loc": "dotted.field.path", "msg": str}`` the
    frontend can show next to the offending fields."""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__(
            "; ".join(f"{e['loc'] or 'brief'}: {e['msg']}" for e in errors)
        )


def _brief_edit_value(edits: list | None) -> tuple[bool, object]:
    """``(found, value)`` of the first ``creative_brief`` edit in ``edits``."""
    for edit in edits or []:
        if isinstance(edit, dict) and edit.get("field") == BRIEF_EDIT_FIELD:
            return True, edit.get("value")
    return False, None


def validate_brief_edit(value: object) -> dict:
    """Validate an edited brief against ``CreativeBrief``; return it as stored.

    ``value`` is the full brief object (or its JSON string). Raises
    ``BriefEditError`` with per-field errors for malformed JSON, an oversized
    value, any schema violation (e.g. fewer than 3 angles, fit_score outside
    1-5, a missing field) or a blank single-minded proposition (the schema
    allows an empty string; an edited brief may not) and empty or duplicate
    angle ids (downstream copies and concepts reference angles by id)."""
    # Lazy import: see merge_research_edit (the facade builds the agent graph).
    from creative_agent import CreativeBrief

    if isinstance(value, str):
        if len(value) > BRIEF_EDIT_MAX_CHARS:
            raise BriefEditError([{"loc": "", "msg": "The brief is too long."}])
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise BriefEditError(
                [{"loc": "", "msg": "The brief is not valid JSON."}]
            ) from exc
    if not isinstance(value, dict):
        raise BriefEditError([{"loc": "", "msg": "The brief must be an object."}])
    if len(json.dumps(value, default=str)) > BRIEF_EDIT_MAX_CHARS:
        raise BriefEditError([{"loc": "", "msg": "The brief is too long."}])
    try:
        brief = CreativeBrief.model_validate(value)
    except ValidationError as exc:
        raise BriefEditError(
            [
                {"loc": ".".join(str(part) for part in err["loc"]), "msg": err["msg"]}
                for err in exc.errors()
            ]
        ) from exc
    errors: list[dict[str, str]] = []
    if not brief.single_minded_proposition.strip():
        errors.append(
            {"loc": "single_minded_proposition", "msg": "The proposition is required."}
        )
    seen: set[str] = set()
    for i, angle in enumerate(brief.angles):
        angle_id = angle.angle_id.strip()
        if not angle_id:
            errors.append(
                {"loc": f"angles.{i}.angle_id", "msg": "The angle id is required."}
            )
        elif angle_id in seen:
            errors.append(
                {
                    "loc": f"angles.{i}.angle_id",
                    "msg": f"Duplicate angle id {angle_id!r}.",
                }
            )
        seen.add(angle_id)
    if errors:
        raise BriefEditError(errors)
    return brief.model_dump()


def merge_brief_edit(state: dict | None, edits: list | None) -> dict:
    """Pure: turn a checkpoint-1 structured-brief edit into a state delta.

    Validates the edited ``creative_brief`` (``validate_brief_edit``; raises
    ``BriefEditError`` when invalid), then writes the brief, its compact
    Markdown ``creative_brief_md`` (what the creative prompts read, re-rendered
    exactly as the brief gate does) and ``creative_brief_edited: True``, and
    clears ``creative_brief__issues`` (the gate's verdict was on the old brief;
    the user's edit is authoritative). No brief edit, or a brief equal to the
    current one, is a no-op ({})."""
    from creative_agent.brief_check import parse_brief
    from creative_agent.brief_render import render_brief_markdown

    found, value = _brief_edit_value(edits)
    if not found:
        return {}
    brief = validate_brief_edit(value)
    state = state if isinstance(state, dict) else {}
    current = parse_brief(state.get(BRIEF_EDIT_FIELD))
    if current is not None and dict(current) == brief:
        return {}
    return {
        BRIEF_EDIT_FIELD: brief,
        "creative_brief_md": render_brief_markdown(brief, heading=False),
        "creative_brief_edited": True,
        "creative_brief__issues": None,
    }


def validate_resume_edits(function_name: str, edits: list | None) -> None:
    """Reject invalid resume edits BEFORE anything is claimed or written.

    Only checkpoint 1's structured brief is validated strictly (raises
    ``BriefEditError``); every other edit stays best-effort (ignored if bad)."""
    if function_name != "review_research":
        return
    found, value = _brief_edit_value(edits)
    if found:
        validate_brief_edit(value)


async def _apply_visual_concept_edits(
    session_service, app_name, user_id, session_id, edits
) -> None:
    """Merge checkpoint-3 direct edits into session state BEFORE the resumed run.

    The renderer reads ``final_visual_concepts`` from STATE, not from the resume
    ``functionResponse`` (which only reaches the LLM). So direct field edits must
    be written deterministically as a ``state_delta`` event appended before the
    Runner relaunches; any free-text notes land in ``visual_revision_notes`` for
    the ``visual_concept_reviser`` to apply. Best-effort: a missing session is a
    no-op (the resume itself will surface the error)."""
    session = await _get_session_or_none(session_service, app_name, user_id, session_id)
    if session is None:
        return
    merged, notes = merge_visual_concept_edits(
        session.state.get("final_visual_concepts"), edits
    )
    # The pre-checkpoint concept_gate verdict is stale once the user edits:
    # clear it (interactive's visual_concept_reviser re-checks when it runs).
    delta: dict = {
        "final_visual_concepts": merged,
        "final_visual_concepts__issues": None,
    }
    if notes:
        delta["visual_revision_notes"] = notes
    event = Event(
        author=RUNSERVER_AUTHOR,
        invocation_id=RUNSERVER_AUTHOR,
        actions=EventActions(state_delta=delta),
    )
    await session_service.append_event(session, event)


async def _apply_research_edit(
    session_service, app_name, user_id, session_id, edits
) -> None:
    """Write checkpoint-1 edits (research report and/or structured brief) into
    session state BEFORE the resumed run.

    The creative prompts read ``{combined_final_cited_report?}`` /
    ``{creative_brief_md?}`` and the PDF tool reads
    ``final_report_with_citations`` + ``creative_brief`` from STATE, so the edits
    are appended as ONE ``state_delta`` event (see merge_research_edit /
    merge_brief_edit). A missing session or no-op edits append nothing; an
    invalid brief raises ``BriefEditError`` (already caught up front by
    ``validate_resume_edits``)."""
    session = await _get_session_or_none(session_service, app_name, user_id, session_id)
    if session is None:
        return
    delta = {
        **merge_research_edit(session.state, edits),
        **merge_brief_edit(session.state, edits),
    }
    if not delta:
        return
    event = Event(
        author=RUNSERVER_AUTHOR,
        invocation_id=RUNSERVER_AUTHOR,
        actions=EventActions(state_delta=delta),
    )
    await session_service.append_event(session, event)


# Resume ``edits`` appliers keyed by the paused checkpoint's function name.
_EDIT_APPLIERS = {
    "review_visual_concepts": _apply_visual_concept_edits,
    "review_research": _apply_research_edit,
}


async def _reset_status_to_running(
    session_service, app_name, user_id, session_id
) -> None:
    """Append a ``running`` status marker, superseding any terminal marker a prior
    detached segment left behind.

    Each segment writes its OWN terminal marker when its Runner generator exhausts
    — INCLUDING when it exhausts by pausing at a ``LongRunningFunctionTool``
    checkpoint (the pause looks like a normal generator completion). So after a
    resume, the previous (paused) segment's stale ``done`` marker is still the last
    one in the log until the new segment finishes; a poll in that window would read
    ``done`` and the client (``pollRun`` stops on any non-``running`` status) would
    give up before the next checkpoint / final completion. Called synchronously
    (awaited) by ``start_resume`` BEFORE the detached task launches, so the very
    next poll already sees ``running``."""
    session = await _get_session_or_none(session_service, app_name, user_id, session_id)
    if session is None:
        return
    await session_service.append_event(session, build_terminal_event("running"))


async def start_resume(
    *,
    app_name,
    user_id,
    session_id,
    function_call_id,
    function_name,
    response,
    session_service,
    runner_factory,
    function_call_event_id=None,
    edits=None,
) -> tuple[dict, asyncio.Task]:
    """Resume a paused ``LongRunningFunctionTool`` run by driving the Runner with
    a ``functionResponse`` message (matched internally by the tool-call id).

    A resume is ``start_run`` with a ``functionResponse`` instead of text. The
    session already exists (the original run created it), so we do NOT create it
    here — a truly missing session lets the Runner error into an ``error``
    terminal marker, which pollers already handle.

    ``function_call_event_id`` is accepted for API symmetry with the frontend
    (which sends ``functionCallEventId``) but is unused: ``Runner.run_async`` has
    no resume-event-id parameter — the ``functionResponse.id`` alone re-binds the
    paused tool call.

    ``edits`` are routed by checkpoint (``_EDIT_APPLIERS``) and merged
    deterministically into session state before relaunch, since downstream
    agents read state, not the functionResponse: checkpoint-1 report and
    structured-brief edits (_apply_research_edit; an invalid brief raises
    ``BriefEditError`` → HTTP 400 before anything is claimed or written) and checkpoint-3 visual-concept edits
    (_apply_visual_concept_edits). Edits for any other checkpoint are ignored.

    Duplicate guard: if the previous (paused) segment's task is still finishing
    (appending its terminal marker), wait up to
    ``RESUME_PRIOR_SEGMENT_GRACE_SECONDS`` for it; if a run is still active after
    that, raise ``RunAlreadyActive`` (→ HTTP 409) with reason
    ``prior_segment_active`` (this response was NOT applied). A duplicate resume
    for the SAME ``function_call_id`` while that resume is active is rejected at
    once with reason ``resume_in_progress`` (the first one is serving it)."""
    key = (app_name, user_id, session_id)
    validate_resume_edits(function_name, edits)  # 400 before any claim/write
    if _is_live(key) and _ACTIVE_RESUME_CALL_IDS.get(key) == function_call_id:
        raise RunAlreadyActive(key, REASON_RESUME_IN_PROGRESS)
    if app_name in PERSON_REFERENCE_APPS:
        # The consent may have been revoked while the run was paused.
        session = await _get_session_or_none(
            session_service, app_name, user_id, session_id
        )
        await check_person_reference(
            app_name,
            user_id,
            session.state if session is not None else None,
            reason="person_reference_revoked",
        )
    await _await_prior_segment(key)
    _claim_run(key, function_call_id)  # sync check+claim, before any further await
    try:
        runner = runner_factory(app_name)
        new_message = build_resume_message(function_call_id, function_name, response)
        if edits and (applier := _EDIT_APPLIERS.get(function_name)):
            await applier(session_service, app_name, user_id, session_id, edits)
        elif edits:
            logging.warning("resume edits ignored for %s", function_name)
        # Clear the paused segment's terminal 'done' marker before relaunching, so
        # a poll during the resumed segment sees 'running' (see
        # _reset_status_to_running).
        await _reset_status_to_running(session_service, app_name, user_id, session_id)
        task = asyncio.create_task(
            _drive_run(
                runner,
                session_service,
                app_name,
                user_id,
                session_id,
                new_message,
            )
        )
    except BaseException:
        _release_claim(key)
        raise
    _register_run_task(key, task)
    return {"runId": session_id, "status": "running"}, task


# --- HTTP router ------------------------------------------------------------
#
# The router delegates to the tested pure functions above, pulling its deps from
# module-level globals set by ``configure(...)`` (called once by the launcher in
# ``deployment/async_app.py``). Kept in this module — not the launcher — so the
# route table is importable and testable without GCP creds: agents are imported
# lazily by ``get_root_agent`` inside ``runner_factory`` at request time, never
# at import time.

_SESSION_SERVICE = None
_RUNNER_FACTORY = None
_AUTHZ_MODE = AuthzMode.TRUST_CLIENT


def configure(
    *,
    session_service,
    runner_factory,
    # TRUST_CLIENT default is for tests/local; async_app always passes the resolved mode.
    authz_mode: AuthzMode = AuthzMode.TRUST_CLIENT,
) -> None:
    """Bind the shared session service + runner factory used by the routes, and
    the per-user authz mode for the ``POST /runs`` body ``userId`` check (the
    path-scoped poll/resume routes are gated by ``UserAuthzMiddleware``)."""
    global _SESSION_SERVICE, _RUNNER_FACTORY, _AUTHZ_MODE
    _SESSION_SERVICE = session_service
    _RUNNER_FACTORY = runner_factory
    _AUTHZ_MODE = authz_mode


class _StartRunBody(BaseModel):
    userId: str  # noqa: N815 -- camelCase matches the frontend JSON payload
    sessionId: str  # noqa: N815
    message: str


class _ResumeBody(BaseModel):
    functionCallId: str  # noqa: N815 -- camelCase matches the frontend payload
    functionName: str  # noqa: N815
    response: dict
    functionCallEventId: str | None = None  # noqa: N815
    # Optional checkpoint edits, merged into session state before the resumed
    # run (see start_resume / _EDIT_APPLIERS): checkpoint 1 [{field:
    # "combined_final_cited_report" | "creative_brief", value}]; checkpoint 3
    # per-concept [{index, image_generation_prompt?, aspect_ratio?,
    # visual_style?, revision_note?}].
    edits: list[dict] | None = None


router = APIRouter()


_ALREADY_ACTIVE_HINTS = {
    REASON_RUN_ACTIVE: "poll GET /runs/{app}/{user}/{session} for its progress "
    "instead of starting another.",
    REASON_RESUME_IN_PROGRESS: "this resume is already being processed; poll "
    "GET /runs/{app}/{user}/{session} for its progress.",
    REASON_PRIOR_SEGMENT_ACTIVE: "the previous step is still finishing, so this "
    "response was NOT applied; retry the resume shortly.",
}


def _already_active_detail(exc: RunAlreadyActive) -> dict:
    """409 ``detail``: a machine-readable ``reason`` (see ``REASON_*``) plus a
    human-readable ``message``."""
    hint = _ALREADY_ACTIVE_HINTS.get(exc.reason, "")
    return {
        "reason": exc.reason,
        "message": f"Run already active for this session ({exc}); {hint}",
    }


@router.post("/runs/{app_name}")
async def http_start_run(app_name: str, body: _StartRunBody, request: Request) -> dict:
    try:
        user_id = authorize_body_user(_AUTHZ_MODE, trusted_user(request), body.userId)
    except UserAuthzError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    try:
        result, _task = await start_run(
            app_name=app_name,
            user_id=user_id,
            session_id=body.sessionId,
            message=body.message,
            session_service=_SESSION_SERVICE,
            runner_factory=_RUNNER_FACTORY,
        )
    except RunAlreadyActive as exc:
        raise HTTPException(
            status_code=409, detail=_already_active_detail(exc)
        ) from exc
    except PersonReferenceError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={"reason": exc.reason, "message": str(exc)},
        ) from exc
    return result


@router.get("/runs/{app_name}/{user_id}/{session_id}")
async def http_get_run_status(
    app_name: str, user_id: str, session_id: str, since: int = Query(0)
) -> dict:
    # A missing session returns 200 with ``status="not_found"`` (not a 404): the
    # frontend poll loop decides whether to keep waiting (the session may not be
    # visible yet) or surface an error, and it avoids noisy proxy 404s.
    return await get_run_status(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
        since=since,
        session_service=_SESSION_SERVICE,
    )


@router.post("/runs/{app_name}/{user_id}/{session_id}/resume")
async def http_start_resume(
    app_name: str, user_id: str, session_id: str, body: _ResumeBody
) -> dict:
    try:
        result, _task = await start_resume(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            function_call_id=body.functionCallId,
            function_name=body.functionName,
            response=body.response,
            session_service=_SESSION_SERVICE,
            runner_factory=_RUNNER_FACTORY,
            function_call_event_id=body.functionCallEventId,
            edits=body.edits,
        )
    except BriefEditError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "invalid_brief",
                "message": f"The edited brief is invalid: {exc}",
                "errors": exc.errors,
            },
        ) from exc
    except RunAlreadyActive as exc:
        raise HTTPException(
            status_code=409, detail=_already_active_detail(exc)
        ) from exc
    except PersonReferenceError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={"reason": exc.reason, "message": str(exc)},
        ) from exc
    return result
