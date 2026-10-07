"""Shared, conservative text matching for the deterministic creative checks.

Pure logic (no ADK imports), used by ``concept_guard`` (does an image prompt
mention the trend motif / product?) and ``copy_gate`` (does a copy name the
product / contain an avoided term?). Every false positive in those checks costs
an LLM call or a user-visible warning, so matching is lenient about surface
form while staying whole-word:

* ``fold`` — Unicode NFKD, combining marks dropped, case-folded, curly
  apostrophes straightened ("L'Oréal" == "L'Oreal", "Nestlé" == "Nestle");
* ``words`` — ``fold`` + possessive 's dropped + split on every non-word
  character (hyphens and apostrophes separate: "friendship-bracelet" →
  ["friendship", "bracelet"]); Unicode-aware (Cyrillic, Greek, CJK runs);
* ``same_word`` — equal, or singular/plural of each other (s / es / ies→y);
* ``content_tokens`` — the words that identify a phrase: no stopwords, pure
  numbers, sizes/units ("16oz", "12-pack", "ml") and, when anything else is
  left, no generic packaging words ("can", "bottle");
* ``mentions`` — the phrase appears in full, OR at least
  ``ceil(MENTION_RATIO * n)`` (min 1) of its ``n`` content tokens appear.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable

# Share of a phrase's content tokens a text must contain to "mention" it.
MENTION_RATIO = 0.6

STOPWORDS = frozenset(
    {
        "a",
        "all",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "its",
        "new",
        "of",
        "on",
        "or",
        "our",
        "the",
        "their",
        "this",
        "to",
        "with",
        "your",
    }
)

# Standalone unit / size words ("16 oz", "12-pack" → "12", "pack").
_UNIT_WORDS = frozenset(
    {
        "cl",
        "count",
        "ct",
        "fl",
        "floz",
        "gb",
        "inch",
        "inches",
        "kg",
        "lb",
        "lbs",
        "liter",
        "litre",
        "mg",
        "ml",
        "ounce",
        "ounces",
        "oz",
        "pk",
        "tb",
    }
)
# A number glued to a unit ("16oz", "500ml", "2l", "1tb"). "m" is deliberately
# absent so a brand like "3M" survives; alphanumeric model codes ("CE24",
# "3D") are not number-led units and are kept.
_SIZE = re.compile(
    r"^\d+(?:[.,]\d+)?(?:cl|ct|fl|floz|gb|in|inch|kg|l|lb|lbs|mg|ml|oz|pk|tb)?$"
)
# Generic container words: dropped only when the phrase has other content
# tokens ("Liquid Death Mountain Water 16oz can" is identified by its name, so
# a "tallboy can" or a bare "Liquid Death Mountain Water" both mention it).
PACKAGING_WORDS = frozenset(
    {
        "bag",
        "box",
        "bottle",
        "can",
        "carton",
        "case",
        "jar",
        "pack",
        "packet",
        "pouch",
        "sachet",
        "tin",
        "tub",
        "tube",
    }
)

_POSSESSIVE = re.compile(r"'s\b")
_WORD = re.compile(r"[^\W_]+")
# Scripts written without spaces between words: whole-word matching cannot
# work there, so a phrase in one of them is matched as a plain substring.
_UNSPACED_SCRIPT = re.compile(
    "[\u0e00-\u0e7f"  # Thai
    "\u3040-\u30ff"  # Hiragana, Katakana
    "\u3400-\u4dbf\u4e00-\u9fff"  # CJK ideographs
    "\uac00-\ud7af"  # Hangul syllables
    "\uff66-\uff9f]"  # half-width Katakana
)


def fold(text: str) -> str:
    """NFKD, combining marks dropped, case-folded, curly apostrophes straightened."""
    decomposed = unicodedata.normalize("NFKD", text.replace("\u2019", "'"))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.casefold()


def words(text: str) -> list[str]:
    """``fold``-ed words of ``text``, possessive 's dropped, split on non-word chars."""
    return _WORD.findall(_POSSESSIVE.sub("", fold(text)))


def compact(text: str) -> str:
    """``text`` folded with every non-word character removed ("AT&T" → "att")."""
    return "".join(words(text))


def _stems(word: str) -> set[str]:
    stems = {word}
    if word.endswith("ies") and len(word) > 4:
        stems.add(word[:-3] + "y")
    if word.endswith("es") and len(word) > 4:
        stems.add(word[:-2])
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        stems.add(word[:-1])
    return stems


def same_word(a: str, b: str) -> bool:
    """Equal, or singular/plural of each other (trailing s / es, ies ↔ y)."""
    return a == b or bool(_stems(a) & _stems(b))


def is_size_or_number(word: str) -> bool:
    """A pure number, a number+unit ("16oz") or a unit word ("ml")."""
    return word in _UNIT_WORDS or _SIZE.match(word) is not None


def content_tokens(phrase: str) -> list[str]:
    """The words of ``phrase`` that identify it (see the module docstring)."""
    tokens = [
        w
        for w in words(phrase)
        if w not in STOPWORDS and not is_size_or_number(w) and len(w) >= 2
    ]
    specific = [w for w in tokens if w not in PACKAGING_WORDS]
    return specific or tokens


def contains_phrase(text: str, phrase: str) -> bool:
    """``phrase`` occurs in ``text`` as whole words (case/accent-insensitive).

    Phrases in a script written without spaces (Japanese, Chinese, Thai,
    Korean) match as a plain substring of the folded text instead.
    """
    phrase_words = words(phrase)
    if not phrase_words:
        return False
    if _UNSPACED_SCRIPT.search(phrase):
        return "".join(phrase_words) in "".join(words(text))
    pattern = r"(?<![^\W_])" + r"[\W_]+".join(map(re.escape, phrase_words))
    folded = _POSSESSIVE.sub("", fold(text))
    return re.search(pattern + r"(?![^\W_])", folded) is not None


def _has_word(word: str, text_words: Iterable[str]) -> bool:
    return any(same_word(word, other) for other in text_words)


def mentions(text: str, phrase: str, *, ratio: float = MENTION_RATIO) -> bool:
    """``text`` mentions ``phrase``: in full, or enough of its content tokens.

    Enough = ``max(1, ceil(ratio * n))`` of the phrase's ``n`` content tokens
    (plural-insensitive). "a PRS SE guitar" mentions "PRS SE CE24 electric
    guitar" (3 of 5); a bare "jacket" does not mention "Patagonia Nano Puff
    Jacket" (1 of 4). A phrase with no content tokens is mentioned only in
    full; an empty phrase is always mentioned (nothing to look for).
    """
    if not words(phrase):
        return True
    if contains_phrase(text, phrase):
        return True
    tokens = content_tokens(phrase)
    if not tokens:
        return False
    text_words = set(words(text))
    hits = sum(1 for token in tokens if _has_word(token, text_words))
    return hits >= max(1, math.ceil(ratio * len(tokens) - 1e-9))
