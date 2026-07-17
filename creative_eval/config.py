"""Configuration for creative evaluation."""

import os
from dataclasses import dataclass, field

from agent_common.locations import MODEL_LOCATION


@dataclass
class EvalConfig:
    """Configuration for the creative evaluation pipeline."""

    # Evaluation model (judge). Overridable via EVAL_MODEL so the judge can be
    # A/B'd against a regional model (e.g. gemini-2.5-pro @ us-central1) without
    # a code change. Default stays gemini-3.1-pro-preview (global) until a
    # validated swap is deliberately shipped.
    eval_model: str = field(
        default_factory=lambda: os.getenv("EVAL_MODEL", "gemini-3.1-pro-preview")
    )

    # Score thresholds
    passing_threshold: float = 0.7
    max_retries: int = 3

    # Max concurrent judge calls. Each creative is scored by an independent judge
    # call. On the DEFAULT judge (gemini-3.1-pro-preview @ `global`) the base
    # model is capped at **5 RPM** project-wide/shared. It's not just the initial
    # wave: at ~28s/call, 3 workers land ~6 calls inside a rolling 60s window,
    # which exceeds the pro quota and trips 429/503; 2 workers stay under it
    # (~4/min). Default is therefore 2. Transient 429s are also retried at the
    # HTTP layer (see _get_client), so this is defence-in-depth, not the only
    # guard. If the judge is moved to a DEDICATED regional bucket
    # (EVAL_MODEL_LOCATION=us-central1, an unused pool), raise via EVAL_MAX_WORKERS
    # for a faster eval. See docs/notes/ambient-agents-vs-cloud-functions.md.
    max_eval_workers: int = field(
        default_factory=lambda: int(os.getenv("EVAL_MAX_WORKERS", "2"))
    )

    # Scoring dimensions and weights for ad copy
    ad_copy_dimensions: list[str] = field(
        default_factory=lambda: [
            "strategic_alignment",
            "trend_authenticity",
            "platform_viability",
            "copy_quality",
            "audience_fit",
            "call_to_action_strength",
        ]
    )

    # Scoring dimensions and weights for visual concepts
    visual_dimensions: list[str] = field(
        default_factory=lambda: [
            "trend_visual_connection",
            "brand_product_representation",
            "audience_appeal",
            "prompt_technical_quality",
            "stopping_power",
            "concept_coherence",
        ]
    )

    # GCP settings (inherit from environment)
    project_id: str = field(
        default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT", "")
    )
    # Judge serving location. The DEFAULT judge (gemini-3.1-pro-preview) is a
    # gemini-3.x model served ONLY from `global` (us-central1 returns 404), so
    # this defaults to MODEL_LOCATION (`global`, driven by the non-reserved
    # MODEL_LOCATION env var — deliberately decoupled from GOOGLE_CLOUD_LOCATION,
    # which Agent Engine reserves/regionalizes and which would 404 the judge).
    # Overridable via EVAL_MODEL_LOCATION: gemini-2.5-* models ARE served
    # regionally, so pairing EVAL_MODEL=gemini-2.5-pro with
    # EVAL_MODEL_LOCATION=us-central1 lands the judge in a dedicated regional
    # per-base-model bucket, separate from the agents' global pools.
    location: str = field(
        default_factory=lambda: os.getenv("EVAL_MODEL_LOCATION", MODEL_LOCATION)
    )
