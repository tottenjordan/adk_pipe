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

`app` (the non-resumable ``App`` carrying the opt-in safety plugins) is
re-exported deliberately: ADK's canned ``AgentLoader`` imports the *package*
first and, because it finds ``root_agent`` here, would otherwise serve the bare
agent and never reach ``creative_agent.agent.app`` — silently dropping the App's
plugins. It checks ``app`` before ``root_agent``, so exporting it here wins.

Importing this package builds the full agent graph (via `from . import agent`),
which is the pre-existing behavior — the facade only adds names, it does not
change import cost or the lazy-import pattern used by `runserver.get_root_agent`.
"""

from . import agent, callbacks, tools  # noqa: F401  (submodule access + graph build)
from .agent import (
    ad_copy_reviser,
    ad_creative_pipeline,
    app,
    combined_research_pipeline,
    finalize_pipeline,
    residual_copy_issues,
    root_agent,
    visual_generation_pipeline,
    visual_generator_resilient,
)
from .brand_history import ALLOWED_TONES as AD_COPY_TONES
from .brand_history import normalize_brand
from .copy_gate import parse_copies as parse_ad_copies
from .copy_gate import user_revision_inputs as user_copy_revision_inputs
from .schemas import CreativeBrief, VisualConceptFinalList
from .style_shortlist import canonical_style

__all__ = [
    # submodules
    "agent",
    "callbacks",
    "tools",
    # root + its App (see NOTE on `app` above) + reusable pipelines
    "root_agent",
    "app",
    "combined_research_pipeline",
    "ad_creative_pipeline",
    "visual_generation_pipeline",
    "visual_generator_resilient",
    "finalize_pipeline",
    # bare ad-copy reviser + its user-revision inputs (interactive checkpoint 2)
    "ad_copy_reviser",
    "user_copy_revision_inputs",
    "parse_ad_copies",
    "residual_copy_issues",
    # shared visual schema + the structured creative brief (downstream contract)
    "VisualConceptFinalList",
    "CreativeBrief",
    # allowlists for the api's rating learning context (runserver/ratings.py)
    "canonical_style",
    "AD_COPY_TONES",
    "normalize_brand",
]
