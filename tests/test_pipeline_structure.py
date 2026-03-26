"""Tests for agent pipeline structure and configuration."""


def test_creative_agent_root_has_expected_tools():
    from creative_agent.agent import root_agent

    tool_names = [getattr(t, "name", getattr(t, "__name__", str(t))) for t in root_agent.tools]
    expected = [
        "combined_research_pipeline",
        "ad_creative_pipeline",
        "visual_generation_pipeline",
        "visual_generator",
        "save_draft_report_artifact",
        "save_creative_gallery_html",
        "write_trends_to_bq",
        "memorize",
    ]
    for name in expected:
        assert name in tool_names, f"Missing tool: {name}"


def test_creative_agent_root_output_key_not_set():
    """Root agent should not have an output_key (it orchestrates)."""
    from creative_agent.agent import root_agent

    assert not hasattr(root_agent, "output_key") or root_agent.output_key is None or root_agent.output_key == ""


def test_combined_research_pipeline_sub_agent_order():
    from creative_agent.agent import combined_research_pipeline

    names = [a.name for a in combined_research_pipeline.sub_agents]
    assert names == [
        "merge_parallel_insights",
        "combined_web_evaluator",
        "enhanced_combined_searcher",
        "combined_report_composer",
    ]


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
        assert agent.output_key == key, f"{agent.name} output_key should be '{key}', got '{agent.output_key}'"


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


def test_trend_trawler_root_has_expected_tools():
    from trend_trawler.agent import root_agent

    tool_names = [getattr(t, "name", getattr(t, "__name__", str(t))) for t in root_agent.tools]
    expected = [
        "gather_trends_agent",
        "understand_trends_agent",
        "pick_trends_agent",
        "save_search_trends_to_session_state",
        "save_session_state_to_gcs",
        "write_trends_to_bq",
        "write_to_file",
        "memorize",
    ]
    for name in expected:
        assert name in tool_names, f"Missing tool: {name}"


def test_trend_trawler_sub_agent_output_keys():
    from trend_trawler.agent import (
        understand_trends_agent,
        pick_trends_agent,
    )

    assert understand_trends_agent.output_key == "info_gtrends"
    assert pick_trends_agent.output_key == "selected_gtrends"


def test_visual_concept_finalizer_has_ad_copy_context():
    """The finalizer must reference ad_copy_critique in its instruction
    to avoid generating duplicate headlines/captions."""
    from creative_agent.agent import visual_concept_finalizer

    assert "ad_copy_critique" in visual_concept_finalizer.instruction
    assert "ad_copy_id" in visual_concept_finalizer.instruction
