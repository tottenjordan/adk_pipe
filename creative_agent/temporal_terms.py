"""Dated and outcome words: a small lexicon that keeps evergreen creatives timeless.

Pure logic (no ADK imports), mirroring ``claims.py``. When a run has a timely
moment (the trend situation validator's ``timely_ok``), at most
``MAX_TIMELY_CONCEPTS`` creatives may reference it; every other (evergreen)
creative must never state an outcome or a date it can't support. The concept
guard and the copy gate flag an evergreen creative whose text contains one of
these terms ("won", "tonight", "Oct 12", "Saturday", …) for a fix round.

Matching (the shared ``lexicon.Lexicon``) is whole-word, case- and
accent-insensitive (``text_match.fold``); spaces and hyphens inside a term are interchangeable ("game day" ==
"game-day"). Explicit dates (month + day either way round, numeric
``10/12``) and weekday names count too. Common idioms ("win-win", "lose
weight", "lost and found", "never miss a beat", "skin in the game", "24/7")
are removed before matching; extend the ``_LEXICON`` idioms as false positives appear.
"""

import re

from .lexicon import Lexicon
from .text_match import fold

TEMPORAL_TERMS = (
    "won",
    "win",
    "wins",
    "victory",
    "lost",
    "loss",
    "lose",
    "defeat",
    "beat",
    "beaten",
    "upset",
    "comeback",
    "score",
    "final score",
    "tonight",
    "tomorrow",
    "yesterday",
    "this weekend",
    "game day",
    "gameday",
    "kickoff",
    "kick-off",
    "rematch",
    "playoffs",
    "championship",
)

_LEXICON = Lexicon(
    TEMPORAL_TERMS,
    idioms=(
        "win win",
        "lose weight",
        "weight loss",
        "lost and found",
        "miss(?:es|ed)? a beat",
        "skin in the game",
        "24/7",
    ),
    boundary=r"\w-",  # "win" never inside "win-win", "post-win" or "win-wins"
    # the hyphen form only ("kick off your day" is fine)
    overrides={"kick-off": r"(?<!\w)kick-off(?!\w)"},
)

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DAY = r"(?:[12]\d|3[01]|0?[1-9])(?:st|nd|rd|th)?"
_DATES = re.compile(
    rf"\b{_MONTH}\.?\s+{_DAY}\b"
    rf"|\b{_DAY}\s+(?:of\s+)?{_MONTH}\b"
    r"|\b(?:1[0-2]|0?[1-9])/(?:[12]\d|3[01]|0?[1-9])(?:/\d{2,4})?\b"
    r"|\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
)


def temporal_terms(text: str) -> list[str]:
    """The dated / outcome terms in ``text`` ([] = timeless).

    Lexicon terms once each in lexicon order, then explicit dates and weekday
    names as written in ``text`` (e.g. "Oct 12", "Saturday").
    """
    if not text:
        return []
    cleaned = _LEXICON.clean(text)  # folded, idioms blanked: fold's length kept
    if len(cleaned) != len(text):  # fold changed lengths: report folded spans
        text = fold(text)
    found = _LEXICON.matches(cleaned)
    for match in _DATES.finditer(cleaned):
        written = text[match.start() : match.end()]
        if written not in found:
            found.append(written)
    return found
