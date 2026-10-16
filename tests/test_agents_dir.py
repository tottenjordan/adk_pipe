"""Tests for the `agents/` directory: ADK's `agents_dir` for the api and `adk web`.

ADK's `AgentLoader.list_agents()` returns every subdirectory of the agents dir that
holds an `__init__.py`, not just the ones with a runnable root agent. `agents/`
therefore holds exactly the three runnable agents (as real directories) and the
shared libraries live in `libs/`, so `/list-apps` returns exactly what we serve.

Real directories (no longer symlinks to root-level packages) matter twice: a
`gcloud run deploy --source` zip drops symlinks, and ADK's per-agent local
`.adk/` store refuses an agent dir that resolves outside `agents/` (the old
"resolves outside base directory" 400 that forced `SESSION_SERVICE_URI=memory://`
in local dev).
"""

import os
import re
import subprocess
import sys
import textwrap

import pytest

from tests.repo_layout import AGENT_DIRS, LIB_DIRS, REPO_ROOT

AGENTS_DIR = REPO_ROOT / "agents"
EXPECTED_AGENTS = {"trend_scout", "creative_agent", "interactive_creative"}


def test_agent_dirs_map_matches_the_served_agents():
    assert set(AGENT_DIRS) == EXPECTED_AGENTS
    assert {p.parent for p in AGENT_DIRS.values()} == {AGENTS_DIR}


def test_agents_dir_lists_exactly_the_runnable_agents():
    from google.adk.cli.utils.agent_loader import AgentLoader

    loader = AgentLoader(str(AGENTS_DIR))
    assert not loader.is_single_agent, "agents/ must stay in multi-agent mode"
    assert set(loader.list_agents()) == EXPECTED_AGENTS


def test_agents_dir_entries_are_real_directories():
    for name in EXPECTED_AGENTS:
        entry = AGENTS_DIR / name
        assert entry.is_dir() and not entry.is_symlink(), (
            f"agents/{name} must be a real directory, not a symlink"
        )
        assert (entry / "__init__.py").is_file() and (entry / "agent.py").is_file()


def test_agent_dirs_pass_adks_local_store_path_check():
    """The check behind ADK's per-agent local .adk/ (session.db, artifacts)."""
    from google.adk.cli.utils.dot_adk_folder import dot_adk_folder_for_agent

    for name in EXPECTED_AGENTS:
        folder = dot_adk_folder_for_agent(agents_root=AGENTS_DIR, app_name=name)
        assert folder.agent_dir == (AGENTS_DIR / name).resolve()


def test_libs_are_not_under_agents():
    # A library under agents/ would be listed by /list-apps as an app.
    for name, path in LIB_DIRS.items():
        assert path.parent == REPO_ROOT / "libs", name
        assert not (AGENTS_DIR / name).exists(), f"{name} must not live in agents/"
    assert not (REPO_ROOT / "libs" / "__init__.py").exists()


def test_agents_dir_has_no_agent_marker_files():
    # A bare agent.py / root_agent.yaml in agents/ would put ADK in single-agent mode.
    assert not (AGENTS_DIR / "agent.py").exists()
    assert not (AGENTS_DIR / "root_agent.yaml").exists()
    assert not (AGENTS_DIR / "__init__.py").exists()


def test_api_image_puts_agents_and_libs_on_pythonpath():
    """The api image never installs the project (no .pth), so ENV PYTHONPATH must
    list the import roots, and the old symlink re-link loop must stay gone."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    match = re.search(r"^ENV PYTHONPATH=(\S+)$", dockerfile, re.MULTILINE)
    assert match, "Dockerfile must set ENV PYTHONPATH"
    assert set(match.group(1).split(":")) == {"/app", "/app/agents", "/app/libs"}
    assert "ln -s" not in dockerfile


# The local-dev launcher with ADK's default per-agent SQLite session store (no
# SESSION_SERVICE_URI). The store's .adk/ dir is redirected into a temp dir so the
# test never writes into the repo; the path check itself stays ADK's own.
_LOCAL_SESSION_SCRIPT = """
    import sys
    from pathlib import Path

    from google.adk.cli.utils import dot_adk_folder

    tmp = Path(sys.argv[1])
    dot_adk_folder.DotAdkFolder.dot_adk_dir = property(
        lambda self: tmp / self.agent_dir.name / ".adk"
    )

    from fastapi.testclient import TestClient

    from deployment import async_app

    headers = {"X-TT-User": "local-user"}
    with TestClient(async_app.app) as client:
        apps = client.get("/list-apps", headers=headers)
        assert apps.status_code == 200, apps.text
        assert set(apps.json()) == {"trend_scout", "creative_agent",
                                    "interactive_creative"}, apps.text
        base = "/apps/trend_scout/users/local-user/sessions"
        created = client.post(base, json={}, headers=headers)
        assert created.status_code == 200, (created.status_code, created.text)
        sid = created.json()["id"]
        fetched = client.get(f"{base}/{sid}", headers=headers)
        assert fetched.status_code == 200, fetched.text
    assert (tmp / "trend_scout" / ".adk" / "session.db").is_file()
    print("OK")
"""


@pytest.mark.subprocess
def test_async_app_creates_a_session_with_the_default_local_store(tmp_path):
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "SESSION_SERVICE_URI",
            "ARTIFACT_SERVICE_URI",
            "K_SERVICE",
            "USER_AUTHZ_MODE",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "ADK_OTEL_TO_CLOUD",
            "MODEL_ARMOR_TEMPLATE",
            "ALLOW_ORIGINS",
        }
    }
    env.update(
        TRUST_CLIENT_USER_ID="1",
        RATINGS_STORE="memory",
        SHARES_STORE="memory",
        PERSON_REFS_STORE="memory",
        BANDIT_DEPLOY_MODE="fake",
        GOOGLE_CLOUD_PROJECT="test-project",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(_LOCAL_SESSION_SCRIPT),
            str(tmp_path),
        ],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0 and "OK" in result.stdout, result.stderr[-3000:]
