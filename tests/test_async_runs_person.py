"""runserver/async_runs.py: the person-reference consent check at kick-off and
resume (offline: InMemorySessionService + InMemoryPersonRefsStore)."""

from __future__ import annotations

import asyncio

import pytest
from google.adk.events import Event
from google.adk.sessions import InMemorySessionService
from google.genai import types

from runserver import async_runs, person_refs
from runserver.async_runs import router, start_resume, start_run
from runserver.person_refs_store import InMemoryPersonRefsStore, utcnow

URI = "gs://b/person-refs/u-0123456789/me.jpg"


class _FakeRunner:
    def __init__(self, svc, app_name, events):
        self._svc, self._app_name, self._events = svc, app_name, events

    async def run_async(self, *, user_id, session_id, new_message, **kwargs):
        for ev in self._events:
            session = await self._svc.get_session(
                app_name=self._app_name, user_id=user_id, session_id=session_id
            )
            await self._svc.append_event(session, ev)
            yield ev


def _event(text: str) -> Event:
    return Event(
        author="root_agent",
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )


@pytest.fixture(autouse=True)
def _reset_person_store():
    yield
    person_refs.configure(store=InMemoryPersonRefsStore())


def _person_store(*, revoked=False, owner="u", uri=URI):
    store = InMemoryPersonRefsStore()
    row = {
        "consent_id": "consent-1234",
        "owner_user": owner,
        "photo_uri": uri,
        "label": "Me",
        "subject": "self",
        "adult_attested": True,
        "allow_public_share": False,
        "consent_text_version": person_refs.CONSENT_TEXT_VERSION,
        "created_at": utcnow(),
        "revoked_at": None,
        "person_renders": [],
    }
    asyncio.run(store.put(row))
    if revoked:
        asyncio.run(store.revoke("consent-1234", owner))
    person_refs.configure(store=store, bucket="b")
    return store


def _ref(**changes):
    return {"uri": URI, "consent_id": "consent-1234", **changes}


def _kick(state, *, app_name="creative_agent"):
    """start_run against a pre-seeded session → (error, runner factory calls)."""

    async def _go():
        svc = InMemorySessionService()
        await svc.create_session(
            app_name=app_name, user_id="u", session_id="s", state=state
        )
        calls = []

        def factory(a):
            calls.append(a)
            return _FakeRunner(svc, a, [_event("one")])

        try:
            _r, task = await start_run(
                app_name=app_name,
                user_id="u",
                session_id="s",
                message="go",
                session_service=svc,
                runner_factory=factory,
            )
        except async_runs.PersonReferenceError as exc:
            return exc, calls
        await task
        return None, calls

    return asyncio.run(_go())


def test_valid_person_reference_starts():
    _person_store()
    err, calls = _kick({"person_reference": _ref()})
    assert err is None and calls == ["creative_agent"]


def test_new_session_without_state_starts():
    # start_run creates the session itself when the browser didn't seed one.
    _person_store()

    async def _go():
        svc = InMemorySessionService()
        _r, task = await start_run(
            app_name="creative_agent",
            user_id="u",
            session_id="s",
            message="go",
            session_service=svc,
            runner_factory=lambda a: _FakeRunner(svc, a, [_event("one")]),
        )
        await task
        return await svc.get_session(
            app_name="creative_agent", user_id="u", session_id="s"
        )

    assert asyncio.run(_go()) is not None


@pytest.mark.parametrize(
    "store_kwargs, ref",
    [
        ({"revoked": True}, _ref()),
        ({"owner": "someone-else"}, _ref()),
        ({}, _ref(uri="gs://b/person-refs/u-0123456789/other.jpg")),
        ({}, _ref(consent_id="nope-0000")),
        ({}, {"uri": URI}),
        ({}, URI),
    ],
)
def test_invalid_person_reference_is_400_before_anything_starts(store_kwargs, ref):
    _person_store(**store_kwargs)
    err, calls = _kick({"person_reference": ref})
    assert err is not None
    assert (err.status, err.reason) == (400, "person_reference_invalid")
    assert calls == []
    assert ("creative_agent", "u", "s") not in async_runs._ACTIVE_RUNS


def test_empty_person_reference_is_unaffected():
    _person_store(revoked=True)
    for state in ({}, {"person_reference": {}}, {"person_reference": None}):
        err, calls = _kick(state)
        assert err is None and calls == ["creative_agent"]


def test_non_creative_apps_are_not_checked():
    _person_store(revoked=True)
    err, calls = _kick({"person_reference": _ref()}, app_name="trend_scout")
    assert err is None and calls == ["trend_scout"]


def test_store_failure_is_503():
    class _Broken:
        async def active_for(self, consent_id, owner):
            raise RuntimeError("bq down")

    person_refs.configure(store=_Broken(), bucket="b")
    err, calls = _kick({"person_reference": _ref()})
    assert err is not None
    assert (err.status, err.reason) == (503, "person_reference_unavailable")
    assert calls == []


def _resume(state):
    async def _go():
        svc = InMemorySessionService()
        await svc.create_session(
            app_name="interactive_creative", user_id="u", session_id="s", state=state
        )
        calls = []

        def factory(a):
            calls.append(a)
            return _FakeRunner(svc, a, [_event("resumed")])

        try:
            _r, task = await start_resume(
                app_name="interactive_creative",
                user_id="u",
                session_id="s",
                function_call_id="call-1",
                function_name="review_ad_copies",
                response={"status": "approved"},
                session_service=svc,
                runner_factory=factory,
            )
        except async_runs.PersonReferenceError as exc:
            return exc, calls
        await task
        return None, calls

    return asyncio.run(_go())


def _resume_state(state):
    """Like ``_resume`` but also returns the final session state."""

    async def _go():
        svc = InMemorySessionService()
        await svc.create_session(
            app_name="interactive_creative", user_id="u", session_id="s", state=state
        )
        calls = []

        def factory(a):
            calls.append(a)
            return _FakeRunner(svc, a, [_event("resumed")])

        try:
            _r, task = await start_resume(
                app_name="interactive_creative",
                user_id="u",
                session_id="s",
                function_call_id="call-1",
                function_name="review_ad_copies",
                response={"status": "approved"},
                session_service=svc,
                runner_factory=factory,
            )
        except async_runs.PersonReferenceError as exc:
            return exc, calls, None
        await task
        session = await svc.get_session(
            app_name="interactive_creative", user_id="u", session_id="s"
        )
        return None, calls, session.state

    return asyncio.run(_go())


def test_resume_with_revoked_consent_continues_without_the_person():
    _person_store(revoked=True)
    err, calls, state = _resume_state(
        {
            "person_reference": _ref(),
            "person_reference_available": "yes",
            "person_casting_rules": "rules",
            "person_reference__issues": ["Hero: earlier note"],
        }
    )
    assert err is None and calls == ["interactive_creative"]
    assert state["person_reference"] == {}
    assert state["person_reference_available"] == ""
    assert state["person_casting_rules"] == ""
    assert state["person_reference__issues"] == [
        "Hero: earlier note",
        async_runs.PERSON_REVOKED_NOTE,
    ]
    assert async_runs.PERSON_REVOKED_NOTE == (
        "Person reference consent was revoked; continued without the person"
    )


def test_resume_store_error_is_503():
    class _Broken:
        async def active_for(self, consent_id, owner):
            raise RuntimeError("bq down")

    person_refs.configure(store=_Broken(), bucket="b")
    err, calls, _ = _resume_state({"person_reference": _ref()})
    assert err is not None
    assert (err.status, err.reason) == (503, "person_reference_unavailable")
    assert calls == []
    assert ("interactive_creative", "u", "s") not in async_runs._ACTIVE_RUNS


def test_resume_with_active_person_reference_continues():
    _person_store()
    err, calls, state = _resume_state({"person_reference": _ref()})
    assert err is None and calls == ["interactive_creative"]
    assert state["person_reference"] == _ref()
    assert "person_reference__issues" not in state


def test_resume_without_person_reference_continues():
    err, calls = _resume({})
    assert err is None and calls == ["interactive_creative"]


def _post(path, json, state):
    import httpx
    from fastapi import FastAPI

    async def _go():
        svc = InMemorySessionService()
        await svc.create_session(
            app_name=path.split("/")[2], user_id="u", session_id="s", state=state
        )
        async_runs.configure(
            session_service=svc, runner_factory=lambda a: _FakeRunner(svc, a, [])
        )
        app = FastAPI()
        app.include_router(router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(path, json=json)

    try:
        return asyncio.run(_go())
    finally:
        async_runs.configure(session_service=None, runner_factory=None)


def test_router_maps_kickoff_error_to_400():
    _person_store(revoked=True)
    resp = _post(
        "/runs/creative_agent",
        {"userId": "u", "sessionId": "s", "message": "go"},
        {"person_reference": _ref()},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "person_reference_invalid"


def test_router_resume_with_revoked_consent_proceeds():
    _person_store(revoked=True)
    resp = _post(
        "/runs/interactive_creative/u/s/resume",
        {
            "functionCallId": "call-1",
            "functionName": "review_ad_copies",
            "response": {"status": "approved"},
        },
        {"person_reference": _ref()},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"
