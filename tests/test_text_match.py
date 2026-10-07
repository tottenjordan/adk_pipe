"""Shared conservative matching behind concept_guard / copy_gate."""

import pytest

from creative_agent.text_match import (
    compact,
    contains_phrase,
    content_tokens,
    mentions,
    same_word,
    words,
)


def test_words_fold_accents_possessives_and_hyphens():
    assert words("L’Oréal's friendship-bracelet") == [
        "l",
        "oreal",
        "friendship",
        "bracelet",
    ]
    assert words("Nestlé") == words("Nestle")
    assert words("Пейте Байкал!") == ["пеите", "баикал"]


def test_compact_strips_punctuation():
    assert compact("AT&T") == "att"
    assert compact("M&M's") == "mm"
    assert compact("7 Up") == compact("7UP")


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("cookie", "cookies"),
        ("candy", "candies"),
        ("lottery", "lotteries"),
        ("bracelet", "bracelets"),
        ("glass", "glasses"),
        ("boot", "boots"),
    ],
)
def test_same_word_plurals(a, b):
    assert same_word(a, b) and same_word(b, a)


def test_same_word_rejects_unrelated():
    assert not same_word("skates", "skating")
    assert not same_word("glass", "glas")


def test_content_tokens_drop_sizes_units_and_packaging():
    assert content_tokens("Liquid Death Mountain Water 16oz can") == [
        "liquid",
        "death",
        "mountain",
        "water",
    ]
    assert content_tokens("Coke 12-pack 330 ml") == ["coke"]
    assert content_tokens("PRS SE CE24 electric guitar") == [
        "prs",
        "se",
        "ce24",
        "electric",
        "guitar",
    ]
    # Packaging is kept when nothing else identifies the phrase.
    assert content_tokens("the can") == ["can"]


def test_contains_phrase_unspaced_scripts_and_boundaries():
    assert contains_phrase("ポカリスエットを飲もう", "ポカリスエット")
    assert contains_phrase("Glow with L'Oreal.", "L'Oréal")
    assert not contains_phrase("Winter is here.", "win")


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        (
            "friendship-bracelet stack under Eras Tour lights",
            "Eras Tour friendship bracelets",
        ),
        ("a PRS SE guitar on a stand", "PRS SE CE24 electric guitar"),
        (
            "a Liquid Death Mountain Water tallboy can",
            "Liquid Death Mountain Water 16oz can",
        ),
        ("an Oreo Double Stuf cookie, twisted open", "Oreo Double Stuf cookies"),
    ],
)
def test_mentions_realistic_paraphrases(text, phrase):
    assert mentions(text, phrase)


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        ("The hiker wears a sleek insulated jacket.", "Patagonia Nano Puff Jacket"),
        ("The hiker wears a sleek insulated jacket.", "Nano Puff jacket"),
        # Documented: the bracelets without the Eras Tour context are 2 of 4.
        ("a friendship-bracelet stack", "Eras Tour friendship bracelets"),
    ],
)
def test_mentions_too_little_overlap(text, phrase):
    assert not mentions(text, phrase)


def test_mentions_edge_cases():
    assert mentions("anything", "")
    assert mentions("a 3M tape roll", "3M")
    assert not mentions("a tape roll", "The One")
