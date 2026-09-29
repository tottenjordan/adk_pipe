"""Offline end-to-end runs of creative_agent's graph-Workflow pipelines.

Drives the REAL ``creative_agent.agent.root_agent`` (real Workflow graphs,
RetryUntilKeyNode wrappers, instructions/state templating and callbacks) with a
scripted stub model per LlmAgent, so it checks what the structure tests can't:

* the two research chains fan out and both feed the JoinNode → barrier →
  merge_planners chain (a no-output mid-graph node still triggers successors);
* ``refinement_gate`` routes around the PRO evaluator on healthy research and
  through the retry-wrapped refinement round on degraded research;
* ``collect_research_sources_callback`` still harvests grounding metadata and
  ``combined_report_composer``'s ``citation_replacement_callback`` survives the
  graph clone AND fires on the node path (it sees the composer's output_key);
* every exposed pipeline returns a function response, so the root is
  re-called (never stalled).

Stubs are patched onto the Workflow's own graph nodes: a Workflow holds
per-graph copies of its agents, not the module-level objects.
"""

import asyncio
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.events.event import Event
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import PrivateAttr, ValidationError

from tests._fakes import StubLlm, fc_response, text_response, user_message

_URL = "https://example.com/trend"
_QUERIES = '{"queries": [{"search_query": "q"}]}'


class _RecordingLlm(StubLlm):
    _requests: list[LlmRequest] = PrivateAttr(default_factory=list)

    @property
    def requests(self) -> list[LlmRequest]:
        return self._requests

    async def generate_content_async(self, llm_request: LlmRequest, stream=False):
        self._requests.append(llm_request)
        async for r in super().generate_content_async(llm_request, stream):
            yield r


def _grounded(text: str) -> LlmResponse:
    """A searcher turn carrying google_search grounding metadata."""
    resp = text_response(text)
    resp.grounding_metadata = types.GroundingMetadata(
        grounding_chunks=[
            types.GroundingChunk(
                web=types.GroundingChunkWeb(
                    uri=_URL, title="Trend Source", domain="example.com"
                )
            )
        ]
    )
    return resp


def _llm_agents(node):
    """Every LlmAgent reachable from ``node`` (Workflow graphs, retry children)."""
    from google.adk.workflow import Workflow

    from agent_common import RetryUntilKeyNode

    if isinstance(node, LlmAgent):
        yield node
    elif isinstance(node, Workflow):
        for child in node.graph.nodes:
            yield from _llm_agents(child)
    elif isinstance(node, RetryUntilKeyNode):
        yield from _llm_agents(node.node)


def _stub_graph(monkeypatch: pytest.MonkeyPatch, workflow) -> dict[str, _RecordingLlm]:
    """Patch a stub model onto every LlmAgent node of ``workflow``; by name."""
    llms: dict[str, _RecordingLlm] = {}
    for agent in _llm_agents(workflow):
        llm = llms.setdefault(agent.name, _RecordingLlm())
        monkeypatch.setattr(agent, "model", llm)
        monkeypatch.setattr(agent, "planner", None)
        monkeypatch.setattr(agent, "tools", [])  # no built-in google_search
        monkeypatch.setattr(agent, "before_model_callback", None)
    return llms


_SEED = {
    "brand": "Acme",
    "target_product": "Rocket Skates",
    "target_audience": "Coyotes",
    "key_selling_points": "fast",
    "target_search_trends": "roadrunner",
    "sources": {},
    "url_to_short_id": {},
}


def _run_root(
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    extra_state: dict[str, Any] | None = None,
) -> tuple[_RecordingLlm, list[Event], dict[str, Any]]:
    """Run the real root once: it calls ``tool`` then finishes with text."""
    import creative_agent.agent as ca

    root_llm = _RecordingLlm()
    monkeypatch.setattr(ca.root_agent, "model", root_llm)
    monkeypatch.setattr(ca.root_agent, "instruction", "orchestrate")
    monkeypatch.setattr(ca.root_agent, "before_agent_callback", None)
    monkeypatch.setattr(ca.root_agent, "before_model_callback", None)
    root_llm.push(fc_response(tool, {"request": "go"}, "fc1"), text_response("DONE"))

    async def go() -> tuple[list[Event], dict[str, Any]]:
        svc = InMemorySessionService()
        runner = Runner(
            agent=ca.root_agent, app_name="creative_agent", session_service=svc
        )
        session = await svc.create_session(
            app_name="creative_agent",
            user_id="u",
            state={**_SEED, **(extra_state or {})},
        )
        events = [
            e
            async for e in runner.run_async(
                user_id="u", session_id=session.id, new_message=user_message("hi")
            )
        ]
        final = await svc.get_session(
            app_name="creative_agent", user_id="u", session_id=session.id
        )
        assert final is not None
        return events, dict(final.state)

    events, state = asyncio.run(go())
    return root_llm, events, state


def _responses(events: list[Event]) -> list[dict[str, Any]]:
    return [
        fr.response or {} for e in events for fr in e.get_function_responses() or []
    ]


def _script_research(llms: dict[str, _RecordingLlm], campaign_synth: list[str]):
    llms["gs_web_planner"].push(text_response(_QUERIES))
    llms["gs_web_searcher"].push(_grounded("gs raw"))
    llms["gs_web_synthesizer"].push(text_response("GS INSIGHTS"))
    llms["campaign_web_planner"].push(text_response(_QUERIES))
    for text in campaign_synth:
        llms["campaign_web_searcher"].push(text_response("ca raw"))
        llms["campaign_web_synthesizer"].push(text_response(text))
    llms["merge_planners"].push(text_response("MERGED BRIEF"))
    llms["combined_report_composer"].push(
        text_response('# Report\nRoadrunners are trending<cite source="src-1" />.')
    )


def test_research_graph_healthy_path_skips_refinement(monkeypatch):
    import creative_agent.agent as ca

    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(llms, ["CA INSIGHTS"])

    root_llm, events, state = _run_root(monkeypatch, "combined_research_pipeline")

    # Both branches ran, then the join → barrier → merge chain fired.
    assert state["gs_web_search_insights"] == "GS INSIGHTS"
    assert state["campaign_web_search_insights"] == "CA INSIGHTS"
    assert state["combined_web_search_insights"] == "MERGED BRIEF"
    merge_prompt = str(llms["merge_planners"].requests[-1].config.system_instruction)
    assert "GS INSIGHTS" in merge_prompt and "CA INSIGHTS" in merge_prompt
    # The barrier kept the JoinNode's {branch: output} dict out of its turn.
    merge_turn = str(llms["merge_planners"].requests[-1].contents)
    assert "GS INSIGHTS" not in merge_turn and "CA INSIGHTS" not in merge_turn
    # Healthy research: the gate skipped the PRO evaluator + refinement round.
    for name in (
        "combined_web_evaluator",
        "enhanced_combined_searcher",
        "refined_web_synthesizer",
    ):
        assert llms[name].calls == 0, name
    # Sources were harvested from the searcher's grounding metadata, and the
    # composer's citation callback fired on the node path.
    assert state["url_to_short_id"] == {_URL: "src-1"}
    assert "<cite" in state["combined_final_cited_report"]
    assert state["final_report_with_citations"] == (
        f"# Report\nRoadrunners are trending [Trend Source]({_URL})."
    )
    # The root got the terminal node's truthy result and was re-called.
    (response,) = _responses(events)
    assert "Research report complete" in str(response)
    assert root_llm.calls == 2


def test_research_graph_degraded_path_runs_refinement(monkeypatch):
    import creative_agent.agent as ca

    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    # The campaign synthesizer never produces → retry exhaustion → "refine".
    _script_research(llms, ["  ", "  ", "  "])
    llms["combined_web_evaluator"].push(
        text_response('{"finding_type": "Gap", "analysis_comment": "thin"}')
    )
    llms["enhanced_combined_searcher"].push(text_response("REFINED RAW"))
    llms["refined_web_synthesizer"].push(text_response("REFINED INSIGHTS"))

    root_llm, events, state = _run_root(monkeypatch, "combined_research_pipeline")

    assert llms["campaign_web_synthesizer"].calls == 3
    assert state["campaign_web_search_insights__retry_exhausted"] is True
    assert state["combined_research_evaluation"]["finding_type"] == "Gap"
    assert state["refined_web_search_insights"] == "REFINED INSIGHTS"
    composer_prompt = str(
        llms["combined_report_composer"].requests[-1].config.system_instruction
    )
    assert "REFINED INSIGHTS" in composer_prompt
    assert "final_report_with_citations" in state
    assert len(_responses(events)) == 1
    assert root_llm.calls == 2


# --------------------------------------------------------------------------
# Ad / visual pipelines
# --------------------------------------------------------------------------

_ADS = '{"ad_copies": []}'
_ADS_FINAL = '{"ad_copies": [{"id": 1, "tone_style": "Humorous"}]}'
_CONCEPTS = '{"visual_concepts": []}'


def test_ad_creative_graph_returns_final_copies(monkeypatch):
    import creative_agent.agent as ca

    llms = _stub_graph(monkeypatch, ca.ad_creative_pipeline)
    llms["ad_copy_drafter"].push(text_response(_ADS))
    # The critic's first turn is empty: the terminal node must still answer.
    llms["ad_copy_critic"].push(text_response("   "))

    root_llm, events, state = _run_root(
        monkeypatch,
        "ad_creative_pipeline",
        {"combined_final_cited_report": "# Report"},
    )

    assert state["ad_copy_draft"] == {"ad_copies": []}
    assert "ad_copy_critique" not in state
    (response,) = _responses(events)
    assert "ad_copy_critique" in str(response)  # the non-empty notice
    assert root_llm.calls == 2  # not stalled


def _fake_generate_image(tool_context) -> dict:
    """Stands in for creative_agent.tools.generate_image (same name + flag)."""
    tool_context.state["_images_generated"] = True
    return {"status": "ok"}


_fake_generate_image.__name__ = "generate_image"


def _script_visuals(llms: dict[str, _RecordingLlm], generator_turns: int):
    llms["art_director"].push(text_response("DIRECTION"))
    llms["visual_concept_drafter"].push(text_response(_CONCEPTS))
    llms["visual_concept_critic"].push(text_response(_CONCEPTS))
    llms["visual_concept_finalizer"].push(text_response(_CONCEPTS))
    # MALFORMED_FUNCTION_CALL stand-in: turns that never call the tool.
    for _ in range(generator_turns - 1):
        llms["visual_generator"].push(text_response(""))
    llms["visual_generator"].push(
        fc_response("generate_image", {}, "img1"), text_response("Rendered.")
    )


def test_visual_production_graph_retries_render_until_images(monkeypatch):
    import creative_agent.agent as ca

    llms = _stub_graph(monkeypatch, ca.visual_production_pipeline)
    (generator,) = [
        a
        for a in _llm_agents(ca.visual_production_pipeline)
        if a.name == "visual_generator"
    ]
    monkeypatch.setattr(generator, "tools", [_fake_generate_image])
    _script_visuals(llms, generator_turns=2)

    root_llm, events, state = _run_root(
        monkeypatch,
        "visual_production_pipeline",
        {"combined_final_cited_report": "# Report", "ad_copy_critique": _ADS_FINAL},
    )

    assert state["visual_direction"] == "DIRECTION"
    assert state["final_visual_concepts"] == {"visual_concepts": []}
    assert state["_images_generated"] is True
    assert "_images_generated__retry_exhausted" not in state
    # The retry node re-ran the bare LlmAgent: empty turn, then call + confirm.
    assert llms["visual_generator"].calls == 3
    # Reaching the generator at all proves render_barrier works: fed the
    # concepts dict directly, the retry node's PipelineRequest input_schema
    # would reject it (asserted below, independently of the run).
    wrapper = [
        n
        for n in ca.visual_production_pipeline.graph.nodes
        if n.name == "visual_generator_resilient"
    ][0]
    with pytest.raises(ValidationError):
        wrapper._validate_input_data({"visual_concepts": []})
    responses = _responses(events)
    assert responses[0] == {"status": "ok"}  # the inner generate_image call
    assert len(responses) == 2
    assert responses[-1]  # truthy pipeline result → root re-called
    assert root_llm.calls == 2
