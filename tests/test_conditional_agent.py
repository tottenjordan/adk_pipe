"""Tests for RunIfAgent — the conditional-block control-flow wrapper.

Fully offline, same style as tests/test_retry_agent.py: fake sub-agents driven
through a real ``InMemoryRunner`` so the predicate reads genuine ADK session
state (the runner merges each yielded event's ``state_delta`` into the session
the wrapper reads). No model calls, no GCP credentials, no quota.

Coroutines are driven with ``asyncio.run`` (no pytest-asyncio in this project).
"""

import asyncio
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import PrivateAttr

from agent_common import RunIfAgent


class _Marker(BaseAgent):
    """Records each invocation and writes ``output_key`` into state."""

    output_key: str
    value: str = "RAN"
    _runs: int = PrivateAttr(default=0)

    @property
    def runs(self) -> int:
        return self._runs

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        self._runs += 1
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            actions=EventActions(state_delta={self.output_key: self.value}),
        )


class _Seeder(BaseAgent):
    """Writes a fixed key/value so a downstream predicate can read state."""

    key: str
    value: str

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            actions=EventActions(state_delta={self.key: self.value}),
        )


def _run(agent: BaseAgent):
    """Drive ``agent`` once through an InMemoryRunner; return the final session."""
    runner = InMemoryRunner(agent=agent, app_name="runif_test")

    async def _go():
        session = await runner.session_service.create_session(
            app_name="runif_test", user_id="u"
        )
        async for _ in runner.run_async(
            user_id="u",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="go")]),
        ):
            pass
        return await runner.session_service.get_session(
            app_name="runif_test", user_id="u", session_id=session.id
        )

    return asyncio.run(_go())


def test_runs_all_sub_agents_in_order_when_predicate_true():
    a = _Marker(name="a", output_key="a_out")
    b = _Marker(name="b", output_key="b_out")
    gate = RunIfAgent(name="gate", predicate=lambda _s: True, sub_agents=[a, b])

    session = _run(gate)

    assert a.runs == 1
    assert b.runs == 1
    assert session.state.get("a_out") == "RAN"
    assert session.state.get("b_out") == "RAN"


def test_skips_all_sub_agents_when_predicate_false():
    a = _Marker(name="a", output_key="a_out")
    b = _Marker(name="b", output_key="b_out")
    gate = RunIfAgent(name="gate", predicate=lambda _s: False, sub_agents=[a, b])

    session = _run(gate)

    assert a.runs == 0
    assert b.runs == 0
    assert session.state.get("a_out") is None
    assert session.state.get("b_out") is None


def test_predicate_reads_live_session_state():
    """The predicate is evaluated against real session state populated upstream.

    A seeder writes ``flag`` before the gate runs; the gate's predicate keys on
    it, proving the decision reflects state produced earlier in the same run
    (not just construction-time closure values).
    """
    seed = _Seeder(name="seed", key="flag", value="go")
    inner = _Marker(name="inner", output_key="inner_out")
    gate = RunIfAgent(
        name="gate",
        predicate=lambda s: s.get("flag") == "go",
        sub_agents=[inner],
    )
    pipeline = SequentialAgent(name="pipe", sub_agents=[seed, gate])

    session = _run(pipeline)

    assert inner.runs == 1
    assert session.state.get("inner_out") == "RAN"


def test_predicate_false_from_upstream_state_skips_block():
    seed = _Seeder(name="seed", key="flag", value="stop")
    inner = _Marker(name="inner", output_key="inner_out")
    gate = RunIfAgent(
        name="gate",
        predicate=lambda s: s.get("flag") == "go",
        sub_agents=[inner],
    )
    pipeline = SequentialAgent(name="pipe", sub_agents=[seed, gate])

    session = _run(pipeline)

    assert inner.runs == 0
    assert session.state.get("inner_out") is None


def test_empty_sub_agents_is_noop():
    gate = RunIfAgent(name="gate", predicate=lambda _s: True, sub_agents=[])
    session = _run(gate)
    assert session is not None  # completed without error
