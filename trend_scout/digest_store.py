"""The trend digest cache: pure helpers and BigQuery I/O (no agents, no graph).

``trend_scout.digest`` builds the agents and the ``trend_digest`` Workflow and
calls into this module for everything that touches the cached rows:

- ``terms_of`` / ``parse_refresh_date`` / ``terms_hash``: the cache key of a
  day's trend list (order-, case- and spacing-insensitive).
- ``normalize_draft`` / ``normalize_flags``: ``trend_summaries`` and
  ``trend_flags`` restricted to the raw terms (verbatim, single-line, capped).
- ``coverage`` / ``flags_complete`` / ``should_cache``: a digest is cached (and a
  cached row used) only when the summaries cover ``MIN_CACHE_COVERAGE`` of the
  terms AND the flags cover every term (``FLAG_CACHE_COVERAGE``; a missing
  verdict reads as "not sensitive", so a partial classify is never shared).
- ``build_lookup_sql`` / ``build_insert_sql``: the parameterised SELECT and the
  insert-only MERGE (only the config table interpolated).
- ``lookup_cached_digest`` / ``write_cached_digest``: the bounded, fail-soft
  async reads and writes (a sync BigQuery call in a worker thread).

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

from google.cloud import bigquery

from agent_common import stable_row_id
from agent_common.clients import get_bigquery_client

from .config import config
from .trend_context import FENCE, is_true, norm_term

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


# --- pure helpers -----------------------------------------------------------------


def terms_of(raw_gtrends: Any) -> list[str]:
    """The raw trend terms (verbatim, non-blank strings, first occurrence kept)."""
    if not isinstance(raw_gtrends, (list, tuple)):
        return []
    seen: set[str] = set()
    terms: list[str] = []
    for term in raw_gtrends:
        key = norm_term(term)
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
    keys = sorted({k for k in (norm_term(t) for t in terms) if k})
    return stable_row_id(refresh_date, *keys, length=16)


def _load(value: Any) -> Any:
    """A dict/list as is, or parsed from (fenced) JSON; None when unparseable."""
    if isinstance(value, str):
        try:
            return json.loads(FENCE.sub("", value))
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
        key = norm_term(item.get("term"))
        summary = _clean(item.get("summary"), MAX_SUMMARY_CHARS)
        if key and summary and key not in by_key:
            by_key[key] = summary
    items = [
        {"term": term, "summary": by_key[norm_term(term)]}
        for term in terms
        if norm_term(term) in by_key
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
        key = norm_term(flag.get("term"))
        if key and key not in by_key:
            sensitive = is_true(flag.get("sensitive"))
            reason = _clean(flag.get("reason"), MAX_REASON_CHARS) if sensitive else ""
            by_key[key] = {"sensitive": sensitive, "reason": reason}
    flags = [
        {"term": term, **by_key[norm_term(term)]}
        for term in terms
        if norm_term(term) in by_key
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


async def write_cached_digest(
    terms: Sequence[str],
    refresh_date: str,
    summaries: Mapping[str, Any],
    flags: Mapping[str, Any],
) -> bool:
    """MERGE-insert one digest row; True when written.

    The caller has checked ``should_cache``. Fail-soft: no configured table, a
    timeout or any BigQuery error only logs a warning and returns False.
    """
    table = _table_id()
    if table is None:
        logger.info("trend digest not cached (no table)")
        return False
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
        return False
    logger.info("trend digest cached for %s/%s", refresh_date, digest_hash)
    return True
