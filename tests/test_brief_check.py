"""Unit tests for the deterministic creative-brief check (creative_agent.brief_check)."""

import copy
import json

import pytest

from creative_agent.brief_check import GENERIC_MOTIFS, check_brief
from tests.test_creative_agent_graph import _BRIEF


def _brief(**changes):
    brief = copy.deepcopy(_BRIEF)
    for dotted, value in changes.items():
        target = brief
        *path, leaf = dotted.split("__")
        for part in path:
            target = target[part]
        target[leaf] = value
    return brief


def test_clean_brief_has_no_issues():
    assert check_brief(_BRIEF) == []
    assert check_brief(json.dumps(_BRIEF)) == []


@pytest.mark.parametrize("bad", [None, "", "not json", "[1, 2]", 42])
def test_missing_or_unparseable(bad):
    assert check_brief(bad) == ["brief missing or unparseable"]


def test_proposition_with_two_sentences():
    (issue,) = check_brief(
        _brief(single_minded_proposition="Skates are fast. Buy them today.")
    )
    assert "single_minded_proposition" in issue and "one sentence" in issue


def test_proposition_with_and():
    (issue,) = check_brief(
        _brief(single_minded_proposition="Rocket Skates are fast and safe.")
    )
    assert "'and'" in issue


def test_proposition_empty():
    (issue,) = check_brief(_brief(single_minded_proposition="  "))
    assert "single_minded_proposition" in issue


def test_proposition_decimal_is_one_sentence():
    assert check_brief(_brief(single_minded_proposition="Go 2.5x faster.")) == []


@pytest.mark.parametrize(
    "insight",
    [
        "Coyotes want to win, yet gadgets fail.",
        "Although coyotes plan, gadgets fail.",
        "Even though they plan, gadgets fail.",
        "WHILE they plan, the bird wins.",
        "They plan, BUT the bird wins.",
    ],
)
def test_insight_tension_markers_accepted(insight):
    assert check_brief(_brief(insight=insight)) == []


def test_insight_without_tension():
    (issue,) = check_brief(_brief(insight="Coyotes love buttery gadgets."))
    assert "insight" in issue and "X, but Y" in issue


def test_rtb_without_source():
    rtbs = [{"claim": "Fast", "source_id": None}, {"claim": "Ok", "source_id": " "}]
    (issue,) = check_brief(_brief(reasons_to_believe=rtbs))
    assert "'Fast'" in issue and "'Ok'" in issue and "src-N" in issue


def test_no_rtbs():
    (issue,) = check_brief(_brief(reasons_to_believe=[]))
    assert "reasons_to_believe" in issue


@pytest.mark.parametrize(
    ("score", "mode", "expected"),
    [
        (5, "cultural", "direct"),
        (3, "direct", "cultural"),
        (2, "cultural", "light_touch"),
    ],
)
def test_fit_mode_inconsistent(score, mode, expected):
    (issue,) = check_brief(
        _brief(trend_bridge__fit_score=score, trend_bridge__fit_mode=mode)
    )
    assert f"'{expected}'" in issue and str(score) in issue


@pytest.mark.parametrize(
    ("score", "mode"),
    [(5, "direct"), (4, "direct"), (3, "cultural"), (1, "light_touch")],
)
def test_fit_mode_consistent(score, mode):
    assert (
        check_brief(_brief(trend_bridge__fit_score=score, trend_bridge__fit_mode=mode))
        == []
    )


def test_duplicate_angle_names():
    angles = [
        {"angle_id": "A1", "name": "Fast", "tension": "t1", "route": "r"},
        {"angle_id": "A2", "name": " fast ", "tension": "t2", "route": "r"},
        {"angle_id": "A3", "name": "Other", "tension": "t3", "route": "r"},
    ]
    (issue,) = check_brief(_brief(angles=angles))
    assert "2 distinct" in issue and "3" in issue


def test_motifs_empty():
    (issue,) = check_brief(_brief(trend_bridge__motifs=[]))
    assert "motifs" in issue


def test_motifs_all_generic():
    motifs = ["a smartphone", "Social feeds", "glowing phone screen", "#hashtag"]
    (issue,) = check_brief(_brief(trend_bridge__motifs=motifs))
    assert "generic" in issue


def test_one_specific_motif_is_enough():
    motifs = ["a phone", "a roadrunner dust cloud"]
    assert check_brief(_brief(trend_bridge__motifs=motifs)) == []


def test_generic_motif_constant_covers_the_basics():
    for term in ("phone", "smartphone", "social feed", "chat bubble", "hashtag"):
        assert term in GENERIC_MOTIFS


def test_distinctive_assets_required_when_brand_colors_given():
    brief = _brief(brand__distinctive_assets=[])
    assert check_brief(brief) == []
    (issue,) = check_brief(brief, brand_colors="ACME red, desert tan")
    assert "distinctive_assets" in issue and "ACME red" in issue


def test_multiple_issues_are_all_reported():
    brief = _brief(
        insight="Coyotes like skates.",
        trend_bridge__fit_score=2,
        trend_bridge__fit_mode="direct",
    )
    assert len(check_brief(brief)) == 2


def test_tolerates_malformed_fields():
    """Robust to a hand-edited / partially missing brief (never raises)."""
    issues = check_brief({"single_minded_proposition": "One idea."})
    assert issues  # flags what's missing, without raising
    assert check_brief({"angles": "x", "trend_bridge": "y", "brand": 3})


def test_identical_angle_tensions_are_flagged():
    angles = [
        {
            "angle_id": "A1",
            "name": "One",
            "tension": "Wants speed, but...",
            "route": "r",
        },
        {"angle_id": "A2", "name": "Two", "tension": " wants SPEED but ", "route": "r"},
        {"angle_id": "A3", "name": "Three", "tension": "Other tension", "route": "r"},
    ]
    (issue,) = check_brief(_brief(angles=angles))
    assert "same tension" in issue


@pytest.mark.parametrize(
    "proposition",
    [
        "Dr. Pepper fans finally get faster skates.",
        "The fastest skates in the U.S. are here.",
        "Built for speed, e.g. desert chases.",
        "Roadrunner vs. coyote ends with ACME Inc. on top.",
    ],
)
def test_abbreviations_are_not_sentence_breaks(proposition):
    assert check_brief(_brief(single_minded_proposition=proposition)) == []


def test_brand_and_product_names_do_not_count_as_and():
    brief = _brief(
        single_minded_proposition="Mac and Cheese makes chases fun.",
        mandatories=["name Mac and Cheese"],
    )
    (issue,) = check_brief(brief)
    assert "'and'" in issue
    assert check_brief(brief, target_product="Mac and Cheese") == []
    assert (
        check_brief(
            _brief(single_minded_proposition="Johnson and Johnson soothes every fall."),
            brand="johnson and johnson",
        )
        == []
    )


def test_rtb_source_id_must_be_src_n_or_brief():
    rtbs = [
        {"claim": "Fast", "source_id": "brief"},
        {"claim": "Trending", "source_id": "source 1"},
    ]
    (issue,) = check_brief(_brief(reasons_to_believe=rtbs))
    assert "invalid source_id" in issue and "'source 1'" in issue


def test_rtb_src_id_must_exist_in_sources_when_given():
    # _BRIEF cites src-1; without sources the id is not cross-checked.
    assert check_brief(_BRIEF) == []
    assert check_brief(_BRIEF, sources={"src-1": {"short_id": "src-1"}}) == []
    (issue,) = check_brief(_BRIEF, sources={"src-2": {}})
    assert "unknown sources" in issue and "'src-1'" in issue


# --- false-positive audit regressions (edge.py / Lay's case) -----------------


def _prop_issues(proposition, **kwargs):
    # Mandatories name the product, so only the proposition rules can fire.
    brief = _brief(
        single_minded_proposition=proposition,
        mandatories=[f"name {kwargs.get('target_product', '')}"],
    )
    return check_brief(brief, **kwargs)


@pytest.mark.parametrize(
    "proposition",
    [
        "Ranked No. 1 in the U.S. for comfort.",
        "Made in the U.S.A. for your commute.",
        "Approx. 20 minutes to a better morning.",
        "Our Jr. size fits small hands.",
        "Plan your trip at 9 a.m. without stress.",
        "Wake up at 6 a.m. ready.",
        "Feel the Eras Tour energy... at home.",
        "Feel the Eras Tour energy… At home.",
    ],
)
def test_no_sentence_break_without_a_following_capital(proposition):
    assert _prop_issues(proposition) == []


@pytest.mark.parametrize(
    "proposition",
    ["Hey! Your coffee is ready.", "Ready? Set. Go.", 'Skates rule. "Buy them."'],
)
def test_real_second_sentences_still_flagged(proposition):
    (issue,) = _prop_issues(proposition)
    assert "one sentence" in issue


@pytest.mark.parametrize(
    ("proposition", "kwargs"),
    [
        ("Rock-and-roll energy in every can.", {}),
        ("Black-and-white photos never looked this good.", {}),
        ("The bread-and-butter cleaner for busy parents.", {}),
        ("R&B nights deserve better sound.", {}),
        # "&" names, written with "and" or "&".
        ("Barnes and Noble makes every chapter epic.", {"brand": "Barnes & Noble"}),
        (
            "Ben and Jerry's makes every scoop a celebration.",
            {"brand": "Ben & Jerry's"},
        ),
        ("Johnson & Johnson keeps babies calm.", {"brand": "Johnson and Johnson"}),
        # An "X and Y" chunk of a longer product / brand / trend name.
        (
            "Lay's Salt and Vinegar delivers an intense kick that matches your adrenaline rush.",
            {"brand": "Lay's", "target_product": "Lay's Salt and Vinegar chips"},
        ),
        (
            "Kraft Mac and Cheese turns any game into a feast.",
            {"brand": "Kraft Heinz", "target_product": "Kraft Mac & Cheese"},
        ),
        (
            "Every Dungeons and Dragons night needs a snack.",
            {"trend": "Dungeons and Dragons movie"},
        ),
        (
            "Pens for the Fast and Furious crowd.",
            {"trend": "Fast and Furious 11 trailer"},
        ),
    ],
)
def test_and_inside_names_and_compounds_is_not_two_ideas(proposition, kwargs):
    assert _prop_issues(proposition, **kwargs) == []


def test_and_still_flagged_outside_names():
    (issue,) = _prop_issues("Fast, light and durable shoes for every run.")
    assert "'and'" in issue
    (issue,) = _prop_issues("Skates & helmets for every chase.")
    assert "'and'" in issue
    (issue,) = _prop_issues(
        "Lay's Salt and Vinegar chips are crunchy and tangy.",
        target_product="Lay's Salt and Vinegar chips",
    )
    assert "'and'" in issue


def test_all_proposition_issues_reported_together():
    issues = _prop_issues("Skates are fast and safe. Buy them.")
    assert len(issues) == 2
    assert "one sentence" in issues[0] and "'and'" in issues[1]


@pytest.mark.parametrize(
    "insight",
    [
        "Despite loving the show, fans can't get tickets.",
        "Fans want in, only to find tickets gone.",
        "Fans want in, though tickets are gone.",
        "Fans want in; however, tickets are gone.",
        "Fans want in; still, tickets are gone.",
        "Fans want in; yet tickets are gone.",
        "Gen Z wants to look effortless, but effortless takes effort.",
        "Parents want screen-free weekends, yet they hand over the tablet.",
        "Runners track every mile, but they never track their sleep.",
        "Fans buy merch instead of tickets they can't afford.",
        "Gen Z wants to look effortless, except effortless takes effort.",
        "Fans want front-row seats, whereas budgets want the nosebleeds.",
    ],
)
def test_more_tension_markers_accepted(insight):
    assert check_brief(_brief(insight=insight)) == []


def test_insight_hyphen_is_not_a_clause_join():
    issues = check_brief(_brief(insight="Coyotes love high-speed gadgets."))
    assert any("no tension" in i for i in issues)


_SOURCES = {f"src-{i}": {"short_id": f"src-{i}"} for i in range(1, 7)}


@pytest.mark.parametrize(
    "source_id",
    [
        "src-3",
        "[src-3]",
        "src_3",
        "SRC-3",
        "src 3",
        "src-03",
        " src-3 ",
        "src-3, src-5",
        "src-3,src-5",
        "Brief",
        "brief ",
        "user brief",
        "[brief]",
    ],
)
def test_source_id_variants_are_normalised(source_id):
    rtbs = [{"claim": "Fast", "source_id": source_id}]
    assert check_brief(_brief(reasons_to_believe=rtbs), sources=_SOURCES) == []


@pytest.mark.parametrize(
    ("source_id", "kind"),
    [
        ("research", "invalid source_id"),
        ("source 3", "invalid source_id"),
        ("src-3, research", "invalid source_id"),
        ("src-9", "unknown sources"),
        ("src-3, src-9", "unknown sources"),
    ],
)
def test_source_id_still_flagged(source_id, kind):
    rtbs = [{"claim": "Fast", "source_id": source_id}]
    (issue,) = check_brief(_brief(reasons_to_believe=rtbs), sources=_SOURCES)
    assert kind in issue


def test_angle_names_compared_normalised():
    angles = [
        {"angle_id": f"A{i}", "name": n, "tension": t, "route": "r"}
        for i, (n, t) in enumerate(
            [("Front Row", "a"), ("Front-Row", "b"), ("front row!", "c")], 1
        )
    ]
    (issue,) = check_brief(_brief(angles=angles))
    assert "1 distinct angle name" in issue


@pytest.mark.parametrize(
    "insight",
    [
        "Fans want to be there; tickets cost a fortune.",
        "Fans crave the stadium experience — tickets are gone in seconds.",
        "Fans crave the stadium experience -- tickets are gone in seconds.",
        "Fans crave the stadium—tickets are gone.",
        "Fans still love the show.",
        "Gamers say they hate ads; they watch every trailer anyway.",
    ],
)
def test_bare_dash_semicolon_or_still_is_not_tension(insight):
    issues = check_brief(_brief(insight=insight))
    assert any("no tension" in i for i in issues)


def test_cjk_angle_names_are_not_emptied():
    angles = [
        {"angle_id": f"A{i}", "name": n, "tension": t, "route": "r"}
        for i, (n, t) in enumerate(
            [("最前列", "a"), ("家で観る", "b"), ("推し活", "c")], 1
        )
    ]
    assert check_brief(_brief(angles=angles)) == []


@pytest.mark.parametrize(
    ("proposition", "kwargs"),
    [
        # A digit after terminal punctuation starts a new sentence.
        ("Skates are fast. 10 minutes is all it takes.", {}),
        # A lowercase-led brand/product word starts a new sentence.
        (
            "Your phone is your instrument. iPhone users play louder.",
            {"brand": "Apple", "target_product": "iPhone 16"},
        ),
        ("They said no. Then they tried it.", {}),
    ],
)
def test_more_sentence_breaks_flagged(proposition, kwargs):
    issues = _prop_issues(proposition, **kwargs)
    assert any("one sentence" in i for i in issues)


@pytest.mark.parametrize(
    "proposition",
    ["The No. 1 skate for coyotes.", "Rated no. 1 by coyotes everywhere."],
)
def test_no_followed_by_a_digit_is_an_abbreviation(proposition):
    assert _prop_issues(proposition) == []


def test_clean_term_strips_wrapping_brackets_and_quotes():
    from creative_agent.brief_check import clean_term

    assert clean_term("[wrestler likenesses]") == "wrestler likenesses"
    assert clean_term(' "coil tap terminology" ') == "coil tap terminology"
    assert clean_term("[[nested]]") == "nested"
    assert clean_term("PRS bird inlays") == "PRS bird inlays"
    assert clean_term("(c) 2026 brand") == "(c) 2026 brand"  # not fully wrapped
    assert clean_term("[]") == ""


def test_normalize_brief_terms_cleans_every_term_list():
    from creative_agent.brief_check import normalize_brief_terms

    brief = {
        "avoid": ["[wrestler likenesses]", "[trademarked belt designs]", "[]"],
        "mandatories": ["[Show the guitar]"],
        "brand": {
            "do_not": ["[Never say coil tap]"],
            "distinctive_assets": ["'bird inlays'"],
        },
        "trend_bridge": {"motifs": ["[folding chair]"], "fit_score": 3},
    }
    out = normalize_brief_terms(brief)
    assert out["avoid"] == ["wrestler likenesses", "trademarked belt designs"]
    assert out["mandatories"] == ["Show the guitar"]
    assert out["brand"]["do_not"] == ["Never say coil tap"]
    assert out["brand"]["distinctive_assets"] == ["bird inlays"]
    assert out["trend_bridge"]["motifs"] == ["folding chair"]
    assert brief["avoid"][0] == "[wrestler likenesses]"  # input not mutated
    assert normalize_brief_terms(None) is None


# --- exact product name in mandatories ---------------------------------------

_PRS = {"brand": "PRS", "target_product": "SE CE24 Electric Guitar"}


def _product_issues(mandatories, **kwargs):
    issues = check_brief(_brief(mandatories=mandatories), **(_PRS | kwargs))
    return [i for i in issues if "exactly as" in i]


def test_brief_must_name_product_exactly_in_mandatories():
    (issue,) = _product_issues(["Feature the SE CE 24 Standard Satin"])
    assert '"SE CE24 Electric Guitar"' in issue
    assert "variant, finish or model" in issue


def test_exact_product_in_mandatories_passes():
    assert _product_issues(["Name the SE CE24 Electric Guitar"]) == []
    assert _product_issues(["show the logo", "name the se ce24 electric guitar"]) == []


def test_brand_and_model_mention_in_mandatories_passes():
    assert _product_issues(["Feature the PRS SE CE24 by name"]) == []


def test_no_mandatories_is_flagged_for_the_product():
    assert len(_product_issues([])) == 1


def test_product_rule_skipped_without_target_product():
    assert _product_issues(["show the logo"], target_product="  ") == []


# --- unsupported absolute claims ----------------------------------------------


def _claim_issues(brief, **kwargs):
    return [i for i in check_brief(brief, **kwargs) if "absolute claims" in i]


def test_brief_flags_an_invented_rtb_claim():
    rtbs = [
        {
            "claim": "Guaranteed to stay in tune through 100 percent humidity",
            "source_id": "src-1",
        },
        {"claim": "Fast, per the brief", "source_id": "brief"},
    ]
    (issue,) = _claim_issues(_brief(reasons_to_believe=rtbs))
    assert "(guaranteed, 100 percent)" in issue
    assert "selling points" in issue


def test_brief_flags_an_absolute_proposition():
    brief = _brief(single_minded_proposition="Rocket Skates are indestructible.")
    (issue,) = _claim_issues(brief)
    assert "indestructible" in issue


def test_brief_claim_allowed_by_selling_points_or_mandatories():
    brief = _brief(single_minded_proposition="Rocket Skates carry a lifetime warranty.")
    assert _claim_issues(brief, key_selling_points="Lifetime warranty") == []
    assert _claim_issues(brief, key_selling_points=["Lifetime warranty"]) == []
    brief["mandatories"] = ["name Rocket Skates", "mention the lifetime warranty"]
    assert _claim_issues(brief) == []


def test_clean_brief_has_no_claim_issues():
    assert _claim_issues(_BRIEF) == []
