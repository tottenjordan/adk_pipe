"""Shared offline test doubles for the tool, retry-on-empty and graph-Workflow tests.

Tool doubles: ``FakeToolContext`` / ``FakeState`` stand in for ADK's
``ToolContext`` in direct tool calls, ``FakeStorageClient`` records GCS uploads
(concurrency tests), and ``noop_async`` replaces ``asyncio.sleep`` so backoff
retries don't wall-clock.

The producer fakes drive ``tests/test_retry_node.py`` (``RetryUntilKeyNode``)
and the graph tests. ``StubLlm`` and the ``fc_response`` / ``text_response`` /
``user_message`` builders script root agents in the graph tests
(``test_workflow_api_contract``, ``test_retry_node``, ``test_trend_scout_graph``,
``test_creative_agent_graph``); ``RecordingLlm`` additionally keeps every
request, and ``walk_nodes`` enumerates a graph's nested nodes.

Run counts live in a shared mutable list, not an int ``PrivateAttr``: a graph
``Workflow`` clones agent nodes per run (``BaseAgent.clone`` -> shallow
``model_copy``), so an int counter would be incremented on the clone only. The
shallow copy shares the list, so the original instance observes every run (see
``tests/test_workflow_api_contract.py``).
"""

import os
from collections.abc import AsyncGenerator, Iterator
from types import SimpleNamespace
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


class RecordingLlm(StubLlm):
    """``StubLlm`` that also records every ``LlmRequest`` it receives."""

    _requests: list[LlmRequest] = PrivateAttr(default_factory=list)

    @property
    def requests(self) -> list[LlmRequest]:
        return self._requests

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self._requests.append(llm_request)
        async for r in super().generate_content_async(llm_request, stream):
            yield r


def walk_nodes(node: Any) -> Iterator[Any]:
    """Yield ``node`` and every node nested in it (Workflow graphs, retry children).

    Graph nodes are per-graph clones: compare them by name, never identity.
    """
    from google.adk.workflow import Workflow

    from agent_common import RetryUntilKeyNode

    yield node
    if isinstance(node, Workflow):
        assert node.graph is not None
        for child in node.graph.nodes:
            if child.name != "__START__":
                yield from walk_nodes(child)
    elif isinstance(node, RetryUntilKeyNode):
        yield from walk_nodes(node.node)


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


# --- Tool doubles -----------------------------------------------------------


async def noop_async(*a: Any, **k: Any) -> None:
    """Stand-in for ``asyncio.sleep`` so backoff retries don't wall-clock."""
    return None


class FakeState(dict):
    """dict-backed ``ToolContext.state``; ``to_dict`` mirrors ADK's ``State``."""

    def to_dict(self) -> dict[str, Any]:
        return dict(self)


class FakeToolContext:
    """Minimal ADK ``ToolContext`` for calling tools directly.

    ``state`` is copied, so a shared seed dict is never mutated across tests.
    BQ writers derive their idempotent row keys from ``session.id``.
    """

    def __init__(
        self, state: dict[str, Any] | None = None, *, session_id: str = "test-session"
    ):
        self.state = FakeState(state or {})
        self.session = SimpleNamespace(id=session_id)

    async def save_artifact(self, *a: Any, **k: Any) -> None:
        return None


class _FakeBlob:
    def __init__(self, name: str, client: "FakeStorageClient"):
        self.name = name
        self._client = client

    def download_to_file(self, file_obj: Any) -> None:
        assert self._client.download_bytes is not None, (
            f"unexpected download: {self.name}"
        )
        file_obj.write(self._client.download_bytes)

    def upload_from_filename(self, path: str) -> None:
        # The scratch file must still exist on disk at upload time (a racing
        # rmtree would have deleted it under the old, shared-CWD code).
        assert os.path.exists(path), path
        self._client.uploads.append((self.name, path))
        with open(path, "rb") as f:
            self._client.contents[self.name] = f.read()


class _FakeBucket:
    def __init__(self, client: "FakeStorageClient"):
        self._client = client

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(name, self._client)


class FakeStorageClient:
    """GCS client double recording ``(object_name, local_path)`` per upload in
    ``uploads`` and the uploaded bytes by object name in ``contents``.

    ``download_bytes`` is what ``blob.download_to_file`` writes; left ``None``,
    any download fails the test (for tools that should only upload).
    """

    def __init__(
        self, uploads: list[tuple[str, str]], download_bytes: bytes | None = None
    ):
        self.uploads = uploads
        self.download_bytes = download_bytes
        self.contents: dict[str, bytes] = {}

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(self)
