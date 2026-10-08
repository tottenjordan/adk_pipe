"""Unit tests for the unsupported absolute-claim lexicon (creative_agent.claims)."""

import pytest

from creative_agent.claims import (
    ABSOLUTE_CLAIM_TERMS,
    claims_allowed_text,
    unsupported_claims,
)


def test_guaranteed_is_flagged():
    assert unsupported_claims("Guaranteed to stay in tune", allowed_text="") == [
        "guaranteed"
    ]


def test_allowed_text_permits_a_term():
    assert unsupported_claims("Backed for a lifetime", allowed_text="") == ["lifetime"]
    assert (
        unsupported_claims("Backed for a lifetime", allowed_text="lifetime warranty")
        == []
    )


def test_allowed_text_permits_the_term_family():
    # "Satisfaction guaranteed" in the selling points allows "our guarantee".
    assert (
        unsupported_claims(
            "Our guarantee: love it", allowed_text="Satisfaction guaranteed"
        )
        == []
    )
    assert unsupported_claims("100 percent cotton", allowed_text="100% cotton") == []


def test_100_percent_cotton_allowed_only_when_in_selling_points():
    assert unsupported_claims("100% cotton tees", allowed_text="") == ["100%"]
    assert unsupported_claims("100% cotton tees", allowed_text="100% cotton") == []


@pytest.mark.parametrize(
    "text",
    [
        "Never miss a moment of the game.",
        "Tested over 100 shows.",
        "A guitar for 100 nights on the road.",
        "The tour of a lifetime starts here.",
        "A once-in-a-lifetime riff.",
        "Guarantor of good vibes",
        "",
    ],
)
def test_ordinary_language_is_not_flagged(text):
    assert unsupported_claims(text) == []


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("Truly INDESTRUCTIBLE.", "indestructible"),
        ("Basically unbreakable", "unbreakable"),
        ("A bulletproof finish", "bulletproof"),
        ("Order risk-free today", "risk-free"),
        ("Order risk free today", "risk-free"),
        ("It never fails.", "never fails"),
        ("It never goes out of tune", "never goes out of tune"),
        ("100 percent humidity proof", "100 percent"),
        ("Guarantees every note", "guarantees"),
        ("A lifetime of riffs", "lifetime"),
    ],
)
def test_each_term_is_flagged(text, term):
    assert unsupported_claims(text) == [term]


def test_every_term_reported_once_in_lexicon_order():
    text = "Indestructible, guaranteed, indestructible and 100% loud."
    assert unsupported_claims(text) == ["guaranteed", "indestructible", "100%"]


def test_lexicon_has_the_planned_terms():
    for term in ("guarantee", "indestructible", "risk-free", "100%", "lifetime"):
        assert term in ABSOLUTE_CLAIM_TERMS


def test_claims_allowed_text_joins_strings_and_lists():
    assert claims_allowed_text("100% cotton", ["lifetime warranty", 3, None]) == (
        "100% cotton\nlifetime warranty"
    )
    assert claims_allowed_text(None, "") == ""
