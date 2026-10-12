"""creative_agent/temporal_terms.py: the dated / outcome word lexicon that keeps
evergreen creatives timeless (trend-state awareness PR 3)."""

import pytest

from creative_agent.temporal_terms import TEMPORAL_TERMS, temporal_terms


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Texas won tonight", ["won", "tonight"]),
        ("They LOST yesterday.", ["lost", "yesterday"]),
        ("Big win for the Longhorns", ["win"]),
        ("A historic victory", ["victory"]),
        ("Gear up for game day", ["game day"]),
        ("Gear up for game-day", ["game day"]),
        ("Gameday ready", ["gameday"]),
        ("Before kickoff", ["kickoff"]),
        ("Before the kick-off", ["kick-off"]),
        ("This weekend only", ["this weekend"]),
        ("The final score says it all", ["score", "final score"]),
        ("Playoffs mode", ["playoffs"]),
        ("A championship comeback", ["comeback", "championship"]),
        ("See you tomorrow", ["tomorrow"]),
        ("They beat Oklahoma", ["beat"]),
        ("An upset for the ages", ["upset"]),
        ("A tough loss", ["loss"]),
        ("The rematch", ["rematch"]),
        ("Ready for Oct 12?", ["Oct 12"]),
        ("Ready for October 12th", ["October 12th"]),
        ("On 12 October", ["12 October"]),
        ("Kickoff 10/12", ["kickoff", "10/12"]),
        ("See you Saturday", ["Saturday"]),
    ],
)
def test_flags_dated_and_outcome_terms(text, expected):
    assert temporal_terms(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "A win-win for your morning",
        "Lose weight without the gym",
        "Weight loss made simple",
        "The lost and found of flavour",
        "Never miss a beat",
        "Skin in the game",
        "Open 24/7",
        "Winter is coming",  # not "win"
        "Lossless audio",  # not "loss"
        "Scored by design",  # not "score"
        "Sundays are for slow coffee",  # plural ritual, not a date
        "Shop the drop now",
        "",
    ],
)
def test_idioms_and_evergreen_text_pass(text):
    assert temporal_terms(text) == []


def test_case_and_accent_insensitive():
    assert temporal_terms("VICTÓRY") == ["victory"]


def test_terms_are_reported_once():
    assert temporal_terms("win win win, tonight and tonight") == ["win", "tonight"]


def test_lexicon_covers_the_plan():
    for term in (
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
        "rematch",
        "playoffs",
        "championship",
    ):
        assert term in TEMPORAL_TERMS, term
