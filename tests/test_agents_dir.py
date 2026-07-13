"""Tests for the `agents/` serving directory used by the Cloud Run api_server.

`adk api_server .` (pointed at the repo root) lists *every* top-level directory as
an "app" — because ADK's `AgentLoader.list_agents()` returns all non-hidden subdirs,
not just the ones containing a runnable root agent. The deployed backend instead points
at `agents/`, a thin directory holding one symlink per *runnable* agent, so `/list-apps`
returns exactly the three we serve. The real packages stay flat at the repo root (the
flat layout Agent Engine's `extra_packages` staging depends on — see CLAUDE.md); the
symlinks are only a serving view.

These tests guard that view: exactly the three expected agents, symlinks that resolve to
the sibling flat packages, and no `agent.py`/`root_agent.yaml` in `agents/` itself (which
would flip ADK into single-agent mode).
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
EXPECTED_AGENTS = {"trend_scout", "creative_agent", "interactive_creative"}


def test_agents_dir_lists_exactly_the_runnable_agents():
    from google.adk.cli.utils.agent_loader import AgentLoader

    loader = AgentLoader(str(AGENTS_DIR))
    assert not loader.is_single_agent, "agents/ must stay in multi-agent mode"
    assert set(loader.list_agents()) == EXPECTED_AGENTS


def test_agents_dir_entries_are_symlinks_to_flat_packages():
    for name in EXPECTED_AGENTS:
        entry = AGENTS_DIR / name
        assert entry.is_symlink(), f"agents/{name} should be a symlink"
        # Relative symlink → sibling package at the repo root (portable into the container).
        assert not Path.readlink(entry).is_absolute(), (
            f"agents/{name} must use a relative symlink so it resolves inside the image"
        )
        target = entry.resolve()
        assert target == (REPO_ROOT / name).resolve()
        assert (target / "agent.py").is_file() or (target / "__init__.py").is_file()


def test_agents_dir_has_no_agent_marker_files():
    # A bare agent.py / root_agent.yaml in agents/ would put ADK in single-agent mode.
    assert not (AGENTS_DIR / "agent.py").exists()
    assert not (AGENTS_DIR / "root_agent.yaml").exists()
