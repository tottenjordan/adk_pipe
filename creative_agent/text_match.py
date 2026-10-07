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
  numbers or sizes/units ("16oz", "12-pack", "ml"), and no packaging word
  ("can", "bottle") that is only a size's container ("16oz can", "12-pack",
  "500ml bottle"); in a two-word phrase, or with no size in the phrase, the
  packaging word is the head noun and stays ("iPhone case", "Fanny pack");
* ``mentions`` — the phrase appears in full, OR (two or more content tokens)
  at least ``ceil(MENTION_RATIO * n)`` of its ``n`` content tokens appear,
  including every kept packaging head noun and, when ``brand`` is given and
  named in the phrase, at least one brand token. With a single content token
  the phrase's core (its words minus sizes / dropped packaging) must appear in
  full ("Pixel 9" is not "pixel art"). A phrase made only of packaging words
  ("can") identifies nothing and is never mentioned.
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
        "of",
        "on",
        "or",
        "the",
        "their",
        "this",
        "to",
        "with",
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
# Generic container words: dropped only as a size's container, in a phrase of
# 3+ words ("Liquid Death Mountain Water 16oz can" is identified by its name,
# so a "tallboy can" mentions it); otherwise they are the head noun ("iPhone
# case", "Gibson guitar case") and must be mentioned.
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
    if word.endswith("s") and not word.endswith("ss") and len(word) > 2:
        stems.add(word[:-1])
    return stems


def same_word(a: str, b: str) -> bool:
    """Equal, or singular/plural of each other (trailing s / es, ies ↔ y)."""
    return a == b or bool(_stems(a) & _stems(b))


def is_size_or_number(word: str) -> bool:
    """A pure number, a number+unit ("16oz") or a unit word ("ml")."""
    return word in _UNIT_WORDS or _SIZE.match(word) is not None


def _is_size(word: str, following: str) -> bool:
    """A size token: a unit word, a number glued to a unit, or a pure number
    followed by a unit or packaging word ("330 ml", "12-pack"). A bare number
    elsewhere ("Pixel 9") is part of the name, not a size."""
    if word in _UNIT_WORDS:
        return True
    if word.isdigit() or re.fullmatch(r"\d+(?:[.,]\d+)?", word):
        return following in _UNIT_WORDS or following in PACKAGING_WORDS
    return _SIZE.match(word) is not None


def _dropped(phrase_words: list[str]) -> list[bool]:
    """Per word: is it a size token or a size's packaging container?"""
    nxt = [*phrase_words[1:], ""][: len(phrase_words)]
    sizes = [_is_size(w, f) for w, f in zip(phrase_words, nxt, strict=True)]
    droppable = len(phrase_words) > 2 and any(sizes)
    return [
        size or (droppable and word in PACKAGING_WORDS)
        for word, size in zip(phrase_words, sizes, strict=True)
    ]


def content_tokens(phrase: str) -> list[str]:
    """The words of ``phrase`` that identify it (see the module docstring)."""
    phrase_words = words(phrase)
    return [
        w
        for w, dropped in zip(phrase_words, _dropped(phrase_words), strict=True)
        if not dropped
        and w not in STOPWORDS
        and not is_size_or_number(w)
        and len(w) >= 2
    ]


def _core(phrase: str) -> list[str]:
    """``phrase``'s words minus sizes and size containers (kept in order)."""
    phrase_words = words(phrase)
    return [
        w
        for w, dropped in zip(phrase_words, _dropped(phrase_words), strict=True)
        if not dropped
    ]


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


def mentions(
    text: str, phrase: str, *, brand: str = "", ratio: float = MENTION_RATIO
) -> bool:
    """``text`` mentions ``phrase``: in full, or enough of its content tokens.

    Enough = ``ceil(ratio * n)`` of the phrase's ``n`` (>= 2) content tokens
    (plural-insensitive), including every packaging head noun it kept; "a PRS
    SE guitar" mentions "PRS SE CE24 electric guitar" (3 of 5); a bare
    "jacket" does not mention "Patagonia Nano Puff Jacket" (1 of 4), nor
    "Gibson guitar" "Gibson guitar case" (head noun missing). When ``brand``'s
    tokens appear in the phrase, the hits must include one of them: "an iced
    coffee" does not mention "Starbucks iced coffee" for the brand Starbucks.
    With ONE content token, the phrase's core (sizes and size containers
    dropped) must appear in full: "Coke" mentions "Coke 12-pack", but "pixel
    art" does not mention "Pixel 9". A phrase with no content tokens, or only
    packaging words ("can"), is never mentioned; an empty phrase always is
    (nothing to look for).
    """
    if not words(phrase):
        return True
    tokens = content_tokens(phrase)
    if not tokens or all(t in PACKAGING_WORDS for t in tokens):
        return False
    if contains_phrase(text, phrase):
        return True
    text_words = set(words(text))
    if len(tokens) == 1:
        core = _core(phrase)
        if len(core) == 1:
            return _has_word(core[0], text_words)
        return contains_phrase(text, " ".join(core))
    hit = [t for t in tokens if _has_word(t, text_words)]
    if len(hit) < math.ceil(ratio * len(tokens) - 1e-9):
        return False
    if any(t in PACKAGING_WORDS and t not in hit for t in tokens):
        return False
    brand_tokens = [
        t for t in tokens if any(same_word(t, b) for b in content_tokens(brand))
    ]
    return not brand_tokens or any(t in hit for t in brand_tokens)
