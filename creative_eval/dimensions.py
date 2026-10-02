"""Human-readable labels for the 12 creative_eval scoring dimensions.

Mirrors ``frontend/src/lib/eval-dimensions.ts`` (a drift test keeps the two maps
identical) so BigQuery rows and the UI show the same short labels.
"""

import re
from collections.abc import Iterable

DIMENSION_LABELS: dict[str, str] = {
    # Ad copy
    "strategic_alignment": "Strategy fit",
    "trend_authenticity": "Trend authenticity",
    "platform_viability": "Platform fit",
    "copy_quality": "Copy quality",
    "audience_fit": "Audience fit",
    "call_to_action_strength": "Call to action",
    # Visual concept
    "trend_visual_connection": "Trend connection",
    "brand_product_representation": "Brand & product",
    "audience_appeal": "Audience appeal",
    "prompt_technical_quality": "Prompt quality",
    "stopping_power": "Stopping power",
    "concept_coherence": "Coherence",
}


def dimension_label(dimension: str) -> str:
    """Label for a dimension; unknown snake_case names become sentence case."""
    if dimension in DIMENSION_LABELS:
        return DIMENSION_LABELS[dimension]
    words = re.sub(r"\s+", " ", re.sub(r"_+", " ", dimension).strip()).lower()
    return words[:1].upper() + words[1:]


def dimension_labels_csv(dimensions: Iterable[str]) -> str:
    """Join the labels of ``dimensions`` with ", " (empty input -> "")."""
    return ", ".join(dimension_label(d) for d in dimensions)
