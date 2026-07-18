"""The curated public reuse surface of the creative_agent package (facade)."""

import types


def test_facade_exposes_reusable_pipelines_and_schema():
    import creative_agent

    # Reusable pipelines/agents (defined in creative_agent.agent)
    assert (
        creative_agent.combined_research_pipeline
        is creative_agent.agent.combined_research_pipeline
    )
    assert (
        creative_agent.ad_creative_pipeline is creative_agent.agent.ad_creative_pipeline
    )
    assert (
        creative_agent.visual_generation_pipeline
        is creative_agent.agent.visual_generation_pipeline
    )
    assert (
        creative_agent.visual_generator_resilient
        is creative_agent.agent.visual_generator_resilient
    )
    assert creative_agent.root_agent is creative_agent.agent.root_agent

    # Shared visual schema
    from creative_agent.schemas import VisualConceptFinalList

    assert creative_agent.VisualConceptFinalList is VisualConceptFinalList

    # Submodules remain accessible as attributes
    assert creative_agent.tools is not None
    assert creative_agent.callbacks is not None


def test_config_submodule_not_shadowed_by_facade():
    """Regression: the facade must NOT re-export the `config` singleton, which
    would shadow the `creative_agent.config` submodule (breaking
    `import creative_agent.config`)."""
    import creative_agent
    import creative_agent.config as ca_config
    from creative_agent.config import config

    # `creative_agent.config` must resolve to the MODULE, not the instance.
    assert isinstance(ca_config, types.ModuleType)
    assert ca_config.config is config
    # The singleton is intentionally reached via the submodule, not the facade.
    assert "config" not in creative_agent.__all__


def test_facade_all_is_complete_and_importable():
    import creative_agent

    expected = {
        "agent",
        "tools",
        "callbacks",
        "root_agent",
        "combined_research_pipeline",
        "ad_creative_pipeline",
        "visual_generation_pipeline",
        "visual_generator_resilient",
        "VisualConceptFinalList",
    }
    assert expected.issubset(set(creative_agent.__all__))
    for name in creative_agent.__all__:
        assert hasattr(creative_agent, name), (
            f"__all__ lists {name} but it is not exported"
        )
