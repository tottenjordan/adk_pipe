"""Storage for human creative ratings (``creative_ratings``, judge calibration).

One row per (session, creative, user): ``rating_id = stable_row_id(session_id,
creative_key, user_id, length=16)``, so re-rating a creative updates the same row.
``BigQueryRatingsStore`` writes with ``MERGE ... WHEN MATCHED THEN UPDATE ... WHEN
NOT MATCHED THEN INSERT`` and binds every value as a query parameter (only the
table name, from env, is interpolated). ``InMemoryRatingsStore`` mirrors it for
local dev and tests (``RATINGS_STORE=memory``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from agent_common.clients import get_bigquery_client
from agent_common.idempotency import stable_row_id

log = logging.getLogger(__name__)

DEFAULT_TABLE = "creative_ratings"
LIST_LIMIT = 10000

# creative_ratings column types (mirrors deployment/create_bq_tables.sh).
RATING_COLUMN_TYPES = {
    "rating_id": "STRING",
    "session_id": "STRING",
    "app_name": "STRING",
    "creative_key": "STRING",
    "kind": "STRING",
    "user_id": "STRING",
    "verdict": "STRING",
    "score": "INT64",
    "note": "STRING",
    "judge_overall": "FLOAT64",
    "judge_passed": "BOOL",
    "judge_gates_passed": "BOOL",
    "judge_model": "STRING",
    # Where the judge fields came from: 'gcs' (the run's report under the configured
    # bucket), 'state' (session state, client-seedable) or 'none' (2026-10-07).
    "judge_source": "STRING",
    # The report's creative_eval JUDGE_VERSION ("" = pre-versioning report or no
    # report) and whether opt-in rating learning steered the run (report
    # learning_used): calibration compares agreement within one judge version and
    # splits it by learning (2026-10-09).
    "judge_version": "STRING",
    "learning_used": "BOOL",
    # Learning context, stamped from session state at PUT time (2026-10-08):
    # normalised brand, canonical visual style, copy tone, brief angle and the
    # rater's allowlisted fail-reason chips (runserver/rating_reasons.py).
    "brand": "STRING",
    "visual_style": "STRING",
    "tone_style": "STRING",
    "angle_id": "STRING",
    "fail_reasons": "ARRAY<STRING>",
    "created_at": "TIMESTAMP",
    "updated_at": "TIMESTAMP",
}
# Rewritten when a user re-rates a creative (the key/identity columns and
# created_at never change).
UPDATABLE = (
    "verdict",
    "score",
    "note",
    "judge_overall",
    "judge_passed",
    "judge_gates_passed",
    "judge_model",
    "judge_source",
    "judge_version",
    "learning_used",
    "brand",
    "visual_style",
    "tone_style",
    "angle_id",
    "fail_reasons",
    "updated_at",
)


def rating_id(session_id: str, creative_key: str, user_id: str) -> str:
    return stable_row_id(session_id, creative_key, user_id, length=16)


class RatingsStore(Protocol):
    async def upsert(self, row: dict) -> None:
        """Insert ``row`` or update the ``UPDATABLE`` columns of its ``rating_id``."""
        ...

    async def list_for_session(self, user_id: str, session_id: str) -> list[dict]:
        """The user's ratings for one session, newest first."""
        ...

    async def list_for_user(self, user_id: str) -> list[dict]:
        """Every rating by the user, newest first (capped at ``LIST_LIMIT``)."""
        ...


def table_name(env: Mapping[str, str] = os.environ) -> str:
    """Fully-qualified ``project.dataset.table`` from the BQ env vars."""
    return f"{env.get('BQ_PROJECT_ID', '')}.{env.get('BQ_DATASET_ID', '')}." + (
        env.get("BQ_TABLE_RATINGS") or DEFAULT_TABLE
    )


def _param(col: str, value: Any):
    from google.cloud import bigquery

    typ = RATING_COLUMN_TYPES[col]
    if typ.startswith("ARRAY<"):
        # BigQuery arrays can't be NULL: an absent list binds as [].
        return bigquery.ArrayQueryParameter(col, typ[6:-1], list(value or []))
    return bigquery.ScalarQueryParameter(col, typ, value)


def build_upsert_sql(table: str, row: Mapping[str, Any]) -> tuple[str, list]:
    """MERGE one rating on ``rating_id`` (pure). Every column is a parameter."""
    cols = list(RATING_COLUMN_TYPES)
    missing = [c for c in cols if c not in row]
    if missing:
        raise KeyError(f"rating row missing columns {missing}")
    params = [_param(c, row[c]) for c in cols]
    select_list = ",\n                ".join(f"@{c} AS {c}" for c in cols)
    sql = f"""
        MERGE `{table}` T
        USING (
            SELECT
                {select_list}
        ) S
        ON T.rating_id = S.rating_id
        WHEN MATCHED THEN
            UPDATE SET {", ".join(f"{c} = S.{c}" for c in UPDATABLE)}
        WHEN NOT MATCHED THEN
            INSERT ({", ".join(cols)})
            VALUES ({", ".join(f"S.{c}" for c in cols)});
        """
    return sql, params


def build_list_session_sql(
    table: str, user_id: str, session_id: str
) -> tuple[str, list]:
    sql = f"""
        SELECT * FROM `{table}`
        WHERE user_id = @user_id AND session_id = @session_id
        ORDER BY updated_at DESC
        LIMIT {LIST_LIMIT}
        """
    return sql, [_param("user_id", user_id), _param("session_id", session_id)]


def build_list_user_sql(table: str, user_id: str) -> tuple[str, list]:
    sql = f"""
        SELECT * FROM `{table}`
        WHERE user_id = @user_id
        ORDER BY updated_at DESC
        LIMIT {LIST_LIMIT}
        """
    return sql, [_param("user_id", user_id)]


def build_calibration_sql(table: str, user_id: str | None = None) -> tuple[str, list]:
    """The columns ``runserver/calibration.py`` reads, for one user or everyone
    (``scripts/eval_calibration.py``)."""
    where = "WHERE user_id = @user_id" if user_id else ""
    sql = f"""
        SELECT session_id, kind, verdict, score, judge_overall, judge_passed,
               judge_gates_passed, judge_source, judge_version, learning_used
        FROM `{table}`
        {where}
        """
    return sql, [_param("user_id", user_id)] if user_id else []


def _newest_first(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: r["updated_at"], reverse=True)


class InMemoryRatingsStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def upsert(self, row: dict) -> None:
        # Like the BigQuery ARRAY column: always a (copied) list, never None.
        row = {**row, "fail_reasons": list(row.get("fail_reasons") or [])}
        existing = self.rows.get(row["rating_id"])
        if existing is None:
            self.rows[row["rating_id"]] = row
        else:
            existing.update({c: row[c] for c in UPDATABLE})

    async def list_for_session(self, user_id: str, session_id: str) -> list[dict]:
        return _newest_first(
            [
                dict(r)
                for r in self.rows.values()
                if r["user_id"] == user_id and r["session_id"] == session_id
            ]
        )[:LIST_LIMIT]

    async def list_for_user(self, user_id: str) -> list[dict]:
        return _newest_first(
            [dict(r) for r in self.rows.values() if r["user_id"] == user_id]
        )[:LIST_LIMIT]


class BigQueryRatingsStore:
    def __init__(
        self,
        table: str | None = None,
        client_factory: Callable[[], Any] = get_bigquery_client,
    ):
        self.table = table or table_name()
        self._client_factory = client_factory
        self._client: Any = None

    def _query(self, sql: str, params: list) -> list[dict]:
        from google.cloud import bigquery

        if self._client is None:
            self._client = self._client_factory()
        job = self._client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=params)
        )
        return [dict(r.items()) for r in job.result()]

    async def _run(self, built: tuple[str, list]) -> list[dict]:
        sql, params = built
        return await asyncio.to_thread(self._query, sql, params)

    async def upsert(self, row: dict) -> None:
        await self._run(build_upsert_sql(self.table, row))

    async def list_for_session(self, user_id: str, session_id: str) -> list[dict]:
        return await self._run(build_list_session_sql(self.table, user_id, session_id))

    async def list_for_user(self, user_id: str) -> list[dict]:
        return await self._run(build_list_user_sql(self.table, user_id))


def build_store_from_env(env: Mapping[str, str] = os.environ) -> tuple[str, Any]:
    """``(mode, store)`` for ``RATINGS_STORE`` (``bigquery`` default | ``memory``).

    ``bigquery`` without ``BQ_PROJECT_ID``/``BQ_DATASET_ID`` falls back to ``memory``
    with a warning locally, but raises on Cloud Run (``K_SERVICE`` set)."""
    mode = (env.get("RATINGS_STORE") or "bigquery").strip().lower()
    if mode not in ("bigquery", "memory"):
        raise RuntimeError(f"RATINGS_STORE must be bigquery|memory, got {mode!r}")
    if mode == "bigquery":
        missing = [n for n in ("BQ_PROJECT_ID", "BQ_DATASET_ID") if not env.get(n)]
        if missing and env.get("K_SERVICE"):
            # On Cloud Run a silent in-memory fallback would lose every rating.
            raise RuntimeError(
                f"RATINGS_STORE=bigquery needs {', '.join(missing)} on Cloud Run "
                "(set RATINGS_STORE=memory to opt out explicitly)"
            )
        if missing:
            log.warning(
                "creative ratings: %s unset; falling back to RATINGS_STORE=memory",
                ", ".join(missing),
            )
            mode = "memory"
    if mode == "memory":
        return mode, InMemoryRatingsStore()
    return mode, BigQueryRatingsStore(table_name(env))


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
