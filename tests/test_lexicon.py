"""creative_agent/lexicon.py: the shared Lexicon engine behind claims, temporal
terms and endorsement (the per-module behaviour is pinned in
test_lexicon_table.py)."""

import pytest

from creative_agent.lexicon import Lexicon


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Risk free", ["risk free"]),
        ("risk-free", ["risk free"]),
        ("RISK - FREE", ["risk free"]),
        ("risk\nfree", ["risk free"]),
        ("riskfree", []),
        ("Rísk free", ["risk free"]),
        ("", []),
    ],
)
def test_whole_word_folded_space_hyphen_interchangeable(text, expected):
    assert Lexicon(("risk free",)).find(text) == expected


def test_terms_once_each_in_term_order():
    lex = Lexicon(("b", "a"))
    assert lex.find("a b a b") == ["b", "a"]


@pytest.mark.parametrize(
    ("boundary", "text", "expected"),
    [
        (r"\w", "post-win", ["win"]),
        (r"\w-", "post-win", []),
        (r"\w", "win%", ["win"]),
        (r"\w%", "win%", []),
        (r"\w", "_win", []),
    ],
)
def test_boundary_class(boundary, text, expected):
    assert Lexicon(("win",), boundary=boundary).find(text) == expected


def test_idioms_blanked_first_and_length_kept():
    lex = Lexicon(("win", "deal"), idioms=("win win",))
    assert lex.find("a win win deal") == ["deal"]
    assert lex.find("a win-win deal") == ["deal"]
    assert lex.find("a win deal") == ["win", "deal"]
    assert lex.clean("A Win Win deal") == "a         deal"


def test_idiom_parts_are_regex_fragments():
    lex = Lexicon(("beat",), idioms=("miss(?:es|ed)? a beat",))
    assert lex.find("never missed a beat") == []
    assert lex.find("a fresh beat") == ["beat"]


def test_override_replaces_the_term_pattern():
    lex = Lexicon(("kick-off",), overrides={"kick-off": r"(?<!\w)kick-off(?!\w)"})
    assert lex.find("Kick-Off party") == ["kick-off"]
    assert lex.find("kick off your day") == []


def test_rewrites_after_idioms():
    lex = Lexicon(
        ("in his words",),
        rewrites=((r"\b(in his) own (words)\b", r"\1 \2"),),
    )
    assert lex.find("In his own words") == ["in his words"]


def test_family_key_is_the_first_member():
    lex = Lexicon(("a", "b", "c"), families=(("a", "b"),))
    assert [lex.family(t) for t in ("a", "b", "c")] == ["a", "a", "c"]
