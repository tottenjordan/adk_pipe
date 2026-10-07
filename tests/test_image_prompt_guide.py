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


def test_trend_motif_must_be_specific_not_generic():
    assert "SPECIFIC to this trend" in G
    assert "do NOT count" in G and "chat bubbles" in G
    assert "never a likeness" in G


def test_background_texture_text_is_blank():
    assert "never readable words and never gibberish" in G
    assert "never readable print" in G


def test_motif_specificity_enforced_downstream():
    assert "trend-SPECIFIC `trend_motif`" in prompts.VISUAL_CONCEPT_DRAFTER_INSTR
    assert "SPECIFIC and recognisable" in prompts.VISUAL_CONCEPT_CRITIC_INSTR
    assert "Trend Connection (MUST)" in prompts.VISUAL_CONCEPT_FINALIZER_INSTR
    assert "SPECIFIC to the trend" in prompts.ART_DIRECTOR_INSTR


def _section(tag: str) -> str:
    start = G.index(f"<{tag}>")
    end = G.index(f"</{tag}>")
    return G[start:end]


def test_reference_images_section_follows_google_formula():
    sec = _section("REFERENCE_IMAGES")
    assert "numbered reference" in sec
    assert "relationship instruction" in sec.lower()
    assert "new scenario" in sec.lower()


def test_reference_images_roles_are_spelled_out():
    sec = _section("REFERENCE_IMAGES").lower()
    assert "reproduce the product exactly" in sec
    assert "shape, colour, label" in sec
    assert "small, legible and undistorted" in sec
    assert "palette, texture and lighting only" in sec


def test_reference_images_ignore_their_text():
    sec = _section("REFERENCE_IMAGES")
    assert (
        "ignore any text, captions or watermarks that appear in the reference images"
        in sec
    )


def test_building_blocks_follow_subject_action_location_composition_style():
    sec = _section("BUILDING_BLOCKS")
    assert "Subject + Action + Location/context + Composition + Style" in sec
    # Style is still CHOSEN first (style-first principle), even when the
    # sentence order follows Google's formula.
    assert "choose the style first" in sec.lower()
    order = [
        sec.index(f"- {name}")
        for name in ("Subject", "Action", "Location/context", "Composition", "Style")
    ]
    assert order == sorted(order)


def test_in_image_text_describes_typography():
    assert "describe the typography (weight, case, placement)" in G


def test_no_unrequested_logos():
    lower = G.lower()
    assert (
        "never show logos, wordmarks or trademarks of any brand other than the campaign brand"
        in lower
    )
    assert "generic products stay unbranded" in lower
