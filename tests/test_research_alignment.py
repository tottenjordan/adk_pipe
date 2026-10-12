"""Research prompts, inputs and brief helpers agree with what each agent
receives and with the deterministic gates (planner fallbacks, the token each
instruction names, source de-duplication, schema/doc wording)."""

import re

import pytest

from creative_agent.sub_agents.campaign_researcher import agent as ca_agent
from creative_agent.sub_agents.trend_researcher import agent as gs_agent

_TOKENS = re.compile(r"\{([^{}]+)\}")


def _tokens(instr: str) -> set[str]:
    return set(_TOKENS.findall(instr))


# --- 1. planner output tokens are optional + planners retry schema errors ---- #
@pytest.mark.parametrize(
    ("searcher", "token"),
    [
        (ca_agent.campaign_web_searcher, "initial_campaign_queries"),
        (gs_agent.gs_web_searcher, "initial_gs_queries"),
    ],
)
def test_searchers_read_planner_queries_optionally_with_a_fallback(searcher, token):
    instr = searcher.instruction
    assert f"{{{token}?}}" in instr
    assert f"{{{token}}}" not in instr
    assert "If no queries are listed, search the intersection of" in instr


def test_campaign_searcher_fallback_names_the_campaign_inputs():
    tokens = _tokens(ca_agent.campaign_web_searcher.instruction)
    assert {"brand", "target_product", "target_audience"} <= tokens


def test_trend_searcher_fallback_names_the_trend_and_audience():
    tokens = _tokens(gs_agent.gs_web_searcher.instruction)
    assert {"target_search_trends", "target_audience"} <= tokens


@pytest.mark.parametrize(
    "planner", [ca_agent.campaign_web_planner, gs_agent.gs_web_planner]
)
def test_planners_retry_schema_errors(planner):
    from creative_agent.config import SCHEMA_RETRY

    assert planner.retry_config is SCHEMA_RETRY
