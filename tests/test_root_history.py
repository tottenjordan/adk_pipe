"""The orchestrator roots' LLM history excludes their pipelines' sub-agent turns.

Finding (ADK 2.10, 2026-10-07): a root agent runs on branch ``None``, and
``_is_event_belongs_to_branch(None, event)`` is True for EVERY event, so the
text turns the sub-agents inside a NodeTool-run Workflow write to the session
(planners, searchers, synthesizers, composer, brief writer, critics …, all on
branch ``<pipeline>@<fc id>``) are replayed into the root's request as
user-role "For context: … [agent] said: …" contents, and the graph nodes'
inputs (user-authored events on the node's branch: a single_turn agent's
injected predecessor output, the NodeTool's ``{"request": …}``) are replayed
verbatim as user turns. Only the LATEST
call/response pair hides its own interior (ADK rearranges the history around
the latest function response), so every earlier pipeline's turns pile up: the
creative_agent root's call after visual_production_pipeline carried the whole
research report, brief and ad copies (~33.5k prompt tokens in prod) and
returned empty turns. NodeTool has no isolation option (it runs the node on
``override_branch`` without an ``isolation_scope``), so the roots drop those
contents in a ``before_model_callback`` (agent_common.history).
"""

import asyncio
from types import SimpleNamespace

from google.adk.agents.llm_agent import _wrap_base_node_as_tool
from google.adk.artifacts import InMemoryArtifactService
from google.adk.events.event import Event
from google.adk.models.llm_request import LlmRequest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent_common.history import (
    OTHER_AGENT_CONTEXT_PREAMBLE,
    drop_other_agent_context,
    is_other_agent_context,
)
from tests._fakes import RecordingLlm, fc_response, text_response, user_message
from tests.test_creative_agent_graph import (  # noqa: F401 (autouse fixture)
    _ADS,
    _SEED,
    _fake_research_pdf,
    _final_ad,
    _final_ads,
    _script_research,
    _stub_graph,
)


def _foreign_texts(request: LlmRequest) -> list[str]:
    return [
        p.text or ""
        for c in request.contents
        if is_other_agent_context(c)
        for p in c.parts or []
    ]


def _user_texts(request: LlmRequest) -> list[str]:
    return [
        p.text
        for c in request.contents
        if c.role == "user" and not is_other_agent_context(c)
        for p in c.parts or []
        if p.text
    ]


def _run_two_pipelines(monkeypatch, root_callback) -> RecordingLlm:
    """The real creative root calls research, then ad copies, then answers."""
    import creative_agent.agent as ca

    for wf in (ca.combined_research_pipeline, ca.ad_creative_pipeline):
        llms = _stub_graph(monkeypatch, wf)
        if wf is ca.combined_research_pipeline:
            _script_research(llms, ["CA INSIGHTS"])
        else:
            llms["ad_copy_drafter"].push(text_response(_ADS))
            ads = [_final_ad(i) for i in range(1, 5)]
            llms["ad_copy_critic"].push(text_response(_final_ads(*ads)))
    root_llm = RecordingLlm()
    root_llm.push(
        fc_response("combined_research_pipeline", {"request": "go"}, "fc1"),
        fc_response("ad_creative_pipeline", {"request": "go"}, "fc2"),
        text_response("DONE"),
    )
    monkeypatch.setattr(
        ca.root_agent,
        "tools",
        [
            _wrap_base_node_as_tool(ca.combined_research_pipeline),
            _wrap_base_node_as_tool(ca.ad_creative_pipeline),
        ],
    )
    monkeypatch.setattr(ca.root_agent, "model", root_llm)
    monkeypatch.setattr(ca.root_agent, "instruction", "orchestrate")
    monkeypatch.setattr(ca.root_agent, "before_agent_callback", None)
    monkeypatch.setattr(ca.root_agent, "before_model_callback", root_callback)

    async def go() -> None:
        svc = InMemorySessionService()
        runner = Runner(
            agent=ca.root_agent,
            app_name="creative_agent",
            session_service=svc,
            artifact_service=InMemoryArtifactService(),
        )
        session = await svc.create_session(
            app_name="creative_agent", user_id="u", state=dict(_SEED)
        )
        async for _ in runner.run_async(
            user_id="u", session_id=session.id, new_message=user_message("hi")
        ):
            pass

    asyncio.run(go())
    assert root_llm.calls == 3
    return root_llm


def test_adk_replays_earlier_pipeline_sub_agent_turns_into_the_root(monkeypatch):
    """Without the trim, the root's third request quotes the research
    pipeline's sub-agents (the report, the brief …) — the bloat."""
    root_llm = _run_two_pipelines(monkeypatch, None)

    foreign = "\n".join(_foreign_texts(root_llm.requests[-1]))
    for author in (
        "gs_web_planner",
        "campaign_web_synthesizer",
        "combined_report_composer",
        "brief_writer",
    ):
        assert f"[{author}] said:" in foreign, author
    # The latest call's own interior (the ad copy agents) is hidden by ADK.
    assert "[ad_copy_critic]" not in foreign
    # The nodes' inputs come back as plain user turns: the NodeTool's request
    # and single_turn agents' injected predecessor outputs (here the report).
    user_texts = _user_texts(root_llm.requests[-1])
    assert '{"request": "go"}' in user_texts
    assert any(t.startswith("# Report") for t in user_texts)


def test_trim_keeps_only_the_roots_own_history(monkeypatch):
    root_llm = _run_two_pipelines(monkeypatch, drop_other_agent_context)

    for request in root_llm.requests:
        assert not _foreign_texts(request)
    contents = root_llm.requests[-1].contents
    # The user's message + the root's two call/response pairs, nothing else.
    assert [c.role for c in contents] == ["user", "model", "user", "model", "user"]
    assert contents[0].parts[0].text == "hi"
    calls = [p.function_call.name for c in contents for p in c.parts if p.function_call]
    responses = [
        p.function_response.name
        for c in contents
        for p in c.parts
        if p.function_response
    ]
    assert calls == responses == ["combined_research_pipeline", "ad_creative_pipeline"]


# --- drop_other_agent_context (unit) --- #


def _request(*contents: types.Content) -> LlmRequest:
    return LlmRequest(contents=list(contents))


def _ctx(*events: Event, branch: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(session=SimpleNamespace(events=list(events)), branch=branch)


def _user_event(text: str, branch: str | None = None) -> Event:
    return Event(author="user", branch=branch, content=_text("user", text))


def _text(role: str, *texts: str) -> types.Content:
    return types.Content(role=role, parts=[types.Part(text=t) for t in texts])


def _foreign(author: str = "brief_writer", text: str = "x") -> types.Content:
    return _text("user", OTHER_AGENT_CONTEXT_PREAMBLE, f"[{author}] said:\n{text}")


def test_drop_removes_only_other_agent_context_contents():
    fc = types.Content(
        role="model",
        parts=[types.Part(function_call=types.FunctionCall(name="p", args={}))],
    )
    fr = types.Content(
        role="user",
        parts=[
            types.Part(function_response=types.FunctionResponse(name="p", response={}))
        ],
    )
    user = _text("user", "Brand: Acme")
    reply = _text("model", "Done.")
    request = _request(user, fc, _foreign(), _foreign("gs_web_planner"), fr, reply)

    # Never short-circuits the model call.
    assert drop_other_agent_context(_ctx(), request) is None
    assert request.contents == [user, fc, fr, reply]


def test_drop_removes_node_inputs_written_on_sub_branches():
    branch = "combined_research_pipeline@fc1"
    events = (
        _user_event("Brand: Acme"),
        _user_event('{"request": "go"}', branch),
        _user_event("# Report", f"{branch}.brief_writer@1"),
    )
    user = _text("user", "Brand: Acme")
    request = _request(
        user, _text("user", '{"request": "go"}'), _text("user", "# Report")
    )

    drop_other_agent_context(_ctx(*events), request)

    assert request.contents == [user]


def test_user_message_equal_to_a_node_input_is_kept():
    # The same text on the root's own branch is a real user turn.
    events = (_user_event("go"), _user_event("go", "p@fc1"))
    request = _request(_text("user", "go"))

    drop_other_agent_context(_ctx(*events), request)

    assert request.contents == [_text("user", "go")]


def test_model_turn_equal_to_a_node_input_is_kept():
    events = (_user_event("Done.", "p@fc1"),)
    reply = _text("model", "Done.")
    request = _request(reply)

    drop_other_agent_context(_ctx(*events), request)

    assert request.contents == [reply]


def test_user_text_that_merely_mentions_the_preamble_is_kept():
    # Only a content whose FIRST part is exactly ADK's preamble is foreign.
    quoted = _text("user", "Why did it say: " + OTHER_AGENT_CONTEXT_PREAMBLE)
    later = _text("user", "first", OTHER_AGENT_CONTEXT_PREAMBLE)
    model = _text("model", OTHER_AGENT_CONTEXT_PREAMBLE)
    empty = types.Content(role="user", parts=[])
    request = _request(quoted, later, model, empty)

    drop_other_agent_context(_ctx(), request)

    assert request.contents == [quoted, later, model, empty]


def test_both_roots_trim_before_rate_limiting():
    import creative_agent.agent as ca
    import interactive_creative.agent as ic
    from creative_agent import callbacks

    for root in (ca.root_agent, ic.root_agent):
        assert root.canonical_before_model_callbacks == [
            drop_other_agent_context,
            callbacks.rate_limit_callback,
        ], root.name
