"""Tests for creative_agent.trend_tie (does an ad copy tie to the trend?)."""

from creative_agent.trend_tie import GENERIC_TREND_TOKENS, has_trend_tie, tie_terms


def test_named_token_counts():
    assert has_trend_tie("Lions fans, rejoice", trend="lions vs cardinals", motifs=[])


def test_generic_tokens_alone_dont():
    assert not has_trend_tie(
        "Live results and today's score", trend="chicago marathon results", motifs=[]
    )


def test_one_distinctive_token_is_enough():
    assert has_trend_tie(
        "Your marathon, your results", trend="chicago marathon results", motifs=[]
    )


def test_full_phrase():
    assert has_trend_tie(
        "Chicago Marathon finishers", trend="chicago marathon results", motifs=[]
    )


def test_motif_counts():
    assert has_trend_tie(
        "Earned at the finish line",
        trend="chicago marathon results",
        motifs=["finish line foil blanket"],
    )


def test_no_tie():
    assert not has_trend_tie(
        "Skincare for guys who hate skincare",
        trend="lions vs cardinals",
        motifs=["stadium lights"],
    )


def test_avoid_terms_never_count():
    assert not has_trend_tie(
        "Shooting stars", trend="shooting", motifs=[], avoid=["shooting"]
    )


def test_avoided_motif_never_counts():
    assert not has_trend_tie(
        "Under the stadium lights",
        trend="lions vs cardinals",
        motifs=["stadium lights"],
        avoid=["stadium lights"],
    )


def test_avoid_phrase_containing_the_token_blocks_it():
    assert not has_trend_tie(
        "Shooting for the stars", trend="shooting", motifs=[], avoid=["school shooting"]
    )


def test_empty_trend_passes():
    assert has_trend_tie("anything", trend="", motifs=[])
    assert has_trend_tie("anything", trend="   ", motifs=["stadium lights"])


def test_plural_and_singular_match():
    assert has_trend_tie("Be the Lion today", trend="lions vs cardinals", motifs=[])
    assert has_trend_tie(
        "For every cardinal fan", trend="lions vs cardinals", motifs=[]
    )


def test_accents_fold():
    assert has_trend_tie("Beyonce fans, assemble", trend="Beyoncé tour", motifs=[])


def test_year_tokens_are_generic():
    assert not has_trend_tie(
        "Ready for 2026?", trend="marathon 2026 results", motifs=[]
    )
    assert not has_trend_tie("Big plans for 2026", trend="nfl draft 2026", motifs=[])


def test_short_tokens_dont_count_alone():
    # "nfl" is 3 chars and counts; a 2-char token like "uk" does not.
    assert has_trend_tie("NFL Sundays", trend="nfl draft", motifs=[])
    assert not has_trend_tie("Made in the UK", trend="uk election results", motifs=[])


def test_whole_word_only():
    assert not has_trend_tie("Lionsgate movies", trend="lions vs cardinals", motifs=[])


def test_generic_list_has_the_spec_tokens():
    for token in ("vs", "versus", "results", "score", "live", "today", "watch"):
        assert token in GENERIC_TREND_TOKENS


def test_tie_terms_distinctive_title_cased_plus_motifs():
    assert tie_terms("chicago marathon results", ["finish line foil blanket"]) == [
        "Chicago",
        "Marathon",
        "finish line foil blanket",
    ]


def test_tie_terms_caps_tokens_and_motifs():
    terms = tie_terms(
        "taylor swift eras tour tickets",
        ["friendship bracelets", "sequins", "stadium lights"],
    )
    assert terms == ["Taylor", "Swift", "Eras", "friendship bracelets", "sequins"]


def test_tie_terms_skips_generic_and_empty():
    assert tie_terms("lions vs cardinals score", []) == ["Lions", "Cardinals"]
    assert tie_terms("", []) == []
    assert tie_terms("", ["  ", "stadium lights"]) == ["stadium lights"]


def test_tie_terms_skips_avoid_terms():
    assert tie_terms("shooting stars", [], avoid=["shooting"]) == ["Stars"]


def test_motif_needs_more_than_one_loose_word():
    assert not has_trend_tie(
        "Lights out, skin on",
        trend="lions vs cardinals",
        motifs=["stadium lights crowd"],
    )
