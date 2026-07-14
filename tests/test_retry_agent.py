"""Tests for RetryUntilKeyAgent — the retry-on-empty producer wrapper.

These are fully offline: a fake inner agent (`_FlakyProducer`) deterministically
emits no `output_key` for its first N runs, then a real value. Everything is
driven through a real `InMemoryRunner`, so the wrapper's state check exercises
the genuine ADK state-delta application path (runner appends each yielded event
and merges its `state_delta` into `session.state` before the wrapper resumes) —
not a mock of it. No model calls, no GCP credentials, no quota.

Coroutines are driven with `asyncio.run` (no pytest-asyncio in this project,
see tests/test_crf_worker_async.py).
"""

import asyncio
import logging
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import PrivateAttr

from agent_common import RetryUntilKeyAgent


class _FlakyProducer(BaseAgent):
    """Test double for a research producer with an ``output_key``.

    Emits an event carrying no ``state_delta`` (simulating a model turn that
    returns only tool calls / thinking and no final text, leaving output_key
    unset) for its first ``fail_first`` runs, then an event that writes the
    real value. ``runs`` counts how many times it was invoked.
    """

    output_key: str
    value: str = "REAL_REPORT"
    fail_first: int = 0
    _runs: int = PrivateAttr(default=0)

    @property
    def runs(self) -> int:
        return self._runs

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        self._runs += 1
        if self._runs <= self.fail_first:
            # No state_delta → output_key never written (the landmine).
            yield Event(invocation_id=ctx.invocation_id, author=self.name)
            return
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            actions=EventActions(state_delta={self.output_key: self.value}),
        )


def _run(agent: BaseAgent):
    """Drive ``agent`` once through an InMemoryRunner; return the final session."""
    runner = InMemoryRunner(agent=agent, app_name="retry_test")

    async def _go():
        session = await runner.session_service.create_session(
            app_name="retry_test", user_id="u"
        )
        async for _ in runner.run_async(
            user_id="u",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="go")]),
        ):
            pass
        return await runner.session_service.get_session(
            app_name="retry_test", user_id="u", session_id=session.id
        )

    return asyncio.run(_go())


def test_recovers_after_empty_attempts():
    """Producer fails twice, succeeds on the third — wrapper retries and recovers."""
    producer = _FlakyProducer(name="producer", output_key="report", fail_first=2)
    wrapper = RetryUntilKeyAgent(
        name="retry_wrapper", sub_agents=[producer], output_key="report", max_attempts=3
    )

    session = _run(wrapper)

    assert producer.runs == 3
    assert session.state.get("report") == "REAL_REPORT"


def test_no_retry_when_first_attempt_succeeds():
    """A healthy producer runs exactly once — no wasted retries."""
    producer = _FlakyProducer(name="producer", output_key="report", fail_first=0)
    wrapper = RetryUntilKeyAgent(
        name="retry_wrapper", sub_agents=[producer], output_key="report", max_attempts=3
    )

    session = _run(wrapper)

    assert producer.runs == 1
    assert session.state.get("report") == "REAL_REPORT"


def test_bounded_and_observable_when_never_populated(caplog):
    """Producer never populates — wrapper stops at max_attempts and logs loudly.

    The wrapper must NOT corrupt ``output_key`` with a placeholder (that would
    feed garbage to the downstream consumer). Instead it leaves the key unset,
    records an observable ``<key>__retry_exhausted`` marker, and logs an error —
    mitigation #3 (observable guardrail), not silent degradation.
    """
    producer = _FlakyProducer(name="producer", output_key="report", fail_first=99)
    wrapper = RetryUntilKeyAgent(
        name="retry_wrapper", sub_agents=[producer], output_key="report", max_attempts=3
    )

    with caplog.at_level(logging.ERROR):
        session = _run(wrapper)

    assert producer.runs == 3
    assert session.state.get("report") is None
    assert session.state.get("report__retry_exhausted") is True
    assert any("report" in r.message for r in caplog.records)
