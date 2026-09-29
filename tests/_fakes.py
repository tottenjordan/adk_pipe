"""Shared offline test doubles for the retry-on-empty and graph-Workflow tests.

The producer fakes are used by both ``tests/test_retry_agent.py``
(``RetryUntilKeyAgent``) and ``tests/test_retry_node.py`` (``RetryUntilKeyNode``),
so the agent and the graph node are exercised against the exact same producer
behaviors. ``StubLlm`` and the ``fc_response`` / ``text_response`` /
``user_message`` builders script root agents in the graph tests
(``test_workflow_api_contract``, ``test_retry_node``, ``test_trend_scout_graph``).

Run counts live in a shared mutable list, not an int ``PrivateAttr``: a graph
``Workflow`` clones agent nodes per run (``BaseAgent.clone`` -> shallow
``model_copy``), so an int counter would be incremented on the clone only. The
shallow copy shares the list, so the original instance observes every run (see
``tests/test_workflow_api_contract.py``).
"""

from collections.abc import AsyncGenerator
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr


class _CountingAgent(BaseAgent):
    """Base for the fakes: ``runs`` counts invocations across clones."""

    _log: list[int] = PrivateAttr(default_factory=list)

    @property
    def runs(self) -> int:
        return len(self._log)


def _empty(agent: BaseAgent, ctx: InvocationContext) -> Event:
    # No state_delta → output_key never written (the landmine).
    return Event(invocation_id=ctx.invocation_id, author=agent.name)


def _write(agent: BaseAgent, ctx: InvocationContext, key: str, value: Any) -> Event:
    return Event(
        invocation_id=ctx.invocation_id,
        author=agent.name,
        actions=EventActions(state_delta={key: value}),
    )


class FlakyProducer(_CountingAgent):
    """Test double for a research producer with an ``output_key``.

    Emits an event carrying no ``state_delta`` (simulating a model turn that
    returns only tool calls / thinking and no final text, leaving output_key
    unset) for its first ``fail_first`` runs, then an event that writes the
    real value (``empty_value`` instead, when set, simulates a whitespace-only
    final text).
    """

    output_key: str
    value: str = "REAL_REPORT"
    fail_first: int = 0
    empty_value: str | None = None

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event]:
        self._log.append(1)
        if self.runs <= self.fail_first:
            if self.empty_value is None:
                yield _empty(self, ctx)
            else:
                yield _write(self, ctx, self.output_key, self.empty_value)
            return
        yield _write(self, ctx, self.output_key, self.value)


class FlakyFlagProducer(_CountingAgent):
    """Test double for a producer whose ``output_key`` is a boolean flag.

    Mirrors ``creative_agent.tools.generate_image``, which signals success by
    writing ``state["_images_generated"] = True`` (not a text summary). Emits an
    event carrying no ``state_delta`` (simulating a MALFORMED_FUNCTION_CALL turn
    that never invoked the tool) for its first ``fail_first`` runs, then an event
    that writes the bool flag.
    """

    output_key: str
    value: bool = True
    fail_first: int = 0

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event]:
        self._log.append(1)
        if self.runs <= self.fail_first:
            yield _empty(self, ctx)
            return
        yield _write(self, ctx, self.output_key, self.value)


class RawSearcher(_CountingAgent):
    """Test double for the tool-using *searcher* half of a split producer.

    Always writes an intermediate ``raw_key`` (simulating a healthy
    google_search turn that emits raw findings).
    """

    raw_key: str
    raw_value: str = "RAW_FINDINGS"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event]:
        self._log.append(1)
        yield _write(self, ctx, self.raw_key, self.raw_value)


class FlakySynthesizer(_CountingAgent):
    """Test double for the tool-free *synthesizer* half of a split producer.

    Reads ``raw_key`` from state (asserting the searcher ran first), then for
    its first ``fail_first`` runs emits no ``output_key`` (the empty-turn
    landmine), and thereafter writes the real value.
    """

    raw_key: str
    output_key: str
    value: str = "REAL_REPORT"
    fail_first: int = 0

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event]:
        self._log.append(1)
        # The searcher in the same sequence must have populated raw_key first.
        assert ctx.session.state.get(self.raw_key) == "RAW_FINDINGS"
        if self.runs <= self.fail_first:
            yield _empty(self, ctx)
            return
        yield _write(self, ctx, self.output_key, self.value)


# --------------------------------------------------------------------------
# Scripted model + content builders
# --------------------------------------------------------------------------


class StubLlm(BaseLlm):
    """Scripted model: each call pops the next canned ``LlmResponse``."""

    model: str = "stub-model"
    _script: list[LlmResponse] = PrivateAttr(default_factory=list)
    _calls: int = PrivateAttr(default=0)

    @property
    def calls(self) -> int:
        return self._calls

    def push(self, *responses: LlmResponse) -> None:
        self._script.extend(responses)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self._calls += 1
        assert self._script, f"stub model called unexpectedly (call #{self._calls})"
        yield self._script.pop(0)


def fc_response(name: str, args: dict[str, Any], fc_id: str) -> LlmResponse:
    """A model turn that makes a single function call."""
    part = types.Part(function_call=types.FunctionCall(id=fc_id, name=name, args=args))
    return LlmResponse(content=types.Content(role="model", parts=[part]))


def text_response(text: str) -> LlmResponse:
    """A model turn that returns plain text."""
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=text)])
    )


def user_message(text: str) -> types.Content:
    """A user turn (the ``new_message`` passed to ``Runner.run_async``)."""
    return types.Content(role="user", parts=[types.Part(text=text)])
