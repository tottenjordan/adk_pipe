"""Prompt wiring for the structured creative brief (the downstream contract).

The ad copy and visual agents read `{creative_brief_md?}` (the compact Markdown
rendering of the brief) in a <CREATIVE_BRIEF> block placed BEFORE the research
report (the report stays as supporting context), with one shared contract rule
(core + a fallback suffix; the visual critic falls back to the campaign inputs
and draft concepts). Also guards that every `{...}` in
these instructions is a well-formed ADK state token (no stray literal braces).
"""

import re

import pytest

from creative_agent import prompts

BRIEF_BLOCK = "<CREATIVE_BRIEF>{creative_brief_md?}</CREATIVE_BRIEF>"
CONSUMERS = {
    "AD_COPY_DRAFTER_INSTR": prompts.AD_COPY_DRAFTER_INSTR,
    "AD_COPY_CRITIC_INSTR": prompts.AD_COPY_CRITIC_INSTR,
    "ART_DIRECTOR_INSTR": prompts.ART_DIRECTOR_INSTR,
    "VISUAL_CONCEPT_DRAFTER_INSTR": prompts.VISUAL_CONCEPT_DRAFTER_INSTR,
    "VISUAL_CONCEPT_CRITIC_INSTR": prompts.VISUAL_CONCEPT_CRITIC_INSTR,
}


@pytest.mark.parametrize("name", sorted(CONSUMERS))
def test_brief_block_present_once(name):
    assert CONSUMERS[name].count(BRIEF_BLOCK) == 1, name


@pytest.mark.parametrize("name", sorted(CONSUMERS))
def test_brief_block_precedes_the_research_report(name):
    instr = CONSUMERS[name]
    report = instr.find("{combined_final_cited_report?}")
    if report != -1:
        assert instr.index(BRIEF_BLOCK) < report, name


@pytest.mark.parametrize("name", sorted(CONSUMERS))
def test_contract_rule_present(name):
    expected = (
        prompts.VISUAL_CRITIC_BRIEF_RULE
        if name == "VISUAL_CONCEPT_CRITIC_INSTR"
        else prompts.CREATIVE_BRIEF_CONTRACT_RULE
    )
    assert expected in CONSUMERS[name], name
    assert prompts.CREATIVE_BRIEF_CONTRACT_CORE in CONSUMERS[name], name


def test_visual_critic_falls_back_to_campaign_inputs_and_drafts():
    instr = prompts.VISUAL_CONCEPT_CRITIC_INSTR
    assert prompts.CREATIVE_BRIEF_CONTRACT_RULE not in instr
    assert prompts.VISUAL_CRITIC_BRIEF_RULE.endswith(
        "fall back to the campaign inputs and the draft concepts."
    )


def test_user_feedback_and_art_direction_override_the_brief():
    assert (
        "Explicit user feedback (research feedback, ad copy feedback) and user art "
        "direction (visual intent, brand colours, avoid) override the brief where "
        "they conflict." in prompts.CREATIVE_BRIEF_CONTRACT_CORE
    )


def test_consumers_read_the_markdown_not_the_dict():
    for name, instr in CONSUMERS.items():
        assert "{creative_brief?}" not in instr, name


def test_contract_rule_wording():
    rule = prompts.CREATIVE_BRIEF_CONTRACT_RULE
    for phrase in (
        "The creative brief is the contract",
        "single-minded proposition",
        "reasons to believe",
        "mandatories",
        "light_touch",
        "never force the product into the trend",
        "If the brief is empty, fall back to the research report.",
    ):
        assert phrase in rule, phrase
    for constant in (rule, prompts.VISUAL_CRITIC_BRIEF_RULE):
        assert "{" not in constant and "}" not in constant


def test_research_report_kept_as_supporting_context():
    for name in (
        "AD_COPY_DRAFTER_INSTR",
        "AD_COPY_CRITIC_INSTR",
        "ART_DIRECTOR_INSTR",
        "VISUAL_CONCEPT_DRAFTER_INSTR",
    ):
        assert "{combined_final_cited_report?}" in CONSUMERS[name], name


def test_art_director_places_distinctive_assets_and_tone_compatible_styles():
    instr = prompts.ART_DIRECTOR_INSTR
    assert "at least one brand distinctive asset per concept" in instr
    assert "{style_shortlist?}" in instr
    assert "compatible with the brand's tone" in instr


_TOKEN = re.compile(r"\{([^{}]*)\}")


@pytest.mark.parametrize("name", sorted([*CONSUMERS, "CREATIVE_BRIEF_WRITER_INSTR"]))
def test_every_brace_is_a_state_token(name):
    instr = getattr(prompts, name)
    for token in _TOKEN.findall(instr):
        assert re.fullmatch(r"[A-Za-z_]\w*\??", token), (name, token)
    # No unmatched braces left once the tokens are removed.
    stripped = _TOKEN.sub("", instr)
    assert "{" not in stripped and "}" not in stripped, name


def test_brief_writer_keeps_avoid_entries_short():
    """copy_gate matches avoid entries literally and skips ones over 4 words."""
    instr = prompts.CREATIVE_BRIEF_WRITER_INSTR
    assert "short terms or phrases (at most 4 words" in instr
    # The format example is a neutral placeholder: a concrete term (the old
    # "gambling odds") leaked verbatim into unrelated briefs' avoid lists.
    assert '"[short term]"' in instr
    assert "gambling" not in instr
