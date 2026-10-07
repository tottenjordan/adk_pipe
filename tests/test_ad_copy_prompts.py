"""Prompt wiring for the ad copy stage (angles, diversity, brief checklist).

The drafter spreads its ideas across the creative brief's angles and self-rates
typicality; the critic keeps angle coverage and at least one unexpected idea.
Also guards that every `{...}` in these instructions is a well-formed ADK state
token (no stray literal braces).
"""

import re

import pytest

from creative_agent import prompts

AD_PROMPTS = ["AD_COPY_DRAFTER_INSTR", "AD_COPY_CRITIC_INSTR"]


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
