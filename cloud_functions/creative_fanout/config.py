# config.py

import os


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


config = AppConfig()
