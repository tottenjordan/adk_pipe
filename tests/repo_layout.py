"""Single source of truth for where the first-party packages live on disk.

Tests that need a package's directory (to read sources, walk modules, stage
files) take it from here instead of hard-coding ``REPO_ROOT / "<name>"``, so a
layout change is a one-line edit per package. Import names stay bare: the
editable install's ``.pth`` puts each container directory on ``sys.path``.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Runnable ADK agents (what `adk web` and the api serve).
AGENT_DIRS = {
    "trend_scout": REPO_ROOT / "trend_scout",
    "creative_agent": REPO_ROOT / "creative_agent",
    "interactive_creative": REPO_ROOT / "interactive_creative",
}

# Shared libraries the agents import.
LIB_DIRS = {
    "agent_common": REPO_ROOT / "agent_common",
    "creative_eval": REPO_ROOT / "creative_eval",
}

# Bandit experimentation stack (never imported by runserver or the agents).
BANDIT_DIRS = {
    "bandit": REPO_ROOT / "bandit",
    "bandit_serving": REPO_ROOT / "bandit_serving",
    "bandit_traffic": REPO_ROOT / "bandit_traffic",
}

# The packages that ship to Agent Engine (agents + libs).
PACKAGE_DIRS = {**AGENT_DIRS, **LIB_DIRS}


def package_dir(name: str) -> Path:
    """Return the on-disk directory of a first-party package, by import name."""
    dirs = {**PACKAGE_DIRS, **BANDIT_DIRS}
    if name not in dirs:
        raise KeyError(f"unknown first-party package: {name!r}")
    return dirs[name]
