"""Import ``deployment.async_app`` for real and check the P3 trust-boundary wiring.

Other tests read ``async_app.py`` as text; these import it (offline, in-memory
stores, no GCP credentials) so deleting the ``UserAuthzMiddleware`` install or
neutering the enforce boot guard fails CI. Each scenario runs in a subprocess:
the module builds the whole app at import time and must not leak into the
pytest(-xdist) worker that runs the rest of the suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Env that changes how the app boots; scrubbed from the inherited environment so
# a developer's shell (or a .env export) can't flip a scenario.
_SCRUBBED = (
    "K_SERVICE",
    "TRUST_CLIENT_USER_ID",
    "USER_AUTHZ_MODE",
    "TRUSTED_PROXY_SA",
    "TRUSTED_PROXY_AUDIENCES",
    "SESSION_SERVICE_URI",
    "ARTIFACT_SERVICE_URI",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "ADK_OTEL_TO_CLOUD",
    "MODEL_ARMOR_TEMPLATE",
    "ALLOW_ORIGINS",
)

_BASE_ENV = {
    "USER_AUTHZ_MODE": "enforce",
    "TRUSTED_PROXY_SA": "tt-web-sa@test-project.iam.gserviceaccount.com",
    "TRUSTED_PROXY_AUDIENCES": "https://trend-trawler-api.test",
    "SESSION_SERVICE_URI": "memory://",
    "RATINGS_STORE": "memory",
    "SHARES_STORE": "memory",
    "PERSON_REFS_STORE": "memory",
    "BANDIT_DEPLOY_MODE": "fake",
    "GOOGLE_CLOUD_PROJECT": "test-project",
}


def _run(script: str, **overrides: str | None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    env.update(_BASE_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


_ENFORCE_SCRIPT = """
    from fastapi.testclient import TestClient

    from deployment import async_app
    import logging

    from runserver.async_runs import ADK_AGENT_ROUTER_LOGGER, RunserverMarkerLogFilter
    from runserver.authz import UserAuthzMiddleware

    app = async_app.app
    router_filters = logging.getLogger(ADK_AGENT_ROUTER_LOGGER).filters
    assert any(isinstance(f, RunserverMarkerLogFilter) for f in router_filters), (
        "the __runserver__ unknown-agent log filter is not installed"
    )
    assert any(m.cls is UserAuthzMiddleware for m in app.user_middleware), (
        "UserAuthzMiddleware is not installed"
    )
    found_session, _ = async_app._find_canned_services(app)
    assert found_session is not None, "canned session service not found"
    assert found_session is async_app.session_service, (
        "/runs and the canned CRUD endpoints use different session stores"
    )

    client = TestClient(app)
    r = client.get("/apps/creative_agent/users/a@x.com/sessions")
    assert r.status_code == 401, (r.status_code, r.text)
    r = client.post("/run", json={})
    assert r.status_code == 404, (r.status_code, r.text)
    print("WIRING_OK")
"""


@pytest.mark.slow
def test_enforce_mode_installs_authz_and_shares_session_store():
    result = _run(_ENFORCE_SCRIPT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WIRING_OK" in result.stdout


@pytest.mark.slow
def test_enforce_mode_without_proxy_sa_refuses_to_boot():
    result = _run("import deployment.async_app", TRUSTED_PROXY_SA=None)
    assert result.returncode != 0, "enforce mode booted without TRUSTED_PROXY_SA"
    assert "RuntimeError" in result.stderr
    assert "USER_AUTHZ_MODE=enforce needs TRUSTED_PROXY_SA" in result.stderr


_CORS_SCRIPT = """
    from fastapi.middleware.cors import CORSMiddleware

    from deployment import async_app

    (cors,) = [m for m in async_app.app.user_middleware if m.cls is CORSMiddleware]
    print("ORIGINS", cors.kwargs["allow_origins"])
"""


@pytest.mark.slow
def test_allow_origins_entries_are_stripped():
    # "a, b" (a space after the comma) used to pass " http://b.test" through,
    # which never matches a browser Origin header.
    result = _run(_CORS_SCRIPT, ALLOW_ORIGINS="http://a.test, http://b.test ,")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ORIGINS ['http://a.test', 'http://b.test']" in result.stdout
