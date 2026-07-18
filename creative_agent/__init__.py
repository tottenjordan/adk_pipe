"""creative_agent package.

Public reuse surface (facade) for the reusable *agent-graph* building blocks.
Consumers such as `interactive_creative` import the pipelines and the shared
visual schema from this top-level package rather than from the volatile
`creative_agent.agent` / `.schemas` internals, so those modules can be
reorganized without touching consumers.

NOTE: the config singleton stays at its stable submodule home
`creative_agent.config` and is deliberately NOT re-exported here — binding a name
`config` on the package would shadow the `creative_agent.config` submodule
(`import creative_agent.config` would return the instance, not the module). Import
it as `from creative_agent.config import config, INFRA_RETRY, SCHEMA_RETRY`.

Importing this package builds the full agent graph (via `from . import agent`),
which is the pre-existing behavior — the facade only adds names, it does not
change import cost or the lazy-import pattern used by `runserver.get_root_agent`.
"""

from . import agent, callbacks, tools  # noqa: F401  (submodule access + graph build)
from .agent import (
    ad_creative_pipeline,
    combined_research_pipeline,
    root_agent,
    visual_generation_pipeline,
    visual_generator_resilient,
)
from .schemas import VisualConceptFinalList

__all__ = [
    # submodules
    "agent",
    "callbacks",
    "tools",
    # root + reusable pipelines
    "root_agent",
    "combined_research_pipeline",
    "ad_creative_pipeline",
    "visual_generation_pipeline",
    "visual_generator_resilient",
    # shared visual schema
    "VisualConceptFinalList",
]
