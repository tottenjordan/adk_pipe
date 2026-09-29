"""The curated public reuse surface of the creative_agent package (facade)."""

import re
import subprocess
import sys
import types
from pathlib import Path


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


# --- P2 T4 guards: no legacy (deprecated) ADK agent containers -------------
#
# Every agent is a google.adk.workflow graph Workflow; the deprecated
# SequentialAgent / ParallelAgent / LoopAgent containers (and the retired
# RunIfAgent / RetryUntilKeyAgent wrappers) must not come back.

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENT_MODULES = (
    "trend_scout.agent",
    "creative_agent.agent",
    "interactive_creative.agent",
)
# The filter is scoped to ADK's agent-container deprecation notice rather than a
# blanket `simplefilter("error", DeprecationWarning)`: unrelated third-party
# imports (e.g. typing's `_UnionGenericAlias` notice on Python 3.14, triggered
# inside dependencies) raise DeprecationWarning at import time, so a blanket
# filter fails for reasons that have nothing to do with our agents.
_LEGACY_AGENT_WARNING = r".*(Sequential|Parallel|Loop)Agent is deprecated"
_IMPORT_UNDER_ERROR = """
import sys, warnings
warnings.filterwarnings("error", message=sys.argv[1], category=DeprecationWarning)
for mod in sys.argv[2:]:
    __import__(mod)
"""


def _import_fresh(*modules: str) -> subprocess.CompletedProcess[str]:
    # A subprocess gives a genuinely fresh import (agents are built at import
    # time); reloading in-process would rebuild agent objects other tests hold.
    return subprocess.run(
        [sys.executable, "-c", _IMPORT_UNDER_ERROR, _LEGACY_AGENT_WARNING, *modules],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_agent_modules_import_without_legacy_agent_deprecation():
    """Building every agent emits no Sequential/Parallel/LoopAgent deprecation."""
    result = _import_fresh(*_AGENT_MODULES)
    assert result.returncode == 0, result.stderr[-4000:]


def test_legacy_agent_deprecation_filter_actually_fires():
    """Positive control: the scoped filter really turns ADK's legacy-container
    notice into an error (so the guard above can't pass vacuously)."""
    probe = (
        "import sys, warnings\n"
        "warnings.filterwarnings('error', message=sys.argv[1], "
        "category=DeprecationWarning)\n"
        "from google.adk.agents import SequentialAgent\n"
        "SequentialAgent(name='probe', sub_agents=[])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, _LEGACY_AGENT_WARNING],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode != 0
    assert "DeprecationWarning" in result.stderr


_LEGACY_NAMES = re.compile(
    r"\b(SequentialAgent|ParallelAgent|LoopAgent|RunIfAgent|RetryUntilKeyAgent)\b"
)
_GUARDED_PACKAGES = (
    "agent_common",
    "creative_agent",
    "creative_eval",
    "interactive_creative",
    "trend_scout",
)


def test_no_legacy_agent_containers_in_agent_packages():
    """No agent package (or agent_common) references a legacy container."""
    hits = [
        f"{path.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}"
        for pkg in _GUARDED_PACKAGES
        for path in sorted((_REPO_ROOT / pkg).rglob("*.py"))
        for lineno, line in enumerate(path.read_text().splitlines(), start=1)
        if _LEGACY_NAMES.search(line)
    ]
    assert not hits, "legacy agent containers referenced:\n" + "\n".join(hits)
