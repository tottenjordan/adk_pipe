"""Configuration for creative evaluation."""

import os
from dataclasses import dataclass, field

from agent_common.locations import MODEL_LOCATION


@dataclass
class EvalConfig:
    """Configuration for the creative evaluation pipeline."""

    # Evaluation model (judge)
    eval_model: str = "gemini-3.1-pro-preview"

    # Score thresholds
    passing_threshold: float = 0.7
    max_retries: int = 3

    # Max concurrent judge calls. Each creative is scored by an independent
    # gemini-3.1-pro-preview call, but that model is capped at **5 RPM** on the
    # `global` endpoint (project-wide, shared). Fanning out 12 at once trips
    # 503 UNAVAILABLE / 429 — a single run's eval alone exceeds the pro quota.
    # Keep this at/below the pro RPM so a paced run stays under quota; raise it
    # only if the quota is raised. See docs/notes/ambient-agents-vs-cloud-functions.md.
    max_eval_workers: int = 3

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
    # The judge is a gemini-3.x model, only served from the `global` Vertex
    # location (us-central1 returns 404 NOT_FOUND). MODEL_LOCATION (default
    # `global`, driven by the non-reserved MODEL_LOCATION env var) is decoupled
    # from GOOGLE_CLOUD_LOCATION on purpose: the latter is reserved by Agent
    # Engine and injected as the regional value, which would 404 the judge.
    location: str = field(default_factory=lambda: MODEL_LOCATION)
