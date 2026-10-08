"""Unsupported absolute claims: a small lexicon shared by the brief and copy gates.

Pure logic (no ADK imports). Absolute product claims ("guaranteed to stay in
tune", "truly indestructible") are a legal and trust risk when nobody gave
them: the brief writer and the copy agents must not invent them. A term counts
only when it is NOT also in the allowed text (the user's key selling points
plus the brief's mandatories), so "100% cotton" passes when the selling points
say it. Terms in one family ("guarantee" / "guaranteed" / "guarantees",
"100%" / "100 percent") allow each other.

Matching is whole-word, case- and accent-insensitive (``text_match.fold``);
spaces and hyphens inside a term are interchangeable ("risk free" ==
"risk-free"), and "100%" never matches a bare "100". The idioms "of a
lifetime" / "once in a lifetime" are not lifetime claims.
"""

import re
from collections.abc import Iterable
from typing import Any

from .text_match import fold

ABSOLUTE_CLAIM_TERMS = (
    "guarantee",
    "guaranteed",
    "guarantees",
    "indestructible",
    "unbreakable",
    "bulletproof",
    "risk-free",
    "100%",
    "100 percent",
    "never fails",
    "never goes out of tune",
    "lifetime",
)

# Terms that allow each other: the family key is the first member.
_FAMILY = {
    "guaranteed": "guarantee",
    "guarantees": "guarantee",
    "100 percent": "100%",
}
_LIFETIME_IDIOM = re.compile(r"\b(?:of|in)[\s-]+a[\s-]+lifetime\b")


def _pattern(term: str) -> re.Pattern[str]:
    parts = (re.escape(p) for p in re.split(r"[\s-]+", term))
    return re.compile(r"(?<![\w%])" + r"[\s-]+".join(parts) + r"(?![\w%])")


_PATTERNS = {term: _pattern(term) for term in ABSOLUTE_CLAIM_TERMS}


def _found(text: str) -> list[str]:
    folded = _LIFETIME_IDIOM.sub(" ", fold(text))
    return [term for term, pattern in _PATTERNS.items() if pattern.search(folded)]


def unsupported_claims(text: str, *, allowed_text: str = "") -> list[str]:
    """The ``ABSOLUTE_CLAIM_TERMS`` in ``text`` that ``allowed_text`` lacks.

    Returned once each, in lexicon order ([] = none). A term is allowed when
    ``allowed_text`` contains it or another term of its family.
    """
    allowed = {_FAMILY.get(t, t) for t in _found(allowed_text)}
    return [t for t in _found(text) if _FAMILY.get(t, t) not in allowed]


def claims_allowed_text(*parts: str | Iterable[Any] | None) -> str:
    """The allowed text from strings and/or lists (non-strings skipped).

    E.g. ``claims_allowed_text(state["key_selling_points"], mandatories)``.
    """
    lines: list[str] = []
    for part in parts:
        items = [part] if isinstance(part, str) or part is None else part
        lines.extend(i.strip() for i in items if isinstance(i, str) and i.strip())
    return "\n".join(lines)
