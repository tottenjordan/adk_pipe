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
  re-called (never stalled), even when a research fan-out branch raises.
* the trend-motif/product guard (``ensure_trend_and_product_callback``) on the
  finalizer and interactive's reviser rewrites ``final_visual_concepts`` so the
  prompt ``generate_image`` reads is the repaired one.

Stubs are patched onto the Workflow's own graph nodes: a Workflow holds
per-graph copies of its agents, not the module-level objects.
"""

import asyncio
import inspect
import json
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.agents.llm_agent import _wrap_base_node_as_tool
from google.adk.artifacts import InMemoryArtifactService
from google.adk.events.event import Event
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow._base_node import BaseNode
from google.genai import types
from pydantic import ValidationError

from creative_agent.brief_render import render_brief_markdown
from creative_eval.schemas import (
    AdCopyEvaluation,
    CreativeScore,
    VisualConceptEvaluation,
)
from tests._fake_bq import FakeBigQueryClient
from tests._fakes import (
    FakeStorageClient,
    FakeToolContext,
    RecordingLlm,
    fc_response,
    text_response,
    user_message,
    walk_nodes,
)
from tests.test_creative_eval import SAMPLE_AD_COPIES, SAMPLE_VISUAL_CONCEPTS

_URL = "https://example.com/trend"
_QUERIES = '{"queries": [{"search_query": "q"}]}'


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


def _llm_agents(node) -> list[LlmAgent]:
    """Every LlmAgent reachable from ``node`` (Workflow graphs, retry children)."""
    return [n for n in walk_nodes(node) if isinstance(n, LlmAgent)]


def _stub_graph(monkeypatch: pytest.MonkeyPatch, workflow) -> dict[str, RecordingLlm]:
    """Patch a stub model onto every LlmAgent node of ``workflow``; by name."""
    llms: dict[str, RecordingLlm] = {}
    for agent in _llm_agents(workflow):
        llm = llms.setdefault(agent.name, RecordingLlm())
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
    root_tools: list[Any] | None = None,
) -> tuple[RecordingLlm, list[Event], dict[str, Any]]:
    """Run the real root once: it calls ``tool`` then finishes with text.

    ``root_tools`` (graph nodes are wrapped as NodeTools) replaces the root's
    tool list for this run; by default it is just the ``creative_agent.agent``
    pipeline named ``tool`` (the production root only carries creative_pipeline,
    so a single stage is exposed directly to test it in isolation).
    """
    import creative_agent.agent as ca

    root_llm = RecordingLlm()
    if root_tools is None:
        root_tools = [getattr(ca, tool)]
    if root_tools:
        monkeypatch.setattr(
            ca.root_agent,
            "tools",
            [
                _wrap_base_node_as_tool(t) if isinstance(t, BaseNode) else t
                for t in root_tools
            ],
        )
    monkeypatch.setattr(ca.root_agent, "model", root_llm)
    monkeypatch.setattr(ca.root_agent, "instruction", "orchestrate")
    monkeypatch.setattr(ca.root_agent, "before_agent_callback", None)
    monkeypatch.setattr(ca.root_agent, "before_model_callback", None)
    root_llm.push(fc_response(tool, {"request": "go"}, "fc1"), text_response("DONE"))

    async def go() -> tuple[list[Event], dict[str, Any]]:
        svc = InMemorySessionService()
        runner = Runner(
            agent=ca.root_agent,
            app_name="creative_agent",
            session_service=svc,
            artifact_service=InMemoryArtifactService(),
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


_PDF_CALLS: list[str] = []


@pytest.fixture(autouse=True)
def _fake_research_pdf(monkeypatch):
    """Keep the research pipeline's PDF save offline: a recording stand-in for
    gcs_tools.save_draft_report_artifact (tests needing the real one restore
    ``_REAL_SAVE_PDF``)."""
    from creative_agent import gcs_tools

    _PDF_CALLS.clear()

    async def fake_save_pdf(ctx) -> dict:
        _PDF_CALLS.append(ctx.state["final_report_with_citations"])
        ctx.state["research_report_gcs_uri"] = "gs://bucket/report.pdf"
        return {"status": "success"}

    monkeypatch.setattr(gcs_tools, "save_draft_report_artifact", fake_save_pdf)


def _real_save_pdf():
    import creative_agent.tools as t

    return t.save_draft_report_artifact  # the tools re-export keeps the original


def _responses(events: list[Event]) -> list[dict[str, Any]]:
    return [
        fr.response or {} for e in events for fr in e.get_function_responses() or []
    ]


# A schema-valid CreativeBrief that also passes creative_agent.brief_check.
_BRIEF: dict[str, Any] = {
    "objective": "Make Rocket Skates the coyote's go-to chase gear this week.",
    "audience": "Coyotes who chase roadrunners for sport.",
    "insight": "Coyotes want to win the chase, but every gadget backfires.",
    "single_minded_proposition": "Rocket Skates finally make you faster.",
    "reasons_to_believe": [
        {"claim": "Fast, per the brief", "source_id": "brief"},
        {"claim": "Roadrunners are trending", "source_id": "src-1"},
    ],
    "brand": {
        "tone_of_voice": "Deadpan slapstick confidence",
        "distinctive_assets": ["the ACME crate"],
        "do_not": ["mock the customer"],
    },
    "trend_bridge": {
        "fit_score": 4,
        "fit_mode": "direct",
        "bridge": "Skate speed meets the roadrunner's signature sprint.",
        "motifs": ["a roadrunner dust cloud", "desert mesa road"],
        "risks": ["cartoon violence"],
    },
    "mandatories": ["show the ACME logo"],
    "avoid": ["cliff falls"],
    "desired_response": "Think fast, feel hopeful, order skates.",
    "angles": [
        {"angle_id": "A1", "name": "Finally fast", "tension": "t1", "route": "r1"},
        {"angle_id": "A2", "name": "Gear that works", "tension": "t2", "route": "r2"},
        {"angle_id": "A3", "name": "Chase as sport", "tension": "t3", "route": "r3"},
    ],
}


def _script_research(
    llms: dict[str, RecordingLlm],
    campaign_synth: list[str],
    briefs: list[str] | None = None,
    report: str = '# Report\nRoadrunners are trending<cite source="src-1" />.',
):
    llms["gs_web_planner"].push(text_response(_QUERIES))
    llms["gs_web_searcher"].push(_grounded("gs raw"))
    llms["gs_web_synthesizer"].push(text_response("GS INSIGHTS"))
    llms["campaign_web_planner"].push(text_response(_QUERIES))
    for text in campaign_synth:
        llms["campaign_web_searcher"].push(text_response("ca raw"))
        llms["campaign_web_synthesizer"].push(text_response(text))
    llms["merge_planners"].push(text_response("MERGED BRIEF"))
    llms["combined_report_composer"].push(text_response(report))
    for brief in [json.dumps(_BRIEF)] if briefs is None else briefs:
        llms["brief_writer"].push(text_response(brief))


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
    # The structured brief was written from the report and stored as a dict.
    assert state["creative_brief"] == _BRIEF
    assert llms["brief_writer"].calls == 1
    brief_prompt = str(llms["brief_writer"].requests[-1].config.system_instruction)
    assert "Roadrunners are trending" in brief_prompt
    assert not state.get("creative_brief__retry_exhausted")
    # The gate wrote the compact Markdown the creative agents read.
    assert state["creative_brief_md"].startswith("**Single-minded proposition:**")
    assert _BRIEF["single_minded_proposition"] in state["creative_brief_md"]
    # The brief passed the deterministic check: no revision round.
    assert llms["brief_reviser"].calls == 0
    assert state["brief_issues"] == ""
    assert state.get("creative_brief__issues") is None
    assert state["brief_revision_rounds_used"] == 0
    # The research PDF was saved once, on the gate's "ok" exit.
    assert _PDF_CALLS == [state["final_report_with_citations"]]
    assert state["research_report_gcs_uri"] == "gs://bucket/report.pdf"
    # The root got the terminal node's truthy result and was re-called.
    (response,) = _responses(events)
    assert "Research report complete" in str(response)
    assert "gs://bucket/report.pdf" in str(response)
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


def test_brief_writer_runs_without_a_report(monkeypatch):
    """Missing report: the writer still runs from the campaign inputs (the
    documented choice), so the creative stages keep a structured contract."""
    import creative_agent.agent as ca

    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(llms, ["CA INSIGHTS"], report="   ")

    root_llm, events, state = _run_root(monkeypatch, "combined_research_pipeline")

    assert (
        "combined_final_cited_report" not in state
        or not str(state["combined_final_cited_report"]).strip()
    )
    assert llms["brief_writer"].calls == 1
    prompt = str(llms["brief_writer"].requests[-1].config.system_instruction)
    assert "Rocket Skates" in prompt and "roadrunner" in prompt
    assert state["creative_brief"] == _BRIEF
    (response,) = _responses(events)
    assert "combined_final_cited_report" in str(response)  # the missing notice
    assert root_llm.calls == 2


def test_brief_writer_exhaustion_still_ends_truthy(monkeypatch):
    """Both writer attempts come back empty: the brief stays unset, the marker
    is recorded, and the pipeline still answers the root."""
    import creative_agent.agent as ca

    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(llms, ["CA INSIGHTS"], briefs=["", ""])

    root_llm, events, state = _run_root(monkeypatch, "combined_research_pipeline")

    assert llms["brief_writer"].calls == 2
    assert not state.get("creative_brief")
    assert state["creative_brief__retry_exhausted"] is True
    # Nothing to revise: the gate skips the reviser; the exhaustion is a
    # human-readable degradation note.
    assert llms["brief_reviser"].calls == 0
    from agent_common import collect_degradation_warnings

    assert state["creative_brief_md"] == ""
    (note,) = collect_degradation_warnings(state)
    assert note == "Step 'creative_brief' exhausted retries and produced no output."
    (response,) = _responses(events)
    assert "Research report complete" in str(response)
    assert root_llm.calls == 2


def _brief_with(**changes: Any) -> str:
    return json.dumps({**_BRIEF, **changes})


def test_brief_failing_the_check_is_revised_once(monkeypatch):
    """A brief whose insight has no tension is sent to the reviser once, with
    the gate's exact issue text in its prompt; the revised (clean) brief
    replaces it after the gate re-checks it."""
    import creative_agent.agent as ca

    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(
        llms, ["CA INSIGHTS"], briefs=[_brief_with(insight="Coyotes like gear.")]
    )
    llms["brief_reviser"].push(text_response(json.dumps(_BRIEF)))

    root_llm, events, state = _run_root(monkeypatch, "combined_research_pipeline")

    assert llms["brief_writer"].calls == 1
    assert llms["brief_reviser"].calls == 1
    prompt = str(llms["brief_reviser"].requests[-1].config.system_instruction)
    assert "- insight has no tension ('Coyotes like gear.')" in prompt
    assert "Coyotes like gear." in prompt  # the previous brief is shown too
    assert state["creative_brief"] == _BRIEF
    assert state["brief_revision_rounds_used"] == 1
    assert state["brief_issues"] == ""  # cleared once the round is over
    assert state.get("creative_brief__issues") is None
    # The re-checked (clean) brief is what the creative agents will read.
    assert "Coyotes like gear." not in state["creative_brief_md"]
    (response,) = _responses(events)
    assert "Research report complete" in str(response)
    assert root_llm.calls == 2


def test_brief_issues_left_after_revision_are_recorded(monkeypatch):
    """With the default budget (1 pass) the gate re-checks the revised brief
    and records what is left as creative_brief__issues (a degradation note)."""
    import creative_agent.agent as ca
    from agent_common import collect_degradation_warnings

    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    still_bad = _brief_with(single_minded_proposition="Fast and fun skates.")
    _script_research(llms, ["CA INSIGHTS"], briefs=[still_bad])
    llms["brief_reviser"].push(text_response(still_bad))

    root_llm, _, state = _run_root(monkeypatch, "combined_research_pipeline")

    assert llms["brief_reviser"].calls == 1
    (issue,) = state["creative_brief__issues"]
    assert "'and'" in issue
    (note,) = collect_degradation_warnings(state)
    assert note.startswith("Creative brief has unresolved issues: 1 (e.g. ")
    assert root_llm.calls == 2


def test_brief_revised_twice_with_two_rounds(monkeypatch):
    """BRIEF_REVISION_ROUNDS=2 really means two reviser passes: the reviser
    loops back to the gate, which sends the still-failing brief round again
    (with the remaining issue in the prompt) and accepts the fixed one."""
    import creative_agent.agent as ca

    monkeypatch.setattr(ca.config, "brief_revision_rounds", 2)
    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    no_tension = _brief_with(insight="Coyotes like gear.")
    two_ideas = _brief_with(single_minded_proposition="Fast and fun skates.")
    _script_research(llms, ["CA INSIGHTS"], briefs=[no_tension])
    llms["brief_reviser"].push(
        text_response(two_ideas), text_response(json.dumps(_BRIEF))
    )

    root_llm, events, state = _run_root(monkeypatch, "combined_research_pipeline")

    assert llms["brief_reviser"].calls == 2
    first, second = (
        str(r.config.system_instruction) for r in llms["brief_reviser"].requests
    )
    assert "- insight has no tension ('Coyotes like gear.')" in first
    assert (
        "- single_minded_proposition joins ideas with 'and' ('Fast and fun skates.')"
        in second
    )
    assert state["creative_brief"] == _BRIEF
    assert state["brief_revision_rounds_used"] == 2
    assert state.get("creative_brief__issues") is None
    (response,) = _responses(events)
    assert "Research report complete" in str(response)
    assert root_llm.calls == 2


def test_brief_revision_stops_at_the_budget_of_two(monkeypatch):
    import creative_agent.agent as ca

    monkeypatch.setattr(ca.config, "brief_revision_rounds", 2)
    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    bad = _brief_with(insight="Coyotes like gear.")
    _script_research(llms, ["CA INSIGHTS"], briefs=[bad])
    llms["brief_reviser"].push(text_response(bad), text_response(bad))

    root_llm, _, state = _run_root(monkeypatch, "combined_research_pipeline")

    assert llms["brief_reviser"].calls == 2
    assert state["brief_revision_rounds_used"] == 2
    (issue,) = state["creative_brief__issues"]
    assert issue.startswith("insight has no tension")
    assert root_llm.calls == 2


def _patch_agent_model(monkeypatch, workflow, name: str, model) -> None:
    (agent,) = [a for a in _llm_agents(workflow) if a.name == name]
    monkeypatch.setattr(agent, "model", model)


def test_raising_brief_writer_does_not_fail_the_research_step(monkeypatch):
    """An exception in the writer (e.g. SCHEMA_RETRY exhausted) is fail-soft:
    no brief, the exhaustion marker, and the report still reaches the root."""
    import creative_agent.agent as ca

    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    boom = _BoomLlm()
    _patch_agent_model(monkeypatch, ca.combined_research_pipeline, "brief_writer", boom)
    _script_research(llms, ["CA INSIGHTS"])

    root_llm, events, state = _run_root(monkeypatch, "combined_research_pipeline")

    assert boom.requests  # the writer really ran and raised
    assert state["combined_final_cited_report"].startswith("# Report")
    assert not state.get("creative_brief")
    assert state["creative_brief_md"] == ""
    assert state["creative_brief__retry_exhausted"] is True
    assert llms["brief_reviser"].calls == 0
    (response,) = _responses(events)
    assert "Research report complete" in str(response)
    assert "No structured creative brief" in str(response)
    assert root_llm.calls == 2


def test_raising_brief_reviser_keeps_the_pre_revision_brief(monkeypatch):
    """An exception in the reviser keeps the writer's brief, records the gate's
    issues, and the pipeline still ends with the report."""
    import creative_agent.agent as ca

    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    boom = _BoomLlm()
    _patch_agent_model(
        monkeypatch, ca.combined_research_pipeline, "brief_reviser", boom
    )
    bad = _brief_with(insight="Coyotes like gear.")
    _script_research(llms, ["CA INSIGHTS"], briefs=[bad])

    root_llm, events, state = _run_root(monkeypatch, "combined_research_pipeline")

    assert boom.requests  # the reviser really ran and raised
    assert state["creative_brief"] == json.loads(bad)
    assert "Coyotes like gear." in state["creative_brief_md"]
    (issue,) = state["creative_brief__issues"]
    assert issue.startswith("insight has no tension")
    assert state["brief_issues"] == ""
    (response,) = _responses(events)
    assert "Research report complete" in str(response)
    assert root_llm.calls == 2


def test_brief_revision_disabled_records_issues(monkeypatch):
    import creative_agent.agent as ca

    monkeypatch.setattr(ca.config, "brief_revision_rounds", 0)
    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(
        llms, ["CA INSIGHTS"], briefs=[_brief_with(insight="Coyotes like gear.")]
    )

    _, _, state = _run_root(monkeypatch, "combined_research_pipeline")

    assert llms["brief_reviser"].calls == 0
    assert len(state["creative_brief__issues"]) == 1


class _BoomLlm(RecordingLlm):
    """A model whose every call raises (a hard, non-retryable branch failure)."""

    async def generate_content_async(self, llm_request, stream=False):
        self._requests.append(llm_request)
        raise RuntimeError("planner branch exploded")
        yield  # pragma: no cover - makes this an async generator


def test_research_graph_branch_failure_does_not_stall_root(monkeypatch):
    import creative_agent.agent as ca

    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    (planner,) = [
        a
        for a in _llm_agents(ca.combined_research_pipeline)
        if a.name == "gs_web_planner"
    ]
    boom = _BoomLlm()
    monkeypatch.setattr(planner, "model", boom)
    _script_research(llms, ["CA INSIGHTS"])

    root_llm, events, state = _run_root(monkeypatch, "combined_research_pipeline")

    assert boom.requests  # the gs branch really was attempted and raised
    # The root still got a function response for the pipeline tool and was
    # re-called: a failing fan-out branch must not stall the root's turn.
    fc_ids = {fr.id for e in events for fr in e.get_function_responses() or [] if fr.id}
    assert "fc1" in fc_ids
    # NodeTool turns the node failure into an error result the root can act on
    # (the whole pipeline fails; there is no partial-research fallback).
    (response,) = _responses(events)
    assert "Error running node combined_research_pipeline" in str(response)
    assert "combined_final_cited_report" not in state
    assert root_llm.calls == 2


# --------------------------------------------------------------------------
# Ad / visual pipelines
# --------------------------------------------------------------------------

# ad_creative_pipeline hands the root a short confirmation, not the copies.
_ADS_CONFIRMATION = (
    "Ad copies complete: 4 final copies saved to session state as 'ad_copy_critique'."
)

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


def test_ad_agents_receive_the_brief_before_the_report(monkeypatch):
    """The brief in state is rendered into the drafter's and critic's prompts,
    ahead of the research report (the brief is the contract)."""
    import creative_agent.agent as ca

    llms = _stub_graph(monkeypatch, ca.ad_creative_pipeline)
    llms["ad_copy_drafter"].push(text_response(_ADS))
    llms["ad_copy_critic"].push(text_response(_ADS))

    _run_root(
        monkeypatch,
        "ad_creative_pipeline",
        {
            "combined_final_cited_report": "# Report",
            "creative_brief": _BRIEF,
            "creative_brief_md": render_brief_markdown(_BRIEF, heading=False),
        },
    )

    for name in ("ad_copy_drafter", "ad_copy_critic"):
        prompt = str(llms[name].requests[-1].config.system_instruction)
        proposition = _BRIEF["single_minded_proposition"]
        assert proposition in prompt, name
        assert prompt.index(proposition) < prompt.index("# Report"), name
        # The compact Markdown, not the dict repr.
        assert "**Single-minded proposition:**" in prompt, name
        assert "'single_minded_proposition':" not in prompt, name


def _fake_generate_image(tool_context) -> dict:
    """Stands in for creative_agent.tools.generate_image (same name + flag)."""
    tool_context.state["_images_generated"] = True
    return {"status": "ok"}


_fake_generate_image.__name__ = "generate_image"


def _script_visuals(llms: dict[str, RecordingLlm], generator_turns: int):
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
    # The images_ready terminal's confirmation, not the bare True flag.
    assert "Image creatives rendered" in str(responses[-1])
    assert root_llm.calls == 2


_DRIFTED_CONCEPTS = (
    '{"visual_concepts": [{"ad_copy_id": 1, "concept_name": "Drifted",'
    ' "trend": "roadrunner", "trend_reference": "r", "markets_product": "m",'
    ' "audience_appeal": "a", "selection_rationale": "s", "headline": "h",'
    ' "social_caption": "c", "call_to_action": "cta", "concept_summary": "sum",'
    ' "visual_style": "Watercolor / gouache", "aspect_ratio": "9:16",'
    ' "trend_motif": "a roadrunner dust cloud", "brand_cue": "the ACME crate",'
    ' "image_generation_prompt": "A soft watercolor of a quiet desert road."}]}'
)


def test_render_sees_guard_repaired_prompts(monkeypatch):
    """The finalizer's after_agent_callback repairs `final_visual_concepts` on the
    REAL node path (survives the graph clone, runs after the output_key write, and
    is not overwritten by the node's own output delta), so the prompt that
    generate_image reads from state names both the trend motif and the product."""
    import creative_agent.agent as ca

    llms = _stub_graph(monkeypatch, ca.visual_production_pipeline)
    (generator,) = [
        a
        for a in _llm_agents(ca.visual_production_pipeline)
        if a.name == "visual_generator"
    ]
    seen: list[str] = []

    def _recording_generate_image(tool_context) -> dict:
        # Same read as creative_agent.image_tools.generate_image.
        concepts = tool_context.state.get("final_visual_concepts")["visual_concepts"]
        seen.extend(c["image_generation_prompt"] for c in concepts)
        tool_context.state["_images_generated"] = True
        return {"status": "ok"}

    _recording_generate_image.__name__ = "generate_image"
    monkeypatch.setattr(generator, "tools", [_recording_generate_image])
    llms["art_director"].push(text_response("DIRECTION"))
    llms["visual_concept_drafter"].push(text_response(_CONCEPTS))
    llms["visual_concept_critic"].push(text_response(_CONCEPTS))
    llms["visual_concept_finalizer"].push(text_response(_DRIFTED_CONCEPTS))
    llms["visual_generator"].push(
        fc_response("generate_image", {}, "img1"), text_response("Rendered.")
    )

    _, _, state = _run_root(
        monkeypatch,
        "visual_production_pipeline",
        {"combined_final_cited_report": "# Report", "ad_copy_critique": _ADS_FINAL},
    )

    (prompt,) = seen
    assert prompt.startswith("A soft watercolor of a quiet desert road.")
    assert "a roadrunner dust cloud" in prompt
    assert "Rocket Skates" in prompt
    assert "The scene features the ACME crate." in prompt
    final = state["final_visual_concepts"]["visual_concepts"][0]
    assert final["image_generation_prompt"] == prompt


def test_interactive_reviser_output_is_guarded(monkeypatch):
    """interactive_creative's visual_concept_reviser (the other writer of
    `final_visual_concepts`, run via AgentTool) gets the same guard: a revision
    that drops the product/motif is repaired in the state the renderer reads."""
    import interactive_creative.agent as ic

    root_llm = RecordingLlm()
    reviser_llm = RecordingLlm()
    monkeypatch.setattr(ic.root_agent, "model", root_llm)
    monkeypatch.setattr(ic.root_agent, "instruction", "orchestrate")
    monkeypatch.setattr(ic.root_agent, "before_agent_callback", None)
    monkeypatch.setattr(ic.root_agent, "before_model_callback", None)
    monkeypatch.setattr(ic.visual_concept_reviser, "model", reviser_llm)
    root_llm.push(
        fc_response("visual_concept_reviser", {"request": "apply notes"}, "fc1"),
        text_response("DONE"),
    )
    reviser_llm.push(text_response(_DRIFTED_CONCEPTS))
    seeded = {
        **_SEED,
        "visual_revision_notes": "Concept 0: make it calmer",
        "final_visual_concepts": {"visual_concepts": [{"concept_name": "Old"}]},
    }

    async def go() -> dict[str, Any]:
        svc = InMemorySessionService()
        runner = Runner(
            agent=ic.root_agent, app_name="interactive_creative", session_service=svc
        )
        session = await svc.create_session(
            app_name="interactive_creative", user_id="u", state=seeded
        )
        async for _ in runner.run_async(
            user_id="u", session_id=session.id, new_message=user_message("hi")
        ):
            pass
        final = await svc.get_session(
            app_name="interactive_creative", user_id="u", session_id=session.id
        )
        assert final is not None
        return dict(final.state)

    state = asyncio.run(go())
    assert reviser_llm.calls == 1  # notes present: the reviser model ran
    (concept,) = state["final_visual_concepts"]["visual_concepts"]
    prompt = concept["image_generation_prompt"]
    assert prompt.startswith("A soft watercolor of a quiet desert road.")
    assert "a roadrunner dust cloud" in prompt
    assert "Rocket Skates" in prompt
    assert "The scene features the ACME crate." in prompt


# --------------------------------------------------------------------------
# Ad copy gate: bounded, targeted revision of flagged copies
# --------------------------------------------------------------------------


def _final_ad(original_id: int, **overrides: Any) -> dict[str, Any]:
    """A schema-valid FinalAdCopy that passes creative_agent.copy_gate."""
    ad = {
        "original_id": original_id,
        "tone_style": "Humorous",
        "angle_id": f"A{min(original_id, 3)}",
        "headline": f"Beep beep {original_id}",
        "body_text": "Rocket Skates finally make you faster.",
        "trend_connection": "Roadrunner sprint.",
        "audience_appeal_rationale": "Coyotes want speed.",
        "social_caption": "Zoom.",
        "typicality": 0.4,
        "call_to_action": "Order yours today",
        "brief_checks": [
            {"item": "proposition", "passed": True, "note": "on message"},
            {"item": "mandatories", "passed": True, "note": "none"},
            {"item": "cta", "passed": True, "note": "specific"},
        ],
        "detailed_performance_rationale": "Speed sells.",
    }
    ad.update(overrides)
    return ad


def _final_ads(*ads: dict[str, Any]) -> str:
    return json.dumps({"ad_copies": list(ads)})


_ADS_STATE = {
    "combined_final_cited_report": "# Report",
    "creative_brief": _BRIEF,
    "creative_brief_md": render_brief_markdown(_BRIEF, heading=False),
}


def _run_ads(
    monkeypatch,
    critic: str,
    reviser: list[str] | None = None,
    model=None,
    extra_state: dict[str, Any] | None = None,
):
    import creative_agent.agent as ca

    llms = _stub_graph(monkeypatch, ca.ad_creative_pipeline)
    llms["ad_copy_drafter"].push(text_response(_ADS))
    llms["ad_copy_critic"].push(text_response(critic))
    for text in reviser or []:
        llms["ad_copy_reviser"].push(text_response(text))
    if model is not None:
        _patch_agent_model(
            monkeypatch, ca.ad_creative_pipeline, "ad_copy_reviser", model
        )
    root_llm, events, state = _run_root(
        monkeypatch, "ad_creative_pipeline", {**_ADS_STATE, **(extra_state or {})}
    )
    return llms, root_llm, events, state


def _ids_and_bodies(state: dict[str, Any]) -> list[tuple[int, str]]:
    return [
        (c["original_id"], c["body_text"])
        for c in state["ad_copy_critique"]["ad_copies"]
    ]


def test_ad_copies_passing_the_gate_skip_the_reviser(monkeypatch):
    ads = [_final_ad(i) for i in range(1, 5)]
    llms, root_llm, events, state = _run_ads(monkeypatch, _final_ads(*ads))

    assert llms["ad_copy_reviser"].calls == 0
    assert state["ad_copy_critique"]["ad_copies"] == ads
    assert state["ad_copy_revision_rounds_used"] == 0
    assert state["ad_copy_issues"] == ""
    assert state.get("ad_copy_critique__issues") is None
    (response,) = _responses(events)
    assert response == {"result": _ADS_CONFIRMATION}
    assert root_llm.calls == 2


def test_flagged_copy_is_revised_and_unflagged_edits_are_reverted(monkeypatch):
    """Copy 2 omits the product and fails the proposition check: the reviser gets
    exactly those issues, rewrites copy 2, and its edit to (unflagged) copy 3
    is reverted by the safety net."""
    bad = _final_ad(
        2,
        body_text="Finally, you are faster.",
        brief_checks=[
            {"item": "proposition", "passed": False, "note": "two ideas"},
            {"item": "tone", "passed": False, "note": "too sarcastic"},
        ],
    )
    ads = [_final_ad(1), bad, _final_ad(3), _final_ad(4)]
    fixed = _final_ad(2, body_text="Rocket Skates: finally faster, deadpan.")
    sneaky = _final_ad(3, body_text="Rocket Skates, rewritten without being asked.")
    revision = _final_ads(_final_ad(1), fixed, sneaky, _final_ad(4))

    llms, root_llm, events, state = _run_ads(monkeypatch, _final_ads(*ads), [revision])

    assert llms["ad_copy_reviser"].calls == 1
    prompt = str(llms["ad_copy_reviser"].requests[-1].config.system_instruction)
    assert '- **Copy 2 ("Beep beep 2"):**' in prompt
    assert "  - product not named: mention 'Rocket Skates'" in prompt
    assert "  - brief check failed: proposition — two ideas" in prompt
    issues_block = prompt.split("<ad_copy_issues>")[-1].split("</ad_copy_issues>")[0]
    assert "brief check failed: proposition" in issues_block
    assert "too sarcastic" not in issues_block  # tone is advisory
    assert "Copy 1" not in prompt and "Copy 3" not in prompt
    assert "Finally, you are faster." in prompt  # the current copies are shown
    assert _BRIEF["single_minded_proposition"] in prompt
    assert _ids_and_bodies(state) == [
        (1, ads[0]["body_text"]),
        (2, fixed["body_text"]),
        (3, ads[2]["body_text"]),  # reverted: copy 3 was not flagged
        (4, ads[3]["body_text"]),
    ]
    assert state["ad_copy_revision_rounds_used"] == 1
    assert state["ad_copy_issues"] == ""
    assert state["ad_copy_critique__before_revision"] is None
    assert state.get("ad_copy_critique__issues") is None
    (response,) = _responses(events)
    assert response == {"result": _ADS_CONFIRMATION}
    assert root_llm.calls == 2


def test_raising_ad_copy_reviser_keeps_the_pre_revision_copies(monkeypatch):
    from agent_common import collect_degradation_warnings

    bad = _final_ad(2, body_text="Finally, you are faster.")
    ads = [_final_ad(1), bad, _final_ad(3), _final_ad(4)]
    boom = _BoomLlm()

    _, root_llm, events, state = _run_ads(monkeypatch, _final_ads(*ads), model=boom)

    assert boom.requests  # the reviser really ran and raised
    assert state["ad_copy_critique"]["ad_copies"] == ads
    (issue,) = state["ad_copy_critique__issues"]
    assert issue.startswith('Copy 2 ("Beep beep 2"): product not named')
    (note,) = collect_degradation_warnings(state)
    assert note.startswith("Ad copy check has unresolved issues: 1 (e.g. ")
    (response,) = _responses(events)
    assert response == {"result": _ADS_CONFIRMATION}
    assert root_llm.calls == 2


def test_missing_copies_are_recorded_without_a_revision(monkeypatch):
    """Fewer than 4 copies is a structural issue the per-copy reviser cannot
    fix: recorded on the ok exit, never routed to the reviser."""
    ads = [_final_ad(1), _final_ad(2), _final_ad(3)]
    llms, _, _, state = _run_ads(monkeypatch, _final_ads(*ads))

    assert llms["ad_copy_reviser"].calls == 0
    assert state["ad_copy_critique__issues"] == ["only 3 of 4 ad copies were produced."]


def test_ad_copy_issues_left_after_the_budget_are_recorded(monkeypatch):
    from agent_common import collect_degradation_warnings

    bad = _final_ad(2, headline="x" * 70)
    ads = [_final_ad(1), bad, _final_ad(3), _final_ad(4)]
    llms, root_llm, events, state = _run_ads(
        monkeypatch, _final_ads(*ads), [_final_ads(*ads)]
    )

    assert llms["ad_copy_reviser"].calls == 1
    assert state["ad_copy_revision_rounds_used"] == 1
    (issue,) = state["ad_copy_critique__issues"]
    assert "headline is 70 characters" in issue
    (note,) = collect_degradation_warnings(state)
    assert note.startswith("Ad copy check has unresolved issues: 1 (e.g. ")
    (response,) = _responses(events)
    assert response == {
        "result": _ADS_CONFIRMATION.replace("4 final", f"{len(ads)} final")
    }
    assert root_llm.calls == 2


# --- finalize_pipeline (evaluate -> persist -> summary) --- #
def _judge_score(passed: bool, overall: float) -> CreativeScore:
    return CreativeScore(
        overall_score=overall,
        passed=passed,
        verdicts=[],
        strengths=[],
        improvements=[],
    )


def _fake_judge(ad_copies, visual_concepts, campaign_context, config, **_kw):
    """Stands in for creative_eval.evaluate_all_concurrently (no judge calls)."""
    ads = [
        AdCopyEvaluation(
            original_id=a["original_id"],
            headline=a["headline"],
            tone_style=a.get("tone_style", ""),
            score=_judge_score(True, 0.8),
        )
        for a in ad_copies
    ]
    visuals = [
        VisualConceptEvaluation(
            ad_copy_id=v["ad_copy_id"],
            concept_name=v["concept_name"],
            score=_judge_score(False, 0.6),
        )
        for v in visual_concepts
    ]
    return ads, visuals


_FINALIZE_STATE = {
    "gcs_folder": "folder",
    "agent_output_dir": "out",
    "ad_copy_critique": SAMPLE_AD_COPIES,
    "final_visual_concepts": SAMPLE_VISUAL_CONCEPTS,
}


def _fake_finalize_io(monkeypatch, gallery=None):
    """Fake judge, GCS and BQ for finalize_pipeline; records the step order.

    Returns (step order, storage, bq). ``gallery`` replaces
    save_creative_gallery_html.
    """
    import creative_eval.agent as eval_agent
    from creative_agent import bq_tools, gcs_tools, tools

    monkeypatch.setattr(eval_agent, "evaluate_all_concurrently", _fake_judge)
    storage = FakeStorageClient([])
    monkeypatch.setattr(gcs_tools, "_get_gcs_client", lambda: storage)
    bq = FakeBigQueryClient()
    monkeypatch.setattr(bq_tools, "_get_bigquery_client", lambda: bq)

    order: list[str] = []
    steps = [
        (gcs_tools, "save_eval_report_to_gcs", None),
        (tools, "save_creative_gallery_html", gallery),
        (bq_tools, "write_trends_to_bq", None),
        (bq_tools, "write_eval_report_to_bq", None),
    ]
    for module, name, replacement in steps:
        target = replacement or getattr(module, name)
        if inspect.iscoroutinefunction(target):

            async def recorded(ctx, _t=target, _n=name):
                order.append(_n)
                return await _t(ctx)

        else:

            def recorded(ctx, _t=target, _n=name):
                order.append(_n)
                return _t(ctx)

        monkeypatch.setattr(module, name, recorded)
    return order, storage, bq


def _run_finalize(monkeypatch, state: dict[str, Any], gallery=None):
    """Run the root once through finalize_pipeline with fake judge, GCS and BQ.

    Returns (root_llm, events, state, step order, storage, bq). ``gallery``
    replaces save_creative_gallery_html.
    """
    import creative_agent.agent as ca

    order, storage, bq = _fake_finalize_io(monkeypatch, gallery)
    root_llm, events, final = _run_root(
        monkeypatch, "finalize_pipeline", state, root_tools=[ca.finalize_pipeline]
    )
    return root_llm, events, final, order, storage, bq


def test_finalize_graph_evaluates_and_persists_everything(monkeypatch):
    root_llm, events, state, order, storage, bq = _run_finalize(
        monkeypatch, dict(_FINALIZE_STATE)
    )

    report = state["creative_evaluation_report"]
    assert report["summary"]["total_ad_copies"] == 2
    assert report["summary"]["visual_concepts_passed"] == 0
    assert state["eval_report_gcs_uri"].endswith("folder/out/creative_eval_report.json")
    assert state["creative_gallery_gcs_uri"].endswith(
        "folder/out/creative_portfolio_gallery.html"
    )
    assert state["creative_row_uuid"]
    assert state["eval_bq_row_uuid"]
    assert state["finalize_done"] is True
    assert not [k for k in state if k.endswith(("__issues", "__retry_exhausted"))]
    # The eval row links to both the creative row and the saved report: last.
    assert order == [
        "save_eval_report_to_gcs",
        "save_creative_gallery_html",
        "write_trends_to_bq",
        "write_eval_report_to_bq",
    ]
    assert len(bq.sqls) == 2 and "creative_eval" not in bq.sqls[0]
    assert "folder/out/creative_eval_report.json" in storage.contents
    assert "folder/out/creative_portfolio_gallery.html" in storage.contents
    # One function response (the NodeTool's) and the root was re-called.
    (response,) = _responses(events)
    assert "Evaluation complete: 2/3 creatives passed" in str(response)
    assert "'The Proposal Riff' (visual, 0.60)" in str(response)
    assert root_llm.calls == 2


def test_finalize_graph_without_creatives_still_ends_truthy(monkeypatch):
    root_llm, events, state, order, _, bq = _run_finalize(
        monkeypatch, {"gcs_folder": "folder", "agent_output_dir": "out"}
    )

    assert state["creative_evaluation_report__retry_exhausted"] is True
    assert "creative_evaluation_report" not in state
    # No report: both eval writes are skipped, the creative row still lands.
    assert order == ["write_trends_to_bq"]
    assert state["creative_row_uuid"] and len(bq.sqls) == 1
    (response,) = _responses(events)
    assert "creative_evaluation_report" in str(response)
    assert root_llm.calls == 2


def test_finalize_graph_gallery_failure_does_not_block_bq(monkeypatch):
    async def broken_gallery(ctx):
        raise RuntimeError("gallery upload failed")

    root_llm, events, state, order, _, bq = _run_finalize(
        monkeypatch, dict(_FINALIZE_STATE), gallery=broken_gallery
    )

    assert "gallery upload failed" in state["creative_gallery_gcs_uri__issues"]
    assert "creative_gallery_gcs_uri" not in state
    assert order[-2:] == ["write_trends_to_bq", "write_eval_report_to_bq"]
    assert state["creative_row_uuid"] and state["eval_bq_row_uuid"]
    assert state["finalize_done"] is True
    assert len(bq.sqls) == 2
    (response,) = _responses(events)
    assert "Failed steps: HTML gallery." in str(response)
    assert root_llm.calls == 2


# --- research PDF (save_research_pdf_node) --- #


class _FakeSection:
    def __init__(self, *a, **k):
        pass


class _FakeMarkdownPdf:
    def __init__(self, *a, **k):
        self.meta: dict = {}

    def add_section(self, *a, **k):
        return None

    def save(self, path):
        with open(path, "wb") as f:
            f.write(b"%PDF-1.4 fake")


def test_research_pdf_is_saved_as_an_artifact_and_to_gcs(monkeypatch):
    import creative_agent.agent as ca
    from creative_agent import gcs_tools

    monkeypatch.setattr(gcs_tools, "save_draft_report_artifact", _real_save_pdf())
    monkeypatch.setattr(gcs_tools, "MarkdownPdf", _FakeMarkdownPdf)
    monkeypatch.setattr(gcs_tools, "Section", _FakeSection)
    storage = FakeStorageClient([])
    monkeypatch.setattr(gcs_tools, "_get_gcs_client", lambda: storage)
    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(llms, ["CA INSIGHTS"])

    _, events, state = _run_root(
        monkeypatch,
        "combined_research_pipeline",
        {"gcs_folder": "folder", "agent_output_dir": "out"},
    )

    blob = "folder/out/research_report_with_citations.pdf"
    assert state["research_report_gcs_uri"].endswith(blob)
    assert [name for name, _ in storage.uploads] == [blob]
    deltas = [e.actions.artifact_delta for e in events if e.actions.artifact_delta]
    assert deltas == [{"research_report_with_citations.pdf": 0}]
    assert "research_report_gcs_uri__issues" not in state


def test_research_pdf_is_skipped_without_a_report(monkeypatch):
    import creative_agent.agent as ca

    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(llms, ["CA INSIGHTS"], report="   ")

    root_llm, events, state = _run_root(monkeypatch, "combined_research_pipeline")

    assert _PDF_CALLS == []
    assert "research_report_gcs_uri" not in state
    assert "research_report_gcs_uri__issues" not in state
    assert len(_responses(events)) == 1 and root_llm.calls == 2


def test_research_pdf_failure_is_recorded_not_raised(monkeypatch):
    import creative_agent.agent as ca
    from creative_agent import gcs_tools

    async def broken(ctx):
        raise RuntimeError("pdf render failed")

    monkeypatch.setattr(gcs_tools, "save_draft_report_artifact", broken)
    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(llms, ["CA INSIGHTS"])

    root_llm, events, state = _run_root(monkeypatch, "combined_research_pipeline")

    assert "pdf render failed" in state["research_report_gcs_uri__issues"]
    (response,) = _responses(events)
    assert "Research report complete" in str(response)
    assert root_llm.calls == 2


# --------------------------------------------------------------------------
# Visual concept gate: bounded, targeted fix of flagged concepts
# --------------------------------------------------------------------------


def _final_concept(ad_copy_id: int, prompt: str = "", **overrides: Any) -> dict:
    """A schema-valid VisualConceptFinal that passes concept_guard (no quotes)."""
    concept = {
        "ad_copy_id": ad_copy_id,
        "concept_name": f"Dust {ad_copy_id}",
        "trend": "roadrunner",
        "trend_reference": "r",
        "markets_product": "m",
        "audience_appeal": "a",
        "selection_rationale": "s",
        "headline": f"Beep beep {ad_copy_id}",
        "social_caption": "Zoom.",
        "call_to_action": "Order yours today",
        "concept_summary": "sum",
        "visual_style": "Watercolor / gouache",
        "aspect_ratio": "9:16",
        "trend_motif": "a roadrunner dust cloud",
        "brand_cue": "the ACME crate",
        "angle_id": "A1",
        "image_generation_prompt": prompt
        or (
            f"Scene {ad_copy_id}: a watercolor of Rocket Skates on the ACME crate "
            "in a roadrunner dust cloud."
        ),
    }
    concept.update(overrides)
    return concept


def _concepts_json(*concepts: dict[str, Any]) -> str:
    return json.dumps({"visual_concepts": list(concepts)})


_VISUAL_STATE = {
    **_ADS_STATE,
    "ad_copy_critique": {"ad_copies": [_final_ad(i) for i in range(1, 5)]},
}


def _run_visuals(
    monkeypatch,
    finalizer: str,
    fixer: list[str] | None = None,
    model=None,
    extra_state: dict[str, Any] | None = None,
):
    import creative_agent.agent as ca

    wf = ca.visual_production_pipeline
    llms = _stub_graph(monkeypatch, wf)
    (generator,) = [a for a in _llm_agents(wf) if a.name == "visual_generator"]
    monkeypatch.setattr(generator, "tools", [_fake_generate_image])
    llms["art_director"].push(text_response("DIRECTION"))
    llms["visual_concept_drafter"].push(text_response(_CONCEPTS))
    llms["visual_concept_critic"].push(text_response(_CONCEPTS))
    llms["visual_concept_finalizer"].push(text_response(finalizer))
    for text in fixer or []:
        llms["visual_concept_fixer"].push(text_response(text))
    if model is not None:
        _patch_agent_model(monkeypatch, wf, "visual_concept_fixer", model)
    llms["visual_generator"].push(
        fc_response("generate_image", {}, "img1"), text_response("Rendered.")
    )
    root_llm, events, state = _run_root(
        monkeypatch,
        "visual_production_pipeline",
        {**_VISUAL_STATE, **(extra_state or {})},
    )
    return llms, root_llm, events, state


def _prompts(state: dict[str, Any]) -> list[str]:
    return [
        c["image_generation_prompt"]
        for c in state["final_visual_concepts"]["visual_concepts"]
    ]


def test_concepts_passing_the_gate_skip_the_fixer(monkeypatch):
    concepts = [_final_concept(i) for i in range(1, 5)]
    concepts[0] = _final_concept(
        1, 'Rocket Skates on the ACME crate, a roadrunner dust cloud, "Beep beep 1".'
    )
    llms, root_llm, events, state = _run_visuals(monkeypatch, _concepts_json(*concepts))

    assert llms["visual_concept_fixer"].calls == 0
    assert state["final_visual_concepts"]["visual_concepts"] == concepts
    assert state["visual_concept_revision_rounds_used"] == 0
    assert state["visual_concept_issues"] == ""
    assert state.get("final_visual_concepts__issues") is None
    assert state["_images_generated"] is True
    assert "Image creatives rendered" in str(_responses(events)[-1])
    assert root_llm.calls == 2


def test_flagged_concept_is_fixed_and_unflagged_edits_are_reverted(monkeypatch):
    """Concept 2 quotes text that is not from its copy: the fixer gets exactly
    that issue, rewrites concept 2, its edit to (unflagged) concept 3 is
    reverted, and the guard then re-adds the brand cue the fix dropped."""
    bad = _final_concept(
        2,
        'Rocket Skates on the ACME crate in a roadrunner dust cloud, a sign reads "Speed!".',
    )
    concepts = [_final_concept(1), bad, _final_concept(3), _final_concept(4)]
    fixed = _final_concept(
        2, 'Rocket Skates in a roadrunner dust cloud, type reads "Beep beep 2".'
    )
    sneaky = _final_concept(3, "Rocket Skates, rewritten without being asked.")
    revision = _concepts_json(_final_concept(1), fixed, sneaky, _final_concept(4))

    llms, root_llm, events, state = _run_visuals(
        monkeypatch, _concepts_json(*concepts), [revision]
    )

    assert llms["visual_concept_fixer"].calls == 1
    prompt = str(llms["visual_concept_fixer"].requests[-1].config.system_instruction)
    issues_block = prompt.split("<visual_concept_issues>")[-1].split(
        "</visual_concept_issues>"
    )[0]
    assert '- **Concept 2 ("Dust 2"):**' in issues_block
    assert '  - in-image text "Speed!" is not the paired ad copy' in issues_block
    assert "Concept 1" not in issues_block and "Concept 3" not in issues_block
    assert '"Speed!"' in prompt.split("<final_visual_concepts>")[-1]
    assert _BRIEF["single_minded_proposition"] in prompt
    assert _prompts(state) == [
        concepts[0]["image_generation_prompt"],
        fixed["image_generation_prompt"] + " The scene features the ACME crate.",
        concepts[2]["image_generation_prompt"],  # reverted: not flagged
        concepts[3]["image_generation_prompt"],
    ]
    assert state["visual_concept_revision_rounds_used"] == 1
    assert state["visual_concept_issues"] == ""
    assert state["final_visual_concepts__before_revision"] is None
    assert state.get("final_visual_concepts__issues") is None
    assert state["_images_generated"] is True
    assert root_llm.calls == 2


def test_raising_concept_fixer_keeps_the_pre_revision_concepts(monkeypatch):
    from agent_common import collect_degradation_warnings

    bad = _final_concept(2, trend_motif="")
    concepts = [_final_concept(1), bad, _final_concept(3), _final_concept(4)]
    boom = _BoomLlm()

    _, root_llm, events, state = _run_visuals(
        monkeypatch, _concepts_json(*concepts), model=boom
    )

    assert boom.requests  # the fixer really ran and raised
    assert state["final_visual_concepts"]["visual_concepts"] == concepts
    (issue,) = state["final_visual_concepts__issues"]
    assert issue.startswith('Concept 2 ("Dust 2"): trend_motif is empty')
    (note,) = collect_degradation_warnings(state)
    assert note.startswith("Visual concept check has unresolved issues: 1 (e.g. ")
    # The pipeline still ends truthy and renders.
    assert state["_images_generated"] is True
    assert "Image creatives rendered" in str(_responses(events)[-1])
    assert root_llm.calls == 2


def test_concept_issues_left_after_the_budget_are_recorded(monkeypatch):
    from agent_common import collect_degradation_warnings

    concepts = [
        _final_concept(i, f"Scene {i}: a centred hero of Rocket Skates.")
        for i in (1, 2)
    ]
    llms, root_llm, events, state = _run_visuals(
        monkeypatch, _concepts_json(*concepts), [_concepts_json(*concepts)]
    )

    assert llms["visual_concept_fixer"].calls == 1
    assert state["visual_concept_revision_rounds_used"] == 1
    (issue,) = state["final_visual_concepts__issues"]
    assert issue.startswith('Concept 2 ("Dust 2"): more than one centred hero')
    (note,) = collect_degradation_warnings(state)
    assert note.startswith("Visual concept check has unresolved issues: 1 (e.g. ")
    assert state["_images_generated"] is True
    assert root_llm.calls == 2


# --- creative_pipeline (the root's single call: every stage end to end) --- #


def test_creative_pipeline_runs_every_stage_and_the_root_finishes(monkeypatch):
    """One creative_pipeline call runs research → ad copies → visuals + render
    → finalize, so evaluation + saves no longer depend on the root making four
    consecutive tool calls. The barriers keep each stage's confirmation out of
    the next stage's PipelineRequest-validated input (a rejected input would
    stop the chain before finalize)."""
    import creative_agent.agent as ca

    wf = ca.creative_pipeline
    llms = _stub_graph(monkeypatch, wf)
    _script_research(llms, ["CA INSIGHTS"])
    llms["ad_copy_drafter"].push(text_response(_ADS))
    ads = [_final_ad(i) for i in range(1, 5)]
    llms["ad_copy_critic"].push(text_response(_final_ads(*ads)))
    (generator,) = [a for a in _llm_agents(wf) if a.name == "visual_generator"]
    monkeypatch.setattr(generator, "tools", [_fake_generate_image])
    concepts = [_final_concept(i) for i in range(1, 5)]
    llms["art_director"].push(text_response("DIRECTION"))
    llms["visual_concept_drafter"].push(text_response(_CONCEPTS))
    llms["visual_concept_critic"].push(text_response(_CONCEPTS))
    llms["visual_concept_finalizer"].push(text_response(_concepts_json(*concepts)))
    llms["visual_generator"].push(
        fc_response("generate_image", {}, "img1"), text_response("Rendered.")
    )
    order, storage, bq = _fake_finalize_io(monkeypatch)

    root_llm, events, state = _run_root(
        monkeypatch,
        "creative_pipeline",
        {"gcs_folder": "folder", "agent_output_dir": "out"},
    )

    # Every stage ran, in order, off the previous stage's state.
    assert state["creative_brief"] == _BRIEF
    assert state["research_report_gcs_uri"] == "gs://bucket/report.pdf"
    assert state["ad_copy_critique"]["ad_copies"] == ads
    assert state["final_visual_concepts"]["visual_concepts"] == concepts
    assert state["_images_generated"] is True
    report = state["creative_evaluation_report"]
    assert report["summary"]["total_ad_copies"] == 4
    assert report["summary"]["total_visual_concepts"] == 4
    assert state["eval_report_gcs_uri"].endswith("folder/out/creative_eval_report.json")
    assert state["eval_bq_row_uuid"]
    assert state["finalize_done"] is True
    assert order == [
        "save_eval_report_to_gcs",
        "save_creative_gallery_html",
        "write_trends_to_bq",
        "write_eval_report_to_bq",
    ]
    # No revision loops on gate-passing output, and nothing degraded.
    for name in ("brief_reviser", "ad_copy_reviser", "visual_concept_fixer"):
        assert llms[name].calls == 0, name
    degraded = {
        k: v
        for k, v in state.items()
        if k.endswith(("__issues", "__retry_exhausted")) and v
    }
    assert not degraded
    # The root got ONE pipeline result (finalize's summary) and answered with
    # text: two model calls in total.
    (result,) = [
        fr.response
        for e in events
        for fr in e.get_function_responses() or []
        if fr.name == "creative_pipeline"
    ]
    assert "Evaluation complete: 4/8 creatives passed" in str(result)
    assert "gs://bucket/report.pdf" in str(result)
    assert root_llm.calls == 2
    assert events[-1].author == "root_agent"
    assert events[-1].content.parts[0].text == "DONE"


def test_creative_pipeline_judges_ad_copies_while_visuals_render(monkeypatch):
    """The ad-copy judge calls start right after ad_creative_pipeline, before
    rendering finishes (fan-out next to visual_production_pipeline); finalize
    then judges only the visuals and the merged report is the one the old
    all-at-once evaluation produced."""
    import threading

    import creative_agent.agent as ca
    import creative_eval.agent as eval_agent

    wf = ca.creative_pipeline
    llms = _stub_graph(monkeypatch, wf)
    _script_research(llms, ["CA INSIGHTS"])
    llms["ad_copy_drafter"].push(text_response(_ADS))
    ads = [_final_ad(i) for i in range(1, 5)]
    llms["ad_copy_critic"].push(text_response(_final_ads(*ads)))
    concepts = [_final_concept(i) for i in range(1, 5)]
    llms["art_director"].push(text_response("DIRECTION"))
    llms["visual_concept_drafter"].push(text_response(_CONCEPTS))
    llms["visual_concept_critic"].push(text_response(_CONCEPTS))
    llms["visual_concept_finalizer"].push(text_response(_concepts_json(*concepts)))
    llms["visual_generator"].push(
        fc_response("generate_image", {}, "img1"), text_response("Rendered.")
    )
    _fake_finalize_io(monkeypatch)

    events: list[str] = []
    ad_eval_started = threading.Event()
    judge_calls: list[tuple[int, int]] = []

    def judge(ad_copies, visual_concepts, campaign_context, config, **kw):
        judge_calls.append((len(ad_copies), len(visual_concepts)))
        if ad_copies and not visual_concepts:
            events.append("ad_eval_start")
            ad_eval_started.set()
        else:
            events.append("visual_eval")
        return _fake_judge(ad_copies, visual_concepts, campaign_context, config)

    monkeypatch.setattr(eval_agent, "evaluate_all_concurrently", judge)

    async def slow_render(tool_context) -> dict:
        # A render that takes a while: the early ad-copy judge must start
        # during it (bounded wait, so a sequential graph fails, not hangs).
        for _ in range(500):
            if ad_eval_started.is_set():
                break
            await asyncio.sleep(0.01)
        events.append("render_done")
        tool_context.state["_images_generated"] = True
        return {"status": "ok"}

    slow_render.__name__ = "generate_image"
    (generator,) = [a for a in _llm_agents(wf) if a.name == "visual_generator"]
    monkeypatch.setattr(generator, "tools", [slow_render])

    _, _, state = _run_root(
        monkeypatch,
        "creative_pipeline",
        {"gcs_folder": "folder", "agent_output_dir": "out"},
    )

    assert events == ["ad_eval_start", "render_done", "visual_eval"]
    # Early: the 4 ad copies only; finalize: the 4 visuals only.
    assert judge_calls == [(4, 0), (0, 4)]
    report = state["creative_evaluation_report"]
    # Same report as the old path (everything judged in finalize).
    monkeypatch.setattr(eval_agent, "evaluate_all_concurrently", _fake_judge)
    baseline_state = {
        k: v for k, v in state.items() if k != eval_agent.AD_COPY_PARTIAL_KEY
    }
    ctx = FakeToolContext(baseline_state)
    eval_agent.evaluate_all_creatives(ctx)
    assert report == ctx.state["creative_evaluation_report"]
    assert report["summary"]["total_ad_copies"] == 4
    assert report["summary"]["total_visual_concepts"] == 4
    assert state["finalize_done"] is True


def test_creative_pipeline_early_ad_copy_eval_failure_falls_back(monkeypatch):
    """A failing early judge never fails the run: finalize judges everything."""
    import creative_agent.agent as ca
    import creative_eval.agent as eval_agent

    wf = ca.creative_pipeline
    llms = _stub_graph(monkeypatch, wf)
    _script_research(llms, ["CA INSIGHTS"])
    llms["ad_copy_drafter"].push(text_response(_ADS))
    ads = [_final_ad(i) for i in range(1, 5)]
    llms["ad_copy_critic"].push(text_response(_final_ads(*ads)))
    (generator,) = [a for a in _llm_agents(wf) if a.name == "visual_generator"]
    monkeypatch.setattr(generator, "tools", [_fake_generate_image])
    concepts = [_final_concept(i) for i in range(1, 5)]
    llms["art_director"].push(text_response("DIRECTION"))
    llms["visual_concept_drafter"].push(text_response(_CONCEPTS))
    llms["visual_concept_critic"].push(text_response(_CONCEPTS))
    llms["visual_concept_finalizer"].push(text_response(_concepts_json(*concepts)))
    llms["visual_generator"].push(
        fc_response("generate_image", {}, "img1"), text_response("Rendered.")
    )
    _fake_finalize_io(monkeypatch)

    def broken_early(tool_context):
        raise RuntimeError("judge down")

    monkeypatch.setattr(eval_agent, "evaluate_ad_copies_only", broken_early)

    _, _, state = _run_root(
        monkeypatch,
        "creative_pipeline",
        {"gcs_folder": "folder", "agent_output_dir": "out"},
    )

    assert eval_agent.AD_COPY_PARTIAL_KEY not in state
    report = state["creative_evaluation_report"]
    assert report["summary"]["total_ad_copies"] == 4
    assert report["summary"]["total_visual_concepts"] == 4
    assert state["finalize_done"] is True


_HISTORY = {
    "brand": "Acme",
    "runs": 2,
    "recent_styles": ["Comic panel", "Meme aesthetic"],
    "strongest_styles": ["Comic panel"],
    "strongest_tones": ["Humorous"],
    "weaknesses": [("Trend connection", 2)],
    "failed_checks": [("product_visible", 1)],
    "reports": 2,
}


def test_brand_history_reaches_the_brief_writer(monkeypatch):
    """load_brand_history runs in the research fan-out; its note lands in the
    brief writer's prompt and the shortlist is re-drawn without recent styles."""
    import creative_agent.agent as ca
    from creative_agent import brand_history

    seen: list[tuple[str, int]] = []

    def fake_fetch(brand: str, *, limit: int) -> dict:
        seen.append((brand, limit))
        return _HISTORY

    monkeypatch.setattr(brand_history, "fetch_brand_history", fake_fetch)
    monkeypatch.setattr(ca.config, "brand_history_enabled", True)
    monkeypatch.setattr(ca.config, "brand_history_runs", 4)
    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(llms, ["CA INSIGHTS"])

    _, events, state = _run_root(
        monkeypatch,
        "combined_research_pipeline",
        extra_state={"style_shortlist": "Comic panel; Meme aesthetic"},
    )

    assert seen == [("Acme", 4)]
    assert state["brand_history"].startswith("Recent runs for Acme (2):")
    prompt = str(llms["brief_writer"].requests[-1].config.system_instruction)
    assert "recurring weaknesses: Trend connection (2 of 2 runs)" in prompt
    shortlist = state["style_shortlist"].split("; ")
    assert len(shortlist) == 6
    assert not {"Comic panel", "Meme aesthetic"} & set(shortlist)
    assert state["creative_brief"] == _BRIEF
    assert len(_responses(events)) == 1


def test_brand_history_disabled_makes_no_query(monkeypatch):
    import creative_agent.agent as ca
    from creative_agent import brand_history

    calls: list[str] = []
    monkeypatch.setattr(
        brand_history, "fetch_brand_history", lambda brand, **_: calls.append(brand)
    )
    monkeypatch.setattr(ca.config, "brand_history_enabled", False)
    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(llms, ["CA INSIGHTS"])

    _, _, state = _run_root(monkeypatch, "combined_research_pipeline")

    assert calls == []
    assert "brand_history" not in state
    assert state["creative_brief"] == _BRIEF


def test_failing_brand_history_does_not_stop_research(monkeypatch):
    import creative_agent.agent as ca
    from creative_agent import brand_history

    async def boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(brand_history, "brand_history_state_delta", boom)
    monkeypatch.setattr(ca.config, "brand_history_enabled", True)
    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(llms, ["CA INSIGHTS"])

    _, events, state = _run_root(monkeypatch, "combined_research_pipeline")

    assert state["creative_brief"] == _BRIEF
    assert "Research report complete" in str(_responses(events)[0])


def _rating_rows() -> list[dict]:
    from tests.test_rating_signals import ROWS

    return ROWS * 2


def _rating_learning_on(monkeypatch, ca) -> list[str]:
    from creative_agent import brand_history, rating_signals

    calls: list[str] = []

    def fake_fetch(brand: str, *, days: int) -> list[dict]:
        calls.append(brand)
        return _rating_rows()

    monkeypatch.setattr(rating_signals, "fetch_ratings", fake_fetch)
    monkeypatch.setattr(brand_history, "fetch_brand_history", lambda *a, **k: {})
    monkeypatch.setattr(ca.config, "brand_history_enabled", True)
    monkeypatch.setattr(ca.config, "rating_learning_enabled", True)
    monkeypatch.setattr(
        ca.config,
        "rating_learning_effects",
        frozenset({"guidance", "styles", "checks"}),
    )
    monkeypatch.setattr(ca.config, "rating_learning_min_ratings", 8)
    return calls


def test_rating_signals_reach_the_brief_writer_when_opted_in(monkeypatch):
    """Toggle on + enough ratings: the note lands in the brief writer's prompt,
    strictness and the applied record are written, and the shortlist is
    steered (poorly rated family out, well-rated family in)."""
    import creative_agent.agent as ca

    calls = _rating_learning_on(monkeypatch, ca)
    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(llms, ["CA INSIGHTS"])

    _, _, state = _run_root(
        monkeypatch,
        "combined_research_pipeline",
        extra_state={"learn_from_ratings": True},
    )

    assert calls == ["Acme"]
    assert state["rating_signals"].startswith("Your team's ratings for Acme (20)")
    prompt = str(llms["brief_writer"].requests[-1].config.system_instruction)
    assert "<rating_signals>Your team's ratings for Acme (20)" in prompt
    assert state["rating_strictness"] == ["product_not_visible"]
    assert state["rating_signals_applied"]["applied"] is True
    shortlist = state["style_shortlist"].split("; ")
    assert "Isometric miniature world" not in shortlist
    assert "Candid 35mm film photo" in shortlist
    assert state["creative_brief"] == _BRIEF


def test_rating_learning_off_makes_no_query(monkeypatch):
    import creative_agent.agent as ca

    calls = _rating_learning_on(monkeypatch, ca)
    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(llms, ["CA INSIGHTS"])

    _, _, state = _run_root(monkeypatch, "combined_research_pipeline")

    assert calls == []
    assert state.get("learn_from_ratings") is not True
    assert not {k for k in state if k.startswith("rating_")}
    prompt = str(llms["brief_writer"].requests[-1].config.system_instruction)
    assert "Your team's ratings" not in prompt
    assert state["creative_brief"] == _BRIEF


def test_rating_strictness_from_the_learning_step_tightens_the_gates(monkeypatch):
    """Toggle on + ratings with recurring fail reasons: the learning step writes
    rating_strictness, and with it the copy gate caps CTAs at 6 words, the
    concept gate allows one text concept and the concept guard asks for a
    large, foreground product. The same copies / concepts pass by default."""
    import creative_agent.agent as ca
    from creative_agent import rating_signals

    _rating_learning_on(monkeypatch, ca)
    fails = [
        ("ad_copy", "weak_cta"),
        ("visual", "text_problem"),
        ("visual", "product_not_visible"),
    ]
    rows = [
        {"kind": kind, "verdict": "fail", "fail_reasons": [reason]}
        for kind, reason in fails
        for _ in range(3)
    ]
    monkeypatch.setattr(rating_signals, "fetch_ratings", lambda *a, **k: rows)
    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(llms, ["CA INSIGHTS"])
    _, _, research = _run_root(
        monkeypatch,
        "combined_research_pipeline",
        extra_state={"learn_from_ratings": True},
    )
    strictness = research["rating_strictness"]
    assert set(strictness) == {"weak_cta", "text_problem", "product_not_visible"}
    learned = {"rating_strictness": strictness}

    # Copy gate: a 7-word CTA passes by default, is revised under weak_cta.
    long_cta = _final_ad(2, call_to_action="Grab your Rocket Skates at ACME today")
    ads = [_final_ad(1), long_cta, _final_ad(3), _final_ad(4)]
    llms, _, _, _ = _run_ads(monkeypatch, _final_ads(*ads))
    assert llms["ad_copy_reviser"].calls == 0
    fixed = _final_ad(2, call_to_action="Grab Rocket Skates today")
    llms, _, _, state = _run_ads(
        monkeypatch,
        _final_ads(*ads),
        [_final_ads(_final_ad(1), fixed, _final_ad(3), _final_ad(4))],
        extra_state=learned,
    )
    assert llms["ad_copy_reviser"].calls == 1
    prompt = str(llms["ad_copy_reviser"].requests[-1].config.system_instruction)
    assert "call_to_action has 7 words" in prompt
    assert "at most 6 words" in prompt
    assert state["ad_copy_critique"]["ad_copies"][1] == fixed
    assert state.get("ad_copy_critique__issues") is None

    # Concept gate + guard: two text concepts pass by default; under the
    # strictness the second is fixed and every prompt asks for a prominent
    # product.
    texts = [
        _final_concept(
            i,
            f'Rocket Skates on the ACME crate in a roadrunner dust cloud, type reads "Beep beep {i}".',
        )
        for i in (1, 2)
    ]
    concepts = [*texts, _final_concept(3), _final_concept(4)]
    llms, _, _, state = _run_visuals(monkeypatch, _concepts_json(*concepts))
    assert llms["visual_concept_fixer"].calls == 0
    assert not any("foreground" in p for p in _prompts(state))

    no_text = _final_concept(2)
    llms, _, _, state = _run_visuals(
        monkeypatch,
        _concepts_json(*concepts),
        [_concepts_json(concepts[0], no_text, *concepts[2:])],
        extra_state=learned,
    )
    assert llms["visual_concept_fixer"].calls == 1
    issues_block = str(
        llms["visual_concept_fixer"].requests[-1].config.system_instruction
    ).split("<visual_concept_issues>")[-1]
    assert "in-image text appears in more than 1 concept:" in issues_block
    prompts = _prompts(state)
    assert prompts[1].startswith(no_text["image_generation_prompt"])
    assert all(
        p.count("The product is large and in the foreground.") == 1 for p in prompts
    )
    assert state.get("final_visual_concepts__issues") is None
    assert state["_images_generated"] is True
