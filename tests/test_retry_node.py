"""Tests for RetryUntilKeyNode — the graph-Workflow retry-on-empty wrapper.

One-for-one port of tests/test_retry_agent.py (``RetryUntilKeyAgent``) to the
``BaseNode`` wrapper the P2 migration uses, with the same fakes
(tests/_fakes.py). The split-producer child is a ``Workflow`` pair instead of a
``SequentialAgent``. Plus the node-only contracts: the wrapper always yields an
truthy output value (the populated key, or an exhaustion notice), so a root LlmAgent
calling it as a ``NodeTool`` never stalls (see
tests/test_workflow_api_contract.py, section 4).

Fully offline: fake producers + a stub ``BaseLlm`` driven through a real
``Runner`` over ``InMemorySessionService``. Coroutines are driven with
``asyncio.run`` (no pytest-asyncio in this project).
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools._node_tool import NodeTool
from google.adk.workflow import BaseNode, Workflow
from google.genai import types
from pydantic import PrivateAttr, ValidationError

from agent_common import PipelineRequest, RetryUntilKeyNode
from tests._fakes import (
    FlakyFlagProducer,
    FlakyProducer,
    FlakySynthesizer,
    RawSearcher,
)

APP = "retry_node_test"
USER = "u"


def _user(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def _run_node(node: BaseNode) -> tuple[list[Event], dict[str, Any]]:
    """Drive ``node`` once as the Runner's root node; return (events, state)."""

    async def go() -> tuple[list[Event], dict[str, Any]]:
        svc = InMemorySessionService()
        runner = Runner(node=node, app_name=APP, session_service=svc)
        session = await svc.create_session(app_name=APP, user_id=USER)
        events = [
            e
            async for e in runner.run_async(
                user_id=USER, session_id=session.id, new_message=_user("go")
            )
        ]
        final = await svc.get_session(app_name=APP, user_id=USER, session_id=session.id)
        assert final is not None
        return events, dict(final.state)

    return asyncio.run(go())


def _outputs(events: list[Event], author: str) -> list[Any]:
    return [e.output for e in events if e.author == author and e.output is not None]


def _wrap(child: BaseNode, key: str, name: str = "retry_wrapper") -> RetryUntilKeyNode:
    return RetryUntilKeyNode(name=name, node=child, output_key=key, max_attempts=3)


def _single(producer: BaseNode) -> Workflow:
    # A one-node Workflow child: the agent-wrapper tests wrapped a bare producer
    # agent; the node wraps any BaseNode, and a Workflow is the realistic child.
    return Workflow(name="single", edges=[("START", producer)])


def _pair(fail_first: int) -> tuple[Workflow, RawSearcher, FlakySynthesizer]:
    searcher = RawSearcher(name="searcher", raw_key="report_raw")
    synth = FlakySynthesizer(
        name="synth", raw_key="report_raw", output_key="report", fail_first=fail_first
    )
    pair = Workflow(name="search_and_synthesize", edges=[("START", searcher, synth)])
    return pair, searcher, synth


# --------------------------------------------------------------------------
# Ported from tests/test_retry_agent.py
# --------------------------------------------------------------------------


def test_recovers_after_empty_attempts():
    """Producer fails twice, succeeds on the third — wrapper retries and recovers."""
    producer = FlakyProducer(name="producer", output_key="report", fail_first=2)

    events, state = _run_node(_wrap(_single(producer), "report"))

    assert producer.runs == 3
    assert state.get("report") == "REAL_REPORT"
    assert "report__retry_exhausted" not in state
    assert _outputs(events, "retry_wrapper") == ["REAL_REPORT"]


def test_no_retry_when_first_attempt_succeeds():
    """A healthy producer runs exactly once — no wasted retries."""
    producer = FlakyProducer(name="producer", output_key="report", fail_first=0)

    _, state = _run_node(_wrap(_single(producer), "report"))

    assert producer.runs == 1
    assert state.get("report") == "REAL_REPORT"


def test_bounded_and_observable_when_never_populated(caplog):
    """Producer never populates — wrapper stops at max_attempts and logs loudly.

    Same contract as the agent: no placeholder in ``output_key``; an observable
    ``<key>__retry_exhausted`` marker + an error log. The node additionally
    yields a non-empty notice so a NodeTool caller still gets a response.
    """
    producer = FlakyProducer(name="producer", output_key="report", fail_first=99)

    with caplog.at_level(logging.ERROR):
        events, state = _run_node(_wrap(_single(producer), "report"))

    assert producer.runs == 3
    assert state.get("report") is None
    assert state.get("report__retry_exhausted") is True
    assert any("report" in r.message for r in caplog.records)
    (notice,) = _outputs(events, "retry_wrapper")
    assert "report__retry_exhausted" in notice


def test_whitespace_only_value_counts_as_empty():
    """A whitespace-only final text is treated as unpopulated and retried."""
    producer = FlakyProducer(
        name="producer", output_key="report", fail_first=2, empty_value="   \n"
    )

    _, state = _run_node(_wrap(_single(producer), "report"))

    assert producer.runs == 3
    assert state.get("report") == "REAL_REPORT"
    assert "report__retry_exhausted" not in state


def test_retries_workflow_pair_until_synthesizer_populates():
    """Each retry re-runs the WHOLE Workflow pair (searcher AND synthesizer) —
    a distinct ``run_id`` per attempt forces re-execution, not a replay."""
    pair, searcher, synth = _pair(fail_first=2)

    events, state = _run_node(_wrap(pair, "report"))

    assert searcher.runs == 3
    assert synth.runs == 3
    assert state.get("report") == "REAL_REPORT"
    assert state.get("report_raw") == "RAW_FINDINGS"
    assert "report__retry_exhausted" not in state
    assert _outputs(events, "retry_wrapper") == ["REAL_REPORT"]


def test_workflow_pair_exhaustion_is_observable():
    """When the synthesizer never populates, the wrapped pair stops at
    max_attempts, leaves the key unset, and records the retry-exhausted marker
    (so the degradation surfaces still fire on the split producer)."""
    pair, searcher, synth = _pair(fail_first=99)

    events, state = _run_node(_wrap(pair, "report"))

    assert searcher.runs == 3
    assert synth.runs == 3
    assert state.get("report") is None
    assert state.get("report__retry_exhausted") is True
    (notice,) = _outputs(events, "retry_wrapper")
    assert "report__retry_exhausted" in notice


def test_recovers_when_producer_writes_bool_flag():
    """Bool-flag producer fails once, then sets the flag — wrapper recovers."""
    producer = FlakyFlagProducer(
        name="imggen", output_key="_images_generated", fail_first=1
    )

    events, state = _run_node(
        _wrap(_single(producer), "_images_generated", name="imggen_resilient")
    )

    assert producer.runs == 2
    assert state.get("_images_generated") is True
    assert state.get("_images_generated__retry_exhausted") is None
    assert _outputs(events, "imggen_resilient") == [True]


def test_no_false_exhaustion_when_flag_set_first_try():
    """A healthy bool-flag producer runs exactly once — no false exhaustion."""
    producer = FlakyFlagProducer(
        name="imggen", output_key="_images_generated", fail_first=0
    )

    _, state = _run_node(
        _wrap(_single(producer), "_images_generated", name="imggen_resilient")
    )

    assert producer.runs == 1
    assert state.get("_images_generated") is True
    assert state.get("_images_generated__retry_exhausted") is None


@pytest.mark.parametrize("bad", [0, -1])
def test_max_attempts_must_be_positive(bad):
    """``max_attempts`` < 1 is a configuration error, rejected at construction."""
    pair, _, _ = _pair(fail_first=0)
    with pytest.raises(ValidationError):
        RetryUntilKeyNode(name="w", node=pair, output_key="report", max_attempts=bad)


# --------------------------------------------------------------------------
# NodeTool from a root LlmAgent (the real trend_scout topology)
# --------------------------------------------------------------------------


class _StubLlm(BaseLlm):
    """Scripted model: each call pops the next canned ``LlmResponse``."""

    model: str = "stub-model"
    _script: list[LlmResponse] = PrivateAttr(default_factory=list)
    _calls: int = PrivateAttr(default=0)

    @property
    def calls(self) -> int:
        return self._calls

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self._calls += 1
        assert self._script, f"stub model called unexpectedly (call #{self._calls})"
        yield self._script.pop(0)


def _fc(name: str, args: dict[str, Any], fc_id: str) -> LlmResponse:
    part = types.Part(function_call=types.FunctionCall(id=fc_id, name=name, args=args))
    return LlmResponse(content=types.Content(role="model", parts=[part]))


def _text(text: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=text)])
    )


def _run_as_tool(fail_first: int, resumable: bool = False):
    pair, searcher, synth = _pair(fail_first=fail_first)
    wrapper = RetryUntilKeyNode(
        name="research_resilient",
        description="Research the trends.",
        node=pair,
        output_key="report",
        max_attempts=3,
        input_schema=PipelineRequest,
    )
    llm = _StubLlm()
    llm._script.extend(
        [_fc("research_resilient", {"request": "go"}, "fc1"), _text("DONE")]
    )
    root = LlmAgent(name="root", model=llm, tools=[wrapper])

    async def go() -> tuple[list[Event], dict[str, Any]]:
        svc = InMemorySessionService()
        if resumable:
            # trend_scout's topology: the root lives in a resumable App.
            app = App(
                name=APP,
                root_agent=root,
                resumability_config=ResumabilityConfig(is_resumable=True),
            )
            runner = Runner(app=app, session_service=svc)
        else:
            runner = Runner(agent=root, app_name=APP, session_service=svc)
        session = await svc.create_session(app_name=APP, user_id=USER)
        events = [
            e
            async for e in runner.run_async(
                user_id=USER, session_id=session.id, new_message=_user("hi")
            )
        ]
        final = await svc.get_session(app_name=APP, user_id=USER, session_id=session.id)
        assert final is not None
        return events, dict(final.state)

    events, state = asyncio.run(go())
    texts = [
        p.text
        for e in events
        if e.content and e.content.parts
        for p in e.content.parts
        if p.text
    ]
    responses = [fr for e in events for fr in e.get_function_responses()]
    return root, llm, searcher, synth, texts, responses, state


def test_node_is_wrapped_as_nodetool_with_request_param():
    """A bare RetryUntilKeyNode in ``tools=[...]`` canonicalizes to a NodeTool
    with the AgentTool-shaped declaration (name, description, ``request: str``)."""
    root, *_ = _run_as_tool(fail_first=0)
    (tool,) = asyncio.run(root.canonical_tools())
    assert isinstance(tool, NodeTool)
    decl = tool._get_declaration()
    assert decl is not None
    assert decl.name == "research_resilient"
    assert decl.description == "Research the trends."
    schema = decl.parameters_json_schema
    assert isinstance(schema, dict)
    assert schema["required"] == ["request"]
    assert schema["properties"]["request"]["type"] == "string"
    # No developer docstring leaks into the model-facing parameters schema.
    assert "description" not in schema


@pytest.mark.parametrize("resumable", [False, True], ids=["plain", "resumable_app"])
def test_node_as_tool_recovers_without_stalling_root(resumable):
    """Retry inside a NodeTool: the pair re-runs until populated, the state
    lands in the PARENT session, and the root model is re-called (no stall)."""
    _, llm, searcher, synth, texts, responses, state = _run_as_tool(
        fail_first=2, resumable=resumable
    )

    assert (searcher.runs, synth.runs) == (3, 3)
    assert state["report"] == "REAL_REPORT"
    assert "report__retry_exhausted" not in state
    assert llm.calls == 2
    assert "DONE" in texts
    (fr,) = responses
    assert fr.id == "fc1"
    assert fr.response == {"result": "REAL_REPORT"}


@pytest.mark.parametrize("resumable", [False, True], ids=["plain", "resumable_app"])
def test_node_as_tool_exhaustion_does_not_stall_root(resumable):
    """Exhaustion still yields a truthy output (the notice), so the NodeTool
    returns a function response and the root keeps going — with the marker in
    state. (A falsy ``""`` would stall: ADK drops a long-running tool's falsy
    result instead of answering the call.)"""
    _, llm, searcher, synth, texts, responses, state = _run_as_tool(
        fail_first=99, resumable=resumable
    )

    assert (searcher.runs, synth.runs) == (3, 3)
    assert state.get("report") is None
    assert state["report__retry_exhausted"] is True
    assert llm.calls == 2
    assert "DONE" in texts
    (fr,) = responses
    assert fr.id == "fc1"
    assert "report__retry_exhausted" in str(fr.response)
