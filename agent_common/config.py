"""Shared base configuration for the agent packages.

``trend_scout`` and ``creative_agent`` carried near-identical
``ResearchConfiguration`` dataclasses (same model names, rate-limit knobs and GCP
env vars). That copy-paste is exactly how the deploy env-var drift crept in, so
this is the single source of truth: each agent subclasses ``BaseAgentConfiguration``
and adds only its genuine differences (trend_scout's ``SetupConfiguration``;
creative_agent's genai ``ServerError`` retry).

Env values are read at class-definition (import) time, matching the previous
per-agent configs. The bucket name comes from ``GOOGLE_CLOUD_STORAGE_BUCKET`` —
the var ``deployment/deploy_agent.py`` actually ships to Agent Engine (reading
the local-only ``GCS_BUCKET_NAME`` gave a deployed engine ``None``).

This module has NO ADK/genai imports so it stays lightweight; the ADK
``RetryConfig`` factory lives in :mod:`agent_common.retry`.
"""

import os
from dataclasses import dataclass, field

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
        critic_fallback_model (str): Failover model for the Pro (critic_model)
            producers on 429/5xx, from CRITIC_FALLBACK_MODEL; empty disables.
        lite_planner_model (str): Lightweight planner model.
        image_gen_model (str): Model for generating images.
        rate_limit_seconds (int): window for the LLM API rate limiter.
        rpm_quota (int): requests-per-minute threshold for the rate limiter.
        GCS_BUCKET (str | None): `gs://` bucket URI used to save artifacts,
            derived from GCS_BUCKET_NAME (None when the name is unset).
        GCS_BUCKET_NAME (str): bucket name (no `gs://` prefix), from
            GOOGLE_CLOUD_STORAGE_BUCKET.
        PROJECT_ID (str): GCP project id.
        PROJECT_NUMBER (str): GCP project number.
        LOCATION (str): GOOGLE_CLOUD_LOCATION (vestigial — model calls pin the
            serving location via agent_common.locations.MODEL_LOCATION instead).
    """

    state_init = "_state_init"

    # Models (2026-09 lineup refresh). All gemini-3.x, served only @ global.
    # critic stays on 3.1-pro-preview: no GA Pro exists yet.
    critic_model: str = "gemini-3.1-pro-preview"
    worker_model: str = "gemini-3.8-flash"
    lite_planner_model: str = "gemini-3.5-flash-lite"
    # Was gemini-3.1-flash-image until 2026-10.
    image_gen_model: str = "gemini-nano-banana-2.1"
    # Pro producers fail over to this on 429/5xx (ADK FallbackModel, see
    # agent_common.models.build_gemini_with_fallback). Defaults to worker_model's
    # bucket — never ALT_GLOBAL_MODEL (PR #101 campaign spread). os.getenv (not
    # `or`) so CRITIC_FALLBACK_MODEL="" stays empty: the kill switch. The literal
    # mirrors worker_model (drift guarded by
    # test_critic_fallback_defaults_to_worker_bucket).
    critic_fallback_model: str = field(
        default_factory=lambda: os.getenv("CRITIC_FALLBACK_MODEL", "gemini-3.8-flash")
    )

    # Image generation ImageConfig knobs (env-overridable). The default 9:16 is
    # the vertical social-reel framing (the model otherwise defaults to 1:1); the
    # allowed set is the social-safe subset of the SDK's supported aspect ratios
    # that the visual agents may choose from per concept.
    image_size: str = os.environ.get("IMAGE_SIZE", "2K")
    image_aspect_ratio_default: str = os.environ.get(
        "IMAGE_ASPECT_RATIO_DEFAULT", "9:16"
    )
    image_aspect_ratios_allowed: tuple[str, ...] = (
        "9:16",
        "1:1",
        "4:5",
        "16:9",
        "3:4",
    )

    # Adjust these values to limit the rate at which the agent queries the LLM API.
    rate_limit_seconds: int = 60
    rpm_quota: int = 1000

    # env vars (read at import)
    # Read GOOGLE_CLOUD_STORAGE_BUCKET (what deploy_agent.py ships to Agent
    # Engine), NOT the local-only GCS_BUCKET_NAME — a deployed engine got None ->
    # "Cannot determine path without bucket name".
    GCS_BUCKET_NAME = os.environ.get("GOOGLE_CLOUD_STORAGE_BUCKET")
    # The gs:// form is derived from the bare name (there is no separate BUCKET
    # env var any more), so the two can never disagree.
    GCS_BUCKET = f"gs://{GCS_BUCKET_NAME}" if GCS_BUCKET_NAME else None
    PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
    PROJECT_NUMBER = os.environ.get("GOOGLE_CLOUD_PROJECT_NUMBER")
    LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION")

    BQ_PROJECT_ID = os.environ.get("BQ_PROJECT_ID")
    BQ_DATASET_ID = os.environ.get("BQ_DATASET_ID")
    BQ_TABLE_TARGETS = os.environ.get("BQ_TABLE_TARGETS")
    BQ_TABLE_CREATIVES = os.environ.get("BQ_TABLE_CREATIVES")
    BQ_TABLE_EVALS = os.environ.get("BQ_TABLE_EVALS")
