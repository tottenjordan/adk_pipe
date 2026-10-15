"""N-1: library modules never configure logging at import.

Entrypoints own the root logger (``deployment/async_app.py``, the deploy and
test scripts); Agent Engine has no entrypoint of ours, so ``log_run_start``
installs a default INFO handler on the first run, only when none exists.
"""

import logging
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


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
        cwd=REPO,
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
