"""N-1: library modules never configure logging at import.

Entrypoints own the root logger (``deployment/async_app.py``, the deploy and
test scripts); Agent Engine has no entrypoint of ours, so ``log_run_start``
installs a default INFO handler on the first run, only when none exists.
"""

import logging
import subprocess
import sys

from tests.repo_layout import PACKAGE_DIRS, REPO_ROOT


def test_importing_trend_scout_and_agent_common_adds_no_root_handlers():
    code = (
        "import logging\n"
        "import agent_common, agent_common.observability\n"
        "import trend_scout.agent, trend_scout.tools, trend_scout.callbacks\n"
        "import trend_scout.digest\n"
        "root = logging.getLogger()\n"
        "assert root.handlers == [], root.handlers\n"
        "assert root.level == logging.WARNING, root.level\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]


def test_ensure_default_logging_only_fills_an_unconfigured_root(monkeypatch):
    from agent_common import observability

    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    observability.ensure_default_logging()
    assert len(root.handlers) == 1
    assert root.level == logging.INFO

    existing = logging.NullHandler()
    monkeypatch.setattr(root, "handlers", [existing])
    monkeypatch.setattr(root, "level", logging.WARNING)
    observability.ensure_default_logging()
    assert root.handlers == [existing]
    assert root.level == logging.WARNING


def test_importing_the_creative_apps_adds_no_root_handlers():
    code = (
        "import logging\n"
        "import creative_agent.agent, creative_agent.tools, creative_agent.callbacks\n"
        "import creative_agent.sub_agents.trend_researcher.agent\n"
        "import creative_agent.sub_agents.campaign_researcher.agent\n"
        "import interactive_creative.agent, creative_eval.agent\n"
        "root = logging.getLogger()\n"
        "assert root.handlers == [], root.handlers\n"
        "assert root.level == logging.WARNING, root.level\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-2000:]


def test_no_agent_package_calls_basicconfig():
    """No module in the agent packages (bundled into the engines) configures
    logging at import; a call inside a function (``ensure_default_logging``)
    is fine, and ``creative_eval/run_eval_test.py`` is a ``python -m`` script."""
    import ast

    scripts = {"creative_eval/run_eval_test.py"}

    offenders = []
    for package, pkg_dir in sorted(PACKAGE_DIRS.items()):
        for path in sorted(pkg_dir.rglob("*.py")):
            name = f"{package}/{path.relative_to(pkg_dir).as_posix()}"
            if name in scripts:
                continue
            tree = ast.parse(path.read_text())
            if any(
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Attribute)
                and stmt.value.func.attr == "basicConfig"
                for stmt in tree.body
            ):
                offenders.append(name)
    assert offenders == []


def test_trend_tie_uses_the_shared_token_length():
    from creative_agent import text_match, trend_tie

    assert not hasattr(trend_tie, "_MIN_TOKEN_CHARS")
    assert trend_tie.MIN_TOKEN_CHARS is text_match.MIN_TOKEN_CHARS
