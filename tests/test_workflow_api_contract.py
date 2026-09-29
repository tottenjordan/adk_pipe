"""Offline API-contract tests for google-adk graph Workflows.

These pin the upstream behaviors the P2 migration (SequentialAgent /
ParallelAgent -> ``google.adk.workflow.Workflow``; root LlmAgents keep calling
pipelines as tools) depends on. If an ADK upgrade changes any of them, the
migration's assumptions are invalid and these tests fail loudly first.

Fully offline: fake ``BaseAgent`` producers, plain-function nodes, and a stub
``BaseLlm`` returning canned ``LlmResponse``s, all driven through a real
``Runner`` over ``InMemorySessionService``. No model calls, no GCP, no quota.
Coroutines are driven with ``asyncio.run`` (no pytest-asyncio in this project).
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.context import Context
from google.adk.agents.invocation_context import InvocationContext
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools._node_tool import NodeTool
from google.adk.tools.agent_tool import AgentTool
from google.adk.workflow import BaseNode, JoinNode, Workflow
from google.genai import types
from pydantic import PrivateAttr

from agent_common import PipelineRequest
from tests._fakes import StubLlm, fc_response, text_response, user_message

APP = "contract_app"
USER = "u"


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class _Writer(BaseAgent):
    """Fake producer: writes ``key=value`` via ``state_delta``; counts runs."""

    key: str
    value: str = "v"
    # A shared mutable log, not an int counter: Workflow clones agent nodes
    # per run (``BaseAgent.clone`` -> shallow ``model_copy``), so an int
    # PrivateAttr would be incremented on the clone only. The shallow copy
    # shares this list, so the original instance observes every run.
    _log: list[int] = PrivateAttr(default_factory=list)

    @property
    def runs(self) -> int:
        return len(self._log)

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event]:
        self._log.append(1)
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            actions=EventActions(state_delta={self.key: self.value}),
        )


class _FlakyWriter(BaseAgent):
    """Writes ``""`` to ``key`` for the first ``fail_first`` runs, then ``value``."""

    key: str
    value: str = "REAL"
    fail_first: int = 2
    # A shared mutable log, not an int counter: Workflow clones agent nodes
    # per run (``BaseAgent.clone`` -> shallow ``model_copy``), so an int
    # PrivateAttr would be incremented on the clone only. The shallow copy
    # shares this list, so the original instance observes every run.
    _log: list[int] = PrivateAttr(default_factory=list)

    @property
    def runs(self) -> int:
        return len(self._log)

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event]:
        self._log.append(1)
        val = "" if self.runs <= self.fail_first else self.value
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            actions=EventActions(state_delta={self.key: val}),
        )


async def _run(runner: Runner, session_id: str, text: str) -> list[Event]:
    return [
        e
        async for e in runner.run_async(
            user_id=USER, session_id=session_id, new_message=user_message(text)
        )
    ]


async def _new_session(svc: InMemorySessionService) -> str:
    session = await svc.create_session(app_name=APP, user_id=USER)
    return session.id


async def _state(svc: InMemorySessionService, session_id: str) -> dict[str, Any]:
    session = await svc.get_session(app_name=APP, user_id=USER, session_id=session_id)
    assert session is not None
    return dict(session.state)


def _texts(events: list[Event]) -> list[str]:
    return [
        p.text
        for e in events
        if e.content and e.content.parts
        for p in e.content.parts
        if p.text
    ]


def _function_responses(events: list[Event]) -> list[types.FunctionResponse]:
    return [fr for e in events for fr in e.get_function_responses()]


def _decl_properties(decl: types.FunctionDeclaration) -> tuple[set[str], set[str]]:
    """(property names, required names) from either declaration schema form."""
    if decl.parameters_json_schema is not None:
        schema = decl.parameters_json_schema
        assert isinstance(schema, dict)
        return set(schema.get("properties", {})), set(schema.get("required", []))
    assert decl.parameters is not None
    return set(decl.parameters.properties or {}), set(decl.parameters.required or [])


def _run_node_workflow(node: BaseNode) -> tuple[list[Event], dict[str, Any]]:
    async def go() -> tuple[list[Event], dict[str, Any]]:
        svc = InMemorySessionService()
        runner = Runner(node=node, app_name=APP, session_service=svc)
        sid = await _new_session(svc)
        events = await _run(runner, sid, "go")
        return events, await _state(svc, sid)

    return asyncio.run(go())


# --------------------------------------------------------------------------
# 1. Fan-out + JoinNode
# --------------------------------------------------------------------------


def test_fan_out_join_runs_merge_once() -> None:
    """Fan-out (a, b) -> JoinNode -> merge runs merge exactly ONCE with both keys.

    The migration replaces ``ParallelAgent`` + a downstream merge agent
    (creative_agent's parallel planners -> merge_planners) with this shape. If
    the JoinNode barrier fired merge per-branch, or before both branches'
    state_deltas landed, merge would see a half-populated state.
    """
    a = _Writer(name="a", key="ka", value="A")
    b = _Writer(name="b", key="kb", value="B")
    seen: list[tuple[Any, Any]] = []
    join_inputs: list[Any] = []

    def merge(ctx: Context, node_input: Any) -> None:
        join_inputs.append(node_input)
        seen.append((ctx.state.get("ka"), ctx.state.get("kb")))

    wf = Workflow(name="wf", edges=[("START", (a, b), JoinNode(name="join"), merge)])
    _, state = _run_node_workflow(wf)

    assert seen == [("A", "B")]
    assert (a.runs, b.runs) == (1, 1)
    assert state["ka"] == "A" and state["kb"] == "B"
    # JoinNode output shape: a dict keyed by predecessor node name (fake agents
    # emit no ``output``, so the values are None).
    assert len(join_inputs) == 1
    assert isinstance(join_inputs[0], dict)
    assert set(join_inputs[0]) == {"a", "b"}


# --------------------------------------------------------------------------
# 2. Routing
# --------------------------------------------------------------------------


def _routing_workflow(route: str) -> tuple[_Writer, _Writer]:
    refine = _Writer(name="refine", key="refined", value="yes")
    compose = _Writer(name="compose", key="composed", value="yes")

    def gate() -> Event:
        return Event(route=route)

    wf = Workflow(
        name="wf",
        edges=[
            ("START", gate),
            (gate, {"refine": refine, "skip": compose}),
            (refine, compose),
        ],
    )
    _run_node_workflow(wf)
    return refine, compose


def test_route_skips_unselected_branch() -> None:
    """A routed edge map runs only the selected branch; the other is skipped.

    The migration replaces ``RunIfAgent`` (creative_agent's
    research_refinement_block, gated on degraded research) with a gate node
    emitting ``Event(route=...)``. Skip must bypass refine entirely yet still
    reach compose; the refine route must run refine then compose exactly once
    (compose has two inbound edges but must not double-fire).
    """
    refine, compose = _routing_workflow("skip")
    assert (refine.runs, compose.runs) == (0, 1)

    refine, compose = _routing_workflow("refine")
    assert (refine.runs, compose.runs) == (1, 1)


# --------------------------------------------------------------------------
# 3. Workflow in LlmAgent.tools -> NodeTool
# --------------------------------------------------------------------------


def test_workflow_in_llm_agent_tools_is_wrapped_as_nodetool() -> None:
    """A Workflow placed in ``LlmAgent.tools`` is canonicalized to a NodeTool
    whose declaration matches the ``AgentTool`` call shape (``request: str``).

    Root agents today call pipelines via ``AgentTool(agent=...)`` with a
    ``request`` string argument; prompts reference those tool names. The
    migration puts Workflows directly in ``tools=[...]``; the tool name,
    description and ``request`` parameter must survive unchanged.
    """
    wf = Workflow(
        name="wf",
        description="Runs the pipeline.",
        edges=[("START", _Writer(name="w", key="wk"))],
        input_schema=PipelineRequest,
    )
    root = LlmAgent(name="r", model=StubLlm(), tools=[wf])

    # Wrapped at construction (``LlmAgent._pre_validate_tools``), so ``tools``
    # already holds the NodeTool; canonical_tools() passes it through.
    assert isinstance(root.tools[0], NodeTool)
    tools = asyncio.run(root.canonical_tools())
    assert len(tools) == 1
    tool = tools[0]
    assert isinstance(tool, NodeTool)
    assert tool.node is wf

    decl = tool._get_declaration()
    assert decl is not None
    assert decl.name == "wf"
    assert decl.description == "Runs the pipeline."
    assert decl.parameters_json_schema is not None
    props, required = _decl_properties(decl)
    assert "request" in props and "request" in required
    assert decl.parameters_json_schema["properties"]["request"]["type"] == "string"

    # Today's shape: AgentTool over an LlmAgent without input_schema. Both
    # expose a required ``request`` property. Difference: NodeTool always emits
    # ``parameters_json_schema`` (pydantic JSON schema, incl. a ``title``);
    # AgentTool emits ``parameters_json_schema`` or genai ``parameters``
    # depending on the JSON_SCHEMA_FOR_FUNC_DECL feature flag.
    legacy = AgentTool(agent=LlmAgent(name="legacy", model=StubLlm(), description="d"))
    legacy_decl = legacy._get_declaration()
    legacy_props, legacy_required = _decl_properties(legacy_decl)
    assert "request" in legacy_props and "request" in legacy_required


# --------------------------------------------------------------------------
# 4. NodeTool from a root LlmAgent: completes iff the Workflow yields an output
# --------------------------------------------------------------------------


class _NonLongRunningNodeTool(NodeTool):
    """Candidate ``PipelineTool`` shape (NodeTool with is_long_running=False).

    Test-only: kept to prove that the long-running flag is NOT what stalls a
    no-output pipeline (see the characterization test below)."""

    def __init__(self, node: BaseNode) -> None:
        super().__init__(node=node)
        self.is_long_running = False


def _tool_root(
    *, with_output: bool, tool: Any = None
) -> tuple[LlmAgent, StubLlm, _Writer]:
    """Root LlmAgent whose only tool is a Workflow ``wf`` running a writer.

    ``with_output=True`` appends a terminal function node that returns a dict,
    giving the Workflow a non-None output (the tool's function response).
    """
    writer = _Writer(name="w", key="wk", value="v")

    def finish() -> dict[str, str]:
        return {"status": "done"}

    edges: list[Any] = (
        [("START", writer, finish)] if with_output else [("START", writer)]
    )
    wf = Workflow(
        name="wf", description="pipeline", edges=edges, input_schema=PipelineRequest
    )
    llm = StubLlm()
    root = LlmAgent(name="root", model=llm, tools=[tool(wf) if tool else wf])
    return root, llm, writer


def test_nodetool_from_plain_llm_root_does_not_pause() -> None:
    """Bare NodeTool (``is_long_running=True``) called from a plain (non-App)
    root LlmAgent completes the tool and re-calls the model in the same run,
    PROVIDED the Workflow yields a non-None output.

    creative_agent's root has no App wrapper; the tool must run the pipeline,
    land its state_delta in the PARENT session, and hand control back to the
    model. (A Workflow with no output stalls instead -- see
    ``test_nodetool_no_output_workflow_stalls_root``.)
    """
    root, llm, writer = _tool_root(with_output=True)
    llm.push(fc_response("wf", {"request": "go"}, "fc1"), text_response("DONE"))

    async def go() -> tuple[list[Event], dict[str, Any]]:
        svc = InMemorySessionService()
        runner = Runner(agent=root, app_name=APP, session_service=svc)
        sid = await _new_session(svc)
        events = await _run(runner, sid, "hi")
        return events, await _state(svc, sid)

    events, state = asyncio.run(go())
    assert llm.calls == 2
    assert writer.runs == 1
    assert "DONE" in _texts(events)
    assert state["wk"] == "v"
    (fr,) = _function_responses(events)
    assert fr.id == "fc1" and fr.response == {"status": "done"}


def test_nodetool_from_resumable_app_root_does_not_pause() -> None:
    """Bare NodeTool from a root inside ``App(resumability_config=is_resumable)``
    completes in-run (output-yielding Workflow), and a follow-up user message
    in the same session is processed normally (no leftover interrupt).

    trend_scout and interactive_creative are resumable Apps. If the
    long-running flag made the root pause (or left a dangling interrupt), the
    pipeline result would never reach the model and the next user turn could
    be swallowed as a "resume".
    """
    root, llm, writer = _tool_root(with_output=True)
    llm.push(fc_response("wf", {"request": "go"}, "fc1"), text_response("DONE"))
    app = App(
        name=APP,
        root_agent=root,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )

    async def go() -> tuple[list[Event], list[Event], dict[str, Any]]:
        svc = InMemorySessionService()
        runner = Runner(app=app, session_service=svc)
        sid = await _new_session(svc)
        first = await _run(runner, sid, "hi")
        llm.push(text_response("AGAIN"))
        second = await _run(runner, sid, "again")
        return first, second, await _state(svc, sid)

    first, second, state = asyncio.run(go())
    assert "DONE" in _texts(first)
    assert state["wk"] == "v"
    assert writer.runs == 1
    assert "AGAIN" in _texts(second)
    assert llm.calls == 3


@pytest.mark.parametrize("resumable", [False, True], ids=["plain", "resumable_app"])
@pytest.mark.parametrize(
    "tool", [None, _NonLongRunningNodeTool], ids=["bare_nodetool", "non_long_running"]
)
def test_nodetool_no_output_workflow_stalls_root(resumable: bool, tool: Any) -> None:
    """CHARACTERIZATION: a Workflow whose terminal node yields no output
    silently ENDS the root's turn when called as a NodeTool.

    Mechanism (ADK 2.10.0): NodeTool calls ``run_node(..., raise_on_wait=True)``;
    ``_dynamic_node_scheduler`` raises ``NodeInterruptedError`` when a child
    *Workflow* finishes with ``output is None``; NodeTool re-raises it, so no
    function response is produced and the model is never re-called. The
    pipeline's state_delta DOES land. Flipping ``is_long_running`` to False
    (a "PipelineTool") does NOT help -- so the migration rule is: every
    pipeline Workflow exposed as a tool must end in a node that yields an
    output (fake BaseAgents / custom wrappers that only write state do not).
    """
    root, llm, writer = _tool_root(with_output=False, tool=tool)
    llm.push(fc_response("wf", {"request": "go"}, "fc1"), text_response("DONE"))

    async def go() -> tuple[list[Event], dict[str, Any]]:
        svc = InMemorySessionService()
        if resumable:
            app = App(
                name=APP,
                root_agent=root,
                resumability_config=ResumabilityConfig(is_resumable=True),
            )
            runner = Runner(app=app, session_service=svc)
        else:
            runner = Runner(agent=root, app_name=APP, session_service=svc)
        sid = await _new_session(svc)
        events = await _run(runner, sid, "hi")
        return events, await _state(svc, sid)

    events, state = asyncio.run(go())
    assert writer.runs == 1
    assert state["wk"] == "v"
    assert llm.calls == 1  # model never re-called
    assert _function_responses(events) == []  # no FR for fc1
    assert "DONE" not in _texts(events)


# --------------------------------------------------------------------------
# 5. ctx.run_node re-executes with distinct run_ids
# --------------------------------------------------------------------------


class _RetryUntilOut(BaseNode):
    """Wrapper node: re-runs ``child`` (distinct run_id per attempt) until
    ``state['out']`` is non-empty, max 3 attempts. The minimal prototype of
    ``agent_common.RetryUntilKeyNode`` (which replaced the pre-P2
    ``RetryUntilKeyAgent``)."""

    child: BaseNode
    rerun_on_resume: bool = True
    input_schema: Any = None

    async def _run_impl(self, *, ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
        for n in range(1, 4):
            await ctx.run_node(
                self.child, node_input=node_input, run_id=f"{self.name}_attempt_{n}"
            )
            if ctx.state.get("out"):
                break
        yield {"attempts": n}


def _searcher_synth_pair() -> tuple[Workflow, _Writer, _FlakyWriter]:
    searcher = _Writer(name="searcher", key="raw", value="raw findings")
    synth = _FlakyWriter(name="synth", key="out", value="REAL", fail_first=2)
    pair = Workflow(name="pair", edges=[("START", searcher, synth)])
    return pair, searcher, synth


def test_run_node_reexecutes_with_distinct_run_ids() -> None:
    """``ctx.run_node(child, run_id=<distinct>)`` genuinely RE-EXECUTES the
    child each attempt (not a replay of the first result), and each attempt's
    state_delta is visible to the wrapper before it decides to retry.

    The migration ports ``RetryUntilKeyAgent`` (re-run a flaky
    searcher -> synthesizer pair until its output_key is populated) to a
    ``BaseNode`` wrapper; that only works if distinct run_ids force re-runs of
    the whole nested Workflow pair.
    """
    pair, searcher, synth = _searcher_synth_pair()
    wrapper = _RetryUntilOut(name="retry", child=pair)
    _, state = _run_node_workflow(wrapper)

    assert (searcher.runs, synth.runs) == (3, 3)
    assert state["out"] == "REAL"


def test_run_node_reexecutes_inside_nodetool_from_llm_root() -> None:
    """Same re-execution contract when the retry wrapper sits inside a
    Workflow invoked as a NodeTool from a root LlmAgent (the real topology:
    root agent -> pipeline tool -> retry-wrapped research pair)."""
    pair, searcher, synth = _searcher_synth_pair()
    wrapper = _RetryUntilOut(name="retry", child=pair)
    wf = Workflow(name="wf", edges=[("START", wrapper)], input_schema=PipelineRequest)
    llm = StubLlm()
    llm.push(fc_response("wf", {"request": "go"}, "fc1"), text_response("DONE"))
    root = LlmAgent(name="root", model=llm, tools=[wf])

    async def go() -> tuple[list[Event], dict[str, Any]]:
        svc = InMemorySessionService()
        runner = Runner(agent=root, app_name=APP, session_service=svc)
        sid = await _new_session(svc)
        events = await _run(runner, sid, "hi")
        return events, await _state(svc, sid)

    events, state = asyncio.run(go())
    assert (searcher.runs, synth.runs) == (3, 3)
    assert state["out"] == "REAL"
    assert "DONE" in _texts(events)
    assert llm.calls == 2


# --------------------------------------------------------------------------
# 6. NodeTool failure characterization
# --------------------------------------------------------------------------


def test_nodetool_failure_characterization() -> None:
    """A node raising inside a NodeTool-invoked Workflow is converted to an
    ``"Error running node ..."`` function response (the root keeps going), and
    a second call of the same tool re-runs the pipeline from the top.

    The migration relies on pipeline failures surfacing to the root model as
    tool errors (as AgentTool failures do today) rather than crashing the run.
    """
    first = _Writer(name="first", key="k1", value="v1")
    boom_calls: list[int] = []

    def boom(ctx: Context) -> dict[str, str]:
        boom_calls.append(1)
        if len(boom_calls) == 1:
            raise RuntimeError("kaboom")
        ctx.state["k2"] = "ok"
        # Non-None output: required for the tool to return (see section 4).
        return {"status": "ok"}

    wf = Workflow(
        name="wf", edges=[("START", first, boom)], input_schema=PipelineRequest
    )
    llm = StubLlm()
    llm.push(
        fc_response("wf", {"request": "one"}, "fc1"),
        fc_response("wf", {"request": "two"}, "fc2"),
        text_response("DONE"),
    )
    root = LlmAgent(name="root", model=llm, tools=[wf])

    async def go() -> tuple[list[Event], dict[str, Any]]:
        svc = InMemorySessionService()
        runner = Runner(agent=root, app_name=APP, session_service=svc)
        sid = await _new_session(svc)
        events = await _run(runner, sid, "hi")
        return events, await _state(svc, sid)

    events, state = asyncio.run(go())
    responses = {fr.id: fr for fr in _function_responses(events)}
    assert "Error running node" in str(responses["fc1"].response)
    assert "Error running node" not in str(responses["fc2"].response)
    assert "DONE" in _texts(events)
    assert llm.calls == 3
    assert len(boom_calls) == 2
    assert state["k2"] == "ok"
    # Characterization (ADK 2.10.0): the FIRST node ran only ONCE across both
    # tool calls. Both calls are in the same invocation; on the second call the
    # already-completed ``first`` (it wrote state) is REPLAYED, not re-run, and
    # only the node that failed (no output/state) runs again. Same rule as
    # BaseNode.retry_config's "children that produced output or state change
    # are replayed". See test_nodetool_repeat_call_replay_scope.
    assert first.runs == 1


def test_nodetool_repeat_call_replay_scope() -> None:
    """CHARACTERIZATION: calling the same pipeline tool twice in ONE
    invocation replays its completed nodes on the second call (they do not
    re-execute; the function responses are still produced); a call in a NEW
    invocation (next user message) re-executes everything.

    Migration impact: a root model that re-invokes a pipeline tool within the
    same turn (e.g. "try the research again") gets the cached node results,
    not a fresh run. Retry-until-populated logic must therefore live INSIDE
    the pipeline via ``ctx.run_node(..., run_id=<distinct>)`` (see
    test_run_node_reexecutes_with_distinct_run_ids), not rely on the root
    re-calling the tool.
    """
    first = _Writer(name="first", key="k1", value="v1")
    fin_calls: list[str] = []

    def fin(node_input: Any) -> dict[str, str]:
        fin_calls.append("x")
        return {"status": "ok"}

    wf = Workflow(
        name="wf", edges=[("START", first, fin)], input_schema=PipelineRequest
    )
    llm = StubLlm()
    root = LlmAgent(name="root", model=llm, tools=[wf])

    async def go() -> tuple[list[Event], list[Event]]:
        svc = InMemorySessionService()
        runner = Runner(agent=root, app_name=APP, session_service=svc)
        sid = await _new_session(svc)
        llm.push(
            fc_response("wf", {"request": "one"}, "fc1"),
            fc_response("wf", {"request": "two"}, "fc2"),
            text_response("DONE"),
        )
        turn1 = await _run(runner, sid, "hi")
        llm.push(fc_response("wf", {"request": "three"}, "fc3"), text_response("DONE2"))
        turn2 = await _run(runner, sid, "next")
        return turn1, turn2

    turn1, turn2 = asyncio.run(go())
    assert [fr.id for fr in _function_responses(turn1)] == ["fc1", "fc2"]
    assert all(fr.response == {"status": "ok"} for fr in _function_responses(turn1))
    assert "DONE2" in _texts(turn2)
    # Two calls in turn 1 -> 1 execution; one call in turn 2 -> +1.
    assert first.runs == 2
    assert len(fin_calls) == 2


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
