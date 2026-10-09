"""Storage for shareable creative links (``creative_shares``).

One row per share ``token`` (random, ``secrets.token_urlsafe``): who shared it
(``owner_user``), from which run (``app_name``, ``session_id``), what (``scope``,
``concept_names``, ``include_eval``), a display ``title`` and ``created_at`` /
``revoked_at``. The owner and session live only here, never in the public
snapshot. ``BigQuerySharesStore`` writes with ``MERGE ... WHEN NOT MATCHED THEN
INSERT`` and revokes with a guarded ``UPDATE``, binding every value as a query
parameter (only the table name, from env, is interpolated).
``InMemorySharesStore`` mirrors it for local dev and tests (``SHARES_STORE=memory``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from agent_common.clients import get_bigquery_client

log = logging.getLogger(__name__)

DEFAULT_TABLE = "creative_shares"
# Above the router's active-share cap, so the cap can count from list_for.
LIST_LIMIT = 1000

# creative_shares column types (mirrors deployment/bq_schemas/creative_shares.json).
SHARE_COLUMN_TYPES = {
    "token": "STRING",
    "owner_user": "STRING",
    "app_name": "STRING",
    "session_id": "STRING",
    "scope": "STRING",
    "concept_names": "ARRAY<STRING>",
    "include_eval": "BOOL",
    "title": "STRING",
    "created_at": "TIMESTAMP",
    "revoked_at": "TIMESTAMP",
}


class SharesStore(Protocol):
    async def put(self, row: dict) -> None:
        """Record a new share (a re-put of the same token is a no-op)."""
        ...

    async def list_for(self, owner: str) -> list[dict]:
        """The owner's active (not revoked) shares, newest first."""
        ...

    async def get(self, token: str) -> dict | None:
        """One share by token (revoked or not), or None."""
        ...

    async def revoke(self, token: str, owner: str) -> bool:
        """Mark the owner's share revoked (idempotent; the first ``revoked_at``
        is kept). False when the token isn't the owner's."""
        ...


def table_name(env: Mapping[str, str] = os.environ) -> str:
    """Fully-qualified ``project.dataset.table`` from the BQ env vars."""
    return f"{env.get('BQ_PROJECT_ID', '')}.{env.get('BQ_DATASET_ID', '')}." + (
        env.get("BQ_TABLE_SHARES") or DEFAULT_TABLE
    )


def _param(col: str, value: Any):
    from google.cloud import bigquery

    typ = SHARE_COLUMN_TYPES[col]
    if typ.startswith("ARRAY<"):
        # BigQuery arrays can't be NULL: an absent list binds as [].
        return bigquery.ArrayQueryParameter(col, typ[6:-1], list(value or []))
    return bigquery.ScalarQueryParameter(col, typ, value)


def build_put_sql(table: str, row: Mapping[str, Any]) -> tuple[str, list]:
    """MERGE one share on ``token`` (pure, insert-only). Every column is a parameter."""
    cols = list(SHARE_COLUMN_TYPES)
    missing = [c for c in cols if c not in row]
    if missing:
        raise KeyError(f"share row missing columns {missing}")
    params = [_param(c, row[c]) for c in cols]
    select_list = ",\n                ".join(f"@{c} AS {c}" for c in cols)
    sql = f"""
        MERGE `{table}` T
        USING (
            SELECT
                {select_list}
        ) S
        ON T.token = S.token
        WHEN NOT MATCHED THEN
            INSERT ({", ".join(cols)})
            VALUES ({", ".join(f"S.{c}" for c in cols)});
        """
    return sql, params


def build_list_sql(table: str, owner: str) -> tuple[str, list]:
    sql = f"""
        SELECT * FROM `{table}`
        WHERE owner_user = @owner_user AND revoked_at IS NULL
        ORDER BY created_at DESC
        LIMIT {LIST_LIMIT}
        """
    return sql, [_param("owner_user", owner)]


def build_get_sql(table: str, token: str) -> tuple[str, list]:
    sql = f"""
        SELECT * FROM `{table}`
        WHERE token = @token
        LIMIT 1
        """
    return sql, [_param("token", token)]


def build_revoke_sql(
    table: str, token: str, owner: str, now: dt.datetime
) -> tuple[str, list]:
    sql = f"""
        UPDATE `{table}`
        SET revoked_at = COALESCE(revoked_at, @revoked_at)
        WHERE token = @token AND owner_user = @owner_user
        """
    return sql, [
        _param("token", token),
        _param("owner_user", owner),
        _param("revoked_at", now),
    ]


def _copy(row: Mapping[str, Any]) -> dict:
    return {**row, "concept_names": list(row.get("concept_names") or [])}


class InMemorySharesStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def put(self, row: dict) -> None:
        self.rows.setdefault(row["token"], _copy(row))

    async def list_for(self, owner: str) -> list[dict]:
        rows = [
            _copy(r)
            for r in self.rows.values()
            if r["owner_user"] == owner and r.get("revoked_at") is None
        ]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)[:LIST_LIMIT]

    async def get(self, token: str) -> dict | None:
        row = self.rows.get(token)
        return _copy(row) if row is not None else None

    async def revoke(self, token: str, owner: str) -> bool:
        row = self.rows.get(token)
        if row is None or row["owner_user"] != owner:
            return False
        if row.get("revoked_at") is None:
            row["revoked_at"] = utcnow()
        return True


class BigQuerySharesStore:
    def __init__(
        self,
        table: str | None = None,
        client_factory: Callable[[], Any] = get_bigquery_client,
    ):
        self.table = table or table_name()
        self._client_factory = client_factory
        self._client: Any = None

    def _job(self, sql: str, params: list):
        from google.cloud import bigquery

        if self._client is None:
            self._client = self._client_factory()
        job = self._client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=params)
        )
        rows = [dict(r.items()) for r in job.result()]
        return job, rows

    async def _rows(self, built: tuple[str, list]) -> list[dict]:
        sql, params = built
        _, rows = await asyncio.to_thread(self._job, sql, params)
        return rows

    async def put(self, row: dict) -> None:
        await self._rows(build_put_sql(self.table, row))

    async def list_for(self, owner: str) -> list[dict]:
        return await self._rows(build_list_sql(self.table, owner))

    async def get(self, token: str) -> dict | None:
        rows = await self._rows(build_get_sql(self.table, token))
        return rows[0] if rows else None

    async def revoke(self, token: str, owner: str) -> bool:
        sql, params = build_revoke_sql(self.table, token, owner, utcnow())
        job, _ = await asyncio.to_thread(self._job, sql, params)
        return bool(job.num_dml_affected_rows)


def build_store_from_env(env: Mapping[str, str] = os.environ) -> tuple[str, Any]:
    """``(mode, store)`` for ``SHARES_STORE`` (``bigquery`` default | ``memory``).

    ``bigquery`` without ``BQ_PROJECT_ID``/``BQ_DATASET_ID`` falls back to ``memory``
    with a warning locally, but raises on Cloud Run (``K_SERVICE`` set)."""
    mode = (env.get("SHARES_STORE") or "bigquery").strip().lower()
    if mode not in ("bigquery", "memory"):
        raise RuntimeError(f"SHARES_STORE must be bigquery|memory, got {mode!r}")
    if mode == "bigquery":
        missing = [n for n in ("BQ_PROJECT_ID", "BQ_DATASET_ID") if not env.get(n)]
        if missing and env.get("K_SERVICE"):
            # On Cloud Run a silent in-memory fallback would lose every share
            # record (and with it the owner's ability to list and revoke).
            raise RuntimeError(
                f"SHARES_STORE=bigquery needs {', '.join(missing)} on Cloud Run "
                "(set SHARES_STORE=memory to opt out explicitly)"
            )
        if missing:
            log.warning(
                "creative shares: %s unset; falling back to SHARES_STORE=memory",
                ", ".join(missing),
            )
            mode = "memory"
    if mode == "memory":
        return mode, InMemorySharesStore()
    return mode, BigQuerySharesStore(table_name(env))


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
