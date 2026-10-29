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
resolver and log a warning. That is correct for the prod remote
``agentengine://`` backend, which multiple clients share, but NOT for an
in-memory store: with ``SESSION_SERVICE_URI=memory://`` the fallback would give
``/runs`` a second, empty store, so sessions created via the canned CRUD endpoints
would 404 on ``/runs`` (and vice versa). ``tests/test_async_app_wiring.py`` asserts the canned instance is found.
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

from runserver import (
    brand_assist,
    changelog,
    delivery_kit,
    experiments,
    person_refs,
    ratings,
    shares,
    shutdown,
    variants,
    variations,
)
from runserver.a2a_mount import a2a_enabled, mount_a2a_apps
from runserver.async_runs import (
    configure,
    get_root_agent,
    install_runserver_marker_log_filter,
    router,
)
from runserver.authz import (
    AuthzMode,
    UserAuthzMiddleware,
    install_ownership_handler,
    parse_allowed_sas,
    resolve_mode,
    verify_proxy_caller,
)
from runserver.otel import otel_to_cloud_enabled
from runserver.person_refs_store import build_store_from_env as build_person_refs_store
from runserver.ratings_store import build_store_from_env as build_ratings_store
from runserver.shares_store import build_store_from_env as build_shares_store

# The api's entrypoint owns logging (library modules never configure it):
# uvicorn sets up only its own loggers, so app INFO lines need a root handler.
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
# Resumes replay runserver's own __runserver__ marker events through ADK's agent
# router, which warns about each as an unknown agent; drop only that record.
install_runserver_marker_log_filter()

_AGENTS_DIR = "agents"
_SESSION_URI = os.getenv("SESSION_SERVICE_URI") or None
_ARTIFACT_URI = os.getenv("ARTIFACT_SERVICE_URI") or None
# Comma-separated; entries stripped like TRUSTED_PROXY_AUDIENCES below, so
# "a, b" doesn't yield an origin " b" that never matches a browser Origin.
_ALLOW = [o.strip() for o in os.getenv("ALLOW_ORIGINS", "").split(",") if o.strip()]


# Our lifespan, passed to get_fast_api_app: ADK nests it INSIDE its own
# (api_server.py internal_lifespan), so ADK's runner cleanup runs after ours.
# Startup: the experiments TTL reaper (full pass every 5 min: expiry, resumes
# deploys/teardowns a previous revision left mid-flight, finishes traffic runs; plus
# a light 60 s pass over watched running_traffic rows). Shutdown (after the yield):
# cancel the reaper and every detached /runs + /variants task and wait, bounded by
# shutdown.SHUTDOWN_DRAIN_SECONDS, for their handlers to persist the interrupted
# terminal marker — inside Cloud Run's 10 s SIGTERM->SIGKILL grace together with
# uvicorn's --timeout-graceful-shutdown (budget: runserver/shutdown.py).
@contextlib.asynccontextmanager
async def _lifespan(_fast_api_app):
    reaper = experiments.start_reaper()
    try:
        yield
    finally:
        await shutdown.cancel_and_wait([reaper, *shutdown.detached_tasks()])


app = get_fast_api_app(
    agents_dir=_AGENTS_DIR,
    session_service_uri=_SESSION_URI,
    artifact_service_uri=_ARTIFACT_URI,
    allow_origins=_ALLOW or None,
    web=False,
    # Opt-in Cloud Trace export (ADK_OTEL_TO_CLOUD); see runserver/otel.py.
    otel_to_cloud=otel_to_cloud_enabled(),
    lifespan=_lifespan,
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
    # servers still share a remote backend in production. An in-memory (or local
    # file) store is NOT shared this way, hence the warning.
    logging.getLogger(__name__).warning(
        "could not find the canned ADK session/artifact services; building fresh "
        "ones from SESSION_SERVICE_URI=%r. /runs and the canned CRUD endpoints only "
        "share a store when it is remote (agentengine://); with memory:// sessions "
        "will 404 across them",
        _SESSION_URI,
    )
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

changelog.configure(
    session_service=session_service,
    artifact_service=artifact_service,
    authz_mode=_AUTHZ_MODE,
)
app.include_router(changelog.router)

variations.configure(
    session_service=session_service,
    artifact_service=artifact_service,
    authz_mode=_AUTHZ_MODE,
)
app.include_router(variations.router)

delivery_kit.configure(
    session_service=session_service,
    artifact_service=artifact_service,
    authz_mode=_AUTHZ_MODE,
)
app.include_router(delivery_kit.router)

brand_assist.configure(authz_mode=_AUTHZ_MODE)
app.include_router(brand_assist.router)

if a2a_enabled():
    mount_a2a_apps(
        app,
        runner_factory=_runner_factory,
        session_service=session_service,
    )

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

# Human creative ratings (/ratings, judge calibration): RATINGS_STORE=bigquery
# (creative_ratings table) or memory (also the fallback without the BigQuery env).
_RATINGS_MODE, _RATINGS_STORE = build_ratings_store()
logging.getLogger(__name__).info("creative ratings store: %s", _RATINGS_MODE)
ratings.configure(session_service=session_service, store=_RATINGS_STORE)
app.include_router(ratings.router)

# Shareable creative links (/shares, owner side): SHARES_STORE=bigquery
# (creative_shares table) or memory (also the fallback without the BigQuery env).
# Frozen snapshots + re-encoded JPEGs go to
# gs://$GOOGLE_CLOUD_STORAGE_BUCKET/shares/<token>/; links are
# $SHARE_BASE_URL/s/<token> (the public share viewer service).
_SHARES_MODE, _SHARES_STORE = build_shares_store()
logging.getLogger(__name__).info("creative shares store: %s", _SHARES_MODE)
shares.configure(session_service=session_service, store=_SHARES_STORE)
app.include_router(shares.router)

# Person-reference consent registry (/person-refs, owner side): PERSON_REFS_STORE=
# bigquery (person_references table) or memory (also the fallback without the
# BigQuery env). Photos live at gs://$GOOGLE_CLOUD_STORAGE_BUCKET/person-refs/<slug>/.
# Revoking a consent cascades: the shares whose person_consent_ids contain it and
# the live experiments with an arm cast with it (hooks below), then every render recorded in its person_renders (cast base renders from
# the /runs path, variants from /variants, plus the renders listed in the state
# of every run recorded on the consent at kick-off), then the photo.
_PERSON_REFS_MODE, _PERSON_REFS_STORE = build_person_refs_store()
logging.getLogger(__name__).info("person references store: %s", _PERSON_REFS_MODE)
person_refs.configure(
    store=_PERSON_REFS_STORE,
    revoke_hooks=[
        shares.revoke_shares_for_consent,
        experiments.stop_experiments_for_consent,
    ],
    # Reads the runs recorded on a consent (session: markers) at revoke.
    session_service=session_service,
)
app.include_router(person_refs.router)

# Personalised variant previews (/variants, owner side, UI only): re-render one
# finished concept with a consented person (detached task, VARIANT_RENDER_CONCURRENCY
# semaphore, VARIANT_DAILY_CAP per user). Results: state person_variants + gs://
# $GOOGLE_CLOUD_STORAGE_BUCKET/<run>/variants/<slug>/; never in experiments/shares.
variants.configure(session_service=session_service)
app.include_router(variants.router)

_A2A_AUDIENCES = [
    a.strip() for a in os.getenv("A2A_AUDIENCES", "").split(",") if a.strip()
] or _PROXY_AUDIENCES
_A2A_ALLOWED_SAS = parse_allowed_sas(os.getenv("A2A_ALLOWED_CALLER_SAS"))
_A2A_PUBLIC_CARD = os.getenv("A2A_PUBLIC_AGENT_CARD", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

install_ownership_handler(app)
app.add_middleware(
    UserAuthzMiddleware,
    mode=_AUTHZ_MODE,
    caller_ok=partial(
        verify_proxy_caller, audiences=_PROXY_AUDIENCES, trusted_sa=_PROXY_SA
    ),
    a2a_audiences=_A2A_AUDIENCES,
    a2a_allowed_sas=_A2A_ALLOWED_SAS,
    a2a_public_agent_card=_A2A_PUBLIC_CARD,
)
