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

The cached data is public Google Trends terms plus model summaries; nothing
user-specific is read, stored or logged.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.planners import BuiltInPlanner
from google.adk.tools import google_search
from google.adk.workflow import Workflow
from google.cloud import bigquery
from google.genai import types

from agent_common import (
    FailSoftNode,
    PipelineRequest,
    RetryUntilKeyNode,
    build_gemini,
    scrub_surrogates_in_response,
    stable_row_id,
)
from agent_common.clients import get_bigquery_client
from agent_common.sampling import temperature as sampled_temperature

from . import callbacks, prompts
from .config import INFRA_RETRY, config
from .schemas import TrendFlags, TrendSummaries
from .trend_context import _FENCE, _is_true, _norm_term

logger = logging.getLogger(__name__)

# Test monkeypatch point (tests/conftest.py points it at a raiser by default).
_get_bigquery_client = get_bigquery_client

LOOKUP_TIMEOUT_SECONDS = 10.0
STORE_TIMEOUT_SECONDS = 10.0
BQ_JOB_TIMEOUT_MS = 8000
MAX_SUMMARY_CHARS = 300
MAX_REASON_CHARS = 200
# Summaries are optional enrichment; caching most of them is useful.
MIN_CACHE_COVERAGE = 0.8
# Flags are a safety floor: a cache hit skips the classifier for every user and
# engine that day, and a missing verdict reads as "not sensitive", so a digest
# is cached (and a cached row used) only with a verdict for EVERY term.
FLAG_CACHE_COVERAGE = 1.0

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


# --- pure helpers -----------------------------------------------------------------


def terms_of(raw_gtrends: Any) -> list[str]:
    """The raw trend terms (verbatim, non-blank strings, first occurrence kept)."""
    if not isinstance(raw_gtrends, (list, tuple)):
        return []
    seen: set[str] = set()
    terms: list[str] = []
    for term in raw_gtrends:
        key = _norm_term(term)
        if key and key not in seen:
            seen.add(key)
            terms.append(term)
    return terms


def parse_refresh_date(value: Any) -> str:
    """``value`` as ISO ``YYYY-MM-DD``, or "" when it isn't a valid date."""
    if not isinstance(value, str):
        return ""
    try:
        return datetime.date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return ""


def terms_hash(refresh_date: str, terms: Sequence[Any]) -> str:
    """The cache key of a trend list: order-, case- and spacing-insensitive."""
    keys = sorted({k for k in (_norm_term(t) for t in terms) if k})
    return stable_row_id(refresh_date, *keys, length=16)


def _load(value: Any) -> Any:
    """A dict/list as is, or parsed from (fenced) JSON; None when unparseable."""
    if isinstance(value, str):
        try:
            return json.loads(_FENCE.sub("", value))
        except ValueError:
            return None
    return value


def _clean(value: Any, cap: int) -> str:
    """A single-line string capped at ``cap`` chars ("" for non-strings)."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:cap].rstrip()


def _entries(data: Any, key: str) -> list[Mapping[str, Any]]:
    data = _load(data)
    items = data.get(key) if isinstance(data, Mapping) else None
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, Mapping)]


def normalize_draft(draft: Any, terms: Sequence[str], refresh_date: str) -> dict:
    """``trend_summaries`` from the describe draft (or a cached item list).

    One item per raw term that has a non-blank summary, in raw order, with the
    raw term verbatim (draft terms are matched exactly or normalised; unknown
    terms are dropped); summaries are single-line and capped at
    ``MAX_SUMMARY_CHARS``. No usable item → source ``unavailable``, no items.
    """
    by_key: dict[str, str] = {}
    for item in _entries(draft, "items"):
        key = _norm_term(item.get("term"))
        summary = _clean(item.get("summary"), MAX_SUMMARY_CHARS)
        if key and summary and key not in by_key:
            by_key[key] = summary
    items = [
        {"term": term, "summary": by_key[_norm_term(term)]}
        for term in terms
        if _norm_term(term) in by_key
    ]
    return {
        "refresh_date": refresh_date,
        "source": "fresh" if items else "unavailable",
        "items": items,
    }


def normalize_flags(trend_flags: Any, terms: Sequence[str]) -> dict | None:
    """``{"flags": [...]}`` restricted to the raw terms (verbatim), or None
    when no raw term has a verdict."""
    by_key: dict[str, dict[str, Any]] = {}
    for flag in _entries(trend_flags, "flags"):
        key = _norm_term(flag.get("term"))
        if key and key not in by_key:
            sensitive = _is_true(flag.get("sensitive"))
            reason = _clean(flag.get("reason"), MAX_REASON_CHARS) if sensitive else ""
            by_key[key] = {"sensitive": sensitive, "reason": reason}
    flags = [
        {"term": term, **by_key[_norm_term(term)]}
        for term in terms
        if _norm_term(term) in by_key
    ]
    return {"flags": flags} if flags else None


def coverage(entries: Sequence[Any], terms: Sequence[str]) -> float:
    """Share of the raw terms that ``entries`` (one per term) cover."""
    return len(entries) / len(terms) if terms else 0.0


def flags_complete(flags: Mapping[str, Any] | None, terms: Sequence[str]) -> bool:
    """True when ``flags`` holds a sensitivity verdict for every raw term."""
    return flags is not None and coverage(flags["flags"], terms) >= FLAG_CACHE_COVERAGE


def should_cache(
    summaries: Mapping[str, Any], flags: Mapping[str, Any] | None, terms: Sequence[str]
) -> bool:
    """Cache only a fresh describe covering ≥ MIN_CACHE_COVERAGE of the terms
    AND a classify with a verdict for every term (FLAG_CACHE_COVERAGE)."""
    if summaries.get("source") != "fresh" or not summaries.get("refresh_date"):
        return False
    if not flags_complete(flags, terms):
        return False
    return coverage(summaries["items"], terms) >= MIN_CACHE_COVERAGE


def build_lookup_sql(
    table: str, refresh_date: str, digest_hash: str
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    """The parameterised cache SELECT (pure; only the config table interpolated)."""
    sql = f"""
        SELECT digest
        FROM `{table}`
        WHERE refresh_date = @refresh_date AND terms_hash = @terms_hash
        ORDER BY created_at DESC
        LIMIT 1
    """
    params = [
        bigquery.ScalarQueryParameter("refresh_date", "DATE", refresh_date),
        bigquery.ScalarQueryParameter("terms_hash", "STRING", digest_hash),
    ]
    return sql, params


def build_insert_sql(
    table: str,
    *,
    digest_id: str,
    refresh_date: str,
    terms_hash: str,
    digest: str,
    models: str,
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    """The insert-only MERGE of one digest row (pure; every value a parameter).

    Keyed on ``digest_id`` (``stable_row_id(refresh_date, terms_hash)``) within
    the ``refresh_date`` partition, with no ``WHEN MATCHED`` clause: of two
    same-day runs racing to write, the first row is kept.
    """
    sql = f"""
    MERGE `{table}` T
    USING (
      SELECT
        @digest_id AS digest_id,
        @refresh_date AS refresh_date,
        @terms_hash AS terms_hash,
        @digest AS digest,
        @models AS models,
        CURRENT_TIMESTAMP() AS created_at
    ) S
    ON T.digest_id = S.digest_id AND T.refresh_date = S.refresh_date
    WHEN NOT MATCHED THEN
      INSERT (digest_id, refresh_date, terms_hash, digest, models, created_at)
      VALUES (S.digest_id, S.refresh_date, S.terms_hash, S.digest, S.models,
        S.created_at);
    """
    params = [
        bigquery.ScalarQueryParameter("digest_id", "STRING", digest_id),
        bigquery.ScalarQueryParameter("refresh_date", "DATE", refresh_date),
        bigquery.ScalarQueryParameter("terms_hash", "STRING", terms_hash),
        bigquery.ScalarQueryParameter("digest", "STRING", digest),
        bigquery.ScalarQueryParameter("models", "STRING", models),
    ]
    return sql, params


def _table_id() -> str | None:
    parts = [config.BQ_PROJECT_ID, config.BQ_DATASET_ID, config.BQ_TABLE_TREND_DIGEST]
    present = [p for p in parts if p]
    return ".".join(present) if len(present) == len(parts) else None


def _models() -> str:
    return (
        f"describe={config.worker_model}+{config.lite_planner_model};"
        f"classify={config.gather_model}"
    )


# --- BigQuery I/O (sync; run in a worker thread) -----------------------------------


def _query_digest(table: str, refresh_date: str, digest_hash: str) -> list[Any]:
    sql, params = build_lookup_sql(table, refresh_date, digest_hash)
    job_config = bigquery.QueryJobConfig(
        query_parameters=params, job_timeout_ms=BQ_JOB_TIMEOUT_MS
    )
    return list(_get_bigquery_client().query(sql, job_config=job_config).result())


def _write_digest(table: str, **row: str) -> None:
    sql, params = build_insert_sql(table, **row)
    job_config = bigquery.QueryJobConfig(
        query_parameters=params, job_timeout_ms=BQ_JOB_TIMEOUT_MS
    )
    # result() raises on a failed job.
    _get_bigquery_client().query(sql, job_config=job_config).result()


def _row_digest(row: Any) -> Any:
    if isinstance(row, Mapping) or hasattr(row, "get"):
        return row.get("digest")
    return getattr(row, "digest", None)


async def lookup_cached_digest(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """``{"trend_summaries", "trend_flags"}`` from the cache, or None (a miss).

    Fail-soft: no terms, no refresh date, no configured table, a timeout, a
    malformed row or any BigQuery error is a miss with a warning log.
    """
    terms = terms_of(state.get("raw_gtrends"))
    refresh_date = parse_refresh_date(state.get("gtrends_refresh_date"))
    if not terms or not refresh_date:
        logger.warning("trend digest lookup skipped: no gathered terms or refresh date")
        return None
    table = _table_id()
    if table is None:
        logger.warning("trend digest lookup skipped: BQ trend digest table not set")
        return None
    digest_hash = terms_hash(refresh_date, terms)
    try:
        rows = await asyncio.wait_for(
            asyncio.to_thread(_query_digest, table, refresh_date, digest_hash),
            LOOKUP_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # incl. TimeoutError and a missing table
        logger.warning(
            "trend digest lookup failed (%s): %s", type(exc).__name__, exc or "timeout"
        )
        return None
    if not rows:
        logger.info("trend digest cache miss for %s/%s", refresh_date, digest_hash)
        return None
    cached = _load(_row_digest(rows[0]))
    summaries = normalize_draft(cached, terms, refresh_date)
    # The row stores the flag list itself: {"items": [...], "flags": [...]}.
    flags = normalize_flags(
        {"flags": cached.get("flags")} if isinstance(cached, Mapping) else None, terms
    )
    if summaries["source"] != "fresh" or not flags_complete(flags, terms):
        # A row without a verdict for every term (e.g. written before full flag
        # coverage was required) is a miss: the classifier runs again.
        logger.warning("trend digest cache row unusable for %s", refresh_date)
        return None
    logger.info("trend digest cache hit for %s/%s", refresh_date, digest_hash)
    return {"trend_summaries": {**summaries, "source": "cache"}, "trend_flags": flags}


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
    table = _table_id()
    if table is None or flags is None or not should_cache(summaries, flags, terms):
        logger.info("trend digest not cached (incomplete or no table)")
        return None
    digest_hash = terms_hash(refresh_date, terms)
    digest = json.dumps({"items": summaries["items"], "flags": flags["flags"]})
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                _write_digest,
                table,
                digest_id=stable_row_id(refresh_date, digest_hash),
                refresh_date=refresh_date,
                terms_hash=digest_hash,
                digest=digest,
                models=_models(),
            ),
            STORE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "trend digest cache write failed (%s): %s",
            type(exc).__name__,
            exc or "timeout",
        )
        return None
    logger.info("trend digest cached for %s/%s", refresh_date, digest_hash)
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
