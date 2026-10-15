"""The endorsement lexicon (safer angle for sensitive trends): wording that
implies the real person endorses the product or speaks for them."""

from __future__ import annotations

import pytest

from creative_agent.endorsement import ENDORSEMENT_TERMS, endorsement_terms


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The official Tupac clavinet", ["official"]),
        ("Officially approved", ["officially"]),
        ("Endorsed by legends", ["endorsed"]),
        ("He endorses it", ["endorses"]),
        ("We endorse dreams", ["endorse"]),
        ("A celebrity endorsement", ["endorsement"]),
        ("Sponsored content", ["sponsored"]),
        ("Our sponsor", ["sponsor"]),
        ("A proud sponsorship", ["sponsorship"]),
        ("We partnered with the family", ["partnered"]),
        ("A partnership for music", ["partnership"]),
        ("Your official partner", ["official", "partner"]),
        ("Approved by the estate", ["approved by", "estate"]),
        ("Licensed merch", ["licensed"]),
        ("Authorised edition", ["authorised"]),
        ("Authorized edition", ["authorized"]),
        ("The Signature Model keys", ["signature model"]),
        ("A signature-edition run", ["signature edition"]),
        ("As used by the greats", ["as used by"]),
        ("His favourite keys", ["his favourite"]),
        ("Her favorite sound", ["her favorite"]),
        ("Their favorite riff", ["their favorite"]),
        ("Rest in peace, legend", ["rest in peace"]),
        ("RIP to a legend", ["rip"]),
        ("R.I.P. legend", ["rip"]),
        ("Just what he would have wanted", ["would have wanted"]),
        ("In his words: play on", ["in his words"]),
        ("In her own words", ["in her words"]),
        ("in their words", ["in their words"]),
        # Fold- and accent-insensitive, hyphen/space interchangeable.
        ("ENDORSÉD by fans", ["endorsed"]),
        ("approved-by nobody", ["approved by"]),
        ("Co-sponsored by", ["sponsored"]),
        # Several terms: once each, in lexicon order.
        ("Licensed, official, official", ["official", "licensed"]),
    ],
)
def test_lexicon_hits(text, expected):
    assert endorsement_terms(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Chase your music dreams",
        "Play like a legend",
        # Whole words only.
        "Unofficial jam night",
        "Unlicensed fun",
        "Estates of mind",
        "Our partnerships matter",  # plural not in the lexicon
        # Pinned idioms (no endorsement sense, cheap to exclude).
        "Partner up with your bandmate",
        "Partners up for the gig",
        "Your partner in crime on stage",
        "Real estate of the stage",
        "Let it rip on the keys",  # lower-case "rip" is a verb, never RIP
        "Rip into a solo",
        "Approved for the stage",  # "approved" alone is not "approved by"
        "A signature sound",  # "signature" alone is not a signature model
        "Your favourite keys",  # the reader's favourite, not the person's
    ],
)
def test_lexicon_non_hits(text):
    assert endorsement_terms(text) == []


def test_bare_partner_still_counts():
    # Erring on the safe side: only the listed idioms are excluded.
    assert endorsement_terms("Your partner on stage") == ["partner"]
    assert endorsement_terms("Partner with the keys") == ["partner"]


def test_all_caps_rip_counts_but_folded_rip_does_not():
    # RIP is matched case-sensitively (an all-caps headline "LET IT RIP" is
    # flagged: a rare, safe-side false positive the reviser can fix).
    assert endorsement_terms("LET IT RIP") == ["rip"]
    assert endorsement_terms("let it rip") == []


def test_lexicon_is_the_documented_list():
    assert "official" in ENDORSEMENT_TERMS
    assert "rest in peace" in ENDORSEMENT_TERMS
    assert len(ENDORSEMENT_TERMS) == len(set(ENDORSEMENT_TERMS))
