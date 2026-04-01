"""Configuration for creative evaluation."""

import os
from dataclasses import dataclass, field


@dataclass
class EvalConfig:
    """Configuration for the creative evaluation pipeline."""

    # Evaluation model (judge)
    eval_model: str = "gemini-2.5-pro"

    # Score thresholds
    passing_threshold: float = 0.7
    max_retries: int = 3

    # Scoring dimensions and weights for ad copy
    ad_copy_dimensions: list[str] = field(default_factory=lambda: [
        "strategic_alignment",
        "trend_authenticity",
        "platform_viability",
        "copy_quality",
        "audience_fit",
        "call_to_action_strength",
    ])

    # Scoring dimensions and weights for visual concepts
    visual_dimensions: list[str] = field(default_factory=lambda: [
        "trend_visual_connection",
        "brand_product_representation",
        "audience_appeal",
        "prompt_technical_quality",
        "stopping_power",
        "concept_coherence",
    ])

    # GCP settings (inherit from environment)
    project_id: str = field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    location: str = field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))
