"""Research prompts, inputs and brief helpers agree with what each agent
receives and with the deterministic gates (planner fallbacks, the token each
instruction names, source de-duplication, schema/doc wording)."""

import re
from types import SimpleNamespace

import pytest
from google.genai import types

from creative_agent import prompts
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


# --- 2. source collection de-duplicates claims on rescan --------------------- #
def _grounded_event(url: str, title: str, segment: str, confidence: float):
    return SimpleNamespace(
        grounding_metadata=types.GroundingMetadata(
            grounding_chunks=[
                types.GroundingChunk(
                    web=types.GroundingChunkWeb(uri=url, title=title, domain="ex.com")
                )
            ],
            grounding_supports=[
                types.GroundingSupport(
                    segment=types.Segment(text=segment),
                    grounding_chunk_indices=[0],
                    confidence_scores=[confidence],
                )
            ],
        )
    )


def _ctx(events, state):
    return SimpleNamespace(
        session=SimpleNamespace(events=events),
        state=state,
    )


def test_rescanning_the_session_does_not_duplicate_claims():
    from creative_agent.callbacks import collect_research_sources_callback

    first = _grounded_event("https://ex.com/a", "A", "Claim A.", 0.9)
    state: dict = {}
    collect_research_sources_callback(_ctx([first], state))
    # A later searcher re-scans the whole session (first event included) and
    # adds a new source plus a new claim for the known one.
    second = _grounded_event("https://ex.com/b", "B", "Claim B.", 0.8)
    again = _grounded_event("https://ex.com/a", "A", "Claim A2.", 0.7)
    collect_research_sources_callback(_ctx([first, second, again], state))
    collect_research_sources_callback(_ctx([first, second, again], state))

    sources = state["sources"]
    assert state["url_to_short_id"] == {
        "https://ex.com/a": "src-1",
        "https://ex.com/b": "src-2",
    }
    assert [c["text_segment"] for c in sources["src-1"]["supported_claims"]] == [
        "Claim A.",
        "Claim A2.",
    ]
    assert [c["text_segment"] for c in sources["src-2"]["supported_claims"]] == [
        "Claim B."
    ]


# --- 7. synthesizers receive the inputs they are told to use ----------------- #
def test_campaign_synthesizer_receives_the_campaign_inputs():
    tokens = _tokens(ca_agent.campaign_web_synthesizer.instruction)
    assert {"target_audience", "target_product", "key_selling_points"} <= tokens


def test_trend_synthesizer_receives_the_campaign_inputs():
    tokens = _tokens(gs_agent.gs_web_synthesizer.instruction)
    assert {"brand", "target_product", "target_audience"} <= tokens


# --- 8. refinement round names what it searches / addresses ------------------ #
def test_enhanced_searcher_names_the_intersection_inputs():
    tokens = _tokens(prompts.ENHANCED_COMBINED_SEARCHER_INSTR)
    assert {"target_product", "target_search_trends", "target_audience"} <= tokens


def test_refined_synthesizer_sees_the_evaluation_gap():
    from creative_agent import agent as ca

    assert ca.combined_web_evaluator.output_key == "combined_research_evaluation"
    assert "{combined_research_evaluation?}" in prompts.REFINED_WEB_SYNTHESIZER_INSTR


# --- 9/13. schema descriptions ------------------------------------------------ #
def test_brief_avoid_description_leaves_brand_donts_to_do_not():
    from creative_agent.schemas import CreativeBrief

    desc = CreativeBrief.model_fields["avoid"].description or ""
    assert "trend risks, brand don'ts" not in desc
    assert "brand don'ts go in brand.do_not" in desc


@pytest.mark.parametrize("model", [ca_agent.CampaignQueryList, gs_agent.TrendQueryList])
def test_query_list_docstrings_describe_a_query_list(model):
    doc = model.__doc__ or ""
    assert "evaluation feedback" not in doc
    assert "queries" in doc


def test_root_description_describes_the_single_pipeline():
    from creative_agent import agent as ca

    desc = ca.root_agent.description
    assert "actor-critic" not in desc
    assert "creative_pipeline" in desc


# --- 10. timeline section names publishers, never links ---------------------- #
def test_trend_synthesizer_timeline_names_publishers_not_links():
    instr = gs_agent.gs_web_synthesizer.instruction
    timeline = instr.split("Current Moment & Timeline")[1].split("</REPORT_STRUCTURE>")[
        0
    ]
    assert "the source it came from" not in timeline
    assert "publisher" in timeline and "no links" in timeline


# --- 13. the research PDF brief carries the current moment ------------------- #
def test_pdf_brief_includes_the_current_moment():
    from creative_agent.brief_render import insert_brief_into_report

    brief = {
        "single_minded_proposition": "Skates that never quit.",
        "trend_bridge": {"fit_score": 4, "fit_mode": "direct", "bridge": "speed"},
    }
    moment = "Upcoming: Texas vs Oklahoma, 2026-10-12."
    out = insert_brief_into_report("# R\n## S\n", brief, current_moment=moment)
    assert moment in out
    assert moment not in insert_brief_into_report("# R\n## S\n", brief)
