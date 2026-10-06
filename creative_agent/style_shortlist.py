"""Per-session style shortlist for the visual concept agents.

The prompt-writing model applies IMAGE_PROMPT_GUIDE's tone→style mapping so
literally that runs converge on the same few looks. Seeding each session with a
random, stratified shortlist of style families (drawn in code, not by the
model) forces cross-run variety while the drafter still matches styles to tone.
Family names must match the IMAGE_PROMPT_GUIDE <STYLE_PALETTE> entries exactly.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

STYLE_GROUPS: dict[str, tuple[str, ...]] = {
    "photographic": (
        "Photoreal / editorial",
        "Cinematic film still",
        "Candid 35mm film photo",
    ),
    "illustrated": (
        "3D character render",
        "2D flat / vector cartoon",
        "Anime / manga",
        "Comic panel",
        "Watercolor / gouache",
        "Collage / mixed-media",
        "Retro / vaporwave",
    ),
    "graphic": (
        "Meme aesthetic",
        "Diecut sticker",
        "Isometric miniature world",
        "Minimalist negative-space",
    ),
}
SHORTLIST_QUOTA: dict[str, int] = {"photographic": 2, "illustrated": 3, "graphic": 1}


def pick_style_shortlist(rng: random.Random | None = None) -> list[str]:
    """Return 6 distinct style families: 2 photographic, 3 illustrated, 1 graphic."""
    rng = rng or random.Random()
    picks: list[str] = []
    for group, n in SHORTLIST_QUOTA.items():
        picks.extend(rng.sample(STYLE_GROUPS[group], n))
    rng.shuffle(picks)
    return picks


def format_shortlist(picks: Sequence[str]) -> str:
    """Render the shortlist for the {style_shortlist?} prompt token (brace-free)."""
    return "; ".join(picks)
