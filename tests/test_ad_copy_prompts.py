"""Prompt wiring for the ad copy stage (angles, diversity, brief checklist).

The drafter spreads its ideas across the creative brief's angles and self-rates
typicality; the critic keeps angle coverage and at least one unexpected idea.
Also guards that every `{...}` in these instructions is a well-formed ADK state
token (no stray literal braces).
"""

import re

import pytest

from creative_agent import prompts

AD_PROMPTS = ["AD_COPY_DRAFTER_INSTR", "AD_COPY_CRITIC_INSTR", "AD_COPY_REVISER_INSTR"]


def test_drafter_spreads_ideas_across_brief_angles():
    instr = prompts.AD_COPY_DRAFTER_INSTR
    assert "{creative_brief_md?}" in instr
    assert "at least 2 ideas per angle" in instr
    assert "`angle_id`" in instr
    assert "`typicality`" in instr
    assert 'set `angle_id` to ""' in instr  # no-brief fallback
    # The tone-diversity rule is kept alongside the angle spread.
    assert "at least 4 of the following creative tones" in instr


def test_critic_keeps_angle_coverage_and_one_unexpected_idea():
    instr = prompts.AD_COPY_CRITIC_INSTR
    assert "at least 3 distinct `angle_id`s" in instr
    assert "`typicality` below 0.5" in instr
    assert "Carry each selected idea's `angle_id` and `typicality`" in instr
    assert "exactly 4" in instr


_TOKEN = re.compile(r"\{([^{}]*)\}")


@pytest.mark.parametrize("name", AD_PROMPTS)
def test_every_brace_is_a_state_token(name):
    instr = getattr(prompts, name)
    for token in _TOKEN.findall(instr):
        assert re.fullmatch(r"[A-Za-z_]\w*\??", token), (name, token)
    stripped = _TOKEN.sub("", instr)
    assert "{" not in stripped and "}" not in stripped, name


def test_critic_fills_one_brief_check_per_item():
    from typing import get_args

    from creative_agent.schemas import BriefCheckItem

    instr = prompts.AD_COPY_CRITIC_INSTR
    assert "fill `brief_checks` with exactly one entry per item" in instr
    for item in get_args(BriefCheckItem):
        assert f"*   `{item}`:" in instr, item
    # Accurate, not harsh: a failed proposition/mandatories item triggers a
    # paid revision, so the critic must not fail items it is merely unsure of.
    assert "Mark `passed` false only when the copy clearly fails the item" in instr
    assert "be accurate, not harsh" in instr
    assert "revised later" not in instr


def test_critic_critiques_and_improves_every_cta():
    instr = prompts.AD_COPY_CRITIC_INSTR
    assert "critique and improve every CTA" in instr
    for phrase in ("action verb", "desired response", "within 8 words"):
        assert phrase in instr, phrase


def test_reviser_instruction_tokens():
    instr = prompts.AD_COPY_REVISER_INSTR
    for token in (
        "{ad_copy_critique?}",
        "{ad_copy_issues?}",
        "{creative_brief_md?}",
        "{brand}",
        "{target_product}",
        "{target_audience}",
        "{key_selling_points}",
        "{ad_copy_feedback?}",
    ):
        assert token in instr, token
    assert instr.count(prompts.BRIEF_BLOCK) == 1
    # The reviser never reads the report, so it falls back to the campaign inputs.
    assert prompts.CREATIVE_BRIEF_CONTRACT_CORE in instr
    assert prompts.BRIEF_FALLBACK_REPORT not in instr
    assert "{combined_final_cited_report?}" not in instr


def test_reviser_rewrites_only_flagged_copies():
    instr = prompts.AD_COPY_REVISER_INSTR
    assert "Rewrite ONLY those copies" in instr
    assert "fix exactly the listed issues" in instr
    assert "stays verbatim" in instr
    assert "return ALL the copies" in instr
    assert "same order with unchanged `original_id`s" in instr
    assert "refresh `brief_checks` on every copy you revise" in instr
    assert "'FinalAdCopyList' schema" in instr
