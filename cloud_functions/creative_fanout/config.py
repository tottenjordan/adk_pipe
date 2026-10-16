"""CRF configuration: one frozen ``CrfConfig`` built from the environment.

``load_config()`` reads every setting with a clamp (``_int_env`` /
``_float_env``: unset or invalid -> the default, out of range -> the nearest
bound), then cross-checks the settings that depend on each other:

- ``BUDGET_SAFETY_SECS`` must cover one session read, the session delete and
  one BigQuery status write (``SESSION_READ_TIMEOUT_SECS +
  SESSION_DELETE_TIMEOUT_SECS + BQ_QUERY_TIMEOUT_SECS``), so the work after the
  run budget still ends before Cloud Run's request timeout;
- ``REAP_STALE_PROCESSING_MINUTES`` must be at least the worker timeout (the
  reaper must never re-queue a row whose worker can still be running); its
  default is ``ceil(WORKER_TIMEOUT_SECS / 60) + 15``;
- ``WORKER_TIMEOUT_SECS`` must not exceed ``TASK_DISPATCH_DEADLINE_SECS``
  (logged only): a shorter deadline frees the queue's one dispatch slot while
  the run continues, so the next task meets a busy worker. The worker's
  Cloud Run ``--timeout`` must equal the deadline;
- a nonblank ``CREATIVE_AGENT_ENGINE_ID`` needs ``GOOGLE_CLOUD_PROJECT_NUMBER``
  (the worker builds the Reasoning Engine path from it): without it the engine
  resolves to unset, so rows stay QUEUED.

Cloud Tasks is the only worker transport. Its settings (``CRF_WORKER_URL``,
``CRF_TASKS_INVOKER_SA``) are checked by the orchestrator when it dispatches
(``orchestrator.dispatch`` logs an ERROR and sends nothing), not here: the
worker loads this config too and never sets them.

An invalid combination logs an ERROR and falls back to safe values; nothing
here raises at import or load time, so a bad env var can't crash-loop the
instance. The one exception is the required ``GOOGLE_CLOUD_PROJECT``, which
raises when it is *used* while unset (a wrong project is worse than a loud
failure).

Settings are read lazily: ``config.X`` (the module-level accessor every CRF
module imports) resolves ``load_config().X`` on each access, and
``load_config()`` caches one ``CrfConfig`` per distinct environment, so a
cross-check error is logged once, not on every access.
"""

import functools
import logging
import math
import os
import re
from dataclasses import dataclass, fields
from typing import cast

logger = logging.getLogger(__name__)

# Defaults (also the safe fallbacks of the cross-checks).
DEFAULT_BQ_QUERY_TIMEOUT_SECS = 60.0
DEFAULT_WORKER_TIMEOUT_SECS = 1800.0
DEFAULT_BUDGET_SAFETY_SECS = 150.0
DEFAULT_SESSION_READ_TIMEOUT_SECS = 60.0
DEFAULT_SESSION_DELETE_TIMEOUT_SECS = 30.0
# The reaper's margin over the worker timeout, in minutes.
REAP_MARGIN_MINUTES = 15
# The Cloud Tasks queue the orchestrator sends worker tasks to.
DEFAULT_TASKS_QUEUE = "creative-worker-queue"
# Cloud Tasks' bounds on an HTTP task's dispatch_deadline.
MIN_DISPATCH_DEADLINE_SECS = 15.0
MAX_DISPATCH_DEADLINE_SECS = 1800.0


# Reasoning Engine IDs are numeric; allow the usual id characters, but nothing
# that could change the resource path it is spliced into (`worker.py`).
_AGENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
# A full Reasoning Engine resource name (`.env` stores the engine this way).
_ENGINE_NAME_RE = re.compile(
    r"projects/(?P<project>[^/]+)/locations/(?P<region>[^/]+)"
    r"/reasoningEngines/(?P<id>[^/]+)"
)


def _engine_id_env(region: str, project_id: str, project_number: str) -> str:
    """``CREATIVE_AGENT_ENGINE_ID`` as a bare engine id ("" when unusable).

    Accepts a bare id or the full resource name
    ``projects/<project>/locations/<region>/reasoningEngines/<id>``, whose
    project (the number or the id) and region must be this function's.
    Requires ``GOOGLE_CLOUD_PROJECT_NUMBER``: the worker builds the engine
    path from it. Unset/blank -> ""; anything else unusable (incl. no project
    number) -> "" plus one ERROR (never raises: the worker then refuses rows
    without taking their lock, so they stay QUEUED).
    """
    raw = os.environ.get("CREATIVE_AGENT_ENGINE_ID", "").strip()
    if not raw:
        return ""
    if not project_number:
        logger.error(
            f"CREATIVE_AGENT_ENGINE_ID={raw!r} is set but "
            "GOOGLE_CLOUD_PROJECT_NUMBER is not (the worker builds the "
            "Reasoning Engine path from it); ignoring the engine."
        )
        return ""
    projects = {p for p in (project_id, project_number) if p}
    engine_id = raw
    match = _ENGINE_NAME_RE.fullmatch(raw)
    if match:
        if match["project"] not in projects or match["region"] != region:
            logger.error(
                f"CREATIVE_AGENT_ENGINE_ID={raw!r} names another project or "
                f"region (expected one of {sorted(projects)} in {region}); "
                "ignoring it."
            )
            return ""
        engine_id = match["id"]
    if not _AGENT_ID_RE.fullmatch(engine_id):
        logger.error(
            f"CREATIVE_AGENT_ENGINE_ID={raw!r} is neither an engine id nor a "
            "Reasoning Engine resource name; ignoring it."
        )
        return ""
    return engine_id


def _invoker_env(project_number: str) -> str:
    """The service account whose OIDC token Cloud Tasks sends to the worker:
    ``CRF_TASKS_INVOKER_SA``, default
    ``<project number>-compute@developer.gserviceaccount.com`` ("" when
    neither is set; ``orchestrator.dispatch`` then sends nothing)."""
    return os.environ.get("CRF_TASKS_INVOKER_SA", "").strip() or (
        f"{project_number}-compute@developer.gserviceaccount.com"
        if project_number
        else ""
    )


def _csv_env(name: str) -> set[str]:
    """Comma-separated env var -> set of non-empty, stripped names."""
    return {v.strip() for v in os.environ.get(name, "").split(",") if v.strip()}


def _str_env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _required_env(name: str) -> str:
    """Read a required env var at *use* time; raise loudly if unset/blank."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. Deploy the function with "
            f"`--set-env-vars {name}=...` (see deployment/README.md)."
        )
    return value


def _float_env(name: str, default: float, low: float, high: float) -> float:
    """A float env var clamped to ``[low, high]``; unset/invalid -> default."""
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        logger.warning(f"{name}={raw!r} is not a number; using {default}.")
        value = default
    if math.isnan(value):
        value = default
    return min(max(value, low), high)


def _int_env(name: str, default: int, low: int, high: int) -> int:
    """An int env var clamped to ``[low, high]``; unset/invalid -> default."""
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        logger.warning(f"{name}={raw!r} is not an integer; using {default}.")
        value = default
    return min(max(value, low), high)


@dataclass(frozen=True)
class CrfConfig:
    """Every CRF setting, read once per distinct environment (see module doc)."""

    # GCP. The project is required but only checked on use (see the property).
    project_id: str
    project_number: str
    # Agent Engine is a *regional* resource — this is us-central1, NOT the
    # `global` model location used for the gemini-3.x endpoints.
    GCP_REGION: str
    # The creative engine the worker runs (bare id; "" = unset or invalid, the
    # worker then leaves rows QUEUED). Set by `engine_handoff.py apply
    # --update-worker`; accepts the full resource name or a bare id.
    CREATIVE_AGENT_ENGINE_ID: str
    # Base user id of the worker's Agent Engine sessions (one per row:
    # `worker.session_user_id`).
    AGENT_WORKER_USER_ID: str
    # The engine's root agent name (= its events' author): the completion
    # check's end-of-invocation signal is that agent's final text.
    ROOT_AGENT_NAME: str
    # BigQuery status-tracking table. The trigger and task payloads name the
    # dataset/table, and BigQuery can't parameterize identifiers, so every SQL
    # builder validates them against these allow-lists before quoting.
    BQ_DATASET_ID: str
    BQ_TABLE_TARGETS: str
    ALLOWED_BQ_DATASETS: frozenset[str]
    # Extra opt-in tables (e.g. a load-test copy): CRF_EXTRA_ALLOWED_TABLES.
    ALLOWED_BQ_TABLES: frozenset[str]
    # Bound on each BigQuery status query/DML (client wait + job_timeout_ms).
    BQ_QUERY_TIMEOUT_SECS: float
    # The worker's Cloud Run request timeout; must match the deployed
    # `--timeout`. The run deadline is request start + this - the safety.
    WORKER_TIMEOUT_SECS: float
    # Kept back from the run budget for the session read/delete and the
    # status write (>= their timeouts, cross-checked).
    BUDGET_SAFETY_SECS: float
    # Bound on one `async_stream_query` call (also clamped to the budget).
    STREAM_TIMEOUT_SECS: float
    # Session poll interval of the completion check.
    POLL_INTERVAL_SECS: float
    # Give-up fallback: a run with no end-of-invocation signal and no new
    # events for this long is FAILED (never retried). >= 600 s, longer than
    # the longest step that writes no events (renders, paced judge calls).
    IDLE_SECS: float
    # Bound on one session read (also clamped to the remaining budget).
    SESSION_READ_TIMEOUT_SECS: float
    # Bound on the best-effort session delete (<= 30 s).
    SESSION_DELETE_TIMEOUT_SECS: float
    # Reaper: a PROCESSING row older than this is presumed hard-crashed.
    REAP_STALE_PROCESSING_MINUTES: int
    # After this many lock acquisitions a reaped row goes FAILED (poison pill).
    MAX_PROCESSING_ATTEMPTS: int
    # The most rows one trigger message dispatches (oldest first, >= 1).
    CRF_MAX_ROWS_PER_RUN: int
    # The Cloud Tasks queue (id) and its location (default GCP_REGION).
    TASKS_QUEUE: str
    TASKS_LOCATION: str
    # The worker's Cloud Run URL: the task target and the OIDC audience
    # (must be https://; checked by `orchestrator.dispatch`).
    WORKER_URL: str
    # The service account whose OIDC token Cloud Tasks sends to the worker
    # ("" = unset and no project number to build the default compute SA).
    TASKS_INVOKER_SA: str
    # The task's dispatch_deadline: how long Cloud Tasks waits for the worker
    # (default the worker timeout, clamped to Cloud Tasks' 15-1800 s; an
    # ERROR when it is below the worker timeout, see the module doc).
    TASK_DISPATCH_DEADLINE_SECS: float

    @property
    def GOOGLE_CLOUD_PROJECT(self) -> str:
        """GCP project ID (required; raises on use while unset/blank)."""
        if not self.project_id:
            return _required_env("GOOGLE_CLOUD_PROJECT")
        return self.project_id

    @property
    def GOOGLE_CLOUD_PROJECT_NUMBER(self) -> str:
        """Project number for the Reasoning Engine path (``worker.py``); falls
        back to the project ID, but the worker's engine id requires the number
        (``_engine_id_env``), so the worker never runs on the fallback."""
        return self.project_number or self.GOOGLE_CLOUD_PROJECT


# Every env var load_config reads: the cache key.
_ENV_NAMES = (
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_PROJECT_NUMBER",
    "GCP_REGION",
    "CREATIVE_AGENT_ENGINE_ID",
    "AGENT_WORKER_USER_ID",
    "CRF_ROOT_AGENT_NAME",
    "BQ_DATASET_ID",
    "BQ_TABLE_TARGETS",
    "CRF_EXTRA_ALLOWED_TABLES",
    "CRF_BQ_QUERY_TIMEOUT_SECS",
    "CRF_WORKER_TIMEOUT_SECS",
    "CRF_BUDGET_SAFETY_SECS",
    "CRF_STREAM_TIMEOUT_SECS",
    "CRF_POLL_INTERVAL_SECS",
    "CRF_IDLE_SECS",
    "CRF_SESSION_READ_TIMEOUT_SECS",
    "CRF_SESSION_DELETE_TIMEOUT_SECS",
    "REAP_STALE_PROCESSING_MINUTES",
    "MAX_PROCESSING_ATTEMPTS",
    "CRF_MAX_ROWS_PER_RUN",
    "CRF_TASKS_QUEUE",
    "CRF_TASKS_LOCATION",
    "CRF_WORKER_URL",
    "CRF_TASKS_INVOKER_SA",
    "CRF_TASK_DISPATCH_DEADLINE_SECS",
)


def default_reap_minutes(worker_timeout_secs: float) -> int:
    """The reaper's default threshold: the worker timeout in whole minutes
    plus ``REAP_MARGIN_MINUTES`` (45 for the default 1800 s)."""
    return math.ceil(worker_timeout_secs / 60) + REAP_MARGIN_MINUTES


def load_config() -> CrfConfig:
    """The current ``CrfConfig`` (cached per distinct environment)."""
    return _build(tuple(os.environ.get(name) for name in _ENV_NAMES))


@functools.lru_cache(maxsize=16)
def _build(_env_key: tuple) -> CrfConfig:
    worker_timeout = _float_env(
        "CRF_WORKER_TIMEOUT_SECS", DEFAULT_WORKER_TIMEOUT_SECS, 300.0, 3600.0
    )
    bq_timeout = _float_env(
        "CRF_BQ_QUERY_TIMEOUT_SECS", DEFAULT_BQ_QUERY_TIMEOUT_SECS, 5.0, 600.0
    )
    read_timeout = _float_env(
        "CRF_SESSION_READ_TIMEOUT_SECS", DEFAULT_SESSION_READ_TIMEOUT_SECS, 5.0, 120.0
    )
    delete_timeout = _float_env(
        "CRF_SESSION_DELETE_TIMEOUT_SECS",
        DEFAULT_SESSION_DELETE_TIMEOUT_SECS,
        1.0,
        30.0,
    )
    safety = _float_env(
        "CRF_BUDGET_SAFETY_SECS", DEFAULT_BUDGET_SAFETY_SECS, 30.0, 600.0
    )

    # Cross-check: the safety margin covers the post-budget work.
    needed = read_timeout + delete_timeout + bq_timeout
    if safety < needed:
        if needed <= worker_timeout / 2:
            logger.error(
                f"CRF_BUDGET_SAFETY_SECS={safety:g} is below the session read + "
                f"delete + BigQuery timeouts ({needed:g}); using {needed:g}."
            )
            safety = needed
        else:
            logger.error(
                f"CRF_BUDGET_SAFETY_SECS={safety:g} can't cover the session read "
                f"+ delete + BigQuery timeouts ({needed:g}) within half of "
                f"CRF_WORKER_TIMEOUT_SECS={worker_timeout:g}; using the default "
                "timeouts and safety margin."
            )
            bq_timeout = DEFAULT_BQ_QUERY_TIMEOUT_SECS
            read_timeout = DEFAULT_SESSION_READ_TIMEOUT_SECS
            delete_timeout = DEFAULT_SESSION_DELETE_TIMEOUT_SECS
            safety = DEFAULT_BUDGET_SAFETY_SECS

    # Cross-check: the reaper never fires while a worker can still be running.
    reap_default = default_reap_minutes(worker_timeout)
    reap = _int_env("REAP_STALE_PROCESSING_MINUTES", reap_default, 1, 1440)
    if reap * 60 < worker_timeout:
        logger.error(
            f"REAP_STALE_PROCESSING_MINUTES={reap} is below the worker timeout "
            f"(CRF_WORKER_TIMEOUT_SECS={worker_timeout:g}); using {reap_default}."
        )
        reap = reap_default

    dataset = _str_env("BQ_DATASET_ID", "trend_trawler")
    table = _str_env("BQ_TABLE_TARGETS", "target_trends_crf")
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    project_number = os.environ.get("GOOGLE_CLOUD_PROJECT_NUMBER", "").strip()
    region = _str_env("GCP_REGION", "us-central1")

    # Cross-check: the dispatch deadline covers the whole worker run.
    dispatch_deadline = _float_env(
        "CRF_TASK_DISPATCH_DEADLINE_SECS",
        min(worker_timeout, MAX_DISPATCH_DEADLINE_SECS),
        MIN_DISPATCH_DEADLINE_SECS,
        MAX_DISPATCH_DEADLINE_SECS,
    )
    if worker_timeout > dispatch_deadline:
        logger.error(
            f"CRF_WORKER_TIMEOUT_SECS={worker_timeout:g} exceeds the task "
            f"dispatch deadline ({dispatch_deadline:g} s): Cloud Tasks frees "
            "the queue's dispatch slot while the run continues, so the next "
            "task meets a busy worker. Set the worker's --timeout and "
            "CRF_WORKER_TIMEOUT_SECS to the deadline."
        )
    return CrfConfig(
        project_id=project_id,
        project_number=project_number,
        GCP_REGION=region,
        CREATIVE_AGENT_ENGINE_ID=_engine_id_env(region, project_id, project_number),
        AGENT_WORKER_USER_ID=_str_env("AGENT_WORKER_USER_ID", "crf_worker"),
        ROOT_AGENT_NAME=_str_env("CRF_ROOT_AGENT_NAME", "root_agent"),
        BQ_DATASET_ID=dataset,
        BQ_TABLE_TARGETS=table,
        ALLOWED_BQ_DATASETS=frozenset({dataset}),
        ALLOWED_BQ_TABLES=frozenset({table} | _csv_env("CRF_EXTRA_ALLOWED_TABLES")),
        BQ_QUERY_TIMEOUT_SECS=bq_timeout,
        WORKER_TIMEOUT_SECS=worker_timeout,
        BUDGET_SAFETY_SECS=safety,
        STREAM_TIMEOUT_SECS=_float_env("CRF_STREAM_TIMEOUT_SECS", 1500.0, 60.0, 3600.0),
        POLL_INTERVAL_SECS=_float_env("CRF_POLL_INTERVAL_SECS", 30.0, 5.0, 300.0),
        IDLE_SECS=_float_env("CRF_IDLE_SECS", 600.0, 600.0, 3600.0),
        SESSION_READ_TIMEOUT_SECS=read_timeout,
        SESSION_DELETE_TIMEOUT_SECS=delete_timeout,
        REAP_STALE_PROCESSING_MINUTES=reap,
        MAX_PROCESSING_ATTEMPTS=_int_env("MAX_PROCESSING_ATTEMPTS", 3, 1, 10),
        CRF_MAX_ROWS_PER_RUN=_int_env("CRF_MAX_ROWS_PER_RUN", 3, 1, 100),
        TASKS_QUEUE=_str_env("CRF_TASKS_QUEUE", DEFAULT_TASKS_QUEUE),
        TASKS_LOCATION=_str_env("CRF_TASKS_LOCATION", region),
        WORKER_URL=os.environ.get("CRF_WORKER_URL", "").strip(),
        TASKS_INVOKER_SA=_invoker_env(project_number),
        TASK_DISPATCH_DEADLINE_SECS=dispatch_deadline,
    )


_FIELD_NAMES = frozenset(f.name for f in fields(CrfConfig))
_PROPERTY_NAMES = frozenset({"GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_NUMBER"})


class _ConfigAccessor:
    """``config.X`` -> ``load_config().X``, resolved on every access.

    Keeps the ``from .config import config`` / ``config.X`` call sites working
    while the values come from one validated ``CrfConfig``. Tests may
    ``monkeypatch.setattr(config, "X", v)``: that stores an override (setting
    a value equal to the environment's drops it, so monkeypatch's undo leaves
    no stale pin).
    """

    def __init__(self) -> None:
        object.__setattr__(self, "_overrides", {})

    def __getattr__(self, name: str):
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(load_config(), name)

    def __setattr__(self, name: str, value) -> None:
        if name not in _FIELD_NAMES and name not in _PROPERTY_NAMES:
            raise AttributeError(f"CrfConfig has no setting {name!r}")
        overrides = object.__getattribute__(self, "_overrides")
        try:
            current = getattr(load_config(), name)
        except RuntimeError:  # the required project is unset
            current = object()
        if value == current:
            overrides.pop(name, None)
        else:
            overrides[name] = value

    def __delattr__(self, name: str) -> None:
        object.__getattribute__(self, "_overrides").pop(name, None)


# Typed as CrfConfig so call sites are type-checked against the dataclass.
config = cast(CrfConfig, _ConfigAccessor())
