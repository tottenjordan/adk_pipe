"""Shared base configuration for the agent packages.

``trend_trawler`` and ``creative_agent`` carried near-identical
``ResearchConfiguration`` dataclasses (same model names, rate-limit knobs and GCP
env vars). That copy-paste is exactly how the deploy env-var drift crept in, so
this is the single source of truth: each agent subclasses ``BaseAgentConfiguration``
and adds only its genuine differences (trend_trawler's ``SetupConfiguration``;
creative_agent's genai ``ServerError`` retry).

Env values are read at class-definition (import) time, matching the previous
per-agent configs. The bucket name comes from ``GOOGLE_CLOUD_STORAGE_BUCKET`` —
the var ``deployment/deploy_agent.py`` actually ships to Agent Engine (reading
the local-only ``GCS_BUCKET_NAME`` gave a deployed engine ``None``).

This module has NO ADK/genai imports so it stays lightweight; the ADK
``RetryConfig`` factory lives in :mod:`agent_common.retry`.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load the repo-root .env (agent_common is a top-level sibling of the agents).
ENV_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
load_dotenv(dotenv_path=ENV_FILE_PATH)


@dataclass
class BaseAgentConfiguration:
    """Shared model + GCP configuration for the agents.

    Attributes:
        state_init (str): a key indicating the state dict is initialized.
        critic_model (str): Model for evaluation tasks.
        worker_model (str): Model for working/generation tasks.
        video_analysis_model (str): Model for video understanding.
        lite_planner_model (str): Lightweight planner model.
        image_gen_model (str): Model for generating images.
        video_gen_model (str): Model for generating video.
        max_results_yt_trends (int): `max_results` for the YouTube API.
        rate_limit_seconds (int): window for the LLM API rate limiter.
        rpm_quota (int): requests-per-minute threshold for the rate limiter.
        GCS_BUCKET (str): Cloud Storage bucket used to save artifacts.
        GCS_BUCKET_NAME (str): bucket name (no `gs://` prefix).
        PROJECT_ID (str): GCP project id.
        PROJECT_NUMBER (str): GCP project number.
        LOCATION (str): GOOGLE_CLOUD_LOCATION (vestigial — model calls pin the
            serving location via agent_common.locations.MODEL_LOCATION instead).
    """

    state_init = "_state_init"

    # Models
    critic_model: str = "gemini-3.1-pro-preview"  # gemini-3-pro-preview
    worker_model: str = "gemini-3.5-flash"  # gemini-3-flash-preview
    video_analysis_model: str = "gemini-3.1-pro-preview"  # gemini-3-pro-preview
    lite_planner_model: str = "gemini-3.1-flash-lite"  # gemini-3-flash-preview
    image_gen_model: str = "gemini-3.1-flash-image"
    video_gen_model: str = "veo-3.1-generate-001"  # "veo-3.0-generate-001"
    max_results_yt_trends: int = 45

    # Adjust these values to limit the rate at which the agent queries the LLM API.
    rate_limit_seconds: int = 60
    rpm_quota: int = 1000

    # env vars (read at import)
    GCS_BUCKET = os.environ.get("BUCKET")
    # Read GOOGLE_CLOUD_STORAGE_BUCKET (what deploy_agent.py ships to Agent
    # Engine), NOT the local-only GCS_BUCKET_NAME — a deployed engine got None ->
    # "Cannot determine path without bucket name".
    GCS_BUCKET_NAME = os.environ.get("GOOGLE_CLOUD_STORAGE_BUCKET")
    PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
    PROJECT_NUMBER = os.environ.get("GOOGLE_CLOUD_PROJECT_NUMBER")
    LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION")

    BQ_PROJECT_ID = os.environ.get("BQ_PROJECT_ID")
    BQ_DATASET_ID = os.environ.get("BQ_DATASET_ID")
    BQ_TABLE_TARGETS = os.environ.get("BQ_TABLE_TARGETS")
    BQ_TABLE_CREATIVES = os.environ.get("BQ_TABLE_CREATIVES")
    BQ_TABLE_ALL_TRENDS = os.environ.get("BQ_TABLE_ALL_TRENDS")
