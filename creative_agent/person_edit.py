"""The person-swap edit request (personalised variants) — pure.

A personalised variant edits the run's original render instead of
re-generating the scene: the image model gets ``EDIT_INSTRUCTION`` ("replace
only the person"), the original ad and the consented person photo, in that
order, each labelled. It is one edit from the original (never an edit chain).
Calibrated in ``docs/notes/person-swap-edit-mode.md`` (edit fidelity 75% vs 0%
for a fresh render). The text constants are brace-free.
"""

from __future__ import annotations

from collections.abc import Sequence

from google.genai import types

from .image_tools import REFERENCE_IGNORE_TEXT_LINE, no_other_logos_line

EDIT_INSTRUCTION = (
    "Edit image 1. Replace only the main person with the person in image 2: keep "
    "their face shape, skin tone, hair, eye colour, apparent age and "
    "distinguishing features. Keep everything else in image 1 exactly as it is: "
    "the composition and framing, camera angle, lighting, colours, background, "
    "every prop and its count, all arrows and graphic elements and their "
    "direction, every word of text, logos and the product. Match image 1's art "
    "style. Do not add, remove or move anything else."
)
BASE_LABEL = "Image 1 (original ad):"
PERSON_LABEL = "Image 2 (person):"


def edit_contents(
    base_part: types.Part,
    person_part: types.Part,
    *,
    brand: str = "",
    strictness: Sequence[str] = (),
    instruction: str = EDIT_INSTRUCTION,
) -> list[types.Part]:
    """The edit request. Order matters: the instruction (a re-render passes
    ``EDIT_INSTRUCTION`` plus the image check's correction), the
    ``unwanted_logo`` strictness line, the ignore-reference-text line, then the
    labelled original, then the labelled person photo."""
    lines = [instruction]
    if "unwanted_logo" in strictness:
        lines.append(no_other_logos_line(brand))
    lines.append(REFERENCE_IGNORE_TEXT_LINE)
    return [
        types.Part.from_text(text="\n\n".join(lines)),
        types.Part.from_text(text=BASE_LABEL),
        base_part,
        types.Part.from_text(text=PERSON_LABEL),
        person_part,
    ]


def _ratio_value(ratio: str) -> float:
    w, h = ratio.split(":")
    return float(w) / float(h)


def nearest_aspect_ratio(
    width: int, height: int, allowed: Sequence[str], default: str = "9:16"
) -> str:
    """The allowed ``"w:h"`` ratio closest to ``width / height``."""
    if width <= 0 or height <= 0 or not allowed:
        return default
    actual = width / height
    return min(allowed, key=lambda r: abs(_ratio_value(r) - actual))
