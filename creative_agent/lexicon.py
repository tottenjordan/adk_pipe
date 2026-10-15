"""One matching engine for the small word lexicons the creative gates share.

Pure logic (no ADK imports). ``claims.py`` (unsupported absolute claims),
``temporal_terms.py`` (dated / outcome words) and ``endorsement.py``
(endorsement wording) each keep their term list and module docstring; the
matching itself lives here:

* text is ``text_match.fold``-ed (case- and accent-insensitive);
* idioms are blanked first, length-preserving, so a lexicon that reports spans
  from the cleaned text (the temporal dates) keeps its offsets;
* optional ``rewrites`` then normalise phrasings ("in his own words" ->
  "in his words"); a rewrite may change lengths;
* every term matches as a whole word, its space/hyphen-separated parts joined
  by ``[\\s-]+`` ("risk free" == "risk-free" == "risk - free"), guarded on both
  sides by the ``boundary`` character class (``\\w`` alone; ``\\w%`` so "100%"
  never sits inside "1000%"; ``\\w-`` so "win" never matches in "post-win");
* ``overrides`` replace a term's pattern with a hand-written regex (e.g. the
  hyphen-only "kick-off");
* ``families`` group terms that stand for each other (``family``).

Idioms are written like terms: space/hyphen-separated parts, each a regex
fragment (``"miss(?:es|ed)? a beat"``), joined by ``[\\s-]+`` and wrapped in
``\\b(?:...)\\b``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from .text_match import fold

_SEP = r"[\s-]+"


def _parts(phrase: str) -> list[str]:
    return re.split(_SEP, phrase)


class Lexicon:
    """A term list matched whole-word over folded, idiom-blanked text."""

    def __init__(
        self,
        terms: Iterable[str],
        *,
        idioms: Iterable[str] = (),
        families: Iterable[Iterable[str]] = (),
        boundary: str = r"\w",
        overrides: Mapping[str, str] | None = None,
        rewrites: Iterable[tuple[str, str]] = (),
    ) -> None:
        self.terms = tuple(terms)
        self.boundary = boundary
        self._overrides = dict(overrides or {})
        self._patterns = {t: self._term_pattern(t) for t in self.terms}
        idiom_bodies = [_SEP.join(_parts(i)) for i in idioms]
        self._idioms = (
            re.compile(r"\b(?:" + "|".join(idiom_bodies) + r")\b")
            if idiom_bodies
            else None
        )
        self._rewrites = tuple((re.compile(p), r) for p, r in rewrites)
        self._family = {t: g[0] for g in map(tuple, families) for t in g}

    def _term_pattern(self, term: str) -> re.Pattern[str]:
        if term in self._overrides:
            return re.compile(self._overrides[term])
        body = _SEP.join(re.escape(p) for p in _parts(term))
        b = self.boundary
        return re.compile(rf"(?<![{b}])" + body + rf"(?![{b}])")

    def clean(self, text: str) -> str:
        """``text`` folded, idioms blanked (length kept), rewrites applied."""
        cleaned = fold(text)
        if self._idioms is not None:
            cleaned = self._idioms.sub(lambda m: " " * len(m.group(0)), cleaned)
        for pattern, replacement in self._rewrites:
            cleaned = pattern.sub(replacement, cleaned)
        return cleaned

    def matches(self, cleaned: str) -> list[str]:
        """The terms in already-``clean``-ed text, once each, in term order."""
        return [t for t, p in self._patterns.items() if p.search(cleaned)]

    def find(self, text: str) -> list[str]:
        """The terms in ``text``, once each, in term order ([] = none)."""
        return self.matches(self.clean(text)) if text else []

    def family(self, term: str) -> str:
        """The family key (its group's first term) of ``term``; itself if none."""
        return self._family.get(term, term)
