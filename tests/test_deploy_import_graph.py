"""Every repo-root package an agent imports must be bundled into its engine.

``deployment/deploy_agent.py`` ``AGENT_EXTRA_PACKAGES`` is maintained by hand. An
agent module that starts importing another repo-root package (say
``creative_eval`` importing ``runserver``) would deploy fine and then fail to
start in Agent Engine with ``No module named ...`` (the creative_eval incident).
Each agent module is imported in a fresh interpreter, and the repo-root packages
left in ``sys.modules`` must be a subset of its bundle.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from deployment.deploy_agent import AGENT_DEPLOY_SPECS, AGENT_EXTRA_PACKAGES

REPO_ROOT = Path(__file__).resolve().parents[1]

_SCRIPT = """
    import importlib, json, sys
    from pathlib import Path

    root = Path.cwd()
    importlib.import_module(sys.argv[1])
    tops = {name.split(".")[0] for name in list(sys.modules)}
    local = sorted(
        t for t in tops
        if (root / t / "__init__.py").is_file() or (root / f"{t}.py").is_file()
    )
    print("LOCAL", json.dumps(local))
"""


def repo_root_imports(module: str) -> set[str]:
    env = {**os.environ, "GOOGLE_CLOUD_PROJECT": "test-project"}
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_SCRIPT), module],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    (line,) = [ln for ln in result.stdout.splitlines() if ln.startswith("LOCAL ")]
    return set(json.loads(line.removeprefix("LOCAL ")))


@pytest.mark.slow
@pytest.mark.subprocess
@pytest.mark.parametrize("agent", sorted(AGENT_DEPLOY_SPECS))
def test_agent_imports_only_bundled_packages(agent):
    bundled = {p.removeprefix("./") for p in AGENT_EXTRA_PACKAGES[agent]}
    imported = repo_root_imports(AGENT_DEPLOY_SPECS[agent]["module"])
    assert agent in imported  # sanity: the probe sees repo-root packages
    unbundled = imported - bundled
    assert not unbundled, (
        f"{agent} imports {sorted(unbundled)} at import time, but "
        f"AGENT_EXTRA_PACKAGES[{agent!r}] bundles only {sorted(bundled)}"
    )
