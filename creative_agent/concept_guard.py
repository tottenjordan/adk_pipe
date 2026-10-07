"""Deterministic last-line guard: every final image prompt names the trend motif
and the product, so the image model can't render a trend-less / product-less ad.

`image_generation_prompt` is the only text the image model sees, and prompt
wording alone is a request, not a guarantee. This pure helper appends a short
scene sentence when either is missing; `callbacks.ensure_trend_and_product_callback`
applies it to `final_visual_concepts` after the finalizer (and interactive's
reviser) write it. It cannot prove the rendered pixels show them.

"Missing" uses `text_match.mentions` (token overlap), not a literal substring:
the finalizer routinely paraphrases ("a PRS SE guitar" for "PRS SE CE24 electric
guitar", "friendship-bracelet stack under Eras Tour lights" for "Eras Tour
friendship bracelets", "cookie" for "cookies"), and a redundant appended sentence
both clutters the prompt and raises a false warning. A phrase counts as present
in full or when at least 60% of its content tokens appear (a bare "jacket" for
"Patagonia Nano Puff Jacket" is 1 of 4: still appended).

Intangible products (`is_intangible`: subscriptions, apps, services, plans,
insurance, internet…) cannot be "clearly visible and recognizable"; for them
the guard skips the append when the brand is already mentioned, and otherwise
appends a depictable cue (`INTANGIBLE_PRODUCT_LINE`) instead.
"""

from __future__ import annotations

import copy
from typing import Any

from .text_match import content_tokens, mentions, same_word

TANGIBLE_PRODUCT_LINE = " The {product} is clearly visible and recognizable."
INTANGIBLE_PRODUCT_LINE = (
    " The {product} is suggested through a branded app screen or logo in the scene."
)
MOTIF_LINE = " The scene visibly includes {motif}."

# Content tokens marking a product the camera cannot show as an object. Kept
# deliberately to unambiguous service words: "card" (a credit card can be
# shown), "premium" or "pass" (also physical product names) are not here.
INTANGIBLE_WORDS = frozenset(
    {
        "account",
        "app",
        "application",
        "banking",
        "broadband",
        "insurance",
        "internet",
        "loan",
        "membership",
        "mortgage",
        "plan",
        "platform",
        "service",
        "software",
        "streaming",
        "subscription",
        "wifi",
    }
)


def is_intangible(target_product: str) -> bool:
    """The product names a service/subscription, not a depictable object."""
    return any(
        same_word(token, word)
        for token in content_tokens(target_product)
        for word in INTANGIBLE_WORDS
    )


def ensure_trend_and_product(
    concepts: list[dict[str, Any]], target_product: str, *, brand: str = ""
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return repaired copies of `concepts` plus one warning per repair/miss."""
    out: list[dict[str, Any]] = []
    warns: list[str] = []
    product = (target_product or "").strip()
    brand = (brand or "").strip()
    intangible = bool(product) and is_intangible(product)
    for concept in concepts:
        c = copy.deepcopy(concept)
        prompt = str(c.get("image_generation_prompt") or "").rstrip()
        original = prompt
        motif = str(c.get("trend_motif") or "").strip()
        name = c.get("concept_name", "?")
        if not motif:
            warns.append(f"{name}: empty trend_motif")
        elif not mentions(original, motif):
            prompt += MOTIF_LINE.format(motif=motif)
            warns.append(f"{name}: trend_motif missing from prompt, appended")
        if product and not mentions(original, product):
            if not intangible:
                prompt += TANGIBLE_PRODUCT_LINE.format(product=product)
                warns.append(f"{name}: product missing from prompt, appended")
            elif not (brand and mentions(original, brand)):
                prompt += INTANGIBLE_PRODUCT_LINE.format(product=product)
                warns.append(f"{name}: product missing from prompt, appended")
        c["image_generation_prompt"] = prompt
        out.append(c)
    return out, warns
