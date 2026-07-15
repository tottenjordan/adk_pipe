"""Async-job run helpers (pure) for the runserver package."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query
from google.adk.events import Event, EventActions
from google.genai import types
from pydantic import BaseModel

RUNSERVER_AUTHOR = "__runserver__"
RUN_STATUS_KEY = "__run_status"
RUN_ERROR_KEY = "__run_error"


def get_root_agent(app_name: str):
    """Map an app_name to its root Agent (lazy import — builds a genai client)."""
    from creative_agent.agent import root_agent as creative
    from interactive_creative.agent import root_agent as interactive
    from trend_scout.agent import root_agent as scout

    agents = {
        "creative_agent": creative,
        "trend_scout": scout,
        "interactive_creative": interactive,
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
    absent, so the caller/router can map it to a 404 while staying testable."""
    session = await session_service.get_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    )
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


async def _drive_run(
    runner, session_service, app_name, user_id, session_id, new_message
) -> None:
    """Drive a Runner to completion detached from any request, then append a
    terminal status marker to the session. Never re-raises — the terminal
    ``error`` marker IS the failure contract for pollers."""
    # Reuse the run's own invocation id on the terminal marker (Vertex requires a
    # non-empty invocation_id); fall back to the marker author if the run emitted
    # no events (e.g. an immediate error).
    invocation_id = RUNSERVER_AUTHOR
    try:
        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=new_message
        ):
            # Runner persists final events to the session service itself.
            if getattr(event, "invocation_id", None):
                invocation_id = event.invocation_id
        session = await session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
        await session_service.append_event(
            session, build_terminal_event("done", invocation_id=invocation_id)
        )
    except Exception as exc:  # noqa: BLE001 — terminal marker is the contract; log+persist, never raise
        logging.exception("detached run failed app=%s session=%s", app_name, session_id)
        session = await session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
        await session_service.append_event(
            session,
            build_terminal_event("error", str(exc), invocation_id=invocation_id),
        )


async def start_run(
    *, app_name, user_id, session_id, message, session_service, runner_factory
) -> tuple[dict, asyncio.Task]:
    """Ensure the session exists, spawn a detached task that drives the run to
    completion, and return ``({"runId", "status": "running"}, task)`` without
    awaiting the task. Returning the task lets callers/tests drain it; the HTTP
    handler ignores the second element."""
    existing = await session_service.get_session(
        app_name=app_name, user_id=user_id, session_id=session_id
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
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return {"runId": session_id, "status": "running"}, task


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
    session = await session_service.get_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    )
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
    paused tool call."""
    runner = runner_factory(app_name)
    new_message = build_resume_message(function_call_id, function_name, response)
    # Clear the paused segment's terminal 'done' marker before relaunching, so a
    # poll during the resumed segment sees 'running' (see _reset_status_to_running).
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
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
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


def configure(*, session_service, runner_factory) -> None:
    """Bind the shared session service + runner factory used by the routes."""
    global _SESSION_SERVICE, _RUNNER_FACTORY
    _SESSION_SERVICE = session_service
    _RUNNER_FACTORY = runner_factory


class _StartRunBody(BaseModel):
    userId: str  # noqa: N815 -- camelCase matches the frontend JSON payload
    sessionId: str  # noqa: N815
    message: str


class _ResumeBody(BaseModel):
    functionCallId: str  # noqa: N815 -- camelCase matches the frontend payload
    functionName: str  # noqa: N815
    response: dict
    functionCallEventId: str | None = None  # noqa: N815


router = APIRouter()


@router.post("/runs/{app_name}")
async def http_start_run(app_name: str, body: _StartRunBody) -> dict:
    result, _task = await start_run(
        app_name=app_name,
        user_id=body.userId,
        session_id=body.sessionId,
        message=body.message,
        session_service=_SESSION_SERVICE,
        runner_factory=_RUNNER_FACTORY,
    )
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
    )
    return result
