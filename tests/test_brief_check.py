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
    brief = _brief(single_minded_proposition="Mac and Cheese makes chases fun.")
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
