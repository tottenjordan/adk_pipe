"""The backend container entrypoint serves deployment/async_app.py under uvicorn.

SESSION_SERVICE_URI is no longer a CLI flag — async_app.py reads it from the
environment (via get_fast_api_app), so it stays opt-in without leaking into the
Dockerfile CMD literal. ADK_DRYRUN=1 makes the script print the argv it would exec.
"""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deployment" / "backend_entrypoint.sh"


def _run(env_extra):
    env = {"PATH": "/usr/bin:/bin", "PORT": "8080", "ADK_DRYRUN": "1", **env_extra}
    out = subprocess.run(
        ["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=True
    )
    return out.stdout.strip()


def test_serves_async_app_under_uvicorn():
    cmd = _run({})
    assert "uvicorn deployment.async_app:app" in cmd
    assert "--host 0.0.0.0" in cmd
    assert "--port 8080" in cmd


def test_session_uri_not_a_cli_flag():
    # The URI is consumed inside async_app.py, never passed as a CLI argument, so
    # it must not appear in the exec'd argv regardless of whether it is set.
    uri = (
        "agentengine://projects/934903580331/locations/us-central1/reasoningEngines/123"
    )
    assert "--session_service_uri" not in _run({})
    cmd = _run({"SESSION_SERVICE_URI": uri})
    assert "--session_service_uri" not in cmd
    assert "uvicorn deployment.async_app:app" in cmd
