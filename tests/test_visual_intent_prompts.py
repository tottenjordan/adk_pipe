"""Prompt-token wiring for optional user visual-intent (image-intent-capture).

These assert that the optional `{key?}` state tokens are present in the right
agent instructions so ADK will interpolate the user's seeded intent, and guard
the IMAGE_PROMPT_GUIDE no-braces invariant (it is string-concatenated into the
drafter/critic instructions, so a stray `{` would be read as a state token).
"""

from __future__ import annotations

from creative_agent import prompts


class TestImageGuideBraceSafety:
    def test_guide_contains_no_curly_braces(self):
        # The guide is spliced into instructions verbatim; any `{` would be
        # misread by ADK as a session-state token. Fill-in slots use [brackets].
        assert "{" not in prompts.IMAGE_PROMPT_GUIDE
        assert "}" not in prompts.IMAGE_PROMPT_GUIDE


class TestVisualIntentToken:
    def test_visual_intent_token_in_art_director(self):
        assert "{visual_intent?}" in prompts.ART_DIRECTOR_INSTR

    def test_visual_intent_token_in_drafter(self):
        assert "{visual_intent?}" in prompts.VISUAL_CONCEPT_DRAFTER_INSTR


class TestTier2IntentTokens:
    def test_brand_colors_token_in_both(self):
        assert "{brand_colors?}" in prompts.ART_DIRECTOR_INSTR
        assert "{brand_colors?}" in prompts.VISUAL_CONCEPT_DRAFTER_INSTR

    def test_style_preference_token_in_drafter_only(self):
        # Seed-with-diversity: bias the drafter, not the art_director brief.
        assert "{visual_style_preference?}" in prompts.VISUAL_CONCEPT_DRAFTER_INSTR

    def test_avoid_token_in_art_director_only(self):
        # visual_avoid is a campaign-wide exclusion → art_director sets it once.
        assert "{visual_avoid?}" in prompts.ART_DIRECTOR_INSTR
