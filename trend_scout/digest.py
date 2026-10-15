"""The trend digest: why each of the day's trends is trending, plus its
sensitivity flags, cached per trend list in BigQuery.

Exposed to the root as the ``trend_digest`` NodeTool (a bare Workflow node,
like ``understand_trends_agent_resilient``), called right after the gather:

    START → digest_lookup ─ "hit"      ──────────────────────────────────────→ digest_ready
                          ├ "miss"     → trend_describe_failsoft → classify_trends_agent → digest_store → digest_ready
                          └ "disabled" ──────────────────────────→ classify_trends_agent → digest_store → digest_ready

- ``digest_lookup`` (no LLM) keys the cache by the public table's
  ``gtrends_refresh_date`` + ``terms_hash`` (the normalised raw terms) and runs
  one parameterised SELECT in a worker thread, bounded to
  ``LOOKUP_TIMEOUT_SECONDS``. A hit restores ``trend_summaries`` (source
  ``cache``) and ``trend_flags``, so every run on the same day sees the same
  flags and no model is called. A missing table/date, a timeout, a malformed
  row or any error is a miss (warning log). It clears a stale
  ``trend_summaries_draft`` on every route.
- ``trend_describe_failsoft`` = FailSoftNode(RetryUntilKeyNode(searcher →
  synthesizer, 2 attempts)) writes ``trend_summaries_draft`` (TrendSummaries).
  Optional: an empty or failed describe leaves the draft unset.
- ``classify_trends_agent`` (unchanged name / output_key / model /
  temperature) reads the draft through ``{trend_summaries_draft?}``, so the
  sensitivity verdicts rest on today's facts when they are available.
- ``digest_store`` normalises the draft into ``trend_summaries`` (raw terms
  only, verbatim, capped; source ``fresh``, or ``unavailable`` with no items)
  and, when the summaries cover at least ``MIN_CACHE_COVERAGE`` of the terms
  AND the flags cover every term (``FLAG_CACHE_COVERAGE``; a missing verdict
  reads as "not sensitive", so a partial classify is never shared), MERGE-inserts one row (insert-only,
  keyed by ``stable_row_id(refresh_date, terms_hash)``: the first writer of a
  racing pair wins). A failed describe is never cached, so the next run
  retries; a failed write only logs.
- ``TREND_DIGEST_ENABLED=false`` routes ``disabled``: only the classifier runs
  (no describe, no cache I/O) and ``trend_summaries`` is not written at all —
  exactly the pre-digest behaviour.
- ``digest_ready`` is the truthy terminal, so a NodeTool caller never stalls.

The cache key, normalisation, coverage rules and BigQuery I/O live in
``trend_scout.digest_store``; this module holds the agents and the graph.

The cached data is public Google Trends terms plus model summaries; nothing
user-specific is read, stored or logged.
"""

from __future__ import annotations

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

from agent_common import (
    FailSoftNode,
    PipelineRequest,
    RetryUntilKeyNode,
    build_gemini,
    scrub_surrogates_in_response,
)
from agent_common.sampling import temperature as sampled_temperature

from . import callbacks, prompts
from .config import INFRA_RETRY, config
from .digest_store import (
    lookup_cached_digest,
    normalize_draft,
    normalize_flags,
    parse_refresh_date,
    should_cache,
    terms_of,
    write_cached_digest,
)
from .schemas import TrendFlags, TrendSummaries

logger = logging.getLogger(__name__)

_LABELS = {"agentic_wf": "trend_scout", "agent": "trend_scout"}


# --- agents ---------------------------------------------------------------------

# Grounded searcher: all 25 terms in one turn, one fact-checked line each (the
# understand_trends searcher pattern). Graph nodes run single_turn (a node with
# a parent otherwise defaults to chat mode and would stall on an empty turn).
trend_describe_searcher = Agent(
    model=build_gemini(config.worker_model),
    name="trend_describe_searcher",
    mode="single_turn",
    include_contents="none",
    description="Searches why each of today's gathered trends is being searched.",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction=prompts.TREND_DESCRIBE_SEARCHER_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=sampled_temperature(0.7),
        labels={**_LABELS, "subagent": "trend_describe_searcher"},
    ),
    tools=[google_search],
    output_key="trend_describe_raw",
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)

# Tool-free structured synthesis into one neutral line per raw term.
trend_describe_synthesizer = Agent(
    model=build_gemini(config.lite_planner_model),
    name="trend_describe_synthesizer",
    mode="single_turn",
    include_contents="none",
    description="Turns the findings into one neutral summary line per trend.",
    instruction=prompts.TREND_DESCRIBE_SYNTHESIZER_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=sampled_temperature(0.2),
        labels={**_LABELS, "subagent": "trend_describe_synthesizer"},
    ),
    output_schema=TrendSummaries,
    output_key="trend_summaries_draft",
    retry_config=INFRA_RETRY,
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=[
        scrub_surrogates_in_response,
        callbacks.log_empty_turn_finish_reason,
    ],
)

trend_describe_search_and_synthesize = Workflow(
    name="trend_describe_search_and_synthesize",
    description="Searches today's trends then writes one summary line per term.",
    edges=[("START", trend_describe_searcher, trend_describe_synthesizer)],
)

trend_describe_resilient = RetryUntilKeyNode(
    name="trend_describe_resilient",
    node=trend_describe_search_and_synthesize,
    output_key="trend_summaries_draft",
    max_attempts=2,
)


def _describe_failed(_state: Mapping[str, Any], _exc: Exception) -> dict[str, Any]:
    """The describe step is optional: on an exception, no draft."""
    return {"trend_summaries_draft": None}


trend_describe_failsoft = FailSoftNode(
    name="trend_describe_failsoft",
    node=trend_describe_resilient,
    on_error=_describe_failed,
)


# Sensitive-trend classifier: one verdict per raw term → `trend_flags`. The auto
# pick never selects a flagged term (PICK_TRENDS_INSTR + the save guard in
# save_search_trends_to_session_state); flagged terms that a human picks carry
# the flag into `trend_context`. Cheap structured classification on the gather
# model's bucket; it reads the describe step's summaries when present.
classify_trends_agent = Agent(
    model=build_gemini(config.gather_model),
    name="classify_trends_agent",
    mode="single_turn",
    include_contents="none",
    description=(
        "Flag the gathered trends that centre on a death, tragedy, disaster, "
        "violence, crime or serious illness (stored in 'trend_flags')."
    ),
    instruction=prompts.CLASSIFY_TRENDS_INSTR,
    output_schema=TrendFlags,
    output_key="trend_flags",
    retry_config=INFRA_RETRY,
    generate_content_config=types.GenerateContentConfig(
        temperature=sampled_temperature(0.0),
        labels={**_LABELS, "subagent": "classify_trends_agent"},
    ),
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=[
        scrub_surrogates_in_response,
        callbacks.log_empty_turn_finish_reason,
    ],
)


# --- graph nodes ------------------------------------------------------------------


async def digest_lookup(ctx: Context) -> Event:
    """Route ``hit`` (state restored), ``miss`` or ``disabled``; no LLM."""
    delta: dict[str, Any] = {"trend_summaries_draft": None}
    if not config.trend_digest_enabled:
        route = "disabled"
    else:
        cached = await lookup_cached_digest(ctx.state.to_dict())
        route = "hit" if cached else "miss"
        delta.update(cached or {})
    return Event(
        output=f"Trend digest: {route}.",
        actions=EventActions(route=route, state_delta=delta),
    )


async def digest_store(ctx: Context) -> None:
    """Write ``trend_summaries`` and cache a complete fresh digest (fail-soft)."""
    if not config.trend_digest_enabled:
        return None
    state = ctx.state.to_dict()
    terms = terms_of(state.get("raw_gtrends"))
    refresh_date = parse_refresh_date(state.get("gtrends_refresh_date"))
    summaries = normalize_draft(state.get("trend_summaries_draft"), terms, refresh_date)
    ctx.state["trend_summaries"] = summaries
    if state.get("trend_summaries_draft__retry_exhausted"):
        # Optional enrichment: "unavailable" says it; not a research gap.
        ctx.state["trend_summaries_draft__retry_exhausted"] = None
    if summaries["source"] != "fresh":
        logger.warning("trend digest: summaries unavailable; not cached")
        return None
    flags = normalize_flags(state.get("trend_flags"), terms)
    unclassified = len(terms) - (len(flags["flags"]) if flags else 0)
    if unclassified:
        logger.warning(
            "trend digest: %d of %d terms have no sensitivity verdict; not cached",
            unclassified,
            len(terms),
        )
    if flags is None or not should_cache(summaries, flags, terms):
        logger.info("trend digest not cached (incomplete)")
        return None
    await write_cached_digest(terms, refresh_date, summaries, flags)
    return None


def digest_ready(ctx: Context) -> str:
    """The truthy terminal: a one-line summary for the root's function response."""
    state = ctx.state.to_dict()
    terms = terms_of(state.get("raw_gtrends"))
    flags = normalize_flags(state.get("trend_flags"), terms) or {"flags": []}
    flagged = sum(1 for f in flags["flags"] if f["sensitive"])
    summaries = state.get("trend_summaries")
    if not config.trend_digest_enabled or not isinstance(summaries, Mapping):
        return f"Trend flags ready: {flagged} of {len(terms)} trends flagged sensitive."
    items = summaries.get("items")
    return (
        f"Trend digest ready ({summaries.get('source')}): "
        f"{len(items) if isinstance(items, list) else 0} of {len(terms)} trends "
        f"summarised; {flagged} flagged sensitive (stored in 'trend_flags')."
    )


trend_digest = Workflow(
    name="trend_digest",
    description=(
        "Summarise why each gathered trend is trending today (stored in "
        "'trend_summaries') and flag the ones centred on a death, tragedy, "
        "disaster, violence, crime or serious illness (stored in 'trend_flags'). "
        "Reuses today's cached digest when there is one."
    ),
    input_schema=PipelineRequest,
    edges=[
        ("START", digest_lookup),
        (
            digest_lookup,
            {
                "hit": digest_ready,
                "miss": trend_describe_failsoft,
                "disabled": classify_trends_agent,
            },
        ),
        (trend_describe_failsoft, classify_trends_agent, digest_store, digest_ready),
    ],
)
