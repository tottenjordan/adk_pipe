"""Tests for FailSoftNode — the fail-soft wrapper for optional graph steps.

A child that raises must not fail the enclosing Workflow: the wrapper logs it,
applies ``on_error(state_before, exc)``'s state delta, yields a truthy notice and
lets successors run. The healthy path passes the child's output through.
"""

import asyncio
from typing import Any

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import BaseNode, Workflow

from agent_common import FailSoftNode
from tests._fakes import user_message

APP = "fail_soft_test"


def _run(node: BaseNode, state: dict[str, Any] | None = None):
    async def go():
        svc = InMemorySessionService()
        runner = Runner(node=node, app_name=APP, session_service=svc)
        session = await svc.create_session(app_name=APP, user_id="u", state=state)
        events = [
            e
            async for e in runner.run_async(
                user_id="u", session_id=session.id, new_message=user_message("go")
            )
        ]
        final = await svc.get_session(app_name=APP, user_id="u", session_id=session.id)
        assert final is not None
        return events, dict(final.state)

    return asyncio.run(go())


def boom(ctx: Context) -> Event:
    ctx.state["value"] = "clobbered"
    raise RuntimeError("optional step exploded")


def fine(ctx: Context) -> Event:
    return Event(output="FINE", actions=EventActions(state_delta={"value": "new"}))


def after(ctx: Context) -> Event:
    return Event(actions=EventActions(state_delta={"after_ran": True}))


def _graph(child_fn, on_error=None) -> Workflow:
    child = Workflow(name="child", edges=[("START", child_fn)])
    kwargs = {"on_error": on_error} if on_error else {}
    soft = FailSoftNode(name="soft", node=child, **kwargs)
    return Workflow(name="outer", edges=[("START", soft, after)])


def test_exception_is_converted_to_the_error_delta_and_successors_run():
    seen: dict[str, Any] = {}

    def on_error(state_before, exc):
        seen["before"] = state_before.get("value")
        seen["exc"] = exc
        return {"value": state_before.get("value"), "value__retry_exhausted": True}

    events, state = _run(_graph(boom, on_error), {"value": "old"})

    assert isinstance(seen["exc"], RuntimeError)
    assert seen["before"] == "old"  # snapshot taken before the child ran
    assert state["value"] == "old"  # restored
    assert state["value__retry_exhausted"] is True
    assert state["after_ran"] is True
    outputs = [str(e.output) for e in events if e.output]
    assert any("child failed (RuntimeError)" in o for o in outputs), outputs


def test_default_on_error_still_continues():
    _, state = _run(_graph(boom))
    assert state["after_ran"] is True


def test_healthy_child_is_untouched():
    called = []
    _, state = _run(_graph(fine, lambda s, e: called.append(e) or {}))
    assert state["value"] == "new"
    assert state["after_ran"] is True
    assert called == []


def test_root_cause_unwraps_nested_error_attributes():
    from agent_common.fail_soft_node import root_cause

    class Wrapper(Exception):
        def __init__(self, error):
            super().__init__("wrapped")
            self.error = error

    original = ValueError("schema")
    assert root_cause(Wrapper(Wrapper(original))) is original
    assert root_cause(original) is original
