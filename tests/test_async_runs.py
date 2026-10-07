"""Tests for the runserver async-run pure helpers (offline, no creds).

Only `runserver.async_runs` is imported at module top. Tests that touch an agent
package import it inside the test body, mirroring the lazy-import convention in
`tests/test_pipeline_structure.py`. Agent imports need no GCP credentials (only a
`GOOGLE_CLOUD_PROJECT` value), so no test here is gated on ADC.
"""

from __future__ import annotations

import asyncio

import pytest
from google.adk.errors.session_not_found_error import SessionNotFoundError
from google.adk.events import Event, EventActions
from google.adk.sessions import InMemorySessionService
from google.genai import types

from runserver import async_runs
from runserver.async_runs import (
    RESEARCH_EDIT_MAX_CHARS,
    RUN_ERROR_KEY,
    RUN_STATUS_KEY,
    RUNSERVER_AUTHOR,
    build_resume_message,
    build_terminal_event,
    build_user_message,
    events_since,
    get_root_agent,
    get_run_status,
    merge_research_edit,
    merge_visual_concept_edits,
    router,
    should_auto_continue,
    start_resume,
    start_run,
)


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


def test_get_root_agent_maps_three_agents():
    from google.adk.apps import App

    for app_name in ("creative_agent", "trend_scout", "interactive_creative"):
        assert get_root_agent(app_name) is not None

    # Every agent returns its App (so App-level plugins reach the Runner). The
    # interactive agents (LongRunningFunctionTool checkpoints) are resumable;
    # creative_agent has no checkpoints, so its App is not.
    for app_name in ("creative_agent", "trend_scout", "interactive_creative"):
        assert isinstance(get_root_agent(app_name), App)
    assert get_root_agent("trend_scout").resumability_config.is_resumable is True
    assert (
        get_root_agent("interactive_creative").resumability_config.is_resumable is True
    )
    creative_rc = get_root_agent("creative_agent").resumability_config
    assert creative_rc is None or creative_rc.is_resumable is False

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


class _NotFoundRaisingInMemory(InMemorySessionService):
    """InMemory service whose ``get_session`` raises ``SessionNotFoundError`` for a
    missing session (the ADK 2.9 changelog contract) instead of returning None."""

    async def get_session(self, **kwargs):
        session = await super().get_session(**kwargs)
        if session is None:
            raise SessionNotFoundError("not found")
        return session


def test_kickoff_creates_session_when_get_session_raises_not_found():
    async def _go():
        svc = _NotFoundRaisingInMemory()
        fake = _FakeRunner(svc, "creative_agent", "u", "s", [_agent_event("one")])
        _result, task = await start_run(
            app_name="creative_agent",
            user_id="u",
            session_id="s",
            message="hello",
            session_service=svc,
            runner_factory=lambda app_name: fake,
        )
        await task  # drain
        return await svc.get_session(
            app_name="creative_agent", user_id="u", session_id="s"
        )

    session = asyncio.run(_go())
    assert session.events[-1].actions.state_delta == {RUN_STATUS_KEY: "done"}


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


class _CountingNotFoundService(_NotFoundRaisingInMemory):
    """Raising-on-missing stub that also counts get_session / append_event calls."""

    def __init__(self):
        super().__init__()
        self.get_calls = 0
        self.append_calls = 0

    async def get_session(self, **kwargs):
        self.get_calls += 1
        return await super().get_session(**kwargs)

    async def append_event(self, session, event):
        self.append_calls += 1
        return await super().append_event(session, event)


def test_append_terminal_safe_gives_up_without_retry_when_session_missing(caplog):
    svc = _CountingNotFoundService()
    with caplog.at_level("ERROR"):
        asyncio.run(
            async_runs._append_terminal_safe(
                svc, "creative_agent", "u", "missing", build_terminal_event("done")
            )
        )
    assert svc.get_calls == 1  # missing session → no bounded retry
    assert svc.append_calls == 0
    assert "session missing" in caplog.text


def test_reset_status_to_running_noop_when_session_missing():
    svc = _CountingNotFoundService()
    asyncio.run(
        async_runs._reset_status_to_running(svc, "interactive_creative", "u", "missing")
    )
    assert svc.get_calls == 1
    assert svc.append_calls == 0


def test_apply_visual_concept_edits_noop_when_session_missing():
    svc = _CountingNotFoundService()
    asyncio.run(
        async_runs._apply_visual_concept_edits(
            svc,
            "interactive_creative",
            "u",
            "missing",
            [{"index": 0, "revision_note": "warmer palette"}],
        )
    )
    assert svc.get_calls == 1
    assert svc.append_calls == 0


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


class _TerminalMarkerFailingService:
    """Wraps InMemorySessionService but raises when appending a terminal
    (runserver) marker — exercising _drive_run's guarantee that a failing marker
    write never escapes the detached task (which would leave the run hung
    ``running`` forever). Normal agent events pass through to the real service."""

    def __init__(self):
        self._inner = InMemorySessionService()

    async def create_session(self, **kwargs):
        return await self._inner.create_session(**kwargs)

    async def get_session(self, **kwargs):
        return await self._inner.get_session(**kwargs)

    async def append_event(self, session, event):
        if event.author == RUNSERVER_AUTHOR:
            raise RuntimeError("marker append boom")
        return await self._inner.append_event(session, event)


def test_kickoff_does_not_raise_when_terminal_marker_append_fails():
    """If the terminal-marker append itself fails (transient session service),
    the detached task must still complete cleanly — never re-raise — so the run
    doesn't hang ``running`` forever with an unretrieved task exception."""

    async def _go():
        svc = _TerminalMarkerFailingService()
        fake = _FakeRunner(svc, "creative_agent", "u", "s", [_agent_event("one")])
        _result, task = await start_run(
            app_name="creative_agent",
            user_id="u",
            session_id="s",
            message="hi",
            session_service=svc,
            runner_factory=lambda app_name: fake,
        )
        await task  # must NOT raise even though every marker write fails
        return await svc.get_session(
            app_name="creative_agent", user_id="u", session_id="s"
        )

    session = asyncio.run(_go())
    # The run's own event survives; the marker never landed (its write failed).
    texts = [e.content.parts[0].text for e in session.events if e.content]
    assert texts == ["one"]
    assert not any(
        (getattr(getattr(e, "actions", None), "state_delta", None) or {}).get(
            RUN_STATUS_KEY
        )
        for e in session.events
    )


class _SlowRunner:
    """Runner double whose run_async blocks past the injected deadline, to trip
    _drive_run's asyncio.timeout guard."""

    async def run_async(self, *, user_id, session_id, new_message, **kwargs):
        await asyncio.sleep(10)
        yield _agent_event("never")  # unreachable — cancelled by the timeout


def test_kickoff_times_out_and_writes_error_marker(monkeypatch):
    """A wedged run must be bounded by RUN_MAX_SECONDS and resolve to a terminal
    ``error`` marker (timeout), not hang ``running`` forever."""
    monkeypatch.setattr(async_runs, "RUN_MAX_SECONDS", 0.05)

    async def _go():
        svc = InMemorySessionService()
        _result, task = await start_run(
            app_name="creative_agent",
            user_id="u",
            session_id="s",
            message="hi",
            session_service=svc,
            runner_factory=lambda app_name: _SlowRunner(),
        )
        await task  # must not raise
        return await svc.get_session(
            app_name="creative_agent", user_id="u", session_id="s"
        )

    session = asyncio.run(_go())
    delta = session.events[-1].actions.state_delta
    assert delta[RUN_STATUS_KEY] == "error"
    assert "timeout" in delta[RUN_ERROR_KEY].lower()


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


# --- merge_visual_concept_edits (pure, checkpoint-3 direct edits) ------------


def _envelope():
    return {
        "visual_concepts": [
            {
                "concept_name": "c0",
                "image_generation_prompt": "p0",
                "aspect_ratio": "9:16",
                "visual_style": "photoreal",
            },
            {
                "concept_name": "c1",
                "image_generation_prompt": "p1",
                "aspect_ratio": "1:1",
                "visual_style": "cartoon",
            },
        ]
    }


def test_merge_applies_direct_field_edits_by_index():
    merged, notes = merge_visual_concept_edits(
        _envelope(),
        [{"index": 1, "image_generation_prompt": "NEW", "aspect_ratio": "16:9"}],
    )
    concepts = merged["visual_concepts"]
    # Edited concept updated; other fields on it untouched.
    assert concepts[1]["image_generation_prompt"] == "NEW"
    assert concepts[1]["aspect_ratio"] == "16:9"
    assert concepts[1]["visual_style"] == "cartoon"
    # Unmentioned concept fully untouched.
    assert concepts[0] == _envelope()["visual_concepts"][0]
    assert notes == ""


def test_merge_preserves_envelope_and_does_not_mutate_input():
    original = _envelope()
    merged, _ = merge_visual_concept_edits(
        original, [{"index": 0, "visual_style": "anime"}]
    )
    assert set(merged.keys()) == {"visual_concepts"}
    assert len(merged["visual_concepts"]) == 2
    # Input envelope not mutated in place.
    assert original["visual_concepts"][0]["visual_style"] == "photoreal"


def test_merge_collects_revision_notes_with_labels():
    _merged, notes = merge_visual_concept_edits(
        _envelope(),
        [
            {"index": 0, "revision_note": "make it brighter"},
            {"index": 1, "image_generation_prompt": "x", "revision_note": "add a dog"},
        ],
    )
    assert "Concept 0 (c0): make it brighter" in notes
    assert "Concept 1 (c1): add a dog" in notes


def test_merge_ignores_out_of_range_and_invalid_indices():
    merged, notes = merge_visual_concept_edits(
        _envelope(),
        [
            {"index": 9, "image_generation_prompt": "ignored"},
            {"index": -1, "image_generation_prompt": "ignored"},
            {"image_generation_prompt": "no index"},
            "not a dict",
        ],
    )
    assert merged["visual_concepts"] == _envelope()["visual_concepts"]
    assert notes == ""


def test_merge_handles_empty_and_missing_inputs():
    # No edits → concepts unchanged, no notes.
    merged, notes = merge_visual_concept_edits(_envelope(), [])
    assert merged["visual_concepts"] == _envelope()["visual_concepts"]
    assert notes == ""
    # Missing/None current → empty envelope, no crash.
    merged2, notes2 = merge_visual_concept_edits(None, [{"index": 0}])
    assert merged2 == {"visual_concepts": []}
    assert notes2 == ""


# --- merge_research_edit (pure, checkpoint-1 report edit) -------------------

SRC = {"src-1": {"title": "T", "url": "u"}}


def test_research_edit_writes_raw_and_rendered_report():
    delta = merge_research_edit(
        {"combined_final_cited_report": "old", "sources": SRC},
        [
            {
                "field": "combined_final_cited_report",
                "value": 'new <cite source="src-1"/>',
            }
        ],
    )
    # render_citations prefixes each link with a space (existing composer
    # behaviour), hence the double space.
    assert delta == {
        "combined_final_cited_report": 'new <cite source="src-1"/>',
        "final_report_with_citations": "new  [T](u)",
        "research_report_edited": True,
    }


def test_research_edit_ignores_unchanged_blank_wrong_field_and_bad_types():
    st = {"combined_final_cited_report": "same", "sources": {}}
    for edits in (
        [{"field": "combined_final_cited_report", "value": " same "}],
        [{"field": "combined_final_cited_report", "value": "   "}],
        [{"field": "other", "value": "x"}],
        [{"field": "combined_final_cited_report", "value": 5}],
        None,
    ):
        assert merge_research_edit(st, edits) == {}


def test_research_edit_rejects_oversized_value():
    st = {"combined_final_cited_report": "a", "sources": {}}
    edits = [
        {
            "field": "combined_final_cited_report",
            "value": "x" * (RESEARCH_EDIT_MAX_CHARS + 1),
        }
    ]
    assert merge_research_edit(st, edits) == {}


def test_resume_with_edits_appends_state_delta_before_relaunch():
    """Direct field edits + notes are merged into final_visual_concepts (and
    visual_revision_notes) via a state_delta event appended BEFORE the resumed
    segment runs, so the renderer/reviser read the user's edits."""

    async def _go():
        svc = InMemorySessionService()
        await svc.create_session(
            app_name="interactive_creative",
            user_id="u",
            session_id="s",
            state={
                "final_visual_concepts": _envelope(),
                "final_visual_concepts__issues": ["Concept 1: stale"],
            },
        )
        fake = _FakeRunner(
            svc, "interactive_creative", "u", "s", [_agent_event("resumed")]
        )
        _result, task = await start_resume(
            app_name="interactive_creative",
            user_id="u",
            session_id="s",
            function_call_id="call-9",
            function_name="review_visual_concepts",
            response={"approved": True},
            session_service=svc,
            runner_factory=lambda a: fake,
            edits=[
                {"index": 0, "image_generation_prompt": "EDITED"},
                {"index": 1, "revision_note": "more neon"},
            ],
        )
        await task
        return await svc.get_session(
            app_name="interactive_creative", user_id="u", session_id="s"
        )

    session = asyncio.run(_go())
    state = session.state
    assert (
        state["final_visual_concepts"]["visual_concepts"][0]["image_generation_prompt"]
        == "EDITED"
    )
    assert "Concept 1 (c1): more neon" in state["visual_revision_notes"]
    # The pre-checkpoint gate verdict is stale once the user edits: cleared
    # (the reviser's recheck re-evaluates when it runs).
    assert state["final_visual_concepts__issues"] is None


def _resume_with_edits(function_name, state, edits):
    """Drive start_resume with ``edits`` against an in-memory session and return
    the final session."""

    async def _go():
        svc = InMemorySessionService()
        await svc.create_session(
            app_name="interactive_creative", user_id="u", session_id="s", state=state
        )
        fake = _FakeRunner(
            svc, "interactive_creative", "u", "s", [_agent_event("resumed")]
        )
        _result, task = await start_resume(
            app_name="interactive_creative",
            user_id="u",
            session_id="s",
            function_call_id="call-1",
            function_name=function_name,
            response={"status": "approved"},
            session_service=svc,
            runner_factory=lambda a: fake,
            edits=edits,
        )
        await task
        return await svc.get_session(
            app_name="interactive_creative", user_id="u", session_id="s"
        )

    return asyncio.run(_go())


def _runserver_state_events(session):
    return [
        (i, ev)
        for i, ev in enumerate(session.events)
        if ev.author == RUNSERVER_AUTHOR
        and ev.actions
        and ev.actions.state_delta
        and RUN_STATUS_KEY not in ev.actions.state_delta
    ]


def test_resume_research_edit_appends_state_delta_before_relaunch():
    """A checkpoint-1 report edit is written to state (raw + rendered report)
    via a runserver state_delta event appended BEFORE the resumed segment."""
    session = _resume_with_edits(
        "review_research",
        {"combined_final_cited_report": "old", "sources": {}},
        [{"field": "combined_final_cited_report", "value": "NEW REPORT"}],
    )
    edit_events = _runserver_state_events(session)
    assert len(edit_events) == 1
    idx, ev = edit_events[0]
    delta = ev.actions.state_delta
    assert delta["combined_final_cited_report"] == "NEW REPORT"
    assert delta["final_report_with_citations"] == "NEW REPORT"
    assert delta["research_report_edited"] is True
    assert "final_visual_concepts" not in delta
    resumed_idx = next(
        i for i, e in enumerate(session.events) if e.author == "creative_agent"
    )
    assert idx < resumed_idx
    assert session.state["combined_final_cited_report"] == "NEW REPORT"


def test_resume_edits_for_other_checkpoints_append_no_state_event():
    session = _resume_with_edits(
        "review_ad_copies",
        {"combined_final_cited_report": "old"},
        [{"field": "combined_final_cited_report", "value": "NEW REPORT"}],
    )
    assert _runserver_state_events(session) == []
    assert session.state["combined_final_cited_report"] == "old"


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


# --- Duplicate-run guard (one active run per (app, user, session)) -----------
#
# A double POST (double-clicked Start, a retried resume) on the same session
# must not start a second concurrent Runner interleaving events into one
# session log. The guard is an in-process registry, so each test starts from a
# clean slate (a key left behind by a task bound to a previous test's event loop
# would otherwise leak across tests).


@pytest.fixture(autouse=True)
def _clear_active_runs():
    async_runs._ACTIVE_RUNS.clear()
    async_runs._ACTIVE_RESUME_CALL_IDS.clear()
    yield
    async_runs._ACTIVE_RUNS.clear()
    async_runs._ACTIVE_RESUME_CALL_IDS.clear()


class _GatedRunner:
    """Runner double that blocks on an ``asyncio.Event`` before emitting a single
    event, so a run can be held 'in flight' while a duplicate kickoff is tried."""

    def __init__(self, svc, app_name, gate):
        self._svc = svc
        self._app_name = app_name
        self._gate = gate

    async def run_async(self, *, user_id, session_id, new_message, **kwargs):
        await self._gate.wait()
        s = await self._svc.get_session(
            app_name=self._app_name, user_id=user_id, session_id=session_id
        )
        ev = _agent_event("gated")
        await self._svc.append_event(s, ev)
        yield ev


def _spy_prior_segment_waits(monkeypatch, expected):
    """Return an Event set once ``expected`` resumes have entered
    ``_await_prior_segment``. The spy calls straight into the real wait with no
    await in between, so when the test wakes on the Event every counted resume
    is parked in the bounded wait on the prior segment (no sleep-ordering)."""
    reached = asyncio.Event()
    entered = 0
    original = async_runs._await_prior_segment

    async def _spy(key):
        nonlocal entered
        entered += 1
        if entered == expected:
            reached.set()
        await original(key)

    monkeypatch.setattr(async_runs, "_await_prior_segment", _spy)
    return reached


def _kick(svc, runner, *, session_id="s", app_name="creative_agent"):
    return start_run(
        app_name=app_name,
        user_id="u",
        session_id=session_id,
        message="hi",
        session_service=svc,
        runner_factory=lambda a: runner,
    )


def test_duplicate_start_on_active_session_raises_run_already_active():
    async def _go():
        svc = InMemorySessionService()
        gate = asyncio.Event()
        runner = _GatedRunner(svc, "creative_agent", gate)
        _r, task = await _kick(svc, runner)
        with pytest.raises(async_runs.RunAlreadyActive) as exc_info:
            await _kick(svc, runner)
        assert exc_info.value.reason == "run_active"
        gate.set()
        await task

    asyncio.run(_go())


def test_claim_treats_done_task_as_released():
    """A stored Task that is already done but whose done-callback (the key
    release) hasn't run yet is a finished run — the claim must overwrite it
    rather than 409."""

    async def _go():
        async def _noop():
            return None

        done = asyncio.ensure_future(_noop())
        await done
        key = ("creative_agent", "u", "s")
        async_runs._ACTIVE_RUNS[key] = done  # release callback never ran
        async_runs._claim_run(key)
        assert async_runs._ACTIVE_RUNS[key] is async_runs._CLAIMING

    asyncio.run(_go())


def test_different_session_is_not_blocked():
    async def _go():
        svc = InMemorySessionService()
        gate = asyncio.Event()
        runner = _GatedRunner(svc, "creative_agent", gate)
        _r, t1 = await _kick(svc, runner, session_id="s1")
        result, t2 = await _kick(svc, runner, session_id="s2")
        assert result == {"runId": "s2", "status": "running"}
        gate.set()
        await asyncio.gather(t1, t2)

    asyncio.run(_go())


def test_key_released_after_run_finishes():
    async def _go():
        svc = InMemorySessionService()
        gate = asyncio.Event()
        runner = _GatedRunner(svc, "creative_agent", gate)
        _r, task = await _kick(svc, runner)
        gate.set()
        await task
        await asyncio.sleep(0)  # let done-callbacks run
        result, t3 = await _kick(svc, runner)
        assert result["status"] == "running"
        await t3

    asyncio.run(_go())


def test_key_released_when_task_is_cancelled():
    async def _go():
        svc = InMemorySessionService()
        gate = asyncio.Event()  # never set — the run is cancelled mid-flight
        _r, task = await _kick(svc, _GatedRunner(svc, "creative_agent", gate))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert async_runs._ACTIVE_RUNS == {}
        gate2 = asyncio.Event()
        gate2.set()
        _r, t2 = await _kick(svc, _GatedRunner(svc, "creative_agent", gate2))
        await t2

    asyncio.run(_go())


def test_key_released_when_task_raises(monkeypatch):
    """_drive_run never raises by contract, but the done-callback release must
    not depend on that — an escaped exception still frees the key."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("escaped")

    monkeypatch.setattr(async_runs, "_drive_run", _boom)

    async def _go():
        svc = InMemorySessionService()
        _r, task = await _kick(svc, object())
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)
        assert async_runs._ACTIVE_RUNS == {}

    asyncio.run(_go())


def test_key_released_when_setup_fails_before_task_spawn():
    """A failure between claiming the key and spawning the task (e.g. an unknown
    app → runner_factory KeyError) must not leave the key claimed forever."""

    async def _go():
        svc = InMemorySessionService()

        def _bad_factory(app_name):
            raise KeyError(app_name)

        with pytest.raises(KeyError):
            await start_run(
                app_name="nope",
                user_id="u",
                session_id="s",
                message="hi",
                session_service=svc,
                runner_factory=_bad_factory,
            )
        assert async_runs._ACTIVE_RUNS == {}

    asyncio.run(_go())


def _resume(svc, runner, *, app_name="interactive_creative"):
    return start_resume(
        app_name=app_name,
        user_id="u",
        session_id="s",
        function_call_id="call-1",
        function_name="review_research",
        response={"status": "approved"},
        session_service=svc,
        runner_factory=lambda a: runner,
    )


def test_resume_while_run_active_beyond_grace_raises(monkeypatch):
    monkeypatch.setattr(async_runs, "RESUME_PRIOR_SEGMENT_GRACE_SECONDS", 0.05)

    async def _go():
        svc = InMemorySessionService()
        gate = asyncio.Event()
        runner = _GatedRunner(svc, "interactive_creative", gate)
        _r, task = await _kick(svc, runner, app_name="interactive_creative")
        with pytest.raises(async_runs.RunAlreadyActive) as exc_info:
            await _resume(svc, runner)
        # The approval was NOT applied (the prior segment is still running), so
        # the client must re-offer the review rather than just poll.
        assert exc_info.value.reason == "prior_segment_active"
        gate.set()
        await task

    asyncio.run(_go())


def test_duplicate_resume_same_call_is_rejected_immediately(monkeypatch):
    """A double-submitted/retried resume for the SAME function call while the
    first resume's segment runs is already being served: reject at once with
    reason 'resume_in_progress' (no grace wait — the running segment is the
    resume itself, not a finishing prior segment)."""
    monkeypatch.setattr(async_runs, "RESUME_PRIOR_SEGMENT_GRACE_SECONDS", 5.0)

    async def _go():
        svc = InMemorySessionService()
        await svc.create_session(
            app_name="interactive_creative", user_id="u", session_id="s"
        )
        gate = asyncio.Event()
        runner = _GatedRunner(svc, "interactive_creative", gate)
        _r, t1 = await _resume(svc, runner)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(async_runs.RunAlreadyActive) as exc_info:
            await _resume(svc, runner)
        assert loop.time() - started < 1.0
        assert exc_info.value.reason == "resume_in_progress"
        gate.set()
        await t1

    asyncio.run(_go())


def test_concurrent_duplicate_resumes_waiting_on_prior_segment(monkeypatch):
    """Two resumes for the same call both waiting on the finishing prior
    segment: the first to claim wins, the other is a served duplicate."""

    async def _go():
        svc = InMemorySessionService()
        gate = asyncio.Event()
        runner = _GatedRunner(svc, "interactive_creative", gate)
        _r, t0 = await _kick(svc, runner, app_name="interactive_creative")
        both_waiting = _spy_prior_segment_waits(monkeypatch, expected=2)
        r1 = asyncio.create_task(_resume(svc, runner))
        r2 = asyncio.create_task(_resume(svc, runner))
        await both_waiting.wait()
        gate.set()
        results = await asyncio.gather(r1, r2, return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        wins = [r for r in results if not isinstance(r, BaseException)]
        assert len(wins) == 1 and len(errors) == 1
        assert isinstance(errors[0], async_runs.RunAlreadyActive)
        assert errors[0].reason == "resume_in_progress"
        await asyncio.gather(t0, wins[0][1])

    asyncio.run(_go())


def test_resume_waits_for_prior_segment_still_finishing(monkeypatch):
    """Pause/resume race: the frontend shows the review panel as soon as it
    polls the long-running function-call event, but the paused segment's task is
    still alive until it appends its terminal marker. A resume in that window
    must wait for the prior segment to finish (not 409, and not let the stale
    'done' marker land AFTER the resume's 'running' reset)."""
    # Bounded so a regression (no wait → 409, or a hang) fails fast instead of
    # stalling on the production 30s grace.
    monkeypatch.setattr(async_runs, "RESUME_PRIOR_SEGMENT_GRACE_SECONDS", 5.0)

    async def _go():
        svc = InMemorySessionService()
        gate = asyncio.Event()
        runner = _GatedRunner(svc, "interactive_creative", gate)
        _r, t1 = await _kick(svc, runner, app_name="interactive_creative")
        waiting = _spy_prior_segment_waits(monkeypatch, expected=1)
        resume = asyncio.create_task(_resume(svc, runner))
        await waiting.wait()
        # Parked in the bounded wait on the (gated, so unfinishable) prior
        # segment rather than rejected: a no-wait 409 would finish the task
        # before it ever yields back here.
        assert not resume.done()
        gate.set()
        result, t2 = await resume
        assert result["status"] == "running"
        await asyncio.gather(t1, t2)
        return await svc.get_session(
            app_name="interactive_creative", user_id="u", session_id="s"
        )

    session = asyncio.run(_go())
    markers = [
        e.actions.state_delta[RUN_STATUS_KEY]
        for e in session.events
        if e.actions and RUN_STATUS_KEY in (e.actions.state_delta or {})
    ]
    # prior segment 'done' → resume reset 'running' → resumed segment 'done'
    assert markers == ["done", "running", "done"]


def test_router_maps_run_already_active_to_409_on_start_and_resume(monkeypatch):
    import httpx
    from fastapi import FastAPI

    monkeypatch.setattr(async_runs, "RESUME_PRIOR_SEGMENT_GRACE_SECONDS", 0.05)

    async def _go():
        svc = InMemorySessionService()
        gate = asyncio.Event()
        runner = _GatedRunner(svc, "interactive_creative", gate)
        async_runs.configure(session_service=svc, runner_factory=lambda a: runner)
        app = FastAPI()
        app.include_router(router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            body = {"userId": "u", "sessionId": "s", "message": "hi"}
            first = await c.post("/runs/interactive_creative", json=body)
            second = await c.post("/runs/interactive_creative", json=body)
            resume_body = {
                "functionCallId": "call-1",
                "functionName": "review_research",
                "response": {"status": "approved"},
            }
            resumed = await c.post(
                "/runs/interactive_creative/u/s/resume", json=resume_body
            )
            # Let the initial segment finish, then resume for real and retry the
            # same resume while it runs → a served duplicate.
            gate.set()
            for t in list(async_runs._ACTIVE_RUNS.values()):
                if isinstance(t, asyncio.Task):
                    await t
            await asyncio.sleep(0)
            gate.clear()
            ok = await c.post("/runs/interactive_creative/u/s/resume", json=resume_body)
            dup = await c.post(
                "/runs/interactive_creative/u/s/resume", json=resume_body
            )
        gate.set()
        for t in list(async_runs._ACTIVE_RUNS.values()):
            if isinstance(t, asyncio.Task):
                await t
        return first, second, resumed, ok, dup

    try:
        first, second, resumed, ok, dup = asyncio.run(_go())
    finally:
        async_runs.configure(session_service=None, runner_factory=None)
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"]["reason"] == "run_active"
    assert "already" in second.json()["detail"]["message"].lower()
    assert resumed.status_code == 409
    assert resumed.json()["detail"]["reason"] == "prior_segment_active"
    assert "already" in resumed.json()["detail"]["message"].lower()
    assert ok.status_code == 200
    assert dup.status_code == 409
    assert dup.json()["detail"]["reason"] == "resume_in_progress"


def test_router_start_run_enforces_body_user_id():
    import httpx
    from fastapi import FastAPI

    from runserver.authz import AuthzMode, UserAuthzMiddleware

    me = "alice@example.com"

    async def _go():
        svc = InMemorySessionService()
        gate = asyncio.Event()
        gate.set()
        runner = _GatedRunner(svc, "creative_agent", gate)
        async_runs.configure(
            session_service=svc,
            runner_factory=lambda a: runner,
            authz_mode=AuthzMode.ENFORCE,
        )
        app = FastAPI()
        app.include_router(router)
        app.add_middleware(
            UserAuthzMiddleware,
            mode=AuthzMode.ENFORCE,
            caller_ok=lambda auth: auth == "Bearer proxy",
        )
        hdrs = {"authorization": "Bearer proxy", "x-tt-user": me}
        t = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
            bad = await c.post(
                "/runs/creative_agent",
                headers=hdrs,
                json={"userId": "bob@x.com", "sessionId": "s", "message": "hi"},
            )
            anon = await c.post(
                "/runs/creative_agent",
                json={"userId": me, "sessionId": "s", "message": "hi"},
            )
            good = await c.post(
                "/runs/creative_agent",
                headers=hdrs,
                json={"userId": me, "sessionId": "s", "message": "hi"},
            )
        for task in list(async_runs._ACTIVE_RUNS.values()):
            if isinstance(task, asyncio.Task):
                await task
        return bad, anon, good

    try:
        bad, anon, good = asyncio.run(_go())
    finally:
        async_runs.configure(session_service=None, runner_factory=None)
    assert bad.status_code == 403
    assert anon.status_code == 401
    assert good.status_code == 200


class _ForeignSessionService(InMemorySessionService):
    """Mimics ``VertexAiSessionService`` on an ownership mismatch: ``get_session``
    raises a bare ``ValueError`` (or ``error`` when given)."""

    def __init__(self, error: Exception | None = None):
        super().__init__()
        self._error = error

    async def get_session(self, *, app_name, user_id, session_id, config=None):
        raise self._error or ValueError(
            f"Session {session_id} does not belong to user {user_id}."
        )


def _foreign_session_requests(svc):
    import httpx
    from fastapi import FastAPI

    from runserver.authz import install_ownership_handler

    async def _go():
        runner = _GatedRunner(svc, "interactive_creative", asyncio.Event())
        async_runs.configure(session_service=svc, runner_factory=lambda a: runner)
        app = FastAPI()
        app.include_router(router)
        install_ownership_handler(app)
        t = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
            start = await c.post(
                "/runs/interactive_creative",
                json={"userId": "u", "sessionId": "s", "message": "hi"},
            )
            resume = await c.post(
                "/runs/interactive_creative/u/s/resume",
                json={
                    "functionCallId": "call-1",
                    "functionName": "review_research",
                    "response": {"status": "approved"},
                },
            )
            poll = await c.get("/runs/interactive_creative/u/s")
        return start, resume, poll

    try:
        return asyncio.run(_go())
    finally:
        async_runs.configure(session_service=None, runner_factory=None)


def test_router_maps_foreign_session_to_404_on_start_and_resume():
    start, resume, poll = _foreign_session_requests(_ForeignSessionService())
    assert start.status_code == 404
    assert resume.status_code == 404
    assert start.json() == resume.json() == {"detail": "Session not found"}
    assert poll.status_code == 200
    assert poll.json()["status"] == "not_found"
    # The claim was released on the 404, so no phantom active run lingers.
    assert not async_runs._ACTIVE_RUNS


def test_router_does_not_map_unrelated_value_errors_to_404():
    start, resume, _poll = _foreign_session_requests(
        _ForeignSessionService(ValueError("backend exploded"))
    )
    assert start.status_code == 500
    assert resume.status_code == 500


# --- auto-continue after an empty root turn ----------------------------------
#
# A gemini Pro root can return an EMPTY final turn (STOP, no text, no function
# call) right after a long NodeTool response; ADK ends the invocation there and
# the run would silently stop. _drive_run re-prompts once (bounded) when the
# workflow is unfinished and the segment did not end at a checkpoint pause.

ROOT = "root_agent"


def _empty_root_event() -> Event:
    # The prod signature: a root event with no content parts, only a state_delta.
    return Event(author=ROOT, actions=EventActions(state_delta={"request_count": 3}))


def _root_text_event(text="done for now") -> Event:
    return Event(
        author=ROOT,
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )


def _root_call_event(name="visual_generation_pipeline", call_id="c-1") -> Event:
    return Event(
        author=ROOT,
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(id=call_id, name=name, args={})
                )
            ],
        ),
    )


def _root_response_event(name="visual_generation_pipeline", call_id="c-1") -> Event:
    return Event(
        author=ROOT,
        content=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=call_id, name=name, response={"result": "ok"}
                    )
                )
            ],
        ),
    )


def _lr_call_event(name="review_visual_concepts", call_id="lr-1") -> Event:
    ev = _root_call_event(name, call_id)
    ev.long_running_tool_ids = {call_id}
    return ev


def _sub_agent_text_event() -> Event:
    return Event(
        author="visual_concept_drafter",
        content=types.Content(role="model", parts=[types.Part(text="concepts")]),
    )


_EMPTY_SEGMENT = [_root_call_event(), _root_response_event(), _empty_root_event()]


def _sac(*, app="interactive_creative", state=None, events=None, attempts=0):
    return should_auto_continue(
        app,
        {} if state is None else state,
        list(_EMPTY_SEGMENT) if events is None else events,
        ROOT,
        attempts,
    )


def test_should_auto_continue_true_on_empty_root_turn_unfinished_workflow():
    assert _sac() is True
    assert _sac(app="creative_agent") is True


def test_should_auto_continue_respects_attempt_cap(monkeypatch):
    monkeypatch.setattr(async_runs, "MAX_AUTO_CONTINUES", 1)
    assert _sac(attempts=1) is False
    monkeypatch.setattr(async_runs, "MAX_AUTO_CONTINUES", 0)
    assert _sac(attempts=0) is False


def test_should_auto_continue_false_when_workflow_complete():
    assert _sac(state={"eval_bq_row_uuid": "abc123"}) is False
    # Empty/blank completion values still count as unfinished.
    assert _sac(state={"eval_bq_row_uuid": ""}) is True
    assert _sac(state={"eval_bq_row_uuid": "  "}) is True


def test_should_auto_continue_after_eval_saved_but_before_bq_write():
    """The eval report in GCS is NOT the end: an empty root turn before the final
    write_eval_report_to_bq (session 8242212012491276288) must be re-prompted."""
    assert _sac(state={"eval_report_gcs_uri": "gs://b/r.json"}) is True
    assert (
        _sac(app="creative_agent", state={"eval_report_gcs_uri": "gs://b/r.json"})
        is True
    )


def test_should_auto_continue_uses_trend_scout_completion_key():
    ev = _empty_root_event()
    ev.author = "trend_scout"
    assert should_auto_continue("trend_scout", {}, [ev], "trend_scout", 0) is True
    done = {"select_trends_markdown_gcs_uri": "gs://b/t.md"}
    assert should_auto_continue("trend_scout", done, [ev], "trend_scout", 0) is False


def test_should_auto_continue_false_for_unknown_app():
    assert _sac(app="some_other_app") is False


def test_should_auto_continue_false_at_unanswered_long_running_pause():
    events = [_root_call_event(), _root_response_event(), _lr_call_event()]
    assert _sac(events=events) is False
    # Even if an empty root event trails the pause call, a pause is a pause.
    assert _sac(events=[*events, _empty_root_event()]) is False


def test_should_auto_continue_ignores_answered_long_running_call():
    events = [
        _lr_call_event(call_id="lr-1"),
        _root_response_event("review_visual_concepts", "lr-1"),
        _empty_root_event(),
    ]
    assert _sac(events=events) is True


def test_should_auto_continue_false_when_last_root_turn_has_text_or_call():
    assert _sac(events=[_root_response_event(), _root_text_event()]) is False
    assert _sac(events=[_root_response_event(), _root_call_event()]) is False
    # A trailing NON-root event doesn't mask the root's last (text) turn.
    assert _sac(events=[_root_text_event(), _sub_agent_text_event()]) is False


def test_should_auto_continue_treats_blank_and_thought_text_as_empty():
    blank = Event(
        author=ROOT,
        content=types.Content(
            role="model",
            parts=[types.Part(text="  "), types.Part(text="hmm", thought=True)],
        ),
    )
    assert _sac(events=[_root_response_event(), blank]) is True


def test_should_auto_continue_false_without_root_event_or_root_author():
    assert _sac(events=[_sub_agent_text_event()]) is False
    assert _sac(events=[]) is False
    assert should_auto_continue("creative_agent", {}, _EMPTY_SEGMENT, "", 0) is False


def test_should_auto_continue_accepts_serialized_dict_events():
    empty = [async_runs._serialize_event(e) for e in _EMPTY_SEGMENT]
    assert should_auto_continue("creative_agent", {}, empty, ROOT, 0) is True
    paused = [
        async_runs._serialize_event(e)
        for e in (_root_response_event(), _lr_call_event())
    ]
    assert should_auto_continue("creative_agent", {}, paused, ROOT, 0) is False


def test_max_auto_continues_env_parsing():
    parse = async_runs._parse_max_auto_continues
    assert parse(None) == 2
    assert parse("") == 2
    assert parse("junk") == 2
    assert parse("0") == 0
    assert parse("2") == 2
    assert parse("9") == 3
    assert parse("-4") == 0


class _ScriptedRunner:
    """Runner double with a root agent name, yielding one scripted event list per
    ``run_async`` call (the last list repeats) and recording every message."""

    def __init__(self, svc, app_name, segments, *, root=ROOT, raise_on_segment=None):
        self._svc = svc
        self._app_name = app_name
        self._segments = segments
        self._raise_on_segment = raise_on_segment
        self.agent = type("_Agent", (), {"name": root})()
        self.messages: list[types.Content] = []

    async def run_async(self, *, user_id, session_id, new_message, **kwargs):
        idx = len(self.messages)
        self.messages.append(new_message)
        if self._raise_on_segment == idx:
            raise RuntimeError("continued boom")
        template = self._segments[min(idx, len(self._segments) - 1)]
        for tmpl in template:
            ev = tmpl.model_copy(deep=True)
            ev.id = Event.new_id()
            ev.invocation_id = f"inv-{idx}"
            s = await self._svc.get_session(
                app_name=self._app_name, user_id=user_id, session_id=session_id
            )
            await self._svc.append_event(s, ev)
            yield ev


def _run_scripted(runner, *, app_name="interactive_creative", state=None):
    async def _go():
        svc = runner._svc
        await svc.create_session(
            app_name=app_name, user_id="u", session_id="s", state=state or {}
        )
        _result, task = await start_run(
            app_name=app_name,
            user_id="u",
            session_id="s",
            message="go",
            session_service=svc,
            runner_factory=lambda a: runner,
        )
        await task
        return await svc.get_session(app_name=app_name, user_id="u", session_id="s")

    return asyncio.run(_go())


def _text_of(msg: types.Content) -> str | None:
    return msg.parts[0].text


def _status_markers(session) -> list:
    return [
        e.actions.state_delta[RUN_STATUS_KEY]
        for e in session.events
        if e.actions.state_delta and RUN_STATUS_KEY in e.actions.state_delta
    ]


def test_drive_run_auto_continues_once_after_empty_root_turn(caplog):
    svc = InMemorySessionService()
    runner = _ScriptedRunner(
        svc,
        "interactive_creative",
        [list(_EMPTY_SEGMENT), [_root_text_event("calling"), _lr_call_event()]],
    )
    with caplog.at_level("WARNING"):
        session = _run_scripted(runner)
    assert len(runner.messages) == 2
    assert _text_of(runner.messages[0]) == "go"
    assert _text_of(runner.messages[1]) == async_runs.AUTO_CONTINUE_MESSAGE
    assert session.state[async_runs.AUTO_CONTINUES_KEY] == 1
    # No 'done' between the segments: exactly one terminal marker, at the end.
    assert _status_markers(session) == ["done"]
    assert session.events[-1].actions.state_delta == {RUN_STATUS_KEY: "done"}
    rec = [
        e
        for e in session.events
        if e.actions.state_delta
        and async_runs.AUTO_CONTINUES_KEY in e.actions.state_delta
    ]
    assert len(rec) == 1
    assert rec[0].author == RUNSERVER_AUTHOR
    assert rec[0].invocation_id  # Vertex rejects an empty invocation_id
    assert "auto-continue after empty root turn" in caplog.text


def test_drive_run_no_continue_at_legitimate_pause():
    svc = InMemorySessionService()
    runner = _ScriptedRunner(
        svc, "interactive_creative", [[_root_response_event(), _lr_call_event()]]
    )
    session = _run_scripted(runner)
    assert len(runner.messages) == 1
    assert async_runs.AUTO_CONTINUES_KEY not in session.state
    assert _status_markers(session) == ["done"]


def test_drive_run_no_continue_when_workflow_complete():
    svc = InMemorySessionService()
    runner = _ScriptedRunner(svc, "interactive_creative", [list(_EMPTY_SEGMENT)])
    session = _run_scripted(runner, state={"eval_bq_row_uuid": "abc123"})
    assert len(runner.messages) == 1
    assert async_runs.AUTO_CONTINUES_KEY not in session.state
    assert _status_markers(session) == ["done"]


def test_drive_run_auto_continue_respects_cap(monkeypatch):
    monkeypatch.setattr(async_runs, "MAX_AUTO_CONTINUES", 1)
    svc = InMemorySessionService()
    runner = _ScriptedRunner(svc, "interactive_creative", [list(_EMPTY_SEGMENT)])
    session = _run_scripted(runner)
    assert len(runner.messages) == 2  # one continue, then give up
    assert session.state[async_runs.AUTO_CONTINUES_KEY] == 1
    assert _status_markers(session) == ["done"]


def test_drive_run_auto_continue_count_is_cumulative_across_segments():
    svc = InMemorySessionService()
    runner = _ScriptedRunner(
        svc, "interactive_creative", [list(_EMPTY_SEGMENT), [_lr_call_event()]]
    )
    session = _run_scripted(runner, state={async_runs.AUTO_CONTINUES_KEY: 2})
    assert len(runner.messages) == 2
    assert session.state[async_runs.AUTO_CONTINUES_KEY] == 3


def test_drive_run_error_in_continued_segment_writes_error_marker():
    svc = InMemorySessionService()
    runner = _ScriptedRunner(
        svc, "interactive_creative", [list(_EMPTY_SEGMENT)], raise_on_segment=1
    )
    session = _run_scripted(runner)
    assert len(runner.messages) == 2
    delta = session.events[-1].actions.state_delta
    assert delta[RUN_STATUS_KEY] == "error"
    assert "continued boom" in delta[RUN_ERROR_KEY]
    assert _status_markers(session) == ["error"]


def test_drive_run_without_root_agent_name_never_continues():
    # Runner doubles / runners without ``.agent`` keep today's behaviour.
    svc = InMemorySessionService()
    runner = _ScriptedRunner(svc, "interactive_creative", [list(_EMPTY_SEGMENT)])
    del runner.agent
    session = _run_scripted(runner)
    assert len(runner.messages) == 1
    assert _status_markers(session) == ["done"]


def test_resume_segment_also_auto_continues():
    async def _go():
        svc = InMemorySessionService()
        await svc.create_session(
            app_name="interactive_creative", user_id="u", session_id="s", state={}
        )
        runner = _ScriptedRunner(
            svc, "interactive_creative", [list(_EMPTY_SEGMENT), [_lr_call_event()]]
        )
        _r, task = await _resume(svc, runner)
        await task
        session = await svc.get_session(
            app_name="interactive_creative", user_id="u", session_id="s"
        )
        return session, runner

    session, runner = asyncio.run(_go())
    assert len(runner.messages) == 2
    assert runner.messages[0].parts[0].function_response.id == "call-1"
    assert _text_of(runner.messages[1]) == async_runs.AUTO_CONTINUE_MESSAGE
    # The resume's 'running' reset, then a single terminal 'done'.
    assert _status_markers(session) == ["running", "done"]
