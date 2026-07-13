"""The backend container entrypoint appends --session_service_uri only when set.

Keeps the session store opt-in (local `adk web` / CI unaffected) and out of the
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


def test_no_session_uri_omits_flag():
    cmd = _run({})
    assert "adk api_server agents" in cmd
    assert "--host 0.0.0.0" in cmd
    assert "--port 8080" in cmd
    assert "--session_service_uri" not in cmd


def test_empty_session_uri_omits_flag():
    assert "--session_service_uri" not in _run({"SESSION_SERVICE_URI": ""})


def test_session_uri_present_appends_flag():
    uri = (
        "agentengine://projects/934903580331/locations/us-central1/reasoningEngines/123"
    )
    cmd = _run({"SESSION_SERVICE_URI": uri})
    assert f"--session_service_uri {uri}" in cmd
