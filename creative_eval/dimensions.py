"""Names + human-readable labels for creative_eval's dimensions and gates.

The 12 scoring dimensions (advisory quality scores) and the binary compliance
gates. Both label maps mirror ``frontend/src/lib/eval-dimensions.ts`` (a drift
test keeps them identical) so BigQuery rows and the UI show the same labels.
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

# Binary compliance gates, in prompt/report order.
AD_COPY_GATES: tuple[str, ...] = (
    "delivers_proposition",
    "product_named",
    "uses_reason_to_believe",
    "mandatories_met",
    "avoid_respected",
)
VISUAL_GATES: tuple[str, ...] = (
    "product_visible",
    "trend_motif_visible",
    "text_correct",
    "brand_cue_present",
    "avoid_respected",
)
# Recorded but never part of gates_passed (mirrors image QA, where a missing
# brand cue never fails an image).
ADVISORY_GATES: frozenset[str] = frozenset({"brand_cue_present"})
# Judged against the creative brief: passed with note "no brief" without one.
BRIEF_GATES: frozenset[str] = frozenset(
    {
        "delivers_proposition",
        "uses_reason_to_believe",
        "mandatories_met",
        "avoid_respected",
    }
)

GATE_LABELS: dict[str, str] = {
    # Ad copy
    "delivers_proposition": "Delivers the proposition",
    "product_named": "Product named",
    "uses_reason_to_believe": "Uses a reason to believe",
    "mandatories_met": "Mandatories met",
    # Shared
    "avoid_respected": "Avoid list respected",
    # Visual concept
    "product_visible": "Product visible",
    "trend_motif_visible": "Trend motif visible",
    "text_correct": "In-image text correct",
    "brand_cue_present": "Brand cue present",
}


def dimension_label(dimension: str) -> str:
    """Label for a dimension; unknown snake_case names become sentence case."""
    if dimension in DIMENSION_LABELS:
        return DIMENSION_LABELS[dimension]
    words = re.sub(r"\s+", " ", re.sub(r"_+", " ", dimension).strip()).lower()
    return words[:1].upper() + words[1:]


def gate_label(gate: str) -> str:
    """Label for a gate; unknown names fall back like :func:`dimension_label`."""
    return GATE_LABELS.get(gate) or dimension_label(gate)


def dimension_labels_csv(dimensions: Iterable[str]) -> str:
    """Join the labels of ``dimensions`` with ", " (empty input -> "")."""
    return ", ".join(dimension_label(d) for d in dimensions)
