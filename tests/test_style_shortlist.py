"""Per-session style shortlist: stratified, distinct, reproducible with a seeded RNG."""

import random

from creative_agent import prompts
from creative_agent.style_shortlist import (
    SHORTLIST_QUOTA,
    STYLE_GROUPS,
    format_shortlist,
    pick_style_shortlist,
)


def test_every_family_appears_in_the_guide():
    for families in STYLE_GROUPS.values():
        for family in families:
            assert family in prompts.IMAGE_PROMPT_GUIDE, family


def test_groups_are_disjoint_and_cover_quota():
    all_families = [f for fs in STYLE_GROUPS.values() for f in fs]
    assert len(all_families) == len(set(all_families))
    for group, n in SHORTLIST_QUOTA.items():
        assert len(STYLE_GROUPS[group]) >= n


def test_shortlist_is_six_distinct_and_stratified():
    picks = pick_style_shortlist(random.Random(7))
    assert len(picks) == sum(SHORTLIST_QUOTA.values()) == 6
    assert len(set(picks)) == 6
    for group, n in SHORTLIST_QUOTA.items():
        assert sum(p in STYLE_GROUPS[group] for p in picks) == n


def test_reproducible_with_seed_and_varies_across_seeds():
    assert pick_style_shortlist(random.Random(3)) == pick_style_shortlist(
        random.Random(3)
    )
    distinct = {
        tuple(sorted(pick_style_shortlist(random.Random(s)))) for s in range(30)
    }
    assert len(distinct) >= 15


def test_format_has_no_braces_and_lists_each_family():
    picks = pick_style_shortlist(random.Random(1))
    text = format_shortlist(picks)
    assert "{" not in text and "}" not in text
    for p in picks:
        assert p in text


def test_exclusion_avoids_recent_styles_and_keeps_stratification():
    recent = frozenset({"Photoreal / editorial", "Comic panel", "Meme aesthetic"})
    for seed in range(20):
        picks = pick_style_shortlist(random.Random(seed), exclude=recent)
        assert not set(picks) & recent
        assert len(set(picks)) == 6
        for group, n in SHORTLIST_QUOTA.items():
            assert sum(p in STYLE_GROUPS[group] for p in picks) == n


def test_exclusion_is_case_insensitive():
    picks = pick_style_shortlist(
        random.Random(0), exclude=frozenset({"photoreal / EDITORIAL "})
    )
    assert "Photoreal / editorial" not in picks


def test_exclusion_tops_up_from_the_group_when_too_few_remain():
    # Excluding every graphic family (and all but one photographic) still
    # yields the 2/3/1 quota: the shortfall is filled from the excluded ones.
    recent = frozenset(STYLE_GROUPS["graphic"]) | frozenset(
        STYLE_GROUPS["photographic"][:2]
    )
    picks = pick_style_shortlist(random.Random(5), exclude=recent)
    assert len(set(picks)) == 6
    for group, n in SHORTLIST_QUOTA.items():
        assert sum(p in STYLE_GROUPS[group] for p in picks) == n
    assert STYLE_GROUPS["photographic"][2] in picks


def test_unknown_exclusions_are_ignored():
    assert pick_style_shortlist(
        random.Random(9), exclude=frozenset({"Bauhaus poster"})
    ) == pick_style_shortlist(random.Random(9))
