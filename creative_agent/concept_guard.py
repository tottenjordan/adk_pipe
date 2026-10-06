"""Deterministic last-line guard: every final image prompt names the trend motif
and the product, so the image model can't render a trend-less / product-less ad.

`image_generation_prompt` is the only text the image model sees, and prompt
wording alone is a request, not a guarantee. This pure helper appends a short
scene sentence when either is missing; `callbacks.ensure_trend_and_product_callback`
applies it to `final_visual_concepts` after the finalizer (and interactive's
reviser) write it. It cannot prove the rendered pixels show them.
"""

from __future__ import annotations

import copy
from typing import Any


def ensure_trend_and_product(
    concepts: list[dict[str, Any]], target_product: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return repaired copies of `concepts` plus one warning per repair/miss."""
    out: list[dict[str, Any]] = []
    warns: list[str] = []
    product = (target_product or "").strip()
    for concept in concepts:
        c = copy.deepcopy(concept)
        prompt = str(c.get("image_generation_prompt") or "").rstrip()
        low = prompt.lower()
        motif = str(c.get("trend_motif") or "").strip()
        name = c.get("concept_name", "?")
        if not motif:
            warns.append(f"{name}: empty trend_motif")
        elif motif.lower() not in low:
            prompt += f" The scene visibly includes {motif}."
            warns.append(f"{name}: trend_motif missing from prompt, appended")
        if product and product.lower() not in low:
            prompt += f" The {product} is clearly visible and recognizable."
            warns.append(f"{name}: product missing from prompt, appended")
        c["image_generation_prompt"] = prompt
        out.append(c)
    return out, warns
