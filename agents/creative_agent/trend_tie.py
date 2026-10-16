"""Does an ad copy tie to the trend? (the copy gate's trend-tie check)

Pure logic (no ADK imports) behind ``copy_gate``'s deterministic trend-tie
issue, the pre-judge counterpart of the judge's blocking ``trend_connected``
ad-copy gate: the critic's self-reported ``trend_bridge`` check passed copies
the judge then failed for having no trend tie at all.

A copy ties to the trend when (all matching via ``text_match``: whole-word,
accent/case-folded, plural-insensitive):

* it contains one of the trend's **distinctive** tokens — its
  ``content_tokens`` minus ``GENERIC_TREND_TOKENS`` (search-query filler such
  as "vs", "results", "live", "near me"), 4-digit years and tokens shorter than
  3 characters ("Lions" ties to "lions vs cardinals"; "results" alone does not
  tie to "chicago marathon results"); or
* it ``mentions`` the whole trend phrase; or
* it ``mentions`` one of the brief's trend motifs
  (``creative_brief.trend_bridge.motifs``), or contains two adjacent content
  words of one as a phrase ("finish line" ties to "finish line foil
  blanket").

A token or motif that is (or is part of, or contains) one of the brief's avoid
terms never counts: a copy must not tie to a trend through a word it was told
to avoid. An empty trend always passes (nothing to tie to).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from .text_match import (
    MIN_TOKEN_CHARS,
    STOPWORDS,
    contains_phrase,
    content_tokens,
    mentions,
    same_word,
    words,
)

# Search-query filler that never identifies a trend on its own.
GENERIC_TREND_TOKENS = frozenset(
    {
        "vs",
        "versus",
        "results",
        "result",
        "score",
        "scores",
        "live",
        "news",
        "update",
        "updates",
        "highlights",
        "today",
        "tonight",
        "game",
        "games",
        "match",
        "where",
        "watch",
        "stream",
        "near",
        "me",
    }
    | STOPWORDS
)
_YEAR = re.compile(r"^\d{4}$")
MAX_TIE_TOKENS = 3
MAX_TIE_MOTIFS = 2


def _avoided(term: str, avoid: Iterable[str]) -> bool:
    """``term`` is, contains or is part of an avoid term."""
    return any(
        a.strip() and (contains_phrase(term, a) or contains_phrase(a, term))
        for a in avoid
    )


def distinctive_trend_tokens(trend: str, avoid: Sequence[str] = ()) -> list[str]:
    """The trend's identifying tokens (folded, first occurrence order)."""
    out: list[str] = []
    for token in content_tokens(trend):
        if (
            token in GENERIC_TREND_TOKENS
            or _YEAR.match(token)
            or len(token) < MIN_TOKEN_CHARS
            or token in out
            or _avoided(token, avoid)
        ):
            continue
        out.append(token)
    return out


def _motifs(motifs: Iterable[str], avoid: Sequence[str]) -> list[str]:
    cleaned = (m.strip() for m in motifs if isinstance(m, str))
    return [m for m in cleaned if m and not _avoided(m, avoid)]


def has_trend_tie(
    text: str,
    *,
    trend: str,
    motifs: Iterable[str] = (),
    avoid: Sequence[str] = (),
) -> bool:
    """``text`` names the trend, one of its distinctive tokens or a motif."""
    if not words(trend):
        return True
    text_words = words(text)
    for token in distinctive_trend_tokens(trend, avoid):
        if any(same_word(token, w) for w in text_words):
            return True
    if not _avoided(trend, avoid) and mentions(text, trend):
        return True
    return any(_mentions_motif(text, motif) for motif in _motifs(motifs, avoid))


def _mentions_motif(text: str, motif: str) -> bool:
    if mentions(text, motif):
        return True
    tokens = content_tokens(motif)
    return any(
        contains_phrase(text, f"{a} {b}")
        for a, b in zip(tokens, tokens[1:], strict=False)
    )


def tie_terms(
    trend: str, motifs: Iterable[str] = (), *, avoid: Sequence[str] = ()
) -> list[str]:
    """Example tie words for an issue text: up to ``MAX_TIE_TOKENS``
    distinctive trend tokens (Title-cased) plus up to ``MAX_TIE_MOTIFS``
    motifs (as written)."""
    tokens = [t.title() for t in distinctive_trend_tokens(trend, avoid)]
    return tokens[:MAX_TIE_TOKENS] + _motifs(motifs, avoid)[:MAX_TIE_MOTIFS]
