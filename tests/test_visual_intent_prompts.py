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

    def test_style_preference_token_in_concept_agents(self):
        # Seed-with-diversity: bias the concept agents, not the art_director brief.
        # The critic/finalizer see it too so their diversity rule doesn't undo it.
        assert "{visual_style_preference?}" in prompts.VISUAL_CONCEPT_DRAFTER_INSTR
        assert "{visual_style_preference?}" in prompts.VISUAL_CONCEPT_CRITIC_INSTR
        assert "{visual_style_preference?}" in prompts.VISUAL_CONCEPT_FINALIZER_INSTR
        assert "{visual_style_preference?}" not in prompts.ART_DIRECTOR_INSTR

    def test_avoid_token_in_art_director_drafter_and_critic(self):
        assert "{visual_avoid?}" in prompts.ART_DIRECTOR_INSTR
        assert "{visual_avoid?}" in prompts.VISUAL_CONCEPT_DRAFTER_INSTR
        assert "{visual_avoid?}" in prompts.VISUAL_CONCEPT_CRITIC_INSTR

    def test_aspect_ratio_override_token_in_drafter_and_critic(self):
        assert "{visual_aspect_ratio?}" in prompts.VISUAL_CONCEPT_DRAFTER_INSTR
        assert "{visual_aspect_ratio?}" in prompts.VISUAL_CONCEPT_CRITIC_INSTR

    def test_reference_role_token_in_drafter(self):
        assert "{reference_image_role?}" in prompts.VISUAL_CONCEPT_DRAFTER_INSTR

    def test_checkpoint_feedback_tokens(self):
        # Interactive checkpoint feedback (memorized by the interactive root);
        # harmlessly empty in creative_agent.
        assert "{research_feedback?}" in prompts.AD_COPY_DRAFTER_INSTR
        assert "{research_feedback?}" in prompts.ART_DIRECTOR_INSTR
        assert "{ad_copy_feedback?}" in prompts.ART_DIRECTOR_INSTR
        assert "{ad_copy_feedback?}" in prompts.VISUAL_CONCEPT_DRAFTER_INSTR


class TestImageDiversityRules:
    def test_shortlist_token_in_concept_agents(self):
        for instr in (
            prompts.VISUAL_CONCEPT_DRAFTER_INSTR,
            prompts.VISUAL_CONCEPT_CRITIC_INSTR,
            prompts.VISUAL_CONCEPT_FINALIZER_INSTR,
        ):
            assert "{style_shortlist?}" in instr

    def test_composition_rule(self):
        for instr in (
            prompts.VISUAL_CONCEPT_DRAFTER_INSTR,
            prompts.VISUAL_CONCEPT_FINALIZER_INSTR,
        ):
            assert "at most ONE centred" in instr
            assert "camera distance" in instr

    def test_finalizer_enforces_diversity(self):
        section = prompts.VISUAL_CONCEPT_FINALIZER_INSTR.split(
            "Style & Composition Diversity"
        )[1]
        assert "MUST" in section[:400]

    def test_text_cap_checked_by_critic_and_finalizer(self):
        for instr in (
            prompts.VISUAL_CONCEPT_CRITIC_INSTR,
            prompts.VISUAL_CONCEPT_FINALIZER_INSTR,
        ):
            assert "at most 2" in instr
