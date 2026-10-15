"""The trend situation branch: the trend's current moment, grounded and validated.

One branch of combined_research_pipeline's START fan-out (beside the two
research planners and load_brand_history), so research_join waits for it too:

    START → trend_situation_gate ─ "run"  → trend_situation_failsoft → validate_trend_situation
                                  └ "skip" ─────────────────────────────┘

- ``trend_situation_gate`` routes "skip" when TREND_STATE_ENABLED is off (no
  model call) and clears any stale draft on both routes.
- ``trend_situation_failsoft`` = FailSoftNode(RetryUntilKeyNode(Workflow(
  searcher → synthesizer))): a google_search searcher (its grounding sources
  join ``state["sources"]`` with ``src-N`` ids via
  collect_research_sources_callback) and a structured synthesizer writing
  ``trend_situation_draft`` (TrendSituationDraft). An empty turn retries the
  pair once; an exception leaves the draft None and the run continues.
- ``validate_trend_situation`` (no LLM) turns the draft into the validated
  ``trend_situation`` + the one-line ``trend_situation_md``
  (creative_agent/trend_situation.py), abstaining to "unknown" when unsure.
"""

import logging
from collections.abc import Mapping
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.planners import BuiltInPlanner
from google.adk.tools import google_search
from google.adk.workflow import Workflow
from google.genai import types

from agent_common import FailSoftNode, RetryUntilKeyNode, build_gemini
from agent_common.sampling import temperature as sampled_temperature

from ... import callbacks, prompts
from ...config import SCHEMA_RETRY, config
from ...schemas import TrendSituationDraft
from ...trend_situation import trend_situation_state_delta

logger = logging.getLogger(__name__)

# Searcher half: google_search + raw dated findings. Grounding metadata lives on
# this turn, so collect_research_sources_callback stays here (the validator
# checks the synthesizer's cited ids against `sources`).
trend_situation_searcher = Agent(
    model=build_gemini(config.worker_model),
    name="trend_situation_searcher",
    mode="single_turn",
    include_contents="none",
    description="Searches the web for the trend's most recent and next dated events.",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction=prompts.TREND_SITUATION_SEARCHER_INSTR,
    tools=[google_search],
    output_key="trend_situation_raw",
    after_agent_callback=callbacks.collect_research_sources_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)

# Synthesizer half: tool-free, structured. Reads the raw findings through an
# optional token, so an empty searcher turn degrades to an abstaining draft (or
# an empty turn the wrapper retries) instead of a KeyError.
trend_situation_synthesizer = Agent(
    model=build_gemini(config.worker_model),
    name="trend_situation_synthesizer",
    mode="single_turn",
    include_contents="none",
    description="Turns the raw findings into a structured trend situation draft.",
    instruction=prompts.TREND_SITUATION_SYNTHESIZER_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=sampled_temperature(0.2)
    ),
    output_schema=TrendSituationDraft,
    retry_config=SCHEMA_RETRY,
    output_key="trend_situation_draft",
    after_model_callback=[
        callbacks.scrub_surrogates_in_response,
        callbacks.log_empty_turn_finish_reason,
    ],
)

trend_situation_search = Workflow(
    name="trend_situation_search",
    description="Searches the trend's dated events then drafts its situation.",
    edges=[("START", trend_situation_searcher, trend_situation_synthesizer)],
)

trend_situation_resilient = RetryUntilKeyNode(
    name="trend_situation_resilient",
    node=trend_situation_search,
    output_key="trend_situation_draft",
    max_attempts=2,
)


def _trend_situation_failed(state: Mapping[str, Any], exc: Exception) -> dict[str, Any]:
    """The situation is optional: on an exception, no draft (→ "unknown")."""
    logger.warning("trend situation branch failed; continuing without it")
    return {"trend_situation_draft": None}


trend_situation_failsoft = FailSoftNode(
    name="trend_situation_failsoft",
    node=trend_situation_resilient,
    on_error=_trend_situation_failed,
)


def trend_situation_gate_route(enabled: bool) -> str:
    """``"run"`` when the trend situation is enabled, else ``"skip"``."""
    return "run" if enabled else "skip"


def trend_situation_gate(ctx: Context) -> Event:
    """Route to the grounded branch only when TREND_STATE_ENABLED is on.

    Both routes clear a stale ``trend_situation_draft`` first, so a draft from
    an earlier turn can neither satisfy the retry wrapper nor reach the
    validator.
    """
    route = trend_situation_gate_route(config.trend_state_enabled)
    return Event(
        actions=EventActions(route=route, state_delta={"trend_situation_draft": None})
    )


def validate_trend_situation(ctx: Context) -> None:
    """Validate the draft into ``trend_situation`` + ``trend_situation_md``, and
    seed ``timely_rules`` (the timely slot's rules; "" unless timely).

    No LLM. Runs on both routes; yields no output (research_barrier drops the
    join dict anyway).
    """
    try:
        delta = trend_situation_state_delta(
            ctx.state.to_dict(),
            enabled=config.trend_state_enabled,
            window_days=config.trend_event_window_days,
            valid_hours_after=config.timely_valid_hours_after,
            max_timely=config.max_timely_concepts,
        )
    except Exception:
        logger.exception("trend situation validation failed; continuing without it")
        return None
    situation = delta["trend_situation"]
    logger.info(
        "trend situation: phase=%s timely_ok=%s reason=%r",
        situation.get("phase"),
        situation.get("timely_ok"),
        situation.get("abstain_reason"),
    )
    for key, value in delta.items():
        ctx.state[key] = value
    return None


trend_situation_pipeline = Workflow(
    name="trend_situation_pipeline",
    description="Drafts and validates the trend's current moment (dated, sourced).",
    edges=[
        ("START", trend_situation_gate),
        (
            trend_situation_gate,
            {"run": trend_situation_failsoft, "skip": validate_trend_situation},
        ),
        (trend_situation_failsoft, validate_trend_situation),
    ],
)
