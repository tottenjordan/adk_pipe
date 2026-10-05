"""Custom launcher: the ADK FastAPI app + our async-job ``/runs`` router.

``get_fast_api_app`` returns its own FastAPI instance and builds its session +
artifact services internally (from ``SESSION_SERVICE_URI``); it does not accept
injected instances. To guarantee the canned CRUD endpoints (``createSession`` /
``getSession`` / artifacts) and our ``/runs`` poll endpoint see **one** store,
we reach the exact instances the canned server closed over (an ``ApiServer``
captured in its route closures) and hand THOSE to our router + runner factory.
This shares a single store for every backend — including the local default and
in-memory. If ADK's internals ever move the instance out of reach, we fall back
to rebuilding the services from the same ``SESSION_SERVICE_URI`` via ADK's own
resolver (correct for the prod remote ``agentengine://`` backend, which multiple
clients share; only a bare in-memory local run would diverge, and local dev uses
the canned ``adk api_server`` path, not this launcher).
"""

from __future__ import annotations

import contextlib
import logging
import os
from functools import partial

from google.adk.apps import App
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.cli.utils.service_factory import (
    create_artifact_service_from_options,
    create_session_service_from_options,
)
from google.adk.runners import Runner

from runserver import experiments
from runserver.async_runs import configure, get_root_agent, router
from runserver.authz import (
    AuthzMode,
    UserAuthzMiddleware,
    install_ownership_handler,
    resolve_mode,
    verify_proxy_caller,
)
from runserver.otel import otel_to_cloud_enabled

_AGENTS_DIR = "agents"
_SESSION_URI = os.getenv("SESSION_SERVICE_URI") or None
_ARTIFACT_URI = os.getenv("ARTIFACT_SERVICE_URI") or None
_ALLOW = os.getenv("ALLOW_ORIGINS")

app = get_fast_api_app(
    agents_dir=_AGENTS_DIR,
    session_service_uri=_SESSION_URI,
    artifact_service_uri=_ARTIFACT_URI,
    allow_origins=_ALLOW.split(",") if _ALLOW else None,
    web=False,
    # Opt-in Cloud Trace export (ADK_OTEL_TO_CLOUD); see runserver/otel.py.
    otel_to_cloud=otel_to_cloud_enabled(),
)


def _find_canned_services(fast_api_app):
    """Return the ``(session_service, artifact_service)`` the canned app built,
    reached via the ``ApiServer`` instance its route handlers close over. Returns
    ``(None, None)`` if ADK's internals change and it can no longer be found."""
    for route in fast_api_app.routes:
        endpoint = getattr(route, "endpoint", None)
        for cell in getattr(endpoint, "__closure__", None) or ():
            try:
                obj = cell.cell_contents
            except ValueError:
                continue
            if hasattr(obj, "session_service") and hasattr(obj, "get_runner_async"):
                return obj.session_service, getattr(obj, "artifact_service", None)
    return None, None


session_service, artifact_service = _find_canned_services(app)

if session_service is None:
    # Fallback: rebuild from the same URI with ADK's own resolver so the two
    # servers still share a remote backend in production.
    session_service = create_session_service_from_options(
        base_dir=_AGENTS_DIR, session_service_uri=_SESSION_URI
    )
    artifact_service = create_artifact_service_from_options(
        base_dir=_AGENTS_DIR, artifact_service_uri=_ARTIFACT_URI
    )


def _runner_factory(app_name: str) -> Runner:
    # get_root_agent returns each agent's App: resumable for the interactive agents
    # (trend_scout, interactive_creative), non-resumable for creative_agent, all
    # carrying the App-level plugins (opt-in Model Armor, agent_common/safety.py).
    # A Runner derives resumability and plugins ONLY from an App — passing a bare
    # agent= wraps it into a default App (is_resumable=False, no plugins), so
    # checkpoints could never resume and the safety screen would be skipped. Build
    # with app= when we get an App; the agent= fallback is kept for robustness.
    obj = get_root_agent(app_name)
    if isinstance(obj, App):
        return Runner(
            app=obj,
            app_name=app_name,
            session_service=session_service,
            artifact_service=artifact_service,
        )
    return Runner(
        app_name=app_name,
        agent=obj,
        session_service=session_service,
        artifact_service=artifact_service,
    )


# P3 per-user authz: trust X-TT-User only from the /api/adk proxy SA (see
# runserver/authz.py). Resolve + validate the mode up front so a misconfigured
# deploy fails at boot, not on the first request.
_AUTHZ_MODE = resolve_mode()
_PROXY_AUDIENCES = [
    a.strip() for a in os.getenv("TRUSTED_PROXY_AUDIENCES", "").split(",") if a.strip()
]
_PROXY_SA = os.getenv("TRUSTED_PROXY_SA", "").strip()
if _AUTHZ_MODE is AuthzMode.ENFORCE and not (_PROXY_AUDIENCES and _PROXY_SA):
    raise RuntimeError(
        "USER_AUTHZ_MODE=enforce needs TRUSTED_PROXY_SA + TRUSTED_PROXY_AUDIENCES"
    )

configure(
    session_service=session_service,
    runner_factory=_runner_factory,
    authz_mode=_AUTHZ_MODE,
)
app.include_router(router)

# Bandit experiments (/experiments): BANDIT_DEPLOY_MODE=vertex (BigQuery + Vertex
# endpoint + Cloud Run Job) or fake (in-memory; also the fallback when the BigQuery
# env is missing, i.e. local dev). See runserver/experiments.py.
_BANDIT = experiments.build_backend_from_env()
logging.getLogger(__name__).info("bandit experiments mode: %s", _BANDIT["mode"])
experiments.configure(
    session_service=session_service,
    store=_BANDIT["store"],
    deployer=_BANDIT["deployer"],
    jobs=_BANDIT["jobs"],
    authz_mode=_AUTHZ_MODE,
    settings=_BANDIT["settings"],
)
app.include_router(experiments.router)

# Start the experiments TTL reaper (full pass every 5 min: expiry, resumes
# deploys/teardowns a previous revision left mid-flight, finishes traffic runs; plus a
# light 60 s pass over watched running_traffic rows) inside ADK's own lifespan,
# which we wrap rather than replace.
_adk_lifespan = app.router.lifespan_context


@contextlib.asynccontextmanager
async def _lifespan(fast_api_app):
    async with _adk_lifespan(fast_api_app) as state:
        reaper = experiments.start_reaper()
        try:
            yield state
        finally:
            reaper.cancel()


app.router.lifespan_context = _lifespan
install_ownership_handler(app)
app.add_middleware(
    UserAuthzMiddleware,
    mode=_AUTHZ_MODE,
    caller_ok=partial(
        verify_proxy_caller, audiences=_PROXY_AUDIENCES, trusted_sa=_PROXY_SA
    ),
)
