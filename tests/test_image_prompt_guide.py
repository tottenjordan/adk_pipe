"""IMAGE_PROMPT_GUIDE content rules (image diversity fixes 1 + 3)."""

import re

from creative_agent import prompts

G = prompts.IMAGE_PROMPT_GUIDE


def test_no_braces():
    assert "{" not in G and "}" not in G


def test_in_image_text_is_capped():
    assert "at most 2 of the 4 concepts" in G
    assert "6 words" in G
    assert "no small print" in G.lower()


def test_no_fill_in_templates_or_jargon():
    assert not re.search(r"of \[subject\]", G)  # palette is descriptors, not templates
    assert "2:1 iso grid" not in G
    assert "Do NOT copy" in G  # anti-verbatim-opener rule


def test_educational_maps_away_from_diagrams():
    line = next(ln for ln in G.splitlines() if ln.startswith("- Educational"))
    assert "isometric" not in line.lower() and "blueprint" not in line.lower()
    assert "product hero" in line.lower()


def test_tone_mapping_is_a_preference_and_shortlist_aware():
    assert "this is the selection rule, not a suggestion" not in G
    assert "style shortlist" in G.lower()


def test_trend_motif_required():
    assert "trend motif" in G.lower()
