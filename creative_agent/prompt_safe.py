"""Tiny shared helpers for the learning notes spliced into ADK instructions.

Used by brand_history.py and rating_signals.py (which brand_history imports, so
these live here to avoid an import cycle): the brand match key, the brace-free
text filter (ADK treats ``{...}`` as state tokens) and the ad-copy tone
allowlist.
"""

from __future__ import annotations

from typing import Any, get_args

from .schemas import FinalAdCopy

# The ad-copy tone Literal: the only copy tones that may reach a prompt.
ALLOWED_TONES: frozenset[str] = frozenset(
    get_args(FinalAdCopy.model_fields["tone_style"].annotation)
)


def normalize_brand(value: Any) -> str:
    """The brand match key: strip + lower (the Python side of the SQL
    ``LOWER(TRIM(brand)) = LOWER(@brand)``); ``""`` for a non-string. Never
    truncates. Shared by the api's rating rows (runserver/ratings.py) and the
    rating-learning query."""
    return value.strip().lower() if isinstance(value, str) else ""


def brace_free(value: Any) -> str:
    """Brace-free text (the note is spliced into ADK instructions as state)."""
    return str(value).replace("{", "").replace("}", "").strip()
