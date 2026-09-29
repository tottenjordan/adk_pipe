"""Offline end-to-end run of trend_scout's understand_trends graph pipeline.

Drives the REAL ``trend_scout.agent.app`` (resumable App, real root agent, real
Workflow pair + RetryUntilKeyNode, real instructions/state templating) with
stub models swapped in, so it checks what the structure tests can't: the
single_turn searcher/synthesizer write their output_keys inside the graph, the
synthesizer reads the searcher's findings via ``{info_gtrends_raw?}``, retries
re-run the pair, and the root always gets a function response (no stall).

Stubs are patched onto the Workflow's own graph nodes: a Workflow holds
per-graph copies of its agents, not the module-level objects.
"""

import asyncio
from typing import Any

import pytest
from google.adk.events.event import Event
from google.adk.models.llm_request import LlmRequest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from pydantic import PrivateAttr

from tests._fakes import StubLlm, fc_response, text_response, user_message


class _RecordingLlm(StubLlm):
    _requests: list[LlmRequest] = PrivateAttr(default_factory=list)

    @property
    def requests(self) -> list[LlmRequest]:
        return self._requests

    async def generate_content_async(self, llm_request: LlmRequest, stream=False):
        self._requests.append(llm_request)
        async for r in super().generate_content_async(llm_request, stream):
            yield r


def _run(monkeypatch: pytest.MonkeyPatch, empty_synth_turns: int):
    import trend_scout.agent as ts

    graph = {n.name: n for n in ts.understand_trends_search_and_synthesize.graph.nodes}
    searcher = graph["understand_trends_searcher"]
    synth = graph["understand_trends_synthesizer"]
    root_llm = _RecordingLlm()
    search_llm = _RecordingLlm()
    synth_llm = _RecordingLlm()
    for agent, llm in ((ts.root_agent, root_llm), (searcher, search_llm)):
        monkeypatch.setattr(agent, "model", llm)
        monkeypatch.setattr(agent, "planner", None)
    monkeypatch.setattr(synth, "model", synth_llm)
    monkeypatch.setattr(searcher, "tools", [])  # no built-in google_search
    # The root's GCS/config-dependent instruction + state loader are out of scope.
    monkeypatch.setattr(ts.root_agent, "instruction", "orchestrate")
    monkeypatch.setattr(ts.root_agent, "before_agent_callback", None)
    for agent in (ts.root_agent, searcher, synth):
        monkeypatch.setattr(agent, "before_model_callback", None)

    root_llm.push(
        fc_response("understand_trends_agent_resilient", {"request": "go"}, "fc1"),
        text_response("ROOT DONE"),
    )
    for i in range(3):
        search_llm.push(text_response(f"RAW {i}"))
        brief = "   " if i < empty_synth_turns else '{"analyzed_trends": []}'
        synth_llm.push(text_response(brief))

    async def go() -> tuple[list[Event], dict[str, Any]]:
        svc = InMemorySessionService()
        runner = Runner(app=ts.app, session_service=svc)
        session = await svc.create_session(
            app_name="trend_scout", user_id="u", state={"raw_gtrends": "t1, t2"}
        )
        events = [
            e
            async for e in runner.run_async(
                user_id="u", session_id=session.id, new_message=user_message("hi")
            )
        ]
        final = await svc.get_session(
            app_name="trend_scout", user_id="u", session_id=session.id
        )
        assert final is not None
        return events, dict(final.state)

    events, state = asyncio.run(go())
    responses = [fr.response for e in events for fr in e.get_function_responses()]
    return root_llm, search_llm, synth_llm, responses, state


def test_understand_trends_graph_recovers_after_empty_synthesis(monkeypatch):
    root_llm, search_llm, synth_llm, responses, state = _run(monkeypatch, 2)

    assert (search_llm.calls, synth_llm.calls) == (3, 3)  # whole pair re-ran
    assert state["info_gtrends_raw"] == "RAW 2"
    assert state["info_gtrends"] == '{"analyzed_trends": []}'
    assert "info_gtrends__retry_exhausted" not in state
    assert responses == [{"result": '{"analyzed_trends": []}'}]
    assert root_llm.calls == 2
    # The synthesizer reads the searcher's findings through its state token.
    assert "RAW 2" in str(synth_llm.requests[-1].config.system_instruction)
    # The root's follow-up turn sees only the user turn, its own call and the
    # function response, not the pipeline's inner events (those live on the
    # tool's branch): no searcher/synthesizer text leaks into its context.
    contents = root_llm.requests[-1].contents
    kinds = [
        "function_call"
        if p.function_call
        else "function_response"
        if p.function_response
        else "text"
        for c in contents
        for p in c.parts or []
    ]
    assert kinds == ["text", "function_call", "function_response"]
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[0].parts and contents[0].parts[0].text == "hi"
    assert not any("RAW" in str(c) for c in contents)


def test_understand_trends_graph_exhaustion_does_not_stall_root(monkeypatch):
    root_llm, search_llm, synth_llm, responses, state = _run(monkeypatch, 99)

    assert (search_llm.calls, synth_llm.calls) == (3, 3)
    assert state["info_gtrends__retry_exhausted"] is True
    (response,) = responses
    assert "info_gtrends__retry_exhausted" in str(response)
    assert root_llm.calls == 2
