"""Tests for the runserver async-run pure helpers (offline, no creds).

Only `runserver.async_runs` is imported at module top. Any test that touches an
agent package builds a module-level genai client (needs GCP ADC), so it is gated
behind a creds check — mirroring the lazy-import convention in
`tests/test_pipeline_structure.py` (agent imports happen inside the test body).
"""

from __future__ import annotations

import asyncio

import pytest
from google.adk.events import Event, EventActions
from google.adk.sessions import InMemorySessionService
from google.genai import types

from runserver.async_runs import (
    RUN_ERROR_KEY,
    RUN_STATUS_KEY,
    build_resume_message,
    build_terminal_event,
    build_user_message,
    events_since,
    get_root_agent,
    get_run_status,
    router,
    start_resume,
    start_run,
)


def _have_adc() -> bool:
    """True when Application Default Credentials resolve (agent imports build a
    module-level genai client that requires ADC)."""
    try:
        import google.auth

        google.auth.default()
        return True
    except Exception:
        return False


def test_build_user_message_text():
    msg = build_user_message("hi")
    assert isinstance(msg, types.Content)
    assert msg.role == "user"
    assert msg.parts[0].text == "hi"


def test_build_resume_message():
    msg = build_resume_message("call-1", "review_research", {"status": "ok"})
    assert isinstance(msg, types.Content)
    assert msg.role == "user"
    fr = msg.parts[0].function_response
    assert fr.id == "call-1"
    assert fr.name == "review_research"
    assert fr.response == {"status": "ok"}


def test_terminal_marker_event_done():
    ev = build_terminal_event("done")
    assert isinstance(ev, Event)
    assert ev.actions.state_delta == {"__run_status": "done"}
    assert ev.author == "__runserver__"
    # No content parts so the marker never renders in the timeline.
    assert not ev.content
    # invocation_id MUST be non-empty — VertexAiSessionService.append_event
    # rejects an event whose invocation_id is unset (400 INVALID_ARGUMENT).
    assert ev.invocation_id


def test_terminal_marker_event_error():
    ev = build_terminal_event("error", "boom")
    assert ev.actions.state_delta == {"__run_status": "error", "__run_error": "boom"}
    assert ev.author == "__runserver__"
    assert not ev.content
    assert ev.invocation_id


def test_terminal_marker_threads_invocation_id():
    ev = build_terminal_event("done", invocation_id="inv-42")
    assert ev.invocation_id == "inv-42"


def test_events_since_slices_by_index():
    a, b, c = "a", "b", "c"
    assert events_since([a, b, c], 1) == [b, c]
    assert events_since([a, b, c], 0) == [a, b, c]
    assert events_since([a, b, c], 5) == []
    assert events_since([a, b, c], -2) == [a, b, c]


@pytest.mark.skipif(
    not _have_adc(),
    reason="agent imports build a module-level genai client requiring GCP ADC",
)
def test_get_root_agent_maps_three_agents():
    from google.adk.apps import App

    for app_name in ("creative_agent", "trend_scout", "interactive_creative"):
        assert get_root_agent(app_name) is not None

    # The interactive agents (LongRunningFunctionTool checkpoints) return a
    # resumable App so the Runner can pause/resume; creative_agent has no
    # checkpoints and stays a bare Agent.
    assert isinstance(get_root_agent("trend_scout"), App)
    assert isinstance(get_root_agent("interactive_creative"), App)
    assert not isinstance(get_root_agent("creative_agent"), App)

    with pytest.raises(KeyError):
        get_root_agent("nope")


# --- Task 2: detached kick-off ------------------------------------------------
#
# Offline: an InMemorySessionService plus a fake Runner double. A real Runner
# appends each final event to the session service as it runs, so the fake
# emulates that (closure over the shared service) — this makes the "events were
# appended + terminal marker is last" assertions meaningful. Coroutines are
# driven with asyncio.run (no pytest-asyncio in this project — see
# tests/test_retry_agent.py).


class _FakeRunner:
    """Async-generator Runner double that appends each event to the shared
    session service before yielding (mirroring the real Runner), optionally
    raising after a given event index."""

    def __init__(
        self,
        session_service,
        app_name,
        user_id,
        session_id,
        events,
        *,
        raise_after=None,
    ):
        self._svc = session_service
        self._app_name = app_name
        self._user_id = user_id
        self._session_id = session_id
        self._events = events
        self._raise_after = raise_after

    async def run_async(self, *, user_id, session_id, new_message, **kwargs):
        self.received_message = new_message  # captured for resume assertions
        for i, ev in enumerate(self._events):
            if self._raise_after is not None and i == self._raise_after:
                raise RuntimeError("boom")
            session = await self._svc.get_session(
                app_name=self._app_name,
                user_id=self._user_id,
                session_id=self._session_id,
            )
            await self._svc.append_event(session, ev)
            yield ev


def _agent_event(text: str) -> Event:
    return Event(
        author="creative_agent",
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )


def test_kickoff_starts_detached_task_and_returns_runid():
    async def _go():
        svc = InMemorySessionService()
        events = [_agent_event("one"), _agent_event("two")]
        fake = _FakeRunner(svc, "creative_agent", "u", "s", events)
        result, task = await start_run(
            app_name="creative_agent",
            user_id="u",
            session_id="s",
            message="hello",
            session_service=svc,
            runner_factory=lambda app_name: fake,
        )
        # Returns immediately, without awaiting the task.
        assert result == {"runId": "s", "status": "running"}
        await task  # drain
        session = await svc.get_session(
            app_name="creative_agent", user_id="u", session_id="s"
        )
        return session

    session = asyncio.run(_go())
    texts = [e.content.parts[0].text for e in session.events if e.content]
    assert texts == ["one", "two"]
    last = session.events[-1]
    assert last.actions.state_delta == {RUN_STATUS_KEY: "done"}


def test_kickoff_records_error_marker_on_exception():
    async def _go():
        svc = InMemorySessionService()
        events = [_agent_event("one"), _agent_event("two")]
        fake = _FakeRunner(svc, "creative_agent", "u", "s", events, raise_after=1)
        result, task = await start_run(
            app_name="creative_agent",
            user_id="u",
            session_id="s",
            message="hello",
            session_service=svc,
            runner_factory=lambda app_name: fake,
        )
        assert result == {"runId": "s", "status": "running"}
        # Draining must NOT raise — _drive_run swallows the exception.
        await task
        session = await svc.get_session(
            app_name="creative_agent", user_id="u", session_id="s"
        )
        return session

    session = asyncio.run(_go())
    delta = session.events[-1].actions.state_delta
    assert delta[RUN_STATUS_KEY] == "error"
    assert RUN_ERROR_KEY in delta
    assert "boom" in delta[RUN_ERROR_KEY]


def test_kickoff_creates_session_if_missing():
    async def _go():
        svc = InMemorySessionService()
        fake = _FakeRunner(svc, "trend_scout", "u", "s", [_agent_event("x")])
        _result, task = await start_run(
            app_name="trend_scout",
            user_id="u",
            session_id="s",
            message="hi",
            session_service=svc,
            runner_factory=lambda app_name: fake,
        )
        await task
        return await svc.get_session(
            app_name="trend_scout", user_id="u", session_id="s"
        )

    session = asyncio.run(_go())
    assert session is not None


def test_kickoff_uses_existing_session():
    async def _go():
        svc = InMemorySessionService()
        await svc.create_session(
            app_name="creative_agent", user_id="u", session_id="s", state={"brand": "X"}
        )
        fake = _FakeRunner(svc, "creative_agent", "u", "s", [_agent_event("x")])
        _result, task = await start_run(
            app_name="creative_agent",
            user_id="u",
            session_id="s",
            message="hi",
            session_service=svc,
            runner_factory=lambda app_name: fake,
        )
        await task
        return await svc.get_session(
            app_name="creative_agent", user_id="u", session_id="s"
        )

    session = asyncio.run(_go())
    # Pre-existing state survives — the run appended events, didn't recreate/wipe.
    assert session.state.get("brand") == "X"


# --- Task 3: poll (status + events-since + state) -----------------------------
#
# Offline: seed an InMemorySessionService session by constructing Events and
# appending them, then drive get_run_status with asyncio.run.


async def _seed_session(svc, *, state=None, events=()):
    await svc.create_session(
        app_name="creative_agent", user_id="u", session_id="s", state=state or {}
    )
    for ev in events:
        session = await svc.get_session(
            app_name="creative_agent", user_id="u", session_id="s"
        )
        await svc.append_event(session, ev)


def _poll(svc, *, since=0):
    return asyncio.run(
        get_run_status(
            app_name="creative_agent",
            user_id="u",
            session_id="s",
            since=since,
            session_service=svc,
        )
    )


def test_poll_returns_events_since_cursor():
    svc = InMemorySessionService()
    asyncio.run(
        _seed_session(
            svc,
            events=[_agent_event("one"), _agent_event("two"), _agent_event("three")],
        )
    )
    result = _poll(svc, since=1)
    assert len(result["events"]) == 2
    assert result["nextCursor"] == 3


def test_poll_status_running_when_no_marker():
    svc = InMemorySessionService()
    asyncio.run(_seed_session(svc, events=[_agent_event("one"), _agent_event("two")]))
    result = _poll(svc)
    assert result["status"] == "running"
    assert result["error"] is None


def test_poll_status_done_on_marker():
    svc = InMemorySessionService()
    asyncio.run(
        _seed_session(svc, events=[_agent_event("one"), build_terminal_event("done")])
    )
    result = _poll(svc)
    assert result["status"] == "done"


def test_poll_status_error_on_marker():
    svc = InMemorySessionService()
    asyncio.run(
        _seed_session(
            svc, events=[_agent_event("one"), build_terminal_event("error", "boom")]
        )
    )
    result = _poll(svc)
    assert result["status"] == "error"
    assert "boom" in result["error"]


def test_poll_status_error_on_error_event():
    svc = InMemorySessionService()
    err_event = Event(
        author="creative_agent", error_code="RESOURCE_EXHAUSTED", error_message="429"
    )
    asyncio.run(_seed_session(svc, events=[_agent_event("one"), err_event]))
    result = _poll(svc)
    assert result["status"] == "error"


def test_poll_state_is_session_state():
    svc = InMemorySessionService()
    asyncio.run(_seed_session(svc, state={"brand": "X"}, events=[_agent_event("one")]))
    result = _poll(svc)
    assert result["state"]["brand"] == "X"


def test_poll_event_serialization_is_camelcase():
    svc = InMemorySessionService()
    ev = Event(
        author="a",
        invocation_id="inv1",
        actions=EventActions(state_delta={"k": "v"}),
        long_running_tool_ids={"t1"},
    )
    asyncio.run(_seed_session(svc, events=[ev]))
    serialized = _poll(svc)["events"][0]
    assert "invocationId" in serialized
    assert "invocation_id" not in serialized
    assert "stateDelta" in serialized["actions"]
    assert "state_delta" not in serialized["actions"]
    assert isinstance(serialized["longRunningToolIds"], list)


def test_poll_not_found():
    svc = InMemorySessionService()
    result = _poll(svc)
    assert result["status"] == "not_found"
    assert result["events"] == []


class _RaisingSessionService:
    """A session service whose get_session raises — mirrors VertexAiSessionService,
    which raises 400/404 for a missing/unknown session instead of returning None
    (unlike the InMemory service ADK's own type hint promises)."""

    async def get_session(self, **kwargs):
        raise RuntimeError("400 INVALID_ARGUMENT")


def test_poll_not_found_when_get_session_raises():
    # A poll must degrade to not_found (which pollRun treats as transient) rather
    # than 500 when the backend raises on an unknown session.
    result = _poll(_RaisingSessionService())
    assert result["status"] == "not_found"
    assert result["events"] == []


# --- Task 4: resume a paused (LongRunningFunctionTool) run --------------------
#
# A resume is start_run with a functionResponse message instead of text. The
# session already exists (created by the original run), so start_resume does NOT
# create it. Reuses _drive_run, so the terminal marker + error swallowing come
# for free.


def test_resume_builds_function_response_message_and_drives_run():
    async def _go():
        svc = InMemorySessionService()
        await svc.create_session(
            app_name="interactive_creative", user_id="u", session_id="s", state={}
        )
        fake = _FakeRunner(
            svc, "interactive_creative", "u", "s", [_agent_event("resumed")]
        )
        result, task = await start_resume(
            app_name="interactive_creative",
            user_id="u",
            session_id="s",
            function_call_id="call-9",
            function_name="review_research",
            response={"approved": True},
            session_service=svc,
            runner_factory=lambda a: fake,
            function_call_event_id="evt-3",
        )
        assert result == {"runId": "s", "status": "running"}
        await task  # drain
        session = await svc.get_session(
            app_name="interactive_creative", user_id="u", session_id="s"
        )
        return session, fake

    session, fake = asyncio.run(_go())
    fr = fake.received_message.parts[0].function_response
    assert fr.id == "call-9"
    assert fr.name == "review_research"
    assert fr.response == {"approved": True}
    assert session.events[-1].actions.state_delta == {RUN_STATUS_KEY: "done"}


# --- Task 5: router registration (creds-light — importing the router must NOT
# import agents; get_root_agent is only called inside runner_factory at request
# time, so the route table is inspectable without GCP ADC). -------------------


def test_router_registers_expected_paths():
    registered = {
        (route.path, method) for route in router.routes for method in route.methods
    }
    assert ("/runs/{app_name}", "POST") in registered
    assert ("/runs/{app_name}/{user_id}/{session_id}", "GET") in registered
    assert ("/runs/{app_name}/{user_id}/{session_id}/resume", "POST") in registered


def test_resume_records_error_marker_on_exception():
    async def _go():
        svc = InMemorySessionService()
        await svc.create_session(
            app_name="interactive_creative", user_id="u", session_id="s", state={}
        )
        fake = _FakeRunner(
            svc,
            "interactive_creative",
            "u",
            "s",
            [_agent_event("resumed")],
            raise_after=0,
        )
        result, task = await start_resume(
            app_name="interactive_creative",
            user_id="u",
            session_id="s",
            function_call_id="call-9",
            function_name="review_research",
            response={"approved": True},
            session_service=svc,
            runner_factory=lambda a: fake,
        )
        assert result == {"runId": "s", "status": "running"}
        await task  # must not raise
        return await svc.get_session(
            app_name="interactive_creative", user_id="u", session_id="s"
        )

    session = asyncio.run(_go())
    delta = session.events[-1].actions.state_delta
    assert delta[RUN_STATUS_KEY] == "error"
    assert RUN_ERROR_KEY in delta
    assert "boom" in delta[RUN_ERROR_KEY]


def test_resume_resets_status_to_running_before_segment_completes():
    """Regression (found by live interactive smoke): each detached segment writes
    its OWN terminal ``done`` marker when its Runner generator exhausts — INCLUDING
    when it exhausts by pausing at a ``LongRunningFunctionTool`` checkpoint. So after
    a resume, a poll during the *new* segment would read the previous segment's stale
    ``done`` and the client (pollRun stops on any non-``running`` status) would give
    up before the next checkpoint / final completion. ``start_resume`` must reset the
    status to ``running`` synchronously, before the detached task launches, so the
    very next poll already sees ``running``."""

    async def _go():
        svc = InMemorySessionService()
        await svc.create_session(
            app_name="interactive_creative", user_id="u", session_id="s", state={}
        )
        # Simulate a previous paused segment: a terminal 'done' marker is already
        # in the log (this is exactly what a checkpoint pause leaves behind).
        prior = await svc.get_session(
            app_name="interactive_creative", user_id="u", session_id="s"
        )
        await svc.append_event(prior, build_terminal_event("done"))

        gate = asyncio.Event()

        class _BlockingRunner:
            """Runner double that blocks until released, so we can observe the
            run's status WHILE the resumed segment is still in flight."""

            async def run_async(self, *, user_id, session_id, new_message, **kwargs):
                await gate.wait()
                s = await svc.get_session(
                    app_name="interactive_creative", user_id="u", session_id="s"
                )
                ev = _agent_event("resumed")
                await svc.append_event(s, ev)
                yield ev

        result, task = await start_resume(
            app_name="interactive_creative",
            user_id="u",
            session_id="s",
            function_call_id="call-1",
            function_name="review_ad_copies",
            response={"status": "approved"},
            session_service=svc,
            runner_factory=lambda a: _BlockingRunner(),
        )
        assert result["status"] == "running"
        # Runner is blocked → resumed segment has NOT finished. The poll must not
        # report the previous segment's stale 'done'.
        mid = await get_run_status(
            app_name="interactive_creative",
            user_id="u",
            session_id="s",
            since=0,
            session_service=svc,
        )
        gate.set()
        await task
        final = await get_run_status(
            app_name="interactive_creative",
            user_id="u",
            session_id="s",
            since=0,
            session_service=svc,
        )
        return mid, final

    mid, final = asyncio.run(_go())
    assert mid["status"] == "running"
    assert final["status"] == "done"
