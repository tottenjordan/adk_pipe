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


DEFAULT_IMAGE_QA_MAX_RERENDERS = 1
MAX_IMAGE_QA_MAX_RERENDERS = 2
DEFAULT_IMAGE_QA_MAX_RERENDERS_PER_RUN = 2
MAX_IMAGE_QA_MAX_RERENDERS_PER_RUN = 8
DEFAULT_IMAGE_RENDER_CONCURRENCY = 2
MAX_IMAGE_RENDER_CONCURRENCY = 4

_FALSE_FLAGS = frozenset({"0", "false", "no", "off"})


def parse_image_qa_enabled(raw: str | None) -> bool:
    """``IMAGE_QA_ENABLED`` → bool; ON unless explicitly 0/false/no/off."""
    return (raw or "").strip().lower() not in _FALSE_FLAGS


def parse_trend_digest_enabled(raw: str | None) -> bool:
    """``TREND_DIGEST_ENABLED`` → bool; ON unless explicitly 0/false/no/off."""
    return (raw or "").strip().lower() not in _FALSE_FLAGS


def parse_image_qa_max_rerenders(raw: str | None) -> int:
    """``IMAGE_QA_MAX_RERENDERS`` → int clamped to 0..2; unset/blank/invalid → 1."""
    try:
        value = int((raw or "").strip())
    except ValueError:
        return DEFAULT_IMAGE_QA_MAX_RERENDERS
    return max(0, min(MAX_IMAGE_QA_MAX_RERENDERS, value))


def parse_image_qa_max_rerenders_per_run(raw: str | None) -> int:
    """``IMAGE_QA_MAX_RERENDERS_PER_RUN`` → int clamped to 0..8; unset/blank/invalid → 2."""
    try:
        value = int((raw or "").strip())
    except ValueError:
        return DEFAULT_IMAGE_QA_MAX_RERENDERS_PER_RUN
    return max(0, min(MAX_IMAGE_QA_MAX_RERENDERS_PER_RUN, value))


def parse_image_render_concurrency(raw: str | None) -> int:
    """``IMAGE_RENDER_CONCURRENCY`` → int clamped to 1..4; unset/blank/invalid → 2."""
    try:
        value = int((raw or "").strip())
    except ValueError:
        return DEFAULT_IMAGE_RENDER_CONCURRENCY
    return max(1, min(MAX_IMAGE_RENDER_CONCURRENCY, value))


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
        image_qa_enabled (bool): inspect each rendered image (IMAGE_QA_ENABLED,
            default on; 0/false/no/off disables).
        image_qa_max_rerenders (int): targeted re-renders per image after a
            failed check (IMAGE_QA_MAX_RERENDERS, default 1, clamped 0..2).
        image_qa_max_rerenders_per_run (int): QA re-renders across ALL images
            of one generate_image call (IMAGE_QA_MAX_RERENDERS_PER_RUN, default
            2, clamped 0..8) — each re-render spends the ~2 RPM image quota.
        image_render_concurrency (int): image-model calls in flight at once in
            one generate_image call, re-renders included (IMAGE_RENDER_CONCURRENCY,
            default 2, clamped 1..4; 1 = strictly sequential renders).
        image_qa_model (str): vision model for the image check (IMAGE_QA_MODEL,
            default the worker model).
        trend_digest_enabled (bool): trend_scout's trend digest (grounded
            one-line summaries + the shared daily BigQuery cache;
            TREND_DIGEST_ENABLED, default on; 0/false/no/off runs only the
            sensitivity classifier, as before the digest).
        trend_timezone (str): IANA timezone for the per-run date state
            (TREND_TIMEZONE, default America/New_York).
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

    # Post-render image QA (creative_agent/image_qa.py): one vision call per
    # rendered image and at most image_qa_max_rerenders targeted re-renders.
    # default_factory: read per instance, so tests can monkeypatch the env.
    # The model literal mirrors worker_model.
    image_qa_enabled: bool = field(
        default_factory=lambda: parse_image_qa_enabled(os.getenv("IMAGE_QA_ENABLED"))
    )
    image_qa_max_rerenders: int = field(
        default_factory=lambda: parse_image_qa_max_rerenders(
            os.getenv("IMAGE_QA_MAX_RERENDERS")
        )
    )
    image_qa_max_rerenders_per_run: int = field(
        default_factory=lambda: parse_image_qa_max_rerenders_per_run(
            os.getenv("IMAGE_QA_MAX_RERENDERS_PER_RUN")
        )
    )
    image_render_concurrency: int = field(
        default_factory=lambda: parse_image_render_concurrency(
            os.getenv("IMAGE_RENDER_CONCURRENCY")
        )
    )
    image_qa_model: str = field(
        default_factory=lambda: os.getenv("IMAGE_QA_MODEL") or "gemini-3.8-flash"
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

    # Timezone for the per-run date state (agent_common/clock.py; seeded once per
    # run as current_date / current_datetime / current_timezone / current_weekday).
    # Only the NAME is read here; the date itself is computed at state init.
    trend_timezone: str = field(
        default_factory=lambda: (
            (os.getenv("TREND_TIMEZONE") or "").strip() or "America/New_York"
        )
    )

    # trend_scout's trend digest (trend_scout/digest.py): grounded one-line
    # summaries of the day's trends + the sensitivity flags, cached per trend
    # list in BQ_TABLE_TREND_DIGEST. Off = only the classifier runs (no
    # describe step, no cache I/O).
    trend_digest_enabled: bool = field(
        default_factory=lambda: parse_trend_digest_enabled(
            os.getenv("TREND_DIGEST_ENABLED")
        )
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
    # Human creative ratings (written by the api, runserver/ratings_store.py);
    # read by creative_agent's opt-in rating learning (rating_signals.py).
    # default_factory: read per instance (the api's default table name).
    BQ_TABLE_RATINGS: str = field(
        default_factory=lambda: os.getenv("BQ_TABLE_RATINGS") or "creative_ratings"
    )
    # trend_scout's daily trend digest cache (trend_scout/digest.py).
    BQ_TABLE_TREND_DIGEST: str = field(
        default_factory=lambda: os.getenv("BQ_TABLE_TREND_DIGEST") or "trend_digest"
    )
