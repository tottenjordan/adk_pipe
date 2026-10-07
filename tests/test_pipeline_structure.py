"""Tests for agent pipeline structure and configuration."""

import json

import pytest
from google.adk.tools._node_tool import NodeTool

from tests._fakes import walk_nodes


def test_creative_agent_root_has_expected_tools():
    from creative_agent.agent import root_agent

    tool_names = [
        getattr(t, "name", getattr(t, "__name__", str(t))) for t in root_agent.tools
    ]
    # The research PDF and the eval + persistence steps run inside the
    # pipelines: the root makes four workflow calls (plus memorize).
    assert tool_names == [
        "combined_research_pipeline",
        "ad_creative_pipeline",
        "visual_production_pipeline",
        "finalize_pipeline",
        "memorize",
    ]


def test_creative_agent_root_output_key_not_set():
    """Root agent should not have an output_key (it orchestrates)."""
    from creative_agent.agent import root_agent

    assert (
        not hasattr(root_agent, "output_key")
        or root_agent.output_key is None
        or root_agent.output_key == ""
    )


def _graph_nodes(wf):
    """Graph nodes by name (graph nodes are clones: compare names, never identity)."""
    assert wf.graph is not None
    return {n.name: n for n in wf.graph.nodes}


def _graph_edges(wf):
    return {(e.from_node.name, e.to_node.name, e.route) for e in wf.graph.edges}


def test_combined_research_pipeline_graph():
    """P2 T2a: the research pipeline is a graph Workflow. The two planner chains
    fan out from START into a JoinNode; a no-output barrier keeps the join's
    dict out of merge_planners' user turn; a routed gate replaces RunIfAgent
    (Lever A: the PRO evaluator + follow-up search run only when the base
    research is degraded, skipping one serial gemini-3.1-pro-preview call on the
    healthy path); the composer always runs."""
    from google.adk.workflow import JoinNode, Workflow

    from creative_agent.agent import combined_research_pipeline as wf

    assert isinstance(wf, Workflow)
    names = set(_graph_nodes(wf))
    assert {
        "gs_sequential_planner",
        "ca_sequential_planner",
        "research_join",
        "research_barrier",
        "merge_planners",
        "refinement_gate",
        "combined_web_evaluator",
        "enhanced_combined_searcher_resilient",
        "combined_report_composer",
        "brief_writer_failsoft",
        "brief_gate",
        "brief_reviser_failsoft",
        "save_research_pdf_node",
        "research_report_ready",
    } <= names
    assert isinstance(_graph_nodes(wf)["research_join"], JoinNode)
    # Exact edges: parallel fan-out from START, the refinement route, the brief
    # writer + bounded revision cycle, then (on the gate's "ok" exit) the
    # research PDF save before the truthy terminal.
    assert _graph_edges(wf) == {
        ("__START__", "gs_sequential_planner", None),
        ("__START__", "ca_sequential_planner", None),
        ("gs_sequential_planner", "research_join", None),
        ("ca_sequential_planner", "research_join", None),
        ("research_join", "research_barrier", None),
        ("research_barrier", "merge_planners", None),
        ("merge_planners", "refinement_gate", None),
        ("refinement_gate", "combined_web_evaluator", "refine"),
        ("refinement_gate", "combined_report_composer", "skip"),
        ("combined_web_evaluator", "enhanced_combined_searcher_resilient", None),
        ("enhanced_combined_searcher_resilient", "combined_report_composer", None),
        ("combined_report_composer", "brief_writer_failsoft", None),
        ("brief_writer_failsoft", "brief_gate", None),
        ("brief_gate", "save_research_pdf_node", "ok"),
        ("brief_gate", "brief_reviser_failsoft", "revise"),
        ("brief_reviser_failsoft", "brief_gate", None),
        ("save_research_pdf_node", "research_report_ready", None),
    }


def test_refinement_pair_is_retry_wrapped_workflow():
    """The follow-up searcher+synthesizer pair runs as a Workflow inside a
    RetryUntilKeyNode keyed on refined_web_search_insights (same key/attempts
    as the pre-graph RetryUntilKeyAgent)."""
    from google.adk.workflow import Workflow

    from agent_common import RetryUntilKeyNode
    from creative_agent.agent import combined_research_pipeline

    w = _graph_nodes(combined_research_pipeline)["enhanced_combined_searcher_resilient"]
    assert isinstance(w, RetryUntilKeyNode)
    assert w.output_key == "refined_web_search_insights"
    assert w.max_attempts == 3
    pair = w.node
    assert isinstance(pair, Workflow)
    assert _graph_edges(pair) == {
        ("__START__", "enhanced_combined_searcher", None),
        ("enhanced_combined_searcher", "refined_web_synthesizer", None),
    }
    by_name = _graph_nodes(pair)
    assert by_name["enhanced_combined_searcher"].output_key == "refined_web_search_raw"
    assert (
        by_name["refined_web_synthesizer"].output_key == "refined_web_search_insights"
    )


def test_refinement_gate_routes_on_degradation():
    from creative_agent.agent import refinement_gate_route

    assert refinement_gate_route({"combined_web_search_insights": "ok"}) == "skip"
    assert refinement_gate_route({}) == "refine"
    assert (
        refinement_gate_route(
            {
                "combined_web_search_insights": "ok",
                "gs_web_search_insights__retry_exhausted": True,
            }
        )
        == "refine"
    )


def test_composer_keeps_citation_callback_in_graph():
    """The composer's citation after_agent_callback survives the graph clone
    (whether it FIRES on the node path is covered by
    tests/test_creative_agent_graph.py)."""
    from creative_agent import callbacks
    from creative_agent.agent import combined_research_pipeline

    composer = _graph_nodes(combined_research_pipeline)["combined_report_composer"]
    assert composer.after_agent_callback is callbacks.citation_replacement_callback
    assert composer.output_key == "combined_final_cited_report"


def test_graph_llm_agents_are_single_turn():
    """Every LlmAgent in the creative graphs sets mode='single_turn' explicitly
    and reads its inputs only from state (include_contents='none', set
    explicitly so the node wrapper's default is not what we rely on)."""
    from google.adk.agents import LlmAgent

    from creative_agent.agent import (
        ad_creative_pipeline,
        combined_research_pipeline,
        visual_production_pipeline,
    )

    agents = [
        n
        for wf in (
            combined_research_pipeline,
            ad_creative_pipeline,
            visual_production_pipeline,
        )
        for n in walk_nodes(wf)
        if isinstance(n, LlmAgent)
    ]
    assert {a.name for a in agents} == {
        "gs_web_planner",
        "gs_web_searcher",
        "gs_web_synthesizer",
        "campaign_web_planner",
        "campaign_web_searcher",
        "campaign_web_synthesizer",
        "merge_planners",
        "combined_web_evaluator",
        "enhanced_combined_searcher",
        "refined_web_synthesizer",
        "combined_report_composer",
        "brief_writer",
        "brief_reviser",
        "ad_copy_drafter",
        "ad_copy_critic",
        "ad_copy_reviser",
        "art_director",
        "visual_concept_drafter",
        "visual_concept_critic",
        "visual_concept_finalizer",
        "visual_concept_fixer",
        "visual_generator",
    }
    for a in agents:
        assert a.mode == "single_turn", a.name
        assert a.include_contents == "none", a.name
        assert "include_contents" in a.model_fields_set, a.name


def _terminal_names(wf):
    sources = {e.from_node.name for e in wf.graph.edges}
    return {n.name for n in wf.graph.nodes if n.name not in sources}


# Function nodes designated as pipeline terminals: each returns the pipeline's
# output-key value (or a short confirmation) when populated, else a non-empty
# notice -- never a falsy/None result.
_RESULT_NODES = {
    "research_report_ready",
    "ad_copies_ready",
    "visual_concepts_ready",
    "images_ready",
    "finalize_ready",
}


def _assert_truthy_terminal(node, path):
    from google.adk.workflow import Workflow

    from agent_common import RetryUntilKeyNode

    if isinstance(node, RetryUntilKeyNode):
        return  # truthy by construction (value or exhaustion notice)
    if isinstance(node, Workflow):
        by_name = _graph_nodes(node)
        terminals = _terminal_names(node)
        assert terminals, f"{path}: no terminal node"
        for t in terminals:
            _assert_truthy_terminal(by_name[t], f"{path}/{t}")
        return
    assert node.name in _RESULT_NODES, (
        f"{path}: terminal node {node.name!r} ({type(node).__name__}) may yield no "
        "or a falsy output, which silently stalls the calling root agent (P2 rule 1)"
    )


def test_exposed_node_tools_end_in_truthy_terminals():
    """P2 rule 1: every node exposed to a root agent as a NodeTool must finish
    with a truthy output, or the root's turn silently stalls (no function
    response). A final LlmAgent is NOT accepted: an empty model turn (the
    recurring flake this repo retries around) would yield ""/None."""
    from creative_agent.agent import root_agent as ca_root
    from interactive_creative.agent import root_agent as ic_root

    for root in (ca_root, ic_root):
        node_tools = [t for t in root.tools if isinstance(t, NodeTool)]
        assert node_tools, root.name
        for tool in node_tools:
            _assert_truthy_terminal(tool.node, f"{root.name}:{tool.name}")


def _result_node_cases():
    from creative_agent import agent as ca

    return [
        (ca.research_report_ready, "combined_final_cited_report", "# Report"),
        (ca.ad_copies_ready, "ad_copy_critique", {"ad_copies": [{"id": 1}]}),
        (ca.visual_concepts_ready, "final_visual_concepts", {"visual_concepts": []}),
        (ca.images_ready, "_images_generated", True),
    ]


def test_populated_result_nodes_return_a_short_confirmation():
    """ad/visual terminals hand the root a short confirmation (with the item
    count), never the copies/concepts JSON itself: later steps read state, and a
    ~10k-char payload in the root's context made the Pro root prone to empty
    turns (session 8242212012491276288)."""
    import json
    from types import SimpleNamespace

    from creative_agent import agent as ca

    ads = {"ad_copies": [{"id": i, "headline": "x" * 500} for i in range(4)]}
    msg = ca.ad_copies_ready(SimpleNamespace(state={"ad_copy_critique": ads}))
    assert (
        isinstance(msg, str) and "4 final copies" in msg and "ad_copy_critique" in msg
    )
    assert len(msg) < 200
    as_json = ca.ad_copies_ready(
        SimpleNamespace(state={"ad_copy_critique": json.dumps(ads)})
    )
    assert "4 final copies" in as_json

    concepts = {"visual_concepts": [{"name": "x"}, {"name": "y"}]}
    msg = ca.visual_concepts_ready(
        SimpleNamespace(state={"final_visual_concepts": concepts})
    )
    assert "2 concepts" in msg and "final_visual_concepts" in msg
    assert len(msg) < 200


def test_exposed_node_tools_have_real_descriptions():
    """No exposed node relies on NodeTool's "Executes the node: <name>" fallback
    description; the root model picks tools by these descriptions."""
    from creative_agent.agent import root_agent as ca_root
    from interactive_creative.agent import root_agent as ic_root

    for root in (ca_root, ic_root):
        for tool in root.tools:
            if isinstance(tool, NodeTool):
                assert tool.description.strip(), tool.name
                assert not tool.description.startswith("Executes the node"), tool.name


def test_exposed_workflows_take_a_pipeline_request():
    """Every node exposed to a root as a NodeTool declares the single
    `request: str` argument (PipelineRequest), keeping the tool declarations the
    pre-graph AgentTools exposed."""
    from agent_common import PipelineRequest
    from creative_agent.agent import root_agent as ca_root
    from interactive_creative.agent import root_agent as ic_root

    for root in (ca_root, ic_root):
        for tool in root.tools:
            if isinstance(tool, NodeTool):
                assert tool.node.input_schema is PipelineRequest, tool.name


def test_pipeline_result_nodes_are_never_falsy():
    """The designated terminal functions return a truthy value when their key
    is populated and a non-empty notice naming the key otherwise (missing,
    None, blank string, empty container)."""
    from types import SimpleNamespace

    for fn, key, value in _result_node_cases():
        for empty in ({}, {key: None}, {key: ""}, {key: "   "}, {key: {}}):
            out = fn(SimpleNamespace(state=empty))
            assert isinstance(out, str) and out.strip(), (fn.__name__, empty)
            assert key in out, (fn.__name__, out)
        assert fn(SimpleNamespace(state={key: value})), fn.__name__


def test_research_refinement_gate_predicate():
    """The gate runs the refinement block ONLY when base research is degraded:
    a blank/missing merged brief, or an upstream producer that exhausted retries.
    On the healthy common path (brief present, no exhaustion markers) it skips,
    dropping the PRO evaluator + a search/synth pass."""
    from creative_agent.agent import _base_research_is_degraded

    # Healthy: substantial brief, no exhaustion → skip refinement.
    assert (
        _base_research_is_degraded({"combined_web_search_insights": "A full brief."})
        is False
    )

    # Degraded: brief missing entirely → refine to compensate.
    assert _base_research_is_degraded({}) is True

    # Degraded: brief present but blank/whitespace → refine.
    assert _base_research_is_degraded({"combined_web_search_insights": "   "}) is True

    # Degraded: brief present but an upstream producer exhausted its retries.
    for marker in (
        "gs_web_search_insights__retry_exhausted",
        "campaign_web_search_insights__retry_exhausted",
    ):
        assert (
            _base_research_is_degraded(
                {"combined_web_search_insights": "A full brief.", marker: True}
            )
            is True
        )
        # Cleared (None) by a later successful RetryUntilKeyNode run → healthy.
        for cleared in (None, False):
            assert (
                _base_research_is_degraded(
                    {"combined_web_search_insights": "A full brief.", marker: cleared}
                )
                is False
            )


def test_refined_searcher_has_tool_synthesizer_is_tool_free():
    from google.adk.tools import google_search

    from creative_agent.agent import (
        enhanced_combined_searcher,
        refined_web_synthesizer,
    )

    assert google_search in enhanced_combined_searcher.tools
    assert not refined_web_synthesizer.tools
    assert refined_web_synthesizer.planner is None


def test_refined_searcher_keeps_source_collection():
    from creative_agent import callbacks
    from creative_agent.agent import (
        enhanced_combined_searcher,
        refined_web_synthesizer,
    )

    assert (
        enhanced_combined_searcher.after_agent_callback
        is callbacks.collect_research_sources_callback
    )
    assert refined_web_synthesizer.after_agent_callback is None


def test_ad_creative_pipeline_graph_edges():
    from google.adk.workflow import Workflow

    from creative_agent.agent import ad_creative_pipeline

    assert isinstance(ad_creative_pipeline, Workflow)
    assert _graph_edges(ad_creative_pipeline) == {
        ("__START__", "ad_copy_drafter", None),
        ("ad_copy_drafter", "ad_copy_critic", None),
        ("ad_copy_critic", "copy_gate", None),
        ("copy_gate", "ad_copies_ready", "ok"),
        ("copy_gate", "ad_copy_reviser_failsoft", "revise"),
        ("ad_copy_reviser_failsoft", "copy_gate", None),
    }


def test_finalize_pipeline_graph():
    """The post-render steps run as one deterministic NodeTool: evaluate, then
    persist (GCS + BigQuery), then a truthy summary terminal."""
    from google.adk.workflow import Workflow

    from agent_common import PipelineRequest
    from creative_agent.agent import finalize_pipeline as wf

    assert isinstance(wf, Workflow)
    assert _graph_edges(wf) == {
        ("__START__", "evaluate_creatives_node", None),
        ("evaluate_creatives_node", "persist_node", None),
        ("persist_node", "finalize_ready", None),
    }
    assert wf.input_schema is PipelineRequest
    assert wf.description.strip() and "Executes the node" not in wf.description
    _assert_truthy_terminal(wf, "finalize_pipeline")


def test_visual_generation_pipeline_graph_edges():
    from google.adk.workflow import Workflow

    from creative_agent.agent import visual_generation_pipeline

    assert isinstance(visual_generation_pipeline, Workflow)
    assert _graph_edges(visual_generation_pipeline) == {
        ("__START__", "art_director", None),
        ("art_director", "visual_concept_drafter", None),
        ("visual_concept_drafter", "visual_concept_critic", None),
        ("visual_concept_critic", "visual_concept_finalizer", None),
        ("visual_concept_finalizer", "concept_gate", None),
        ("concept_gate", "visual_concepts_ready", "ok"),
        ("concept_gate", "visual_concept_fixer_failsoft", "revise"),
        ("visual_concept_fixer_failsoft", "concept_gate", None),
    }


def test_structured_output_producers_carry_schema_retry():
    """Structured-output producers retry on ValidationError + infra (issue #104):
    a bad-JSON model turn (e.g. visual_concept_finalizer at high temp emitting raw
    control chars) crashed the run unretried. Identity check keeps them on one
    shared config. Checked on the graph nodes that actually run (looked up by
    name: graph nodes are clones)."""
    from creative_agent.agent import (
        ad_creative_pipeline,
        combined_research_pipeline,
        visual_production_pipeline,
    )
    from creative_agent.config import SCHEMA_RETRY

    by_name = {
        n.name: n
        for wf in (
            combined_research_pipeline,
            ad_creative_pipeline,
            visual_production_pipeline,
        )
        for n in walk_nodes(wf)
    }
    for name in (
        "combined_web_evaluator",
        "brief_writer",
        "brief_reviser",
        "ad_copy_drafter",
        "ad_copy_critic",
        "ad_copy_reviser",
        "visual_concept_drafter",
        "visual_concept_critic",
        "visual_concept_finalizer",
        "visual_concept_fixer",
    ):
        assert by_name[name].retry_config is SCHEMA_RETRY, name


def test_visual_production_pipeline_wraps_generator_in_retry():
    """The image step must be retry-wrapped: visual_generator intermittently
    returns MALFORMED_FUNCTION_CALL and never emits generate_image, shipping an
    empty gallery. RetryUntilKeyNode re-runs it until _images_generated is set
    (generate_image's idempotency guard makes a re-run safe). A no-output
    render_barrier sits between the concepts and the render step: it keeps the
    concepts payload out of the retry node's PipelineRequest-validated input."""
    from google.adk.workflow import Workflow

    from agent_common import RetryUntilKeyNode
    from creative_agent import agent as ca

    wf = ca.visual_production_pipeline
    assert isinstance(wf, Workflow)
    assert _graph_edges(wf) == {
        ("__START__", "visual_generation_pipeline", None),
        ("visual_generation_pipeline", "render_barrier", None),
        ("render_barrier", "visual_generator_resilient", None),
        # Terminal: a short truthy confirmation, not the bare True flag.
        ("visual_generator_resilient", "images_ready", None),
    }

    w = _graph_nodes(wf)["visual_generator_resilient"]
    assert isinstance(w, RetryUntilKeyNode)
    assert w.output_key == "_images_generated"
    assert w.node.name == "visual_generator"
    # MALFORMED_FUNCTION_CALL is a transient producer flake (issue #116); each
    # attempt is an independent turn, so a higher cap materially raises recovery.
    assert w.max_attempts == 6


def test_campaign_producer_is_retry_wrapped():
    """WS2 + P2: campaign_web_searcher is split into a tool-using searcher (writes
    `campaign_web_search_raw`) + a tool-free synthesizer (writes `campaign_web_search_insights`), run as a graph Workflow
    pair inside a RetryUntilKeyNode, so an empty turn retries the WHOLE pair
    instead of crashing merge_planners. The chain is planner -> resilient pair."""
    from google.adk.workflow import Workflow

    from agent_common import RetryUntilKeyNode
    from creative_agent.sub_agents.campaign_researcher.agent import (
        ca_sequential_planner,
    )

    assert isinstance(ca_sequential_planner, Workflow)
    assert _graph_edges(ca_sequential_planner) == {
        ("__START__", "campaign_web_planner", None),
        ("campaign_web_planner", "campaign_web_searcher_resilient", None),
    }
    w = _graph_nodes(ca_sequential_planner)["campaign_web_searcher_resilient"]
    assert isinstance(w, RetryUntilKeyNode)
    assert w.output_key == "campaign_web_search_insights"
    assert w.max_attempts == 3

    pair = w.node
    assert isinstance(pair, Workflow)
    assert pair.name == "campaign_search_and_synthesize"
    assert _graph_edges(pair) == {
        ("__START__", "campaign_web_searcher", None),
        ("campaign_web_searcher", "campaign_web_synthesizer", None),
    }
    by_name = _graph_nodes(pair)
    assert by_name["campaign_web_searcher"].output_key == "campaign_web_search_raw"
    assert (
        by_name["campaign_web_synthesizer"].output_key == "campaign_web_search_insights"
    )


def test_campaign_searcher_has_tool_synthesizer_is_tool_free():
    from google.adk.tools import google_search

    from creative_agent.sub_agents.campaign_researcher.agent import (
        campaign_web_searcher,
        campaign_web_synthesizer,
    )

    assert google_search in campaign_web_searcher.tools
    assert not campaign_web_synthesizer.tools
    assert campaign_web_synthesizer.planner is None


def test_campaign_searcher_keeps_source_collection():
    from creative_agent import callbacks
    from creative_agent.sub_agents.campaign_researcher.agent import (
        campaign_web_searcher,
        campaign_web_synthesizer,
    )

    assert (
        campaign_web_searcher.after_agent_callback
        is callbacks.collect_research_sources_callback
    )
    assert campaign_web_synthesizer.after_agent_callback is None


def test_trend_producer_is_retry_wrapped():
    """WS2 + P2: gs_web_searcher is split into a tool-using searcher (writes
    `gs_web_search_raw`) + a tool-free synthesizer (writes `gs_web_search_insights`), run as a graph Workflow
    pair inside a RetryUntilKeyNode, so an empty turn retries the WHOLE pair
    instead of crashing merge_planners. The chain is planner -> resilient pair."""
    from google.adk.workflow import Workflow

    from agent_common import RetryUntilKeyNode
    from creative_agent.sub_agents.trend_researcher.agent import gs_sequential_planner

    assert isinstance(gs_sequential_planner, Workflow)
    assert _graph_edges(gs_sequential_planner) == {
        ("__START__", "gs_web_planner", None),
        ("gs_web_planner", "gs_web_searcher_resilient", None),
    }
    w = _graph_nodes(gs_sequential_planner)["gs_web_searcher_resilient"]
    assert isinstance(w, RetryUntilKeyNode)
    assert w.output_key == "gs_web_search_insights"
    assert w.max_attempts == 3

    pair = w.node
    assert isinstance(pair, Workflow)
    assert pair.name == "gs_search_and_synthesize"
    assert _graph_edges(pair) == {
        ("__START__", "gs_web_searcher", None),
        ("gs_web_searcher", "gs_web_synthesizer", None),
    }
    by_name = _graph_nodes(pair)
    assert by_name["gs_web_searcher"].output_key == "gs_web_search_raw"
    assert by_name["gs_web_synthesizer"].output_key == "gs_web_search_insights"


def test_gs_searcher_has_tool_synthesizer_is_tool_free():
    """The searcher runs google_search; the synthesizer is tool-free and
    planner-free (its reliability is the whole point of the split)."""
    from google.adk.tools import google_search

    from creative_agent.sub_agents.trend_researcher.agent import (
        gs_web_searcher,
        gs_web_synthesizer,
    )

    assert google_search in gs_web_searcher.tools
    assert not gs_web_synthesizer.tools
    assert gs_web_synthesizer.planner is None


def test_gs_searcher_keeps_source_collection():
    """collect_research_sources_callback reads google_search grounding metadata,
    so it must stay on the searcher (which has the tool), not the synthesizer."""
    from creative_agent import callbacks
    from creative_agent.sub_agents.trend_researcher.agent import (
        gs_web_searcher,
        gs_web_synthesizer,
    )

    assert (
        gs_web_searcher.after_agent_callback
        is callbacks.collect_research_sources_callback
    )
    assert gs_web_synthesizer.after_agent_callback is None


def test_campaign_pipeline_uses_distinct_global_bucket():
    """Quota spread (#94/#101-style): the campaign-research half of the one
    parallel fan-out runs on gemini-3.5-flash @ global — a different per-base-model
    quota bucket from the trend half's gemini-3.8-flash / gemini-3.5-flash-lite."""
    from creative_agent.config import config
    from creative_agent.sub_agents.campaign_researcher.agent import (
        campaign_web_planner,
        campaign_web_searcher,
        campaign_web_synthesizer,
    )

    for a in (campaign_web_planner, campaign_web_searcher, campaign_web_synthesizer):
        assert a.model.client_kwargs["location"] == "global"
        assert a.model.model == "gemini-3.5-flash"
        assert a.model.model not in {config.worker_model, config.lite_planner_model}


def test_campaign_pipeline_respects_placement_env(monkeypatch, fresh_config):
    """DoE arm seam: the campaign half's models+location follow
    CAMPAIGN_RESEARCH_PLACEMENT, resolved via config.campaign_models(). The models
    are bound at import time, so re-import fresh under the patched env.

    Arm A (global_3x) is asserted here; the default (global_altbucket) arm is
    covered by test_campaign_pipeline_uses_distinct_global_bucket. The trend half
    is unaffected.
    """
    monkeypatch.setenv("CAMPAIGN_RESEARCH_PLACEMENT", "global_3x")
    camp = fresh_config("creative_agent.sub_agents.campaign_researcher.agent")

    for a in (
        camp.campaign_web_planner,
        camp.campaign_web_searcher,
        camp.campaign_web_synthesizer,
    ):
        assert a.model.client_kwargs["location"] == "global"
    assert camp.campaign_web_planner.model.model == "gemini-3.5-flash-lite"
    assert camp.campaign_web_searcher.model.model == "gemini-3.8-flash"
    assert camp.campaign_web_synthesizer.model.model == "gemini-3.8-flash"


def test_trend_pipeline_stays_global():
    """The trend-research half is left on the global buckets, becoming their sole
    occupant during the parallel phase (the other side of the #94-style spread)."""
    from creative_agent.sub_agents.trend_researcher.agent import (
        gs_web_planner,
        gs_web_searcher,
        gs_web_synthesizer,
    )

    for a in (gs_web_planner, gs_web_searcher, gs_web_synthesizer):
        assert a.model.client_kwargs["location"] == "global"
    assert gs_web_planner.model.model == "gemini-3.5-flash-lite"
    assert gs_web_searcher.model.model == "gemini-3.8-flash"
    assert gs_web_synthesizer.model.model == "gemini-3.8-flash"


def test_trend_scout_gather_and_pick_run_global_3x():
    """2026-09: trend_scout's gather/pick left the retiring gemini-2.5 regional pool
    for distinct gemini-3.x base models @ global (quota spread preserved)."""
    from trend_scout.agent import gather_trends_agent, pick_trends_agent

    assert gather_trends_agent.model.model == "gemini-3.1-flash-lite"
    assert pick_trends_agent.model.model == "gemini-3.5-flash"
    for a in (gather_trends_agent, pick_trends_agent):
        assert a.model.client_kwargs["location"] == "global"


def test_merge_planners_inputs_are_optional():
    """merge_planners' two research inputs must use the optional `{var?}` syntax so
    a producer that exhausted its retries (key unset) degrades observably instead of
    raising KeyError: Context variable not found. Matched pair for the wrappers."""
    from creative_agent.agent import merge_planners

    instr = merge_planners.instruction
    assert "{campaign_web_search_insights?}" in instr
    assert "{gs_web_search_insights?}" in instr


def test_output_keys_are_set_correctly():
    from creative_agent.agent import (
        ad_copy_critic,
        ad_copy_drafter,
        art_director,
        combined_report_composer,
        combined_web_evaluator,
        enhanced_combined_searcher,
        merge_planners,
        refined_web_synthesizer,
        visual_concept_critic,
        visual_concept_drafter,
        visual_concept_finalizer,
    )

    expected = [
        (merge_planners, "combined_web_search_insights"),
        (combined_web_evaluator, "combined_research_evaluation"),
        (enhanced_combined_searcher, "refined_web_search_raw"),
        (refined_web_synthesizer, "refined_web_search_insights"),
        (combined_report_composer, "combined_final_cited_report"),
        (ad_copy_drafter, "ad_copy_draft"),
        (ad_copy_critic, "ad_copy_critique"),
        (art_director, "visual_direction"),
        (visual_concept_drafter, "visual_draft"),
        (visual_concept_critic, "visual_concept_critique"),
        (visual_concept_finalizer, "final_visual_concepts"),
    ]
    for agent, key in expected:
        assert agent.output_key == key, (
            f"{agent.name} output_key should be '{key}', got '{agent.output_key}'"
        )


def test_output_schemas_assigned():
    from creative_agent.agent import (
        AdCopyList,
        FinalAdCopyList,
        ResearchFeedback,
        VisualConceptCritiqueList,
        VisualConceptFinalList,
        VisualConceptList,
        ad_copy_critic,
        ad_copy_drafter,
        combined_web_evaluator,
        visual_concept_critic,
        visual_concept_drafter,
        visual_concept_finalizer,
    )

    assert combined_web_evaluator.output_schema == ResearchFeedback
    assert ad_copy_drafter.output_schema == AdCopyList
    assert ad_copy_critic.output_schema == FinalAdCopyList
    assert visual_concept_drafter.output_schema == VisualConceptList
    assert visual_concept_critic.output_schema == VisualConceptCritiqueList
    assert visual_concept_finalizer.output_schema == VisualConceptFinalList


def test_lever_c_critics_run_on_worker_model():
    """Lever C: ad_copy_critic and visual_concept_critic are downgraded from
    critic_model (pro) to worker_model (flash) — narrowing already-generated
    creatives is low-quality-dependence, and dropping the two serial 5-RPM PRO
    turns is the latency win being measured. The pro-tier stages that DO carry
    quality weight (drafters' critic, report composer) stay on critic_model."""
    from creative_agent import agent as ca
    from creative_agent.config import ResearchConfiguration

    config = ResearchConfiguration()
    assert config.worker_model != config.critic_model  # guard the test's premise
    assert ca.ad_copy_critic.model.model == config.worker_model
    assert ca.visual_concept_critic.model.model == config.worker_model


def test_creative_model_agents_have_finish_reason_callback():
    """Every creative model agent must carry the empty-turn finish_reason
    after_model_callback so a MAX_TOKENS/empty producer turn is logged (WS3 log
    parity with trend_scout). The existing after_agent callbacks (citation /
    source collection) live in a distinct slot and are untouched."""
    from creative_agent import agent as ca
    from creative_agent import callbacks

    agents = [
        ca.merge_planners,
        ca.combined_web_evaluator,
        ca.enhanced_combined_searcher,
        ca.refined_web_synthesizer,
        ca.combined_report_composer,
        ca.ad_copy_drafter,
        ca.ad_copy_critic,
        ca.ad_copy_reviser,
        ca.art_director,
        ca.visual_concept_drafter,
        ca.visual_concept_critic,
        ca.visual_concept_finalizer,
        ca.visual_concept_fixer,
        ca.visual_generator,
        ca.root_agent,
    ]
    for a in agents:
        cbs = a.canonical_after_model_callbacks
        assert callbacks.log_empty_turn_finish_reason in cbs, (
            f"{a.name} missing log_empty_turn_finish_reason after_model_callback"
        )


def test_ad_copy_agents_are_rate_limited():
    from creative_agent import agent as ca
    from creative_agent import callbacks

    for a in (ca.ad_copy_drafter, ca.ad_copy_critic, ca.ad_copy_reviser):
        assert callbacks.rate_limit_callback in a.canonical_before_model_callbacks, (
            a.name
        )


def test_ad_copy_agents_scrub_lone_surrogates():
    """The two ad-copy agents parse model text against an output_schema, so they
    must carry the surrogate scrubber as an after_model_callback (before the
    empty-turn logger) to survive lone Unicode surrogates in the JSON output."""
    from creative_agent import agent as ca
    from creative_agent import callbacks

    for a in (ca.ad_copy_drafter, ca.ad_copy_critic, ca.ad_copy_reviser):
        cbs = a.canonical_after_model_callbacks
        assert callbacks.scrub_surrogates_in_response in cbs, (
            f"{a.name} missing scrub_surrogates_in_response after_model_callback"
        )
        # Scrubber must run BEFORE the empty-turn logger.
        assert cbs.index(callbacks.scrub_surrogates_in_response) < cbs.index(
            callbacks.log_empty_turn_finish_reason
        )


def test_creative_researcher_agents_have_finish_reason_callback():
    """The planner + searcher + synthesizer sub-agents (both halves of each split
    producer) all get the finish_reason callback (WS3 log parity)."""
    from creative_agent import callbacks
    from creative_agent.sub_agents.campaign_researcher import agent as cr
    from creative_agent.sub_agents.trend_researcher import agent as tr

    for a in (tr.gs_web_planner, tr.gs_web_searcher, tr.gs_web_synthesizer):
        assert a.after_model_callback is callbacks.log_empty_turn_finish_reason
    for a in (
        cr.campaign_web_planner,
        cr.campaign_web_searcher,
        cr.campaign_web_synthesizer,
    ):
        assert a.after_model_callback is callbacks.log_empty_turn_finish_reason


def test_trend_scout_split_agents_have_finish_reason_callback():
    """Both halves of trend_scout's split understand_trends producer keep the
    finish_reason callback (WS3 log parity)."""
    from agent_common import log_empty_turn_finish_reason
    from trend_scout.agent import (
        understand_trends_searcher,
        understand_trends_synthesizer,
    )

    for a in (understand_trends_searcher, understand_trends_synthesizer):
        assert a.after_model_callback is log_empty_turn_finish_reason


def test_creative_root_has_final_state_summary():
    from creative_agent import callbacks
    from creative_agent.agent import root_agent

    assert root_agent.after_agent_callback is callbacks.log_final_state_summary
    assert callable(root_agent.after_agent_callback)


def test_creative_eval_agent_has_finish_reason_callback():
    from agent_common import log_empty_turn_finish_reason
    from creative_eval.agent import creative_eval_agent

    assert creative_eval_agent.after_model_callback is log_empty_turn_finish_reason


def test_interactive_root_has_observability_callbacks():
    from creative_agent import callbacks
    from interactive_creative.agent import root_agent

    assert root_agent.after_model_callback is callbacks.log_empty_turn_finish_reason
    assert root_agent.after_agent_callback is callbacks.log_final_state_summary


def test_interactive_creative_uses_resilient_visual_generator():
    """interactive_creative renders images standalone (a bare node → NodeTool)
    after a review checkpoint, so it has the same MALFORMED_FUNCTION_CALL flaw as
    creative_agent. It must invoke the resilient retry node, never the raw
    visual_generator (compared by name)."""
    from agent_common import RetryUntilKeyNode
    from interactive_creative import agent as ic

    node_tools = {
        t.name: t.node for t in ic.root_agent.tools if isinstance(t, NodeTool)
    }
    assert "visual_generator_resilient" in node_tools, (
        "interactive_creative must invoke the resilient image wrapper"
    )
    w = node_tools["visual_generator_resilient"]
    assert w.name == "visual_generator_resilient"
    assert isinstance(w, RetryUntilKeyNode)
    assert w.node.name == "visual_generator"
    assert w.max_attempts == 6

    # The checkpoint-3 reviser runs BEFORE the render as its own AgentTool step
    # (not a node in the render graph).
    from google.adk.tools.agent_tool import AgentTool

    assert any(
        isinstance(t, AgentTool) and t.agent.name == "visual_concept_reviser"
        for t in ic.root_agent.tools
    )

    # The raw generator must NOT be exposed directly (would bypass the retry).
    names = {getattr(t, "name", None) for t in ic.root_agent.tools}
    assert "visual_generator" not in names, (
        "raw visual_generator must not be exposed; use the wrapper"
    )


def test_interactive_creative_exposes_pipelines_as_node_tools():
    """G3 minimal change: the reused creative_agent pipelines are bare nodes
    (auto-wrapped NodeTools, now including finalize_pipeline); only the reviser
    stays an AgentTool."""
    from google.adk.tools.agent_tool import AgentTool

    from interactive_creative import agent as ic

    node_tools = {t.name for t in ic.root_agent.tools if isinstance(t, NodeTool)}
    assert node_tools == {
        "combined_research_pipeline",
        "ad_creative_pipeline",
        "visual_generation_pipeline",
        "visual_generator_resilient",
        "finalize_pipeline",
    }
    agent_tools = {
        t.agent.name for t in ic.root_agent.tools if isinstance(t, AgentTool)
    }
    assert agent_tools == {"visual_concept_reviser"}
    # The human-review checkpoints stay LongRunningFunctionTools (they pause the
    # resumable App until the resume's function response arrives).
    from google.adk.tools.long_running_tool import LongRunningFunctionTool

    checkpoints = {
        t.name for t in ic.root_agent.tools if isinstance(t, LongRunningFunctionTool)
    }
    assert checkpoints == {
        "review_research",
        "review_ad_copies",
        "review_visual_concepts",
    }


def test_trend_scout_root_has_expected_tools():
    import asyncio

    from trend_scout.agent import root_agent

    # canonical_tools(): the resolved tool list the model sees (the bare
    # understand_trends graph node surfaces as a NodeTool).
    tool_names = [t.name for t in asyncio.run(root_agent.canonical_tools())]
    expected = [
        "gather_trends_agent",
        "understand_trends_agent_resilient",
        "pick_trends_agent",
        "save_search_trends_to_session_state",
        "save_session_state_to_gcs",
        "write_trends_to_bq",
        "write_to_file",
        "memorize",
    ]
    for name in expected:
        assert name in tool_names, f"Missing tool: {name}"


def test_trend_scout_exposes_resumable_app():
    """The opt-in `review_trends` LongRunningFunctionTool checkpoint only pauses/
    resumes when the Runner is built from an App carrying
    ResumabilityConfig(is_resumable=True). trend_scout.agent must expose such an
    `app` (while still exporting the bare `root_agent` for deploy_agent.py)."""
    from google.adk.apps import App

    from trend_scout.agent import app, root_agent

    assert isinstance(app, App)
    assert app.resumability_config is not None
    assert app.resumability_config.is_resumable is True
    assert app.root_agent is root_agent


def test_trend_scout_registers_review_trends_tool():
    """The opt-in trend-picking checkpoint tool must be registered on the root
    orchestrator so the instruction can call it when interactive_trend_pick is on."""
    from trend_scout.agent import root_agent
    from trend_scout.review_tools import review_trends_tool

    assert review_trends_tool in root_agent.tools


def test_trend_scout_instruction_branches_on_interactive_flag():
    """The pick phase must branch on the optional `{interactive_trend_pick?}` var:
    the interactive branch calls `review_trends`, and BOTH branches call
    `pick_trends_agent` (the interactive branch repurposes it to narrate the
    human's already-chosen trends into `selected_gtrends` for the handoff UI)."""
    from trend_scout.agent import root_agent

    instr = str(root_agent.instruction)
    assert "{interactive_trend_pick?}" in instr
    assert "review_trends" in instr
    assert "pick_trends_agent" in instr
    # regression: the interactive branch must NOT skip pick_trends_agent, or the
    # frontend handoff (gated on selected_gtrends) never renders.
    assert "Do NOT call `pick_trends_agent`" not in instr


def test_pick_trends_agent_enriches_human_selected_trends():
    """In interactive mode pick_trends_agent must narrate the human's already-chosen
    trends (from target_search_trends) into selected_gtrends instead of re-selecting,
    so the trend->creative_agent handoff cards render. It therefore reads the picks
    via the optional `{target_search_trends?}` context var."""
    from trend_scout.agent import pick_trends_agent

    assert "{target_search_trends?}" in pick_trends_agent.instruction
    assert pick_trends_agent.output_key == "selected_gtrends"


def test_trend_scout_sub_agent_output_keys():
    """WS2: understand_trends_agent is split into a searcher (writes
    `info_gtrends_raw`) + a synthesizer (writes JSON `info_gtrends`)."""
    from trend_scout.agent import (
        pick_trends_agent,
        understand_trends_searcher,
        understand_trends_synthesizer,
    )

    assert understand_trends_searcher.output_key == "info_gtrends_raw"
    assert understand_trends_synthesizer.output_key == "info_gtrends"
    assert pick_trends_agent.output_key == "selected_gtrends"


def test_understand_trends_searcher_has_tool_synthesizer_is_tool_free():
    """The searcher runs google_search under a thinking planner; the synthesizer
    is tool-free / planner-free and emits the JSON analyzed_trends structure."""
    from google.adk.tools import google_search

    from trend_scout.agent import (
        understand_trends_searcher,
        understand_trends_synthesizer,
    )

    assert google_search in understand_trends_searcher.tools
    assert not understand_trends_synthesizer.tools
    assert understand_trends_synthesizer.planner is None


def test_understand_trends_is_retry_wrapped():
    """WS2 + P2: understand_trends is split into searcher + synthesizer, run as a
    graph Workflow pair inside a RetryUntilKeyNode so an empty turn retries the
    WHOLE pair instead of crashing pick_trends_agent. The wrapper is a bare node
    in the orchestrator's tools (auto-wrapped into a NodeTool)."""
    from google.adk.workflow import Workflow

    from agent_common import RetryUntilKeyNode
    from trend_scout.agent import root_agent

    # LlmAgent's tools validator wraps a bare BaseNode into a NodeTool at
    # construction, so the wrapper must appear as a NodeTool.
    matching = [
        t
        for t in root_agent.tools
        if isinstance(getattr(t, "node", None), RetryUntilKeyNode)
        and t.node.output_key == "info_gtrends"
    ]
    assert matching, "no RetryUntilKeyNode producing info_gtrends in root tools"
    tool = matching[0]
    assert isinstance(tool, NodeTool)
    wrapper = tool.node
    assert isinstance(wrapper, RetryUntilKeyNode)
    assert wrapper.max_attempts == 3

    pair = wrapper.node
    assert isinstance(pair, Workflow)
    assert pair.graph is not None
    names = [n.name for n in pair.graph.nodes if n.name != "__START__"]
    assert names == ["understand_trends_searcher", "understand_trends_synthesizer"]
    edges = {(e.from_node.name, e.to_node.name) for e in pair.graph.edges}
    assert edges == {
        ("__START__", "understand_trends_searcher"),
        ("understand_trends_searcher", "understand_trends_synthesizer"),
    }
    by_name = {n.name: n for n in pair.graph.nodes}
    searcher = by_name["understand_trends_searcher"]
    synthesizer = by_name["understand_trends_synthesizer"]
    assert searcher.output_key == "info_gtrends_raw"
    assert synthesizer.output_key == "info_gtrends"
    # Graph LlmAgents set single_turn explicitly (a parented node would default
    # to "chat" mode, which stalls the graph on an empty turn).
    assert searcher.mode == "single_turn"
    assert synthesizer.mode == "single_turn"


def test_understand_trends_tool_declaration_unchanged():
    """The NodeTool declaration keeps the pre-migration AgentTool shape (name +
    description + a required ``request`` string), so TREND_SCOUT_INSTR's tool
    calls keep working unchanged."""
    import asyncio

    from trend_scout.agent import root_agent, understand_trends_agent_resilient

    expected_description = understand_trends_agent_resilient.description
    assert "5-8" in expected_description  # describes what it researches

    tools = asyncio.run(root_agent.canonical_tools())
    (tool,) = [t for t in tools if t.name == "understand_trends_agent_resilient"]
    assert isinstance(tool, NodeTool)
    decl = tool._get_declaration()
    assert decl is not None
    assert decl.name == "understand_trends_agent_resilient"
    assert decl.description == expected_description
    schema = decl.parameters_json_schema
    assert isinstance(schema, dict)
    assert schema["required"] == ["request"]
    assert set(schema["properties"]) == {"request"}
    assert schema["properties"]["request"]["type"] == "string"
    assert "description" not in schema  # no schema docstring shown to the model


def test_pick_trends_info_gtrends_optional():
    """pick_trends_agent must tolerate a missing info_gtrends (orchestrator-skip
    or retry-exhaustion) via the optional `{info_gtrends?}` template syntax rather
    than raising KeyError: Context variable not found."""
    from trend_scout.agent import pick_trends_agent

    assert "{info_gtrends?}" in pick_trends_agent.instruction
    assert "{info_gtrends}" not in pick_trends_agent.instruction


def test_understand_trends_searcher_raw_gtrends_is_optional():
    """The searcher must tolerate a missing raw_gtrends (e.g. gather skipped or the
    gather tool errored) via the optional `{raw_gtrends?}` template syntax rather
    than raising KeyError inside the retry wrapper. A missing raw_gtrends then
    degrades to a bounded retry-exhaustion (surfaced via research_gaps) instead of
    a hard crash — the same class of fix as pick_trends' `{info_gtrends?}` guard."""
    from trend_scout.agent import understand_trends_searcher

    instr = understand_trends_searcher.instruction
    assert "{raw_gtrends?}" in instr
    assert "{raw_gtrends}" not in instr  # the bare non-optional form is gone


def test_trend_scout_exposes_record_research_gaps():
    """The orchestrator must expose `record_research_gaps` and surface its output
    in the handoff via the optional `{research_gaps?}` var, so a retry-exhausted
    run reports WHY (parity with the creative gallery banner) while the happy path
    (empty research_gaps) renders nothing."""
    from trend_scout import agent as ts
    from trend_scout.tools import record_research_gaps

    assert record_research_gaps in ts.trend_scout.tools
    assert "{research_gaps?}" in ts.trend_scout.instruction


def test_trend_scout_orchestrator_thinking_level_is_bounded_low():
    """The orchestrator's thinking must be bounded to LOW — not off, not HIGH.

    Disabled thinking (legacy thinking_budget=0, and its thinking_level analog
    MINIMAL) makes gemini-3.5-flash emit MALFORMED_FUNCTION_CALL when invoking an
    AgentTool with a structured argument (e.g. understand_trends_agent), aborting the
    pipeline right after gather_trends so nothing is ever persisted to BigQuery/GCS.
    HIGH (the default) hits MAX_TOKENS. LOW is the bounded middle. gemini-3.x
    deprecated the numeric thinking_budget, so we pin thinking_level instead.
    """
    from google.genai import types

    from trend_scout.agent import root_agent

    tc = root_agent.planner.thinking_config
    assert tc.thinking_budget is None, (
        "use thinking_level (not the deprecated numeric thinking_budget) on gemini-3"
    )
    assert tc.thinking_level == types.ThinkingLevel.LOW, (
        f"orchestrator thinking_level must be LOW (was {tc.thinking_level})"
    )


def test_visual_concept_finalizer_has_ad_copy_context():
    """The finalizer must reference ad_copy_critique in its instruction
    to avoid generating duplicate headlines/captions."""
    from creative_agent.agent import visual_concept_finalizer

    assert "ad_copy_critique" in visual_concept_finalizer.instruction
    assert "ad_copy_id" in visual_concept_finalizer.instruction


def test_interactive_registers_visual_concept_reviser():
    """The NL-revision reviser must be exposed as an AgentTool and be a proper
    structured-output producer that re-emits final_visual_concepts."""
    from google.adk.tools.agent_tool import AgentTool

    from creative_agent.config import SCHEMA_RETRY
    from creative_agent.schemas import VisualConceptFinalList
    from interactive_creative import agent as ic
    from interactive_creative.agent import visual_concept_reviser

    exposed = [
        t
        for t in ic.root_agent.tools
        if isinstance(t, AgentTool) and t.agent is visual_concept_reviser
    ]
    assert exposed, "visual_concept_reviser must be exposed as an AgentTool"

    assert visual_concept_reviser.output_key == "final_visual_concepts"
    assert visual_concept_reviser.output_schema is VisualConceptFinalList
    assert visual_concept_reviser.retry_config is SCHEMA_RETRY

    instr = visual_concept_reviser.instruction
    assert "{final_visual_concepts?}" in instr
    assert "{visual_revision_notes?}" in instr


def test_interactive_reviser_rechecks_concepts_after_the_guard():
    """The reviser's after_agent chain: the motif/product guard, THEN the
    deterministic recheck (so the residual marker reflects the guarded,
    post-checkpoint concepts, not the pre-checkpoint gate's verdict)."""
    from creative_agent import callbacks
    from interactive_creative.agent import visual_concept_reviser

    assert visual_concept_reviser.after_agent_callback == [
        callbacks.ensure_trend_and_product_callback,
        callbacks.recheck_concept_issues_callback,
    ]


def test_interactive_workflow_revises_before_render():
    """The checkpoint-3 → render step must apply revision notes (reviser) BEFORE
    rendering images, so a run's user edits/notes actually change the output."""
    from interactive_creative.agent import root_agent

    instr = root_agent.instruction
    assert "visual_concept_reviser" in instr
    # The reviser must be invoked before the image renderer in the workflow text.
    assert instr.index("visual_concept_reviser") < instr.index(
        "visual_generator_resilient"
    )


def test_interactive_creative_memorizes_target_search_trends():
    """The interactive orchestrator's memorize step must explicitly enumerate
    target_search_trends. A vague 'store all campaign metadata' instruction lets
    the LLM drop the trend, leaving target_search_trends empty in state (and in
    the trend_creatives / creative_evals BigQuery rows)."""
    from interactive_creative.agent import root_agent

    instr = root_agent.instruction
    # The memorize step must name every state key it has to persist, mirroring
    # creative_agent, not just say "all campaign metadata". (It memorizes only
    # the fields still missing from state; seeded ones are shown via tokens.)
    assert "`key_selling_points`, `target_search_trends`" in instr


def test_pick_trends_agent_excludes_brand_unsafe_trends():
    """A live run picked "unabomber" as campaign-relevant. The picker must carry an
    explicit brand-safety exclusion (violence/terrorism, crime, tragedies, etc.)
    that overrides search volume, and prefer returning fewer trends over an
    unsafe one."""
    from trend_scout.agent import pick_trends_agent

    instr = pick_trends_agent.instruction.lower()
    assert "brand safety" in instr
    for term in ("terrorism", "violence", "crime", "hate", "self-harm", "political"):
        assert term in instr, f"brand-safety rule missing {term!r}"
    assert "regardless of search volume" in instr
    assert "fewer" in instr and "unsafe" in instr


def test_creative_agent_root_exposes_pipelines_as_node_tools():
    """The pipelines are bare graph nodes on the root (auto-wrapped NodeTools);
    the eval judge runs inside finalize_pipeline, so there are no AgentTools."""
    from google.adk.tools.agent_tool import AgentTool

    from creative_agent.agent import root_agent

    node_tools = {t.name for t in root_agent.tools if isinstance(t, NodeTool)}
    assert node_tools == {
        "combined_research_pipeline",
        "ad_creative_pipeline",
        "visual_production_pipeline",
        "finalize_pipeline",
    }
    assert not [t for t in root_agent.tools if isinstance(t, AgentTool)]


# The six Pro (critic_model) producers fail over to worker_model on 429/5xx via
# ADK FallbackModel. Agents are built at import time, so the backup is compared to
# the agent package's own config.critic_fallback_model (as resolved at import).
@pytest.mark.parametrize(
    "path",
    [
        "creative_agent.agent:root_agent",
        "creative_agent.agent:visual_generator",
        "creative_agent.agent:combined_report_composer",
        "creative_agent.agent:combined_web_evaluator",
        "interactive_creative.agent:root_agent",
        "trend_scout.agent:root_agent",
    ],
)
def test_pro_producers_fall_back_to_worker(path):
    import importlib

    from google.adk.models import FallbackModel

    mod, attr = path.split(":")
    module = importlib.import_module(mod)
    agent, config = getattr(module, attr), module.config
    assert isinstance(agent.model, FallbackModel)
    assert [m.model for m in agent.model.models] == [
        config.critic_model,
        config.critic_fallback_model,
    ]
    # Both delegates must carry the global pin (bare strings would lose it).
    for m in agent.model.models:
        assert m.client_kwargs == {"location": "global"}


# --- Structured creative brief (brief_writer / brief_reviser) ---------------


def test_brief_writer_is_retry_wrapped_after_the_composer():
    """The brief writer runs after the composer, wrapped in a RetryUntilKeyNode
    keyed on creative_brief (2 attempts: a fresh structured turn usually
    recovers an empty one; more would only delay the creative stages), itself
    wrapped fail-soft so a raising writer cannot fail the research step."""
    from agent_common import FailSoftNode, RetryUntilKeyNode
    from creative_agent.agent import combined_research_pipeline as wf

    edges = _graph_edges(wf)
    assert ("combined_report_composer", "brief_writer_failsoft", None) in edges
    soft = _graph_nodes(wf)["brief_writer_failsoft"]
    assert isinstance(soft, FailSoftNode)
    w = soft.node
    assert isinstance(w, RetryUntilKeyNode)
    assert w.output_key == "creative_brief"
    assert w.max_attempts == 2
    assert w.node.name == "brief_writer"


def test_brief_writer_and_reviser_share_one_factory_config():
    """Writer and reviser are built by one factory: same worker-bucket model,
    schema, retry and callbacks; only the name (and the writer's per-run reset)
    differ."""
    from creative_agent import agent as ca
    from creative_agent import callbacks, prompts
    from creative_agent.config import SCHEMA_RETRY, config
    from creative_agent.schemas import CreativeBrief

    assert ca.brief_writer.name == "brief_writer"
    assert ca.brief_reviser.name == "brief_reviser"
    for a in (ca.brief_writer, ca.brief_reviser):
        assert a.model.model == config.worker_model
        assert a.output_schema is CreativeBrief
        assert a.output_key == "creative_brief"
        assert a.retry_config is SCHEMA_RETRY
        assert a.instruction == prompts.CREATIVE_BRIEF_WRITER_INSTR
        assert a.mode == "single_turn"
        assert a.include_contents == "none"
        assert callbacks.rate_limit_callback in a.canonical_before_model_callbacks
        cbs = a.canonical_after_model_callbacks
        assert cbs.index(callbacks.scrub_surrogates_in_response) < cbs.index(
            callbacks.log_empty_turn_finish_reason
        )
    assert ca.brief_writer.before_agent_callback is callbacks.reset_brief_state
    assert ca.brief_reviser.before_agent_callback is None


def test_reset_brief_state_clears_previous_run_values():
    """The writer starts each research run clean: a stale brief from an earlier
    run would otherwise count as populated (RetryUntilKeyNode limitation) and
    stale issues/counters would leak into the gate."""
    from types import SimpleNamespace

    from creative_agent.callbacks import reset_brief_state

    state = {
        "creative_brief": {"old": True},
        "creative_brief_md": "**Single-minded proposition:** old",
        "brief_issues": "- old",
        "brief_revision_rounds_used": 1,
        "creative_brief__issues": ["old"],
        "creative_brief__retry_exhausted": True,
    }
    assert reset_brief_state(SimpleNamespace(state=state)) is None
    assert state == {
        "creative_brief": None,
        "creative_brief_md": "",
        "brief_issues": "",
        "brief_revision_rounds_used": 0,
        "creative_brief__issues": None,
        "creative_brief__retry_exhausted": None,
    }


def test_brief_writer_instruction_tokens():
    """Required campaign tokens + optional research/revision tokens."""
    from creative_agent.prompts import CREATIVE_BRIEF_WRITER_INSTR as instr

    for token in (
        "{brand}",
        "{target_product}",
        "{target_audience}",
        "{key_selling_points}",
        "{target_search_trends}",
        "{combined_final_cited_report?}",
        "{sources?}",
        "{visual_avoid?}",
        "{brand_colors?}",
        "{brief_issues?}",
        "{brand_history?}",
        "{creative_brief?}",
    ):
        assert token in instr, token
    assert '"X, but Y"' in instr
    assert "light_touch" in instr
    assert "never a likeness" in instr


def test_brief_gate_routes_through_a_bounded_revision_cycle():
    """composer → writer → gate → ok: PDF → ready | revise: reviser → gate. The
    reviser loops back to the gate (a routed cycle), whose revision counter
    bounds the passes; both brief agents are fail-soft wrapped."""
    from agent_common import FailSoftNode
    from creative_agent.agent import combined_research_pipeline as wf

    edges = _graph_edges(wf)
    assert ("brief_writer_failsoft", "brief_gate", None) in edges
    assert ("brief_gate", "save_research_pdf_node", "ok") in edges
    assert ("brief_gate", "brief_reviser_failsoft", "revise") in edges
    assert ("brief_reviser_failsoft", "brief_gate", None) in edges
    assert "brief_recheck" not in _graph_nodes(wf)
    reviser = _graph_nodes(wf)["brief_reviser_failsoft"]
    assert isinstance(reviser, FailSoftNode)
    assert reviser.node.name == "brief_reviser"
    assert {src for src, dst, _ in edges if dst == "brief_gate"} == {
        "brief_writer_failsoft",
        "brief_reviser_failsoft",
    }
    assert not any(
        src == "brief_writer_failsoft" and dst == "research_report_ready"
        for src, dst, _ in edges
    )


def _clean_brief():
    from tests.test_creative_agent_graph import _BRIEF

    return dict(_BRIEF)


def test_brief_gate_decision_clean_brief():
    from creative_agent.agent import brief_gate_decision
    from creative_agent.brief_render import render_brief_markdown

    route, delta = brief_gate_decision({"creative_brief": _clean_brief()}, 1)
    assert route == "ok"
    assert delta == {
        "brief_issues": "",
        "creative_brief__issues": None,
        "creative_brief_md": render_brief_markdown(_clean_brief(), heading=False),
    }


def test_brief_gate_decision_passes_the_trend_to_the_and_check():
    from creative_agent.agent import brief_gate_decision

    brief = {
        **_clean_brief(),
        "single_minded_proposition": "Every Dungeons and Dragons night needs skates.",
    }
    assert brief_gate_decision({"creative_brief": brief}, 1)[0] == "revise"
    state = {"creative_brief": brief, "target_search_trends": "Dungeons and Dragons"}
    assert brief_gate_decision(state, 1)[0] == "ok"


def test_brief_gate_decision_revises_within_budget():
    from creative_agent.agent import brief_gate_decision

    brief = {**_clean_brief(), "insight": "Coyotes like skates."}
    route, delta = brief_gate_decision({"creative_brief": brief}, 1)
    assert route == "revise"
    assert delta["brief_revision_rounds_used"] == 1
    assert delta["brief_issues"].startswith("- insight has no tension")
    assert "Coyotes like skates." in delta["creative_brief_md"]


def test_brief_gate_decision_records_residual_issues_when_budget_spent():
    from creative_agent.agent import brief_gate_decision

    brief = {**_clean_brief(), "insight": "Coyotes like skates."}
    state = {"creative_brief": brief, "brief_revision_rounds_used": 1}
    route, delta = brief_gate_decision(state, 1)
    assert route == "ok"
    assert delta["brief_issues"] == ""
    (issue,) = delta["creative_brief__issues"]
    assert issue.startswith("insight has no tension")
    # Revision disabled (0 rounds): straight to ok with the issues recorded.
    route, delta = brief_gate_decision({"creative_brief": brief}, 0)
    assert route == "ok" and delta["creative_brief__issues"]
    assert delta["creative_brief_md"].startswith("**Single-minded proposition:**")


def test_brief_gate_decision_second_round_within_a_budget_of_two():
    from creative_agent.agent import brief_gate_decision

    brief = {**_clean_brief(), "insight": "Coyotes like skates."}
    state = {"creative_brief": brief, "brief_revision_rounds_used": 1}
    route, delta = brief_gate_decision(state, 2)
    assert route == "revise" and delta["brief_revision_rounds_used"] == 2
    state["brief_revision_rounds_used"] = 2
    assert brief_gate_decision(state, 2)[0] == "ok"


def test_brief_gate_passes_brand_product_and_sources_to_the_check():
    from creative_agent.agent import brief_gate_decision

    brief = {**_clean_brief(), "single_minded_proposition": "Mac and Cheese wins."}
    assert brief_gate_decision({"creative_brief": brief}, 1)[0] == "revise"
    state = {"creative_brief": brief, "target_product": "Mac and Cheese"}
    assert brief_gate_decision(state, 1)[0] == "ok"
    # _BRIEF cites src-1: unknown when the run's sources lack it.
    state = {"creative_brief": _clean_brief(), "sources": {"src-9": {}}}
    route, delta = brief_gate_decision(state, 1)
    assert route == "revise" and "unknown sources" in delta["brief_issues"]


def test_brief_gate_decision_skips_revision_for_a_missing_brief():
    """A missing brief (writer exhausted) is not revised: there is nothing to
    revise, and creative_brief__retry_exhausted already reports it."""
    from creative_agent.agent import brief_gate_decision

    for state in ({}, {"creative_brief": None}, {"creative_brief": ""}):
        assert brief_gate_decision(state, 2) == (
            "ok",
            {"brief_issues": "", "creative_brief_md": ""},
        )


def test_brief_gate_honours_brand_colors():
    from creative_agent.agent import brief_gate_decision

    brief = {**_clean_brief(), "brand": {**_clean_brief()["brand"]}}
    brief["brand"]["distinctive_assets"] = []
    assert brief_gate_decision({"creative_brief": brief}, 1)[0] == "ok"
    state = {"creative_brief": brief, "brand_colors": "ACME red"}
    assert brief_gate_decision(state, 1)[0] == "revise"


def test_brief_failsoft_error_deltas():
    """Writer failure → no brief + exhaustion marker; reviser failure → keep the
    pre-revision brief, record its issues, spend the revision budget."""
    from creative_agent import agent as ca

    delta = ca._brief_writer_failed({"creative_brief": {"stale": 1}}, ValueError())
    assert delta["creative_brief"] is None
    assert delta["creative_brief_md"] == ""
    assert delta["creative_brief__retry_exhausted"] is True

    brief = {**_clean_brief(), "insight": "Coyotes like skates."}
    state = {"creative_brief": brief, "brief_revision_rounds_used": 1}
    delta = ca._brief_reviser_failed(state, ValueError())
    assert delta["creative_brief"] == brief
    assert "Coyotes like skates." in delta["creative_brief_md"]
    assert delta["brief_issues"] == ""
    (issue,) = delta["creative_brief__issues"]
    assert issue.startswith("insight has no tension")
    assert delta["brief_revision_rounds_used"] >= ca.config.brief_revision_rounds


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 1), ("", 1), ("0", 0), ("2", 2), ("5", 2), ("-1", 0), ("x", 1)],
)
def test_brief_revision_rounds_env(monkeypatch, raw, expected):
    from creative_agent.config import ResearchConfiguration

    if raw is None:
        monkeypatch.delenv("BRIEF_REVISION_ROUNDS", raising=False)
    else:
        monkeypatch.setenv("BRIEF_REVISION_ROUNDS", raw)
    assert ResearchConfiguration().brief_revision_rounds == expected


def test_brief_revision_rounds_ships_to_agent_engine():
    import deployment.deploy_agent as da

    assert "BRIEF_REVISION_ROUNDS" in da.ENV_VAR_DICT
    assert da.ENV_VAR_DICT["BRIEF_REVISION_ROUNDS"] is not None


def test_creative_final_state_summary_includes_brief(caplog):
    import logging
    from types import SimpleNamespace

    from creative_agent import callbacks

    ctx = SimpleNamespace(state={"creative_brief": {"a": 1}}, invocation_id="i")
    with caplog.at_level(logging.INFO):
        callbacks.log_final_state_summary(ctx)
    assert "'creative_brief': 'present(dict, n=1)'" in caplog.text


# --- Ad copy gate (copy_gate / ad_copy_reviser) ------------------------------


def test_ad_copy_reviser_mirrors_the_critic_config():
    """Same worker bucket, schema, output key, retry and callbacks as the
    critic (it rewrites the critic's output in place), plus the safety net."""
    from creative_agent import agent as ca
    from creative_agent import callbacks, prompts
    from creative_agent.config import SCHEMA_RETRY, config
    from creative_agent.schemas import FinalAdCopyList

    r = ca.ad_copy_reviser
    assert r.model.model == config.worker_model == ca.ad_copy_critic.model.model
    assert r.output_schema is FinalAdCopyList
    assert r.output_key == "ad_copy_critique" == ca.ad_copy_critic.output_key
    assert r.retry_config is SCHEMA_RETRY
    assert r.instruction == prompts.AD_COPY_REVISER_INSTR
    assert r.mode == "single_turn"
    assert r.include_contents == "none"
    assert (
        r.canonical_after_model_callbacks
        == ca.ad_copy_critic.canonical_after_model_callbacks
    )
    assert (
        r.canonical_before_model_callbacks
        == ca.ad_copy_critic.canonical_before_model_callbacks
    )
    assert r.after_agent_callback is callbacks.restore_unflagged_copies_callback
    assert r.generate_content_config.temperature == 0.7


def test_ad_copy_reviser_is_fail_soft_wrapped_in_the_graph():
    from agent_common import FailSoftNode
    from creative_agent import agent as ca

    nodes = _graph_nodes(ca.ad_creative_pipeline)
    soft = nodes["ad_copy_reviser_failsoft"]
    assert isinstance(soft, FailSoftNode)
    assert soft.node.name == "ad_copy_reviser"
    assert soft.on_error is ca._ad_copy_reviser_failed
    edges = _graph_edges(ca.ad_creative_pipeline)
    assert {src for src, dst, _ in edges if dst == "copy_gate"} == {
        "ad_copy_critic",
        "ad_copy_reviser_failsoft",
    }


def test_ad_copy_drafter_resets_the_copy_revision_state():
    from types import SimpleNamespace

    from creative_agent import agent as ca
    from creative_agent import callbacks

    assert ca.ad_copy_drafter.before_agent_callback is callbacks.reset_copy_state
    state = {
        "ad_copy_issues": "- old",
        "ad_copy_flagged_ids": ["1"],
        "ad_copy_critique__before_revision": {"ad_copies": []},
        "ad_copy_revision_rounds_used": 1,
        "ad_copy_critique__issues": ["old"],
        "ad_copy_critique": {"kept": True},
        "ad_copy_feedback": "punchier",  # user input: never reset here
    }
    assert callbacks.reset_copy_state(SimpleNamespace(state=state)) is None
    assert state == {
        "ad_copy_issues": "",
        "ad_copy_flagged_ids": None,
        "ad_copy_critique__before_revision": None,
        "ad_copy_revision_rounds_used": 0,
        "ad_copy_critique__issues": None,
        "ad_copy_critique": {"kept": True},
        "ad_copy_feedback": "punchier",
    }


def _final_copy(original_id=1, **overrides):
    copy = {
        "original_id": original_id,
        "tone_style": "Humorous",
        "headline": f"Headline {original_id}",
        "body_text": "Rocket Skates make you fast.",
        "trend_connection": "t",
        "audience_appeal_rationale": "a",
        "social_caption": "Zoom.",
        "call_to_action": "Order yours today",
        "brief_checks": [
            {"item": "proposition", "passed": True, "note": ""},
            {"item": "mandatories", "passed": True, "note": ""},
        ],
        "detailed_performance_rationale": "r",
    }
    copy.update(overrides)
    return copy


def _four(*copies):
    """Pad ``copies`` with clean copies (ids 11+) to the expected 4."""
    pad = [_final_copy(11 + i) for i in range(4 - len(copies))]
    return (*copies, *pad)


def _copy_state(*copies, **extra):
    return {
        "ad_copy_critique": {"ad_copies": list(_four(*copies))},
        "target_product": "Rocket Skates",
        "brand": "Acme",
        **extra,
    }


def test_copy_gate_decision_clean_copies():
    from creative_agent.agent import copy_gate_decision

    route, delta = copy_gate_decision(_copy_state(_final_copy(1), _final_copy(2)), 1)
    assert route == "ok"
    assert delta == {
        "ad_copy_issues": "",
        "ad_copy_flagged_ids": None,
        "ad_copy_critique__before_revision": None,
        "ad_copy_critique__issues": None,
    }


def test_copy_gate_decision_revises_flagged_copies_within_budget():
    from creative_agent.agent import copy_gate_decision

    bad = _final_copy(2, body_text="Go fast.")
    state = _copy_state(_final_copy(1), bad)
    route, delta = copy_gate_decision(state, 1)
    assert route == "revise"
    assert delta["ad_copy_flagged_ids"] == ["2"]
    assert delta["ad_copy_revision_rounds_used"] == 1
    assert delta["ad_copy_critique__before_revision"] == state["ad_copy_critique"]
    assert delta["ad_copy_critique__before_revision"] is not state["ad_copy_critique"]
    assert delta["ad_copy_issues"].startswith('- **Copy 2 ("Headline 2"):**')
    assert "  - product not named" in delta["ad_copy_issues"]
    assert "Copy 1" not in delta["ad_copy_issues"]


def test_copy_gate_decision_records_residual_issues_when_budget_spent():
    from creative_agent.agent import copy_gate_decision

    state = _copy_state(
        _final_copy(3, body_text="Go fast."), ad_copy_revision_rounds_used=1
    )
    route, delta = copy_gate_decision(state, 1)
    assert route == "ok"
    assert delta["ad_copy_issues"] == ""
    assert delta["ad_copy_flagged_ids"] is None
    (issue,) = delta["ad_copy_critique__issues"]
    assert issue.startswith('Copy 3 ("Headline 3"): product not named')
    # Revision disabled (0 rounds): straight to ok with the issues recorded.
    route, delta = copy_gate_decision(_copy_state(_final_copy(3, body_text="x")), 0)
    assert route == "ok" and delta["ad_copy_critique__issues"]


def test_copy_gate_decision_self_reported_gating_policy():
    """Advisory self-reports never revise; proposition/mandatories revise but
    are never recorded as residual issues (only deterministic ones are)."""
    from creative_agent.agent import copy_gate_decision

    def checks(*items):
        failed = [{"item": i, "passed": False, "note": "n"} for i in items]
        complete = [
            {"item": i, "passed": True, "note": ""}
            for i in ("proposition", "mandatories")
            if i not in items
        ]
        return failed + complete

    advisory = _final_copy(1, brief_checks=checks("tone", "trend_bridge", "cta"))
    assert copy_gate_decision(_copy_state(advisory), 1)[0] == "ok"

    gating = _final_copy(1, brief_checks=checks("mandatories"))
    route, delta = copy_gate_decision(_copy_state(gating), 1)
    assert route == "revise"
    assert "brief check failed: mandatories" in delta["ad_copy_issues"]
    route, delta = copy_gate_decision(
        _copy_state(gating, ad_copy_revision_rounds_used=1), 1
    )
    assert route == "ok"
    assert delta["ad_copy_critique__issues"] is None

    both = _final_copy(2, body_text="Go fast.", brief_checks=checks("proposition"))
    route, delta = copy_gate_decision(
        _copy_state(both, ad_copy_revision_rounds_used=1), 1
    )
    (issue,) = delta["ad_copy_critique__issues"]
    assert issue.startswith('Copy 2 ("Headline 2"): product not named')


def test_copy_gate_decision_reads_the_brief_avoid_list():
    from creative_agent.agent import copy_gate_decision

    copy = _final_copy(1, body_text="Rocket Skates: no more cliff falls.")
    assert copy_gate_decision(_copy_state(copy), 1)[0] == "ok"
    brief = {**_clean_brief(), "avoid": ["cliff falls"]}
    for value in (brief, json.dumps(brief)):
        route, delta = copy_gate_decision(_copy_state(copy, creative_brief=value), 1)
        assert route == "revise"
        assert "avoided term 'cliff falls'" in delta["ad_copy_issues"]
    # visual_avoid is the visual stage's input, not a copy rule.
    state = _copy_state(copy, visual_avoid="cliff falls")
    assert copy_gate_decision(state, 1)[0] == "ok"


def test_copy_gate_decision_records_structural_issues_without_revising():
    """Too few copies / an incomplete gating checklist are recorded on the ok
    exit but never route a revision (the per-copy reviser cannot fix them)."""
    from creative_agent.agent import copy_gate_decision

    state = {
        "ad_copy_critique": {
            "ad_copies": [_final_copy(1), _final_copy(2, brief_checks=[])]
        },
        "target_product": "Rocket Skates",
    }
    route, delta = copy_gate_decision(state, 2)
    assert route == "ok"
    # No brief: no checklist to apply, so only the missing copies are noted.
    assert delta["ad_copy_critique__issues"] == ["only 2 of 4 ad copies were produced."]
    route, delta = copy_gate_decision({**state, "creative_brief": _clean_brief()}, 2)
    assert route == "ok"
    assert delta["ad_copy_critique__issues"] == [
        "only 2 of 4 ad copies were produced.",
        "1 of 2 ad copies lack the proposition/mandatories brief check.",
    ]


def test_copy_gate_decision_passes_brand_to_the_product_check():
    from creative_agent.agent import copy_gate_decision

    copy = _final_copy(1, headline="Go", body_text="Only on Apple.")
    copy["social_caption"] = "Go."
    copy["call_to_action"] = "Shop now"
    copies = {"ad_copies": [copy]}
    state = {"ad_copy_critique": copies, "target_product": "iPhone 16 Pro"}
    assert copy_gate_decision(state, 2)[0] == "revise"
    assert copy_gate_decision({**state, "brand": "Apple"}, 2)[0] == "ok"


def test_copy_gate_decision_passes_trend_and_mandatories_to_the_avoid_filter():
    from creative_agent.agent import copy_gate_decision

    copy = _final_copy(
        1, body_text="Rocket Skates for Taylor Swift fans. Gambling help: call."
    )
    brief = {
        **_clean_brief(),
        "avoid": ["Taylor Swift", "gambling"],
        "mandatories": ["Include the problem gambling helpline"],
    }
    state = _copy_state(
        copy, creative_brief=brief, target_search_trends="Taylor Swift Eras Tour"
    )
    assert copy_gate_decision(state, 1)[0] == "ok"


def test_copy_gate_decision_skips_missing_copies():
    from creative_agent.agent import copy_gate_decision

    for critique in (None, "", {"ad_copies": []}, "not json"):
        state = {"ad_copy_critique": critique, "target_product": "Rocket Skates"}
        route, delta = copy_gate_decision(state, 2)
        assert route == "ok", critique
        assert delta["ad_copy_critique__issues"] is None


def test_ad_copy_reviser_failsoft_error_delta():
    """A raising reviser keeps the pre-revision copies, records their issues and
    spends the revision budget."""
    from creative_agent import agent as ca

    before = {"ad_copies": list(_four(_final_copy(2, body_text="Go fast.")))}
    state = _copy_state(
        _final_copy(2, body_text="half-written"),
        ad_copy_critique__before_revision=before,
        ad_copy_revision_rounds_used=1,
        ad_copy_issues="- x",
    )
    delta = ca._ad_copy_reviser_failed(state, ValueError())
    assert delta["ad_copy_critique"] == before
    assert delta["ad_copy_issues"] == ""
    assert delta["ad_copy_critique__before_revision"] is None
    (issue,) = delta["ad_copy_critique__issues"]
    assert "product not named" in issue
    assert delta["ad_copy_revision_rounds_used"] >= ca.config.copy_revision_rounds


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 1), ("", 1), ("0", 0), ("2", 2), ("5", 2), ("-1", 0), ("x", 1)],
)
def test_copy_revision_rounds_env(monkeypatch, raw, expected):
    from creative_agent.config import ResearchConfiguration

    if raw is None:
        monkeypatch.delenv("COPY_REVISION_ROUNDS", raising=False)
    else:
        monkeypatch.setenv("COPY_REVISION_ROUNDS", raw)
    assert ResearchConfiguration().copy_revision_rounds == expected


def test_copy_revision_rounds_ships_to_agent_engine():
    import deployment.deploy_agent as da

    assert da.ENV_VAR_DICT["COPY_REVISION_ROUNDS"] is not None


# --- Visual concept gate (concept_gate / visual_concept_fixer) --------------


def test_visual_concept_agents_are_rate_limited():
    from creative_agent import agent as ca
    from creative_agent import callbacks

    for a in (
        ca.art_director,
        ca.visual_concept_drafter,
        ca.visual_concept_critic,
        ca.visual_concept_finalizer,
        ca.visual_concept_fixer,
    ):
        assert callbacks.rate_limit_callback in a.canonical_before_model_callbacks, (
            a.name
        )


def test_visual_concept_fixer_config():
    """Same worker bucket, schema, output key and retry as the finalizer (it
    rewrites the finalizer's output in place); restore THEN guard."""
    from creative_agent import agent as ca
    from creative_agent import callbacks, prompts
    from creative_agent.config import SCHEMA_RETRY, config
    from creative_agent.schemas import VisualConceptFinalList

    f = ca.visual_concept_fixer
    assert f.model.model == config.worker_model
    assert f.model.model == ca.visual_concept_finalizer.model.model
    assert f.output_schema is VisualConceptFinalList
    assert f.output_key == "final_visual_concepts"
    assert f.output_key == ca.visual_concept_finalizer.output_key
    assert f.retry_config is SCHEMA_RETRY
    assert f.instruction == prompts.VISUAL_CONCEPT_FIXER_INSTR
    assert f.mode == "single_turn"
    assert f.include_contents == "none"
    assert f.canonical_before_model_callbacks == [callbacks.rate_limit_callback]
    assert f.canonical_after_model_callbacks == [
        callbacks.scrub_surrogates_in_response,
        callbacks.log_empty_turn_finish_reason,
    ]
    assert f.canonical_after_agent_callbacks == [
        callbacks.restore_unflagged_concepts_callback,
        callbacks.ensure_trend_and_product_callback,
    ]


def test_visual_concept_fixer_is_fail_soft_wrapped_in_the_graph():
    from agent_common import FailSoftNode
    from creative_agent import agent as ca

    nodes = _graph_nodes(ca.visual_generation_pipeline)
    soft = nodes["visual_concept_fixer_failsoft"]
    assert isinstance(soft, FailSoftNode)
    assert soft.node.name == "visual_concept_fixer"
    assert soft.on_error is ca._visual_concept_fixer_failed
    fixer = soft.node
    assert fixer.canonical_after_agent_callbacks == (
        ca.visual_concept_fixer.canonical_after_agent_callbacks
    )


def test_visual_concept_fixer_instruction_tokens():
    import re

    from creative_agent import prompts

    instr = prompts.VISUAL_CONCEPT_FIXER_INSTR
    for token in (
        "{final_visual_concepts?}",
        "{visual_concept_issues?}",
        "{ad_copy_critique?}",
        "{creative_brief_md?}",
        "{brand}",
        "{target_product}",
        "{target_search_trends}",
        "{style_shortlist?}",
        "{visual_intent?}",
        "{visual_avoid?}",
        "{brand_colors?}",
    ):
        assert token in instr, token
    assert prompts.IMAGE_PROMPT_GUIDE in instr
    assert prompts.VISUAL_CONCEPT_RULES in instr
    assert "Rewrite ONLY those concepts" in instr
    assert "stays verbatim, field for field" in instr
    assert "keeps its `ad_copy_id`, `concept_name`" in instr
    tokens = re.findall(r"\{([^{}]*)\}", instr)
    for token in tokens:
        assert re.fullmatch(r"[A-Za-z_]\w*\??", token), token
    stripped = re.sub(r"\{[^{}]*\}", "", instr)
    assert "{" not in stripped and "}" not in stripped


def test_art_director_resets_the_concept_revision_state():
    from types import SimpleNamespace

    from creative_agent import agent as ca
    from creative_agent import callbacks

    assert ca.art_director.before_agent_callback is callbacks.reset_concept_state
    state = {
        "visual_concept_issues": "- old",
        "visual_concept_flagged_ids": ["1"],
        "final_visual_concepts__before_revision": {"visual_concepts": []},
        "visual_concept_revision_rounds_used": 1,
        "final_visual_concepts__issues": ["old"],
        "final_visual_concepts": {"kept": True},
    }
    assert callbacks.reset_concept_state(SimpleNamespace(state=state)) is None
    assert state == {
        "visual_concept_issues": "",
        "visual_concept_flagged_ids": None,
        "final_visual_concepts__before_revision": None,
        "visual_concept_revision_rounds_used": 0,
        "final_visual_concepts__issues": None,
        "final_visual_concepts": {"kept": True},
    }


def _concept(ad_copy_id=1, **overrides):
    concept = {
        "ad_copy_id": ad_copy_id,
        "concept_name": f"Concept {ad_copy_id}",
        "trend_motif": "a roadrunner dust cloud",
        "image_generation_prompt": "A watercolor of Rocket Skates in a dust cloud.",
    }
    concept.update(overrides)
    return concept


def _concept_state(*concepts, **extra):
    return {
        "final_visual_concepts": {"visual_concepts": list(concepts)},
        "ad_copy_critique": {
            "ad_copies": [
                _final_copy(i, headline=f"Beep beep {i}") for i in range(1, 5)
            ]
        },
        "target_product": "Rocket Skates",
        **extra,
    }


def test_concept_gate_decision_clean_concepts():
    from creative_agent.agent import concept_gate_decision

    quoted = _concept(2, image_generation_prompt='Bold type reads "Beep beep 2".')
    route, delta = concept_gate_decision(_concept_state(_concept(1), quoted), 1)
    assert route == "ok"
    assert delta == {
        "visual_concept_issues": "",
        "visual_concept_flagged_ids": None,
        "final_visual_concepts__before_revision": None,
        "final_visual_concepts__issues": None,
    }


def test_concept_gate_decision_revises_flagged_concepts_within_budget():
    from creative_agent.agent import concept_gate_decision

    bad = _concept(2, image_generation_prompt='Neon sign reads "Speed is life".')
    state = _concept_state(_concept(1), bad)
    route, delta = concept_gate_decision(state, 1)
    assert route == "revise"
    assert delta["visual_concept_flagged_ids"] == ["2"]
    assert delta["visual_concept_revision_rounds_used"] == 1
    before = delta["final_visual_concepts__before_revision"]
    assert before == state["final_visual_concepts"]
    assert before is not state["final_visual_concepts"]
    issues = delta["visual_concept_issues"]
    assert issues.startswith('- **Concept 2 ("Concept 2"):**')
    assert '  - in-image text "Speed is life" is not the paired' in issues
    assert '"Beep beep 2"' in issues
    assert "Concept 1" not in issues


def test_concept_gate_decision_records_residual_issues_when_budget_spent():
    from creative_agent.agent import concept_gate_decision

    state = _concept_state(
        _concept(3, trend_motif=""), visual_concept_revision_rounds_used=1
    )
    route, delta = concept_gate_decision(state, 1)
    assert route == "ok"
    assert delta["visual_concept_issues"] == ""
    assert delta["visual_concept_flagged_ids"] is None
    (issue,) = delta["final_visual_concepts__issues"]
    assert issue.startswith('Concept 3 ("Concept 3"): trend_motif is empty')
    route, delta = concept_gate_decision(_concept_state(_concept(3, trend_motif="")), 0)
    assert route == "ok" and delta["final_visual_concepts__issues"]


def test_concept_gate_decision_allows_quoted_brand_and_product():
    """The gate reads brand/target_product from state: a quoted logo or product
    name is neither a mismatch nor counted toward the text cap."""
    from creative_agent.agent import concept_gate_decision

    concepts = [
        _concept(1, image_generation_prompt='Type reads "Beep beep 1".'),
        _concept(2, image_generation_prompt='Type reads "Beep beep 2".'),
        _concept(3, image_generation_prompt='A decal with lettering "ACME".'),
        _concept(4, image_generation_prompt='Box label reads "Rocket Skates".'),
    ]
    route, delta = concept_gate_decision(_concept_state(*concepts, brand="Acme"), 1)
    assert route == "ok", delta


def test_concept_gate_decision_skips_missing_concepts():
    from creative_agent.agent import concept_gate_decision

    for concepts in (None, "", {"visual_concepts": []}, "not json"):
        state = {"final_visual_concepts": concepts, "ad_copy_critique": None}
        route, delta = concept_gate_decision(state, 2)
        assert route == "ok", concepts
        assert delta["final_visual_concepts__issues"] is None


def test_visual_concept_fixer_failsoft_error_delta():
    """A raising fixer keeps the pre-revision concepts, records their issues and
    spends the fix budget."""
    from creative_agent import agent as ca

    before = {"visual_concepts": [_concept(2, trend_motif="")]}
    state = _concept_state(
        _concept(2, trend_motif="", concept_name="half-written"),
        final_visual_concepts__before_revision=before,
        visual_concept_revision_rounds_used=1,
        visual_concept_issues="- x",
    )
    delta = ca._visual_concept_fixer_failed(state, ValueError())
    assert delta["final_visual_concepts"] == before
    assert delta["visual_concept_issues"] == ""
    assert delta["final_visual_concepts__before_revision"] is None
    (issue,) = delta["final_visual_concepts__issues"]
    assert "trend_motif is empty" in issue
    assert (
        delta["visual_concept_revision_rounds_used"]
        >= ca.config.concept_revision_rounds
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 1), ("", 1), ("0", 0), ("2", 2), ("5", 2), ("-1", 0), ("x", 1)],
)
def test_concept_revision_rounds_env(monkeypatch, raw, expected):
    from creative_agent.config import ResearchConfiguration

    if raw is None:
        monkeypatch.delenv("CONCEPT_REVISION_ROUNDS", raising=False)
    else:
        monkeypatch.setenv("CONCEPT_REVISION_ROUNDS", raw)
    assert ResearchConfiguration().concept_revision_rounds == expected


def test_concept_revision_rounds_ships_to_agent_engine():
    import deployment.deploy_agent as da

    assert da.ENV_VAR_DICT["CONCEPT_REVISION_ROUNDS"] is not None
