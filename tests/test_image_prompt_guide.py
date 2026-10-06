"""IMAGE_PROMPT_GUIDE content rules (image diversity fixes 1 + 3)."""

import re

from creative_agent import prompts

G = prompts.IMAGE_PROMPT_GUIDE


def test_no_braces():
    assert "{" not in G and "}" not in G


def test_in_image_text_is_capped():
    assert "at most 2 of the 4 concepts" in G
    assert "6 words" not in G
    assert "meme and comic exception" in G.lower()
    assert "do not count toward the 2-concept text cap" in G.lower()
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


def test_trend_motif_field_on_concept_models():
    from creative_agent.schemas import (
        VisualConcept,
        VisualConceptCritique,
        VisualConceptFinal,
    )

    for model in (VisualConcept, VisualConceptCritique, VisualConceptFinal):
        field = model.model_fields["trend_motif"]
        assert field.default == ""  # old session states stay valid


def test_prompts_require_trend_motif_not_a_subtle_reference():
    assert "subtly reference" not in prompts.VISUAL_CONCEPT_DRAFTER_INSTR
    for instr in (
        prompts.VISUAL_CONCEPT_DRAFTER_INSTR,
        prompts.VISUAL_CONCEPT_CRITIC_INSTR,
        prompts.VISUAL_CONCEPT_FINALIZER_INSTR,
    ):
        assert "trend_motif" in instr
