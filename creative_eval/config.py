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
    # Gemini call, so evaluating them in a thread pool cuts eval wall-clock from
    # ~N*28s (sequential) to roughly one call's latency. Keep this at/above the
    # typical creative count (6 ad copies + 6 visual concepts = 12).
    max_eval_workers: int = 12

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
