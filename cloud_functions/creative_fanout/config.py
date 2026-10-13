# config.py

import os


def _csv_env(name: str) -> set[str]:
    """Comma-separated env var -> set of non-empty, stripped names."""
    return {v.strip() for v in os.environ.get(name, "").split(",") if v.strip()}


def _required_env(name: str) -> str:
    """Read a required env var at *use* time; raise loudly if unset/blank.

    Deliberately not evaluated at import so the module (and `main.py`) stays
    importable without GCP config — the tests import it with only a dummy
    `GOOGLE_CLOUD_PROJECT` (or none at all).
    """
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
        value = default
    return min(max(value, low), high)


class AppConfig:
    @property
    def GOOGLE_CLOUD_PROJECT(self) -> str:
        """GCP project ID (required; no default — a wrong project is worse
        than a loud failure). Read on access, not at import."""
        return _required_env("GOOGLE_CLOUD_PROJECT")

    @property
    def GOOGLE_CLOUD_PROJECT_NUMBER(self) -> str:
        """Project number for resource paths (Pub/Sub topic, Reasoning Engine).

        Optional: both APIs accept the project ID in place of the number, so an
        unset value falls back to `GOOGLE_CLOUD_PROJECT` (which is required).
        """
        return (
            os.environ.get("GOOGLE_CLOUD_PROJECT_NUMBER", "").strip()
            or self.GOOGLE_CLOUD_PROJECT
        )

    @property
    def BQ_QUERY_TIMEOUT_SECS(self) -> float:
        """Bound on each BigQuery status query/DML (`CRF_BQ_QUERY_TIMEOUT_SECS`,
        default 60, clamped 5-600; invalid -> 60). Used as the client wait
        (the client then tries to cancel the job) and as the job's server-side
        `job_timeout_ms`, so a stuck query fails the invocation instead of
        hanging until the Cloud Run timeout."""
        raw = os.environ.get("CRF_BQ_QUERY_TIMEOUT_SECS", "").strip()
        try:
            value = float(raw) if raw else 60.0
        except ValueError:
            value = 60.0
        return min(max(value, 5.0), 600.0)

    @property
    def WORKER_TIMEOUT_SECS(self) -> float:
        """The worker's Cloud Run request timeout (`CRF_WORKER_TIMEOUT_SECS`,
        default 1800, clamped 300-3600). Must match the deployed `--timeout`:
        the run budget (stream + completion poll + one retry) is this minus
        `BUDGET_SAFETY_SECS`, measured from when the row lock is taken."""
        return _float_env("CRF_WORKER_TIMEOUT_SECS", 1800.0, 300.0, 3600.0)

    @property
    def BUDGET_SAFETY_SECS(self) -> float:
        """Time kept back from the run budget for the session delete and the
        PROCESSED/FAILED write (`CRF_BUDGET_SAFETY_SECS`, default 150, 30-600)."""
        return _float_env("CRF_BUDGET_SAFETY_SECS", 150.0, 30.0, 600.0)

    @property
    def STREAM_TIMEOUT_SECS(self) -> float:
        """Bound on one `async_stream_query` call (`CRF_STREAM_TIMEOUT_SECS`,
        default 1500, 60-3600; also clamped to the remaining budget). A timeout
        leads into the completion check, never straight to FAILED."""
        return _float_env("CRF_STREAM_TIMEOUT_SECS", 1500.0, 60.0, 3600.0)

    @property
    def POLL_INTERVAL_SECS(self) -> float:
        """Session poll interval of the completion check
        (`CRF_POLL_INTERVAL_SECS`, default 30, 5-300)."""
        return _float_env("CRF_POLL_INTERVAL_SECS", 30.0, 5.0, 300.0)

    @property
    def IDLE_SECS(self) -> float:
        """After a stream error/timeout, a session with no new events for this
        long counts as idle (run over) (`CRF_IDLE_SECS`, default 180, 30-900)."""
        return _float_env("CRF_IDLE_SECS", 180.0, 30.0, 900.0)

    # Agent Engine is a *regional* resource — this is us-central1, NOT the
    # `global` model location used for the gemini-3.x endpoints.
    GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
    CREATIVE_WORKER_TOPIC_NAME = os.environ.get(
        "CREATIVE_WORKER_TOPIC_NAME", "creative-worker-queue-topic"
    )
    # Base user ID for the worker's Agent Engine sessions; each row's session
    # runs under `f"{AGENT_WORKER_USER_ID}_{index}"`.
    AGENT_WORKER_USER_ID = os.environ.get("AGENT_WORKER_USER_ID", "crf_worker")
    # Reaper: a PROCESSING row older than this (worker presumed hard-crashed) is
    # reclaimed. Must exceed the worker's 1800s/30min Cloud Run timeout + margin.
    REAP_STALE_PROCESSING_MINUTES = int(
        os.environ.get("REAP_STALE_PROCESSING_MINUTES", "45")
    )
    # After this many lock acquisitions, a reaped row goes FAILED instead of
    # QUEUED (poison-pill guard).
    MAX_PROCESSING_ATTEMPTS = int(os.environ.get("MAX_PROCESSING_ATTEMPTS", "3"))
    # Orchestrator row limit: the most rows one trigger message dispatches (oldest
    # first). A guard against fanning out the whole backlog by accident — the
    # rest stay unclaimed for the next trigger. A message may ask for fewer via
    # `max_rows`, never more.
    CRF_MAX_ROWS_PER_RUN = int(os.environ.get("CRF_MAX_ROWS_PER_RUN", "3"))
    # BigQuery status-tracking table. The Pub/Sub payloads name the dataset/table
    # (`bq_dataset`/`bq_table`), and BigQuery can't parameterize identifiers, so
    # every SQL builder validates them against these allow-lists before quoting.
    BQ_DATASET_ID = os.environ.get("BQ_DATASET_ID", "trend_trawler")
    BQ_TABLE_TARGETS = os.environ.get("BQ_TABLE_TARGETS", "target_trends_crf")
    ALLOWED_BQ_DATASETS = frozenset({BQ_DATASET_ID})
    # Extra opt-in tables (e.g. a `target_trends_crf_p95` load-test copy):
    # CRF_EXTRA_ALLOWED_TABLES="t1,t2".
    ALLOWED_BQ_TABLES = frozenset(
        {BQ_TABLE_TARGETS} | _csv_env("CRF_EXTRA_ALLOWED_TABLES")
    )


config = AppConfig()
