"""Tests for agent pipeline structure and configuration."""


def test_creative_agent_root_has_expected_tools():
    from creative_agent.agent import root_agent

    tool_names = [
        getattr(t, "name", getattr(t, "__name__", str(t))) for t in root_agent.tools
    ]
    expected = [
        "combined_research_pipeline",
        "ad_creative_pipeline",
        "visual_production_pipeline",
        "creative_eval_agent",
        "save_eval_report_to_gcs",
        "save_draft_report_artifact",
        "save_creative_gallery_html",
        "write_trends_to_bq",
        "write_eval_report_to_bq",
        "memorize",
    ]
    for name in expected:
        assert name in tool_names, f"Missing tool: {name}"


def test_creative_agent_root_output_key_not_set():
    """Root agent should not have an output_key (it orchestrates)."""
    from creative_agent.agent import root_agent

    assert (
        not hasattr(root_agent, "output_key")
        or root_agent.output_key is None
        or root_agent.output_key == ""
    )


def test_combined_research_pipeline_sub_agent_order():
    from creative_agent.agent import combined_research_pipeline
    from agent_common import RetryUntilKeyAgent

    names = [a.name for a in combined_research_pipeline.sub_agents]
    assert names == [
        "merge_parallel_insights",
        "combined_web_evaluator",
        "enhanced_combined_searcher_resilient",
        "combined_report_composer",
    ]

    w = combined_research_pipeline.sub_agents[2]
    assert isinstance(w, RetryUntilKeyAgent)
    assert w.output_key == "refined_web_search_insights"

    from google.adk.agents import SequentialAgent

    pair = w.sub_agents[0]
    assert isinstance(pair, SequentialAgent)
    assert pair.sub_agents[0].output_key == "refined_web_search_raw"
    assert pair.sub_agents[-1].output_key == "refined_web_search_insights"


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


def test_ad_creative_pipeline_sub_agent_order():
    from creative_agent.agent import ad_creative_pipeline

    names = [a.name for a in ad_creative_pipeline.sub_agents]
    assert names == ["ad_copy_drafter", "ad_copy_critic"]


def test_visual_generation_pipeline_sub_agent_order():
    from creative_agent.agent import visual_generation_pipeline

    names = [a.name for a in visual_generation_pipeline.sub_agents]
    assert names == [
        "visual_concept_drafter",
        "visual_concept_critic",
        "visual_concept_finalizer",
    ]


def test_visual_production_pipeline_wraps_generator_in_retry():
    """The image step must be retry-wrapped: visual_generator intermittently
    returns MALFORMED_FUNCTION_CALL and never emits generate_image, shipping an
    empty gallery. RetryUntilKeyAgent re-runs it until _images_generated is set
    (generate_image's idempotency guard makes a re-run safe)."""
    from creative_agent import agent as ca
    from agent_common import RetryUntilKeyAgent

    names = [a.name for a in ca.visual_production_pipeline.sub_agents]
    assert names == ["visual_generation_pipeline", "visual_generator_resilient"]

    w = ca.visual_production_pipeline.sub_agents[-1]
    assert isinstance(w, RetryUntilKeyAgent)
    assert w.output_key == "_images_generated"
    assert w.sub_agents[0] is ca.visual_generator


def test_parallel_planner_has_both_researchers():
    from creative_agent.agent import parallel_planner_agent

    names = [a.name for a in parallel_planner_agent.sub_agents]
    assert "gs_sequential_planner" in names
    assert "ca_sequential_planner" in names


def test_campaign_producer_is_retry_wrapped():
    """WS2: campaign_web_searcher is split into a tool-using searcher (writes
    `campaign_web_search_raw`) + a tool-free synthesizer (writes
    `campaign_web_search_insights`), wrapped as a SequentialAgent inside the
    existing RetryUntilKeyAgent so an empty turn retries the pair instead of
    crashing merge_planners."""
    from agent_common import RetryUntilKeyAgent
    from google.adk.agents import SequentialAgent
    from creative_agent.sub_agents.campaign_researcher.agent import (
        ca_sequential_planner,
    )

    w = ca_sequential_planner.sub_agents[-1]
    assert isinstance(w, RetryUntilKeyAgent)
    assert w.output_key == "campaign_web_search_insights"

    pair = w.sub_agents[0]
    assert isinstance(pair, SequentialAgent)
    assert pair.sub_agents[0].output_key == "campaign_web_search_raw"
    assert pair.sub_agents[-1].output_key == "campaign_web_search_insights"


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
    """WS2: gs_web_searcher is split into a tool-using searcher (writes
    `gs_web_search_raw`) + a tool-free synthesizer (writes
    `gs_web_search_insights`), wrapped as a SequentialAgent inside the existing
    RetryUntilKeyAgent so an empty turn retries the pair instead of crashing
    merge_planners."""
    from agent_common import RetryUntilKeyAgent
    from google.adk.agents import SequentialAgent
    from creative_agent.sub_agents.trend_researcher.agent import (
        gs_sequential_planner,
    )

    w = gs_sequential_planner.sub_agents[-1]
    assert isinstance(w, RetryUntilKeyAgent)
    assert w.output_key == "gs_web_search_insights"

    pair = w.sub_agents[0]
    assert isinstance(pair, SequentialAgent)
    assert pair.sub_agents[0].output_key == "gs_web_search_raw"
    assert pair.sub_agents[-1].output_key == "gs_web_search_insights"


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
        merge_planners,
        combined_web_evaluator,
        enhanced_combined_searcher,
        refined_web_synthesizer,
        combined_report_composer,
        ad_copy_drafter,
        ad_copy_critic,
        visual_concept_drafter,
        visual_concept_critic,
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
        combined_web_evaluator,
        ad_copy_drafter,
        ad_copy_critic,
        visual_concept_drafter,
        visual_concept_critic,
        visual_concept_finalizer,
        ResearchFeedback,
        AdCopyList,
        FinalAdCopyList,
        VisualConceptList,
        VisualConceptCritiqueList,
        VisualConceptFinalList,
    )

    assert combined_web_evaluator.output_schema == ResearchFeedback
    assert ad_copy_drafter.output_schema == AdCopyList
    assert ad_copy_critic.output_schema == FinalAdCopyList
    assert visual_concept_drafter.output_schema == VisualConceptList
    assert visual_concept_critic.output_schema == VisualConceptCritiqueList
    assert visual_concept_finalizer.output_schema == VisualConceptFinalList


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
        ca.visual_concept_drafter,
        ca.visual_concept_critic,
        ca.visual_concept_finalizer,
        ca.visual_generator,
        ca.root_agent,
    ]
    for a in agents:
        cbs = a.canonical_after_model_callbacks
        assert callbacks.log_empty_turn_finish_reason in cbs, (
            f"{a.name} missing log_empty_turn_finish_reason after_model_callback"
        )


def test_ad_copy_agents_scrub_lone_surrogates():
    """The two ad-copy agents parse model text against an output_schema, so they
    must carry the surrogate scrubber as an after_model_callback (before the
    empty-turn logger) to survive lone Unicode surrogates in the JSON output."""
    from creative_agent import agent as ca
    from creative_agent import callbacks

    for a in (ca.ad_copy_drafter, ca.ad_copy_critic):
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
    from creative_agent.sub_agents.trend_researcher import agent as tr
    from creative_agent.sub_agents.campaign_researcher import agent as cr

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
    from creative_agent.agent import root_agent
    from creative_agent import callbacks

    assert root_agent.after_agent_callback is callbacks.log_final_state_summary
    assert callable(root_agent.after_agent_callback)


def test_creative_eval_agent_has_finish_reason_callback():
    from creative_eval.agent import creative_eval_agent
    from agent_common import log_empty_turn_finish_reason

    assert creative_eval_agent.after_model_callback is log_empty_turn_finish_reason


def test_interactive_root_has_observability_callbacks():
    from interactive_creative.agent import root_agent
    from creative_agent import callbacks

    assert root_agent.after_model_callback is callbacks.log_empty_turn_finish_reason
    assert root_agent.after_agent_callback is callbacks.log_final_state_summary


def test_interactive_creative_uses_resilient_visual_generator():
    """interactive_creative renders images standalone via AgentTool after a review
    checkpoint, so it has the same MALFORMED_FUNCTION_CALL flaw as creative_agent.
    It must invoke the SAME shared resilient wrapper instance (AgentTool does not
    reparent), not the raw visual_generator."""
    from interactive_creative import agent as ic
    from creative_agent.agent import visual_generator, visual_generator_resilient
    from google.adk.tools.agent_tool import AgentTool
    from agent_common import RetryUntilKeyAgent

    matching = [
        t
        for t in ic.root_agent.tools
        if isinstance(t, AgentTool) and t.agent is visual_generator_resilient
    ]
    assert matching, "interactive_creative must invoke the resilient image wrapper"
    assert isinstance(matching[0].agent, RetryUntilKeyAgent)

    # The raw generator must NOT be exposed directly (would bypass the retry).
    raw = [
        t
        for t in ic.root_agent.tools
        if isinstance(t, AgentTool) and t.agent is visual_generator
    ]
    assert not raw, "raw visual_generator must not be exposed; use the wrapper"


def test_trend_scout_root_has_expected_tools():
    from trend_scout.agent import root_agent

    tool_names = [
        getattr(t, "name", getattr(t, "__name__", str(t))) for t in root_agent.tools
    ]
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


def test_trend_scout_sub_agent_output_keys():
    """WS2: understand_trends_agent is split into a searcher (writes
    `info_gtrends_raw`) + a synthesizer (writes JSON `info_gtrends`)."""
    from trend_scout.agent import (
        understand_trends_searcher,
        understand_trends_synthesizer,
        pick_trends_agent,
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
    """WS2: understand_trends is split into searcher + synthesizer, wrapped as a
    SequentialAgent inside the existing RetryUntilKeyAgent so an empty turn
    retries the pair instead of crashing pick_trends_agent. The wrapper is still
    exposed to the orchestrator as an AgentTool."""
    from agent_common import RetryUntilKeyAgent
    from google.adk.agents import SequentialAgent
    from google.adk.tools.agent_tool import AgentTool
    from trend_scout.agent import root_agent

    wrapped = [
        t.agent
        for t in root_agent.tools
        if isinstance(t, AgentTool) and isinstance(t.agent, RetryUntilKeyAgent)
    ]
    matching = [a for a in wrapped if a.output_key == "info_gtrends"]
    assert matching, "no AgentTool wraps a RetryUntilKeyAgent producing info_gtrends"

    pair = matching[0].sub_agents[0]
    assert isinstance(pair, SequentialAgent)
    assert pair.sub_agents[0].output_key == "info_gtrends_raw"
    assert pair.sub_agents[-1].output_key == "info_gtrends"


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


def test_interactive_creative_memorizes_target_search_trends():
    """The interactive orchestrator's memorize step must explicitly enumerate
    target_search_trends. A vague 'store all campaign metadata' instruction lets
    the LLM drop the trend, leaving target_search_trends empty in state (and in
    the trend_creatives / creative_evals BigQuery rows)."""
    from interactive_creative.agent import root_agent

    instr = root_agent.instruction
    # The memorize step must name every state key it has to persist, mirroring
    # creative_agent, not just say "all campaign metadata".
    assert "`key_selling_points`, and `target_search_trends`" in instr
