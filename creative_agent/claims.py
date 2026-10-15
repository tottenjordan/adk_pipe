"""Unsupported absolute claims: a small lexicon shared by the brief and copy gates.

Pure logic (no ADK imports). Absolute product claims ("guaranteed to stay in
tune", "truly indestructible") are a legal and trust risk when nobody gave
them: the brief writer and the copy agents must not invent them. A term counts
only when it is NOT also in the allowed text (the user's key selling points
only: the brief writer writes the mandatories, so they never license one), so "100% cotton" passes when the selling points
say it. Terms in one family ("guarantee" / "guaranteed" / "guarantees",
"100%" / "100 percent") allow each other.

Matching (the shared ``lexicon.Lexicon``) is whole-word, case- and
accent-insensitive (``text_match.fold``); spaces and hyphens inside a term are
interchangeable ("risk free" == "risk-free"), and "100%" never matches a bare
"100". The idioms "of a lifetime" / "once in a lifetime" are not lifetime
claims.
"""

from collections.abc import Iterable
from typing import Any

from .lexicon import Lexicon

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

_LEXICON = Lexicon(
    ABSOLUTE_CLAIM_TERMS,
    idioms=("(?:of|in) a lifetime",),
    # Terms that allow each other: the family key is the first member.
    families=(("guarantee", "guaranteed", "guarantees"), ("100%", "100 percent")),
    boundary=r"\w%",  # "100%" never inside "1000%" or "100%%"
)


def unsupported_claims(text: str, *, allowed_text: str = "") -> list[str]:
    """The ``ABSOLUTE_CLAIM_TERMS`` in ``text`` that ``allowed_text`` lacks.

    Returned once each, in lexicon order ([] = none). A term is allowed when
    ``allowed_text`` contains it or another term of its family.
    """
    allowed = {_LEXICON.family(t) for t in _LEXICON.find(allowed_text)}
    return [t for t in _LEXICON.find(text) if _LEXICON.family(t) not in allowed]


def claims_allowed_text(*parts: str | Iterable[Any] | None) -> str:
    """The allowed text from strings and/or lists (non-strings skipped).

    E.g. ``claims_allowed_text(state["key_selling_points"])``.
    """
    lines: list[str] = []
    for part in parts:
        items = [part] if isinstance(part, str) or part is None else part
        lines.extend(i.strip() for i in items if isinstance(i, str) and i.strip())
    return "\n".join(lines)
