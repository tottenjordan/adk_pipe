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


# --- teeth restored (review of the false-positive fixes) ---------------------


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        # A packaging word is only dropped after a size/number token; in a
        # two-word phrase, or as the head noun, it identifies the product.
        ("an iPhone on a desk", "iPhone case"),
        ("a Gibson guitar on a stand", "Gibson guitar case"),
        ("a fanny wearing a hat", "Fanny pack"),
        ("pack your bags", "Fanny pack"),
        # "new" / "our" / "your" / "all" are content, not stopwords.
        ("a balance of light and shade", "New Balance"),
        ("a place setting for two", "Our Place"),
        # One content token: the full phrase is required, not 60% of it.
        ("pixel art style", "Pixel 9"),
        ("a 3D model of a car", "Model 3"),
        # A bare generic container word identifies nothing ("can" is a verb).
        ("you can do it", "can"),
    ],
)
def test_mentions_keeps_its_teeth(text, phrase):
    assert not mentions(text, phrase)


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        ("a Gibson guitar case on stage", "Gibson guitar case"),
        ("a Gibson case on stage", "Gibson guitar case"),
        ("a neon fanny pack", "Fanny pack"),
        ("Google Pixel 9 phone on a desk", "Pixel 9"),
        ("a red Tesla Model 3", "Model 3"),
        ("an Our Place pan", "Our Place"),
        ("New Balance sneakers", "New Balance"),
        ("an ice-cold Coke", "Coke 12-pack 330 ml"),
        ("a Coke and fries", "Coke 12-pack"),
        ("a box of cookies", "cookie"),
        (
            "a Liquid Death Mountain Water tallboy can",
            "Liquid Death Mountain Water 16oz can",
        ),
        ("500ml bottle of Evian water", "Evian water 500ml bottle"),
    ],
)
def test_mentions_still_accepts_paraphrases(text, phrase):
    assert mentions(text, phrase)


def test_content_tokens_drop_packaging_only_after_a_size():
    assert content_tokens("iPhone case") == ["iphone", "case"]
    assert content_tokens("Gibson guitar case") == ["gibson", "guitar", "case"]
    assert content_tokens("Evian water 500ml bottle") == ["evian", "water"]
    assert content_tokens("Coke 12-pack") == ["coke"]
    assert content_tokens("New Balance") == ["new", "balance"]


def test_mentions_brand_anchor():
    phrase = "Starbucks iced coffee"
    assert not mentions("an iced coffee on a desk", phrase, brand="Starbucks")
    assert mentions("a Starbucks iced latte", phrase, brand="Starbucks")
    assert mentions("a Starbucks iced coffee", phrase, brand="Starbucks")
    # Without a brand, 2 of 3 tokens is enough.
    assert mentions("an iced coffee on a desk", phrase)
    # A brand that is not in the phrase anchors nothing.
    assert mentions("a cold brew drink", "Cold Brew Coffee", brand="Starbucks")


def test_mentions_brand_anchor_plural_documented():
    # Documented acceptance: plural stemming makes "van" == "vans", so "an old
    # van" carries the brand token and 2 of 3 tokens of "Vans Old Skool".
    assert mentions("an old van by the beach", "Vans Old Skool", brand="Vans")
    assert not mentions("an old skool vibe", "Vans Old Skool", brand="Vans")


def test_mentions_brand_plus_model_tokens():
    """Distinctive model tokens (a digit, or all caps) + the brand name the
    product even when its generic words ("Electric Guitar") are left out."""
    phrase = "SE CE24 Electric Guitar"
    assert mentions("A PRS SE CE24 on a stage", phrase, brand="PRS")
    # Brand-anchored only: without a brand the ratio decides (2 of 4), so a
    # brand-less motif check never accepts an acronym alone.
    assert not mentions("An SE CE24 on stage", phrase)
    assert not mentions("an NFL stadium at night", "NFL Draft stage")
    # A missing distinctive token falls back to the ratio (1 of 4).
    assert not mentions("A CE24 on a stage", phrase, brand="PRS")
    # A given brand must be named too; else the ratio decides (2 of 4).
    assert not mentions("An SE CE24 on stage", phrase, brand="PRS")
    # Distinctive = in the ORIGINAL phrase: lowercase "se ce" has none.
    assert not mentions("a PRS se ce on stage", "se ce electric guitar", brand="PRS")


def test_distinctive_rule_keeps_packaging_and_size_teeth():
    # A packaging head noun must still be named ("Gibson SG case" is the case).
    assert not mentions("a Gibson SG on stage", "Gibson SG case", brand="Gibson")
    assert mentions("a Gibson SG case on stage", "Gibson SG case", brand="Gibson")
    # Sizes and bare numbers are never distinctive ("12" in "Coke 12-pack").
    assert not mentions("12 apples on a table", "Coke 12-pack")
    assert not mentions("a 3D model of a car", "Model 3")
