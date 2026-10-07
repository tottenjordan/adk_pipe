"""Prompt-token wiring for optional user visual-intent (image-intent-capture).

These assert that the optional `{key?}` state tokens are present in the right
agent instructions so ADK will interpolate the user's seeded intent, and guard
the IMAGE_PROMPT_GUIDE no-braces invariant (it is string-concatenated into the
drafter/critic instructions, so a stray `{` would be read as a state token).
Also pins the campaign-context tokens (brand/audience/product) each creative
agent needs.
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

    def test_legacy_reference_role_block_dropped_from_drafter(self):
        # Already folded into {reference_roles?}; the state key stays for the
        # image tool only.
        assert "{reference_image_role?}" not in prompts.VISUAL_CONCEPT_DRAFTER_INSTR
        assert "<reference_image_role>" not in prompts.VISUAL_CONCEPT_DRAFTER_INSTR

    def test_reference_roles_token_in_drafter(self):
        # Multiple references: the ordered role list derived at state init.
        assert "{reference_roles?}" in prompts.VISUAL_CONCEPT_DRAFTER_INSTR

    def test_reference_roles_token_in_critic_and_finalizer(self):
        for instr in (
            prompts.VISUAL_CONCEPT_CRITIC_INSTR,
            prompts.VISUAL_CONCEPT_FINALIZER_INSTR,
        ):
            assert "{reference_roles?}" in instr

    def test_drafter_reference_block_is_role_only(self):
        instr = prompts.VISUAL_CONCEPT_DRAFTER_INSTR
        start = instr.index("<reference_images>")
        block = instr[start : instr.index("</reference_images>")]
        assert "numbered" not in block
        assert "does not override" in block.lower()

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


class TestCampaignContextTokens:
    """Brand/audience/product context reaches every agent that needs it
    (required `{key}` tokens: the state init always setdefaults them)."""

    def test_ad_copy_drafter_sees_brand_and_audience(self):
        assert "{brand}" in prompts.AD_COPY_DRAFTER_INSTR
        assert "{target_audience}" in prompts.AD_COPY_DRAFTER_INSTR

    def test_ad_copy_critic_sees_brand(self):
        assert "{brand}" in prompts.AD_COPY_CRITIC_INSTR

    def test_report_composer_sees_campaign_fields(self):
        for token in ("{brand}", "{target_product}", "{target_audience}"):
            assert token in prompts.COMBINED_REPORT_COMPOSER_INSTR

    def test_visual_critic_sees_brand_audience_and_paired_copy(self):
        instr = prompts.VISUAL_CONCEPT_CRITIC_INSTR
        assert "{brand}" in instr
        assert "{target_audience}" in instr
        assert "{ad_copy_critique?}" in instr
        assert "paired" in instr.lower()

    def test_campaign_planner_researches_the_brand(self):
        from creative_agent.sub_agents.campaign_researcher.agent import (
            campaign_web_planner,
        )

        instr = str(campaign_web_planner.instruction)
        assert "{brand}" in instr
        assert "1–2" in instr
        for phrase in ("voice", "recent campaigns", "distinctive brand assets"):
            assert phrase in instr.lower()

    def test_campaign_synthesizer_has_brand_section(self):
        from creative_agent.sub_agents.campaign_researcher.agent import (
            campaign_web_synthesizer,
        )

        assert "Brand Voice & Distinctive Assets" in str(
            campaign_web_synthesizer.instruction
        )


class TestRiskSections:
    """Risk guidance flows from the trend research into the final report."""

    def test_trend_synthesizer_has_live_risk_section(self):
        from creative_agent.sub_agents.trend_researcher.agent import (
            gs_web_synthesizer,
        )

        instr = str(gs_web_synthesizer.instruction)
        structure = instr.split("<REPORT_STRUCTURE>", 1)[1].split(
            "</REPORT_STRUCTURE>"
        )[0]
        assert "4.  **Risk Assessment:**" in structure
        for phrase in ("controvers", "real people", "negative associations"):
            assert phrase in structure

    def test_report_composer_has_risks_and_constraints_section(self):
        structure = prompts.COMBINED_REPORT_COMPOSER_INSTR.split(
            "<FINAL_REPORT_STRUCTURE>", 1
        )[1].split("</FINAL_REPORT_STRUCTURE>")[0]
        assert "5.  **## Risks & Constraints**" in structure
        assert "No more than 3" in structure

    def test_merge_planners_carries_risks_and_brand_assets_forward(self):
        # The composer only sees the merged brief, so the merge step must not
        # drop the trend risks or the brand's distinctive assets.
        assert "Risks & Constraints" in prompts.MERGE_PLANNERS_INSTR
        assert "distinctive assets" in prompts.MERGE_PLANNERS_INSTR
