"""Date awareness in the prompts (trend state plan, Task 1.3).

Every trend-research and brief/copy/art-direction prompt carries the shared
date line, built from the per-run `current_*` state keys as OPTIONAL tokens (a
session seeded before the feature still renders), and never a literal date.
"""

import re

import pytest

from agent_common.clock import DATE_CONTEXT_LINE
from creative_agent import prompts as creative_prompts
from creative_agent.sub_agents.trend_researcher import agent as trend_researcher
from trend_scout import prompts as scout_prompts

EXPECTED_LINE = (
    "Today is {current_date?} ({current_weekday?}, {current_timezone?}). "
    'Interpret "yesterday", "tonight", "tomorrow" and "this weekend" relative to '
    "this date, and never assume an event's timing or result without a dated source."
)


def _date_prompts() -> dict[str, str]:
    return {
        "trend_scout.UNDERSTAND_TRENDS_SEARCHER_INSTR": (
            scout_prompts.UNDERSTAND_TRENDS_SEARCHER_INSTR
        ),
        "trend_scout.UNDERSTAND_TRENDS_SYNTHESIZER_INSTR": (
            scout_prompts.UNDERSTAND_TRENDS_SYNTHESIZER_INSTR
        ),
        "gs_web_planner": str(trend_researcher.gs_web_planner.instruction),
        "gs_web_searcher": str(trend_researcher.gs_web_searcher.instruction),
        "gs_web_synthesizer": str(trend_researcher.gs_web_synthesizer.instruction),
        "CREATIVE_BRIEF_WRITER_INSTR": creative_prompts.CREATIVE_BRIEF_WRITER_INSTR,
        "AD_COPY_DRAFTER_INSTR": creative_prompts.AD_COPY_DRAFTER_INSTR,
        "AD_COPY_CRITIC_INSTR": creative_prompts.AD_COPY_CRITIC_INSTR,
        "ART_DIRECTOR_INSTR": creative_prompts.ART_DIRECTOR_INSTR,
    }


def test_date_line_is_exactly_as_specified():
    assert DATE_CONTEXT_LINE == EXPECTED_LINE


@pytest.mark.parametrize("name", list(_date_prompts()))
def test_prompt_carries_the_date_line_once(name):
    instr = _date_prompts()[name]
    assert instr.count(EXPECTED_LINE) == 1, name
    # Never a literal date baked into the prompt (pickled at deploy time).
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", instr), name


_TOKEN = re.compile(r"\{([^{}]*)\}")


@pytest.mark.parametrize(
    "name",
    [
        n
        for n in _date_prompts()
        if n != "trend_scout.UNDERSTAND_TRENDS_SYNTHESIZER_INSTR"
    ],
)
def test_every_brace_is_a_state_token(name):
    instr = _date_prompts()[name]
    for token in _TOKEN.findall(instr):
        assert re.fullmatch(r"[A-Za-z_]\w*\??", token), (name, token)
    stripped = _TOKEN.sub("", instr)
    assert "{" not in stripped and "}" not in stripped, name


@pytest.mark.parametrize("name", list(_date_prompts()))
def test_date_tokens_are_optional(name):
    instr = _date_prompts()[name]
    for key in ("current_date", "current_weekday", "current_timezone"):
        assert "{" + key + "}" not in instr, (name, key)


def test_scout_synthesizer_items_carry_as_of_and_event_hint():
    instr = scout_prompts.UNDERSTAND_TRENDS_SYNTHESIZER_INSTR
    assert '"as_of": "{current_date?}"' in instr
    assert '"event_hint":' in instr


def test_trend_synthesizer_reports_the_current_moment():
    instr = str(trend_researcher.gs_web_synthesizer.instruction)
    assert "**Current Moment & Timeline:**" in instr
    assert "no notable dated event" in instr


def _render(instruction: str, state: dict) -> str:
    import asyncio
    from types import SimpleNamespace

    from google.adk.flows.llm_flows.prompt._instructions_utils import (
        inject_session_state,
    )

    ctx = SimpleNamespace(
        _invocation_context=SimpleNamespace(
            session=SimpleNamespace(state=state), artifact_service=None
        )
    )
    return asyncio.run(
        inject_session_state(instruction, ctx)  # ty: ignore[invalid-argument-type]
    )


def test_scout_synthesizer_renders_the_run_date():
    instr = scout_prompts.UNDERSTAND_TRENDS_SYNTHESIZER_INSTR
    rendered = _render(
        instr,
        {
            "current_date": "2026-10-11",
            "current_weekday": "Sunday",
            "current_timezone": "America/New_York",
            "info_gtrends_raw": "raw",
        },
    )
    assert "Today is 2026-10-11 (Sunday, America/New_York)." in rendered
    assert '"as_of": "2026-10-11"' in rendered
    # The JSON template's own braces survive.
    assert '"analyzed_trends": [' in rendered
