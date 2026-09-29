# config.py

import os


def _csv_env(name: str) -> set[str]:
    """Comma-separated env var -> set of non-empty, stripped names."""
    return {v.strip() for v in os.environ.get(name, "").split(",") if v.strip()}


class AppConfig:
    # gcp project
    GOOGLE_CLOUD_PROJECT = "hybrid-vertex"
    # Agent Engine is a *regional* resource — this is us-central1, NOT the
    # `global` model location used for the gemini-3.x endpoints.
    GCP_REGION = "us-central1"
    GOOGLE_CLOUD_PROJECT_NUMBER = 934903580331
    CREATIVE_WORKER_TOPIC_NAME = "creative-worker-queue-topic"
    # Reaper: a PROCESSING row older than this (worker presumed hard-crashed) is
    # reclaimed. Must exceed the worker's 1800s/30min Cloud Run timeout + margin.
    REAP_STALE_PROCESSING_MINUTES = int(
        os.environ.get("REAP_STALE_PROCESSING_MINUTES", "45")
    )
    # After this many lock acquisitions, a reaped row goes FAILED instead of
    # QUEUED (poison-pill guard).
    MAX_PROCESSING_ATTEMPTS = int(os.environ.get("MAX_PROCESSING_ATTEMPTS", "3"))
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
