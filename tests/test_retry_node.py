"""Tests for RetryUntilKeyNode — the graph-Workflow retry-on-empty wrapper.

Carries every case of the retired pre-P2 agent wrapper's suite (deleted in P2
T4) over to the ``BaseNode`` wrapper, using the shared fakes (tests/_fakes.py);
the split-producer child is a ``Workflow`` pair. Also pins the ``is_populated``
truth table, plus the node-only contracts: the wrapper always yields a
truthy output value (the populated key, or an exhaustion notice), so a root LlmAgent
calling it as a ``NodeTool`` never stalls (see
tests/test_workflow_api_contract.py, section 4).

Fully offline: fake producers + a stub ``BaseLlm`` driven through a real
``Runner`` over ``InMemorySessionService``. Coroutines are driven with
``asyncio.run`` (no pytest-asyncio in this project).
"""

import asyncio
import logging
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools._node_tool import NodeTool
from google.adk.workflow import BaseNode, Workflow
from pydantic import ValidationError

from agent_common import PipelineRequest, RetryUntilKeyNode, is_populated
from tests._fakes import (
    FlakyFlagProducer,
    FlakyProducer,
    FlakySynthesizer,
    RawSearcher,
    StubLlm,
    fc_response,
    text_response,
    user_message,
)

APP = "retry_node_test"
USER = "u"


def _run_node(
    node: BaseNode, state: dict[str, Any] | None = None
) -> tuple[list[Event], dict[str, Any]]:
    """Drive ``node`` once as the Runner's root node; return (events, state)."""

    async def go() -> tuple[list[Event], dict[str, Any]]:
        svc = InMemorySessionService()
        runner = Runner(node=node, app_name=APP, session_service=svc)
        session = await svc.create_session(app_name=APP, user_id=USER, state=state)
        events = [
            e
            async for e in runner.run_async(
                user_id=USER, session_id=session.id, new_message=user_message("go")
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
    # A one-node Workflow child: the node wraps any BaseNode, and a Workflow is
    # the realistic child.
    return Workflow(name="single", edges=[("START", producer)])


def _pair(fail_first: int) -> tuple[Workflow, RawSearcher, FlakySynthesizer]:
    searcher = RawSearcher(name="searcher", raw_key="report_raw")
    synth = FlakySynthesizer(
        name="synth", raw_key="report_raw", output_key="report", fail_first=fail_first
    )
    pair = Workflow(name="search_and_synthesize", edges=[("START", searcher, synth)])
    return pair, searcher, synth


# --------------------------------------------------------------------------
# Retry-on-empty contract (ported from the retired agent-wrapper suite)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("REAL", True),
        ("  x ", True),
        ("", False),
        ("   ", False),
        (True, True),  # the image-generation flag
        (False, False),
        (["k.png"], True),  # non-empty artifact-keys list
        ([], False),
        (0, False),
        (None, False),
    ],
)
def test_is_populated_accepts_truthy_non_strings(value, expected):
    """A non-blank string OR any truthy non-string counts as populated.

    Research producers write a non-blank string; the image producer writes a
    bool ``_images_generated`` flag. Falsy values (blank string, ``[]``, ``0``,
    ``False``, ``None``) must count as unpopulated so the wrapper retries.
    """
    assert is_populated(value) is expected


def test_recovers_after_empty_attempts():
    """Producer fails twice, succeeds on the third — wrapper retries and recovers."""
    producer = FlakyProducer(name="producer", output_key="report", fail_first=2)

    events, state = _run_node(_wrap(_single(producer), "report"))

    assert producer.runs == 3
    assert state.get("report") == "REAL_REPORT"
    assert "report__retry_exhausted" not in state
    assert _outputs(events, "retry_wrapper") == ["REAL_REPORT"]


def test_success_clears_a_stale_exhaustion_marker():
    """A marker left by an earlier run in the same session is cleared (set to
    None) once the producer succeeds, so it can't raise a stale degradation
    warning or route a needless refinement round."""
    producer = FlakyProducer(name="producer", output_key="report", fail_first=1)

    events, state = _run_node(
        _wrap(_single(producer), "report"), state={"report__retry_exhausted": True}
    )

    assert state.get("report") == "REAL_REPORT"
    assert "report__retry_exhausted" in state
    assert state["report__retry_exhausted"] is None
    assert _outputs(events, "retry_wrapper") == ["REAL_REPORT"]


def test_no_retry_when_first_attempt_succeeds():
    """A healthy producer runs exactly once — no wasted retries."""
    producer = FlakyProducer(name="producer", output_key="report", fail_first=0)

    _, state = _run_node(_wrap(_single(producer), "report"))

    assert producer.runs == 1
    assert state.get("report") == "REAL_REPORT"


def test_bounded_and_observable_when_never_populated(caplog):
    """Producer never populates — wrapper stops at max_attempts and logs loudly.

    The wrapper must NOT corrupt ``output_key`` with a placeholder (that would
    feed garbage to the downstream consumer): it leaves the key unset, records
    an observable ``<key>__retry_exhausted`` marker, and logs an error. It also
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
    """A whitespace-only final text is treated as unpopulated and retried
    (the full predicate truth table is pinned by
    ``test_is_populated_accepts_truthy_non_strings``)."""
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
    llm = StubLlm()
    llm.push(
        fc_response("research_resilient", {"request": "go"}, "fc1"),
        text_response("DONE"),
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
                user_id=USER, session_id=session.id, new_message=user_message("hi")
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


# --------------------------------------------------------------------------
# Transient errors count as a missed attempt (incident 2026-10-13: a 429 from
# campaign_web_searcher escaped the wrapper and failed creative_pipeline)
# --------------------------------------------------------------------------


def _quota_error() -> Exception:
    from google.genai import errors

    return errors.ClientError(
        429,
        {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}},
    )


def _bad_request() -> Exception:
    from google.genai import errors

    return errors.ClientError(
        400, {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}}
    )


def _server_error() -> Exception:
    from google.genai import errors

    return errors.ServerError(
        503, {"error": {"code": 503, "message": "down", "status": "UNAVAILABLE"}}
    )


def _raising_child(errors_to_raise: list[Exception], key: str = "report"):
    """A one-node Workflow whose producer raises the queued errors, then writes."""
    calls: list[int] = []

    def producer(ctx: Context) -> Event:
        calls.append(1)
        if errors_to_raise:
            raise errors_to_raise.pop(0)
        return Event(output="ok", actions=EventActions(state_delta={key: "REAL"}))

    return Workflow(name="raising", edges=[("START", producer)]), calls


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Record the wrapper's backoff waits instead of sleeping."""
    import agent_common.retry_node as rn

    waited: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(rn, "_sleep", fake_sleep)
    return waited


@pytest.mark.parametrize(
    "make_exc",
    [_quota_error, _server_error, lambda: TimeoutError("timed out")],
    ids=["429", "503", "timeout"],
)
def test_transient_error_then_success_recovers_without_marker(make_exc, sleeps):
    child, calls = _raising_child([make_exc()])

    events, state = _run_node(_wrap(child, "report"))

    assert len(calls) == 2
    assert state["report"] == "REAL"
    assert "report__retry_exhausted" not in state
    assert sleeps == [15.0]
    assert _outputs(events, "retry_wrapper") == ["REAL"]


def test_transient_classification():
    from google.adk.models.google_llm import _ResourceExhaustedError
    from google.genai import errors

    from agent_common.retry_node import is_transient_error

    quota = _quota_error()
    assert isinstance(quota, errors.ClientError)
    assert is_transient_error(_ResourceExhaustedError(quota))
    assert is_transient_error(quota)
    assert is_transient_error(_server_error())
    assert is_transient_error(TimeoutError())
    assert not is_transient_error(_bad_request())
    assert not is_transient_error(RuntimeError("bug"))
    assert not is_transient_error(KeyError("Context variable not found"))


def test_transient_error_on_every_attempt_exhausts_like_empty_output(sleeps, caplog):
    child, calls = _raising_child([_quota_error() for _ in range(3)])

    with caplog.at_level(logging.WARNING):
        events, state = _run_node(_wrap(child, "report"))

    assert len(calls) == 3
    assert state.get("report") is None
    assert state["report__retry_exhausted"] is True
    # Bounded backoff between attempts, none after the last one.
    assert sleeps == [15.0, 30.0]
    (notice,) = _outputs(events, "retry_wrapper")
    assert "report__retry_exhausted" in notice
    assert any("RESOURCE_EXHAUSTED" in r.getMessage() for r in caplog.records)


def test_backoff_is_capped(sleeps):
    child, _ = _raising_child([_quota_error() for _ in range(4)])
    wrapper = RetryUntilKeyNode(
        name="retry_wrapper", node=child, output_key="report", max_attempts=5
    )

    _, state = _run_node(wrapper)

    assert state["report"] == "REAL"
    assert sleeps == [15.0, 30.0, 30.0, 30.0]


@pytest.mark.parametrize(
    "make_exc", [_bad_request, lambda: RuntimeError("bug")], ids=["400", "runtime"]
)
def test_non_transient_error_still_propagates(make_exc, sleeps):
    from agent_common.fail_soft_node import root_cause

    exc = make_exc()
    child, calls = _raising_child([exc])

    with pytest.raises(Exception) as raised:
        _run_node(_wrap(child, "report"))

    assert root_cause(raised.value) is exc
    assert len(calls) == 1
    assert sleeps == []


def test_transient_retry_can_be_disabled(sleeps):
    child, calls = _raising_child([_quota_error()])
    wrapper = RetryUntilKeyNode(
        name="retry_wrapper",
        node=child,
        output_key="report",
        retry_on_transient=False,
    )

    with pytest.raises(Exception):
        _run_node(wrapper)

    assert len(calls) == 1
    assert sleeps == []
