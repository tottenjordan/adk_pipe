"""trend_scout's root LLM history excludes replayed sub-agent transcripts.

Measurement (ADK 2.10, 2026-10-08), running the REAL ``trend_scout`` root with
stub models through gather (AgentTool) → understand_trends (NodeTool-run
Workflow) → pick (AgentTool) → final answer:

* The AgentTool sub-agents (``gather_trends_agent``, ``pick_trends_agent``)
  do NOT leak: AgentTool runs its agent in a fresh ``InMemorySessionService``
  session, so their turns never reach the parent session; only the function
  response does.
* The NodeTool-run ``understand_trends_agent_resilient`` DOES leak once a
  later call is the latest pair: the root's final request carried 2 foreign
  "For context: …" contents (``understand_trends_searcher`` and
  ``understand_trends_synthesizer`` said …) plus 2 replayed node inputs (the
  NodeTool's ``{"request": …}`` and the synthesizer's injected predecessor
  output, the searcher's raw findings) — 4 extra contents on top of the 7 that
  are the root's own (user message + three call/response pairs). With
  ``drop_other_agent_context`` first on the root: 0 foreign, 0 node inputs.

Everything the root needs downstream reaches it through function responses
(``pick_trends_agent``'s write-up, the ``review_trends`` checkpoint response)
or session state (``{interactive_trend_pick?}``, ``{research_gaps?}`` …), never
through the dropped transcripts — the trim keeps every call/response pair.
"""

import asyncio
from typing import Any

import pytest
from google.adk.models.llm_request import LlmRequest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent_common.history import drop_other_agent_context, is_other_agent_context
from tests._fakes import RecordingLlm, fc_response, text_response, user_message

_SEED: dict[str, Any] = {
    "brand": "Acme",
    "target_product": "Rocket Skates",
    "key_selling_points": "fast",
    "target_audience": "coyotes",
    "raw_gtrends": ["t1", "t2"],
}
_GATHERED = "GATHERED: t1, t2"
_RAW = "RAW FINDINGS t1"
_BRIEF = '{"analyzed_trends": ["t1"]}'
_PICKED = "### t1\nThe strategic bridge."
_PICK = {"status": "selected", "selected_trends": ["t1"], "instruction": "x"}


def _foreign_authors(request: LlmRequest) -> list[str]:
    """Authors of the "For context: …" contents (``[author] said:`` parts)."""
    return [
        (p.text or "").split("]", 1)[0].lstrip("[")
        for c in request.contents
        if is_other_agent_context(c)
        for p in (c.parts or [])[1:]
        if p.text and "] said:" in p.text
    ]


def _user_texts(request: LlmRequest) -> list[str]:
    return [
        p.text
        for c in request.contents
        if c.role == "user" and not is_other_agent_context(c)
        for p in c.parts or []
        if p.text
    ]


def _pairs(request: LlmRequest) -> tuple[list[str], list[str]]:
    parts = [p for c in request.contents for p in c.parts or []]
    calls = [p.function_call.name or "" for p in parts if p.function_call]
    responses = [p.function_response.name or "" for p in parts if p.function_response]
    return calls, responses


def _run(
    monkeypatch: pytest.MonkeyPatch, root_callback: Any, *, interactive: bool = False
) -> RecordingLlm:
    """Drive the real resumable trend_scout App (stub models) end to end."""
    import trend_scout.agent as ts

    graph = {n.name: n for n in ts.understand_trends_search_and_synthesize.graph.nodes}
    llms = {
        name: RecordingLlm() for name in ("root", "gather", "pick", "searcher", "synth")
    }
    agents = {
        "root": ts.root_agent,
        "gather": ts.gather_trends_agent,
        "pick": ts.pick_trends_agent,
        "searcher": graph["understand_trends_searcher"],
        "synth": graph["understand_trends_synthesizer"],
    }
    for name, agent in agents.items():
        monkeypatch.setattr(agent, "model", llms[name])
        monkeypatch.setattr(agent, "planner", None)
        if name != "root":
            monkeypatch.setattr(agent, "before_model_callback", None)
    for name in ("gather", "searcher"):  # no BigQuery / built-in google_search
        monkeypatch.setattr(agents[name], "tools", [])
    # The root's GCS/config-dependent instruction + state loader are out of scope.
    monkeypatch.setattr(ts.root_agent, "instruction", "orchestrate")
    monkeypatch.setattr(ts.root_agent, "before_agent_callback", None)
    monkeypatch.setattr(ts.root_agent, "before_model_callback", root_callback)

    llms["gather"].push(text_response(_GATHERED))
    llms["searcher"].push(text_response(_RAW))
    llms["synth"].push(text_response(_BRIEF))
    llms["pick"].push(text_response(_PICKED))
    root = llms["root"]
    root.push(fc_response("gather_trends_agent", {"request": "go"}, "fc1"))
    if interactive:
        root.push(fc_response("review_trends", {}, "fc_review"))
    root.push(
        fc_response("understand_trends_agent_resilient", {"request": "go"}, "fc2"),
        fc_response("pick_trends_agent", {"request": "go"}, "fc3"),
        text_response("ROOT DONE"),
    )

    async def go() -> None:
        svc = InMemorySessionService()
        runner = Runner(app=ts.app, session_service=svc)
        session = await svc.create_session(
            app_name="trend_scout", user_id="u", state=dict(_SEED)
        )

        async def turn(message: types.Content) -> None:
            async for _ in runner.run_async(
                user_id="u", session_id=session.id, new_message=message
            ):
                pass

        await turn(user_message("hi"))
        if interactive:
            assert root.calls == 2  # paused on the review_trends checkpoint
            resume = types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="fc_review", name="review_trends", response=_PICK
                        )
                    )
                ],
            )
            await turn(resume)

    asyncio.run(go())
    assert root.calls == (5 if interactive else 4)
    return root


def test_without_trim_only_the_nodetool_pipeline_leaks(monkeypatch):
    root = _run(monkeypatch, None)
    final = root.requests[-1]

    # NodeTool-run Workflow: both sub-agents' turns are replayed …
    assert _foreign_authors(final) == [
        "understand_trends_searcher",
        "understand_trends_synthesizer",
    ]
    # … and so are its node inputs (the NodeTool request, the injected raw findings).
    user_texts = _user_texts(final)
    assert user_texts == ["hi", '{"request": "go"}', _RAW]
    # AgentTool sub-agents run in their own session: nothing of theirs leaks
    # except their function responses.
    assert not any(a in _foreign_authors(final) for a in ("gather", "pick"))
    assert _GATHERED not in user_texts and _PICKED not in user_texts
    assert len(final.contents) == 7 + 4


def test_trim_keeps_only_the_roots_own_history(monkeypatch):
    root = _run(monkeypatch, drop_other_agent_context)

    for request in root.requests:
        assert not _foreign_authors(request)
    final = root.requests[-1]
    assert [c.role for c in final.contents] == ["user"] + ["model", "user"] * 3
    assert _user_texts(final) == ["hi"]
    calls, responses = _pairs(final)
    expected = [
        "gather_trends_agent",
        "understand_trends_agent_resilient",
        "pick_trends_agent",
    ]
    assert calls == responses == expected
    # The write-up the root summarizes arrives in pick's function response.
    assert _PICKED in str(final.contents[-1])


def test_trim_keeps_the_review_trends_checkpoint_response(monkeypatch):
    """Resumable interactive trend pick: pause on review_trends, resume with the
    human's pick; the checkpoint response survives the trim."""
    root = _run(monkeypatch, drop_other_agent_context, interactive=True)

    final = root.requests[-1]
    assert not _foreign_authors(final)
    assert _user_texts(final) == ["hi"]
    calls, responses = _pairs(final)
    expected = [
        "gather_trends_agent",
        "review_trends",
        "understand_trends_agent_resilient",
        "pick_trends_agent",
    ]
    assert calls == expected
    assert responses == expected
    review = next(
        p.function_response
        for c in final.contents
        for p in c.parts or []
        if p.function_response and p.function_response.name == "review_trends"
    )
    assert review.response == _PICK


def test_trend_scout_root_trims_before_rate_limiting():
    import trend_scout.agent as ts
    from trend_scout import callbacks

    assert ts.root_agent.canonical_before_model_callbacks == [
        drop_other_agent_context,
        callbacks.rate_limit_callback,
    ]
