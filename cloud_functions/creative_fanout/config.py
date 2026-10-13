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
  default is ``ceil(WORKER_TIMEOUT_SECS / 60) + 15``.

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
    CREATIVE_WORKER_TOPIC_NAME: str
    # Base user id of the worker's Agent Engine sessions (one per row:
    # `worker.session_user_id`).
    AGENT_WORKER_USER_ID: str
    # The engine's root agent name (= its events' author): the completion
    # check's end-of-invocation signal is that agent's final text.
    ROOT_AGENT_NAME: str
    # BigQuery status-tracking table. The Pub/Sub payloads name the
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

    @property
    def GOOGLE_CLOUD_PROJECT(self) -> str:
        """GCP project ID (required; raises on use while unset/blank)."""
        if not self.project_id:
            return _required_env("GOOGLE_CLOUD_PROJECT")
        return self.project_id

    @property
    def GOOGLE_CLOUD_PROJECT_NUMBER(self) -> str:
        """Project number for resource paths (Pub/Sub topic, Reasoning Engine);
        falls back to the project ID (both APIs accept either)."""
        return self.project_number or self.GOOGLE_CLOUD_PROJECT


# Every env var load_config reads: the cache key.
_ENV_NAMES = (
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_PROJECT_NUMBER",
    "GCP_REGION",
    "CREATIVE_WORKER_TOPIC_NAME",
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
    return CrfConfig(
        project_id=os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip(),
        project_number=os.environ.get("GOOGLE_CLOUD_PROJECT_NUMBER", "").strip(),
        GCP_REGION=_str_env("GCP_REGION", "us-central1"),
        CREATIVE_WORKER_TOPIC_NAME=_str_env(
            "CREATIVE_WORKER_TOPIC_NAME", "creative-worker-queue-topic"
        ),
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
