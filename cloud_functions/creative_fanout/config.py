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
