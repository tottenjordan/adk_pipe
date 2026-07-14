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
    assert w.sub_agents[0].output_key == "refined_web_search_insights"


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


def test_parallel_planner_has_both_researchers():
    from creative_agent.agent import parallel_planner_agent

    names = [a.name for a in parallel_planner_agent.sub_agents]
    assert "gs_sequential_planner" in names
    assert "ca_sequential_planner" in names


def test_campaign_producer_is_retry_wrapped():
    """campaign_web_searcher must be wrapped in a RetryUntilKeyAgent so an empty
    turn (no `campaign_web_search_insights`) retries instead of crashing
    merge_planners."""
    from agent_common import RetryUntilKeyAgent
    from creative_agent.sub_agents.campaign_researcher.agent import (
        ca_sequential_planner,
    )

    w = ca_sequential_planner.sub_agents[-1]
    assert isinstance(w, RetryUntilKeyAgent)
    assert w.output_key == "campaign_web_search_insights"
    assert w.sub_agents[0].output_key == "campaign_web_search_insights"


def test_trend_producer_is_retry_wrapped():
    """gs_web_searcher must be wrapped in a RetryUntilKeyAgent so an empty turn
    (no `gs_web_search_insights`) retries instead of crashing merge_planners."""
    from agent_common import RetryUntilKeyAgent
    from creative_agent.sub_agents.trend_researcher.agent import (
        gs_sequential_planner,
    )

    w = gs_sequential_planner.sub_agents[-1]
    assert isinstance(w, RetryUntilKeyAgent)
    assert w.output_key == "gs_web_search_insights"
    assert w.sub_agents[0].output_key == "gs_web_search_insights"


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
        (enhanced_combined_searcher, "refined_web_search_insights"),
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
    from trend_scout.agent import (
        understand_trends_agent,
        pick_trends_agent,
    )

    assert understand_trends_agent.output_key == "info_gtrends"
    assert pick_trends_agent.output_key == "selected_gtrends"


def test_understand_trends_is_retry_wrapped():
    """understand_trends_agent (google_search + thinking) intermittently emits no
    final text, leaving `info_gtrends` unset and crashing pick_trends_agent. It
    must be exposed to the orchestrator wrapped in a RetryUntilKeyAgent so an
    empty turn retries instead of aborting the run."""
    from agent_common import RetryUntilKeyAgent
    from google.adk.tools.agent_tool import AgentTool
    from trend_scout.agent import root_agent

    wrapped = [
        t.agent
        for t in root_agent.tools
        if isinstance(t, AgentTool) and isinstance(t.agent, RetryUntilKeyAgent)
    ]
    matching = [a for a in wrapped if a.output_key == "info_gtrends"]
    assert matching, "no AgentTool wraps a RetryUntilKeyAgent producing info_gtrends"
    assert matching[0].sub_agents[0].output_key == "info_gtrends"


def test_pick_trends_info_gtrends_optional():
    """pick_trends_agent must tolerate a missing info_gtrends (orchestrator-skip
    or retry-exhaustion) via the optional `{info_gtrends?}` template syntax rather
    than raising KeyError: Context variable not found."""
    from trend_scout.agent import pick_trends_agent

    assert "{info_gtrends?}" in pick_trends_agent.instruction
    assert "{info_gtrends}" not in pick_trends_agent.instruction


def test_trend_scout_orchestrator_thinking_budget_is_bounded_nonzero():
    """The orchestrator's thinking budget must be a small POSITIVE number.

    thinking_budget=0 makes gemini-3.5-flash emit MALFORMED_FUNCTION_CALL when
    invoking an AgentTool with a structured argument (e.g. understand_trends_agent),
    aborting the pipeline right after gather_trends so nothing is ever persisted to
    BigQuery/GCS. An unbounded budget hits MAX_TOKENS. It must stay in between.
    """
    from trend_scout.agent import root_agent

    budget = root_agent.planner.thinking_config.thinking_budget
    assert budget is not None and budget > 0, (
        f"orchestrator thinking_budget must be > 0 (was {budget})"
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
