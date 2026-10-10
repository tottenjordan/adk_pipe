"""Storage for the person-reference consent registry (``person_references``).

One row per ``consent_id`` (random, ``secrets.token_urlsafe``): who registered it
(``owner_user``), the consented photo (``photo_uri``, under
``gs://<bucket>/person-refs/<owner slug>/``), a display ``label``, the ``subject``
(``self`` | ``third_party_with_consent``), the attestations (``adult_attested``,
``allow_public_share``), the ``consent_text_version`` the owner agreed to,
``created_at`` / ``revoked_at`` and ``person_renders`` (the ``gs://`` URIs of
images made with the photo: cast base renders, recorded by the api run path when a
run segment ends, and personalised variants, recorded at upload; deduplicated,
``add_renders``) so a revoke can delete them. ``BigQueryPersonRefsStore`` writes with ``MERGE ... WHEN NOT
MATCHED THEN INSERT`` and revokes with a guarded ``UPDATE``, binding every value
as a query parameter (only the table name, from env, is interpolated).
``InMemoryPersonRefsStore`` mirrors it for local dev and tests
(``PERSON_REFS_STORE=memory``).
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

DEFAULT_TABLE = "person_references"
# Above the router's active-record cap, so the cap can count from list_for.
LIST_LIMIT = 1000

# person_references column types (mirrors deployment/bq_schemas/person_references.json).
PERSON_REF_COLUMN_TYPES = {
    "consent_id": "STRING",
    "owner_user": "STRING",
    "photo_uri": "STRING",
    "label": "STRING",
    "subject": "STRING",
    "adult_attested": "BOOL",
    "allow_public_share": "BOOL",
    "consent_text_version": "STRING",
    "created_at": "TIMESTAMP",
    "revoked_at": "TIMESTAMP",
    "person_renders": "ARRAY<STRING>",
}


class PersonRefsStore(Protocol):
    async def put(self, row: dict) -> None:
        """Record a new consent (a re-put of the same consent_id is a no-op)."""
        ...

    async def get(self, consent_id: str) -> dict | None:
        """One consent by id (any owner, revoked or not), or None."""
        ...

    async def list_for(self, owner: str) -> list[dict]:
        """The owner's active (not revoked) consents, newest first."""
        ...

    async def revoke(self, consent_id: str, owner: str) -> bool:
        """Mark the owner's consent revoked (idempotent; the first ``revoked_at``
        is kept). False when the consent isn't the owner's."""
        ...

    async def active_for(self, consent_id: str, owner: str) -> dict | None:
        """The consent when it exists, is owned by ``owner`` and isn't revoked."""
        ...

    async def add_renders(self, consent_id: str, owner: str, uris: list[str]) -> bool:
        """Append ``uris`` to the owner's consent's ``person_renders`` (deduped;
        revoked consents too, so a retried revoke can delete them). False when
        the consent isn't the owner's."""
        ...


def table_name(env: Mapping[str, str] = os.environ) -> str:
    """Fully-qualified ``project.dataset.table`` from the BQ env vars."""
    return f"{env.get('BQ_PROJECT_ID', '')}.{env.get('BQ_DATASET_ID', '')}." + (
        env.get("BQ_TABLE_PERSON_REFS") or DEFAULT_TABLE
    )


def _param(col: str, value: Any):
    from google.cloud import bigquery

    typ = PERSON_REF_COLUMN_TYPES[col]
    if typ.startswith("ARRAY<"):
        # BigQuery arrays can't be NULL: an absent list binds as [].
        return bigquery.ArrayQueryParameter(col, typ[6:-1], list(value or []))
    return bigquery.ScalarQueryParameter(col, typ, value)


def build_put_sql(table: str, row: Mapping[str, Any]) -> tuple[str, list]:
    """MERGE one consent on ``consent_id`` (pure, insert-only). Every column is a
    parameter."""
    cols = list(PERSON_REF_COLUMN_TYPES)
    missing = [c for c in cols if c not in row]
    if missing:
        raise KeyError(f"person reference row missing columns {missing}")
    params = [_param(c, row[c]) for c in cols]
    select_list = ",\n                ".join(f"@{c} AS {c}" for c in cols)
    sql = f"""
        MERGE `{table}` T
        USING (
            SELECT
                {select_list}
        ) S
        ON T.consent_id = S.consent_id
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


def build_get_sql(table: str, consent_id: str) -> tuple[str, list]:
    sql = f"""
        SELECT * FROM `{table}`
        WHERE consent_id = @consent_id
        LIMIT 1
        """
    return sql, [_param("consent_id", consent_id)]


def build_active_sql(table: str, consent_id: str, owner: str) -> tuple[str, list]:
    sql = f"""
        SELECT * FROM `{table}`
        WHERE consent_id = @consent_id AND owner_user = @owner_user
            AND revoked_at IS NULL
        LIMIT 1
        """
    return sql, [_param("consent_id", consent_id), _param("owner_user", owner)]


def build_revoke_sql(
    table: str, consent_id: str, owner: str, now: dt.datetime
) -> tuple[str, list]:
    sql = f"""
        UPDATE `{table}`
        SET revoked_at = COALESCE(revoked_at, @revoked_at)
        WHERE consent_id = @consent_id AND owner_user = @owner_user
        """
    return sql, [
        _param("consent_id", consent_id),
        _param("owner_user", owner),
        _param("revoked_at", now),
    ]


def build_add_renders_sql(
    table: str, consent_id: str, owner: str, uris: list[str]
) -> tuple[str, list]:
    from google.cloud import bigquery

    sql = f"""
        UPDATE `{table}`
        SET person_renders = ARRAY(
            SELECT DISTINCT uri
            FROM UNNEST(ARRAY_CONCAT(IFNULL(person_renders, []), @uris)) AS uri
        )
        WHERE consent_id = @consent_id AND owner_user = @owner_user
        """
    return sql, [
        _param("consent_id", consent_id),
        _param("owner_user", owner),
        bigquery.ArrayQueryParameter("uris", "STRING", list(uris)),
    ]


def _copy(row: Mapping[str, Any]) -> dict:
    return {**row, "person_renders": list(row.get("person_renders") or [])}


class InMemoryPersonRefsStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def put(self, row: dict) -> None:
        self.rows.setdefault(row["consent_id"], _copy(row))

    async def get(self, consent_id: str) -> dict | None:
        row = self.rows.get(consent_id)
        return _copy(row) if row is not None else None

    async def list_for(self, owner: str) -> list[dict]:
        rows = [
            _copy(r)
            for r in self.rows.values()
            if r["owner_user"] == owner and r.get("revoked_at") is None
        ]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)[:LIST_LIMIT]

    async def revoke(self, consent_id: str, owner: str) -> bool:
        row = self.rows.get(consent_id)
        if row is None or row["owner_user"] != owner:
            return False
        if row.get("revoked_at") is None:
            row["revoked_at"] = utcnow()
        return True

    async def active_for(self, consent_id: str, owner: str) -> dict | None:
        row = self.rows.get(consent_id)
        if row is None or row["owner_user"] != owner or row.get("revoked_at"):
            return None
        return _copy(row)

    async def add_renders(self, consent_id: str, owner: str, uris: list[str]) -> bool:
        row = self.rows.get(consent_id)
        if row is None or row["owner_user"] != owner:
            return False
        renders = row.setdefault("person_renders", [])
        renders.extend(u for u in dict.fromkeys(uris) if u not in renders)
        return True


class BigQueryPersonRefsStore:
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

    async def get(self, consent_id: str) -> dict | None:
        rows = await self._rows(build_get_sql(self.table, consent_id))
        return rows[0] if rows else None

    async def list_for(self, owner: str) -> list[dict]:
        return await self._rows(build_list_sql(self.table, owner))

    async def revoke(self, consent_id: str, owner: str) -> bool:
        sql, params = build_revoke_sql(self.table, consent_id, owner, utcnow())
        job, _ = await asyncio.to_thread(self._job, sql, params)
        return bool(job.num_dml_affected_rows)

    async def active_for(self, consent_id: str, owner: str) -> dict | None:
        rows = await self._rows(build_active_sql(self.table, consent_id, owner))
        return rows[0] if rows else None

    async def add_renders(self, consent_id: str, owner: str, uris: list[str]) -> bool:
        sql, params = build_add_renders_sql(self.table, consent_id, owner, uris)
        job, _ = await asyncio.to_thread(self._job, sql, params)
        return bool(job.num_dml_affected_rows)


def build_store_from_env(env: Mapping[str, str] = os.environ) -> tuple[str, Any]:
    """``(mode, store)`` for ``PERSON_REFS_STORE`` (``bigquery`` default | ``memory``).

    ``bigquery`` without ``BQ_PROJECT_ID``/``BQ_DATASET_ID`` falls back to ``memory``
    with a warning locally, but raises on Cloud Run (``K_SERVICE`` set)."""
    mode = (env.get("PERSON_REFS_STORE") or "bigquery").strip().lower()
    if mode not in ("bigquery", "memory"):
        raise RuntimeError(f"PERSON_REFS_STORE must be bigquery|memory, got {mode!r}")
    if mode == "bigquery":
        missing = [n for n in ("BQ_PROJECT_ID", "BQ_DATASET_ID") if not env.get(n)]
        if missing and env.get("K_SERVICE"):
            # On Cloud Run a silent in-memory fallback would lose every consent
            # record on restart (and with it the owner's ability to revoke).
            raise RuntimeError(
                f"PERSON_REFS_STORE=bigquery needs {', '.join(missing)} on Cloud Run "
                "(set PERSON_REFS_STORE=memory to opt out explicitly)"
            )
        if missing:
            log.warning(
                "person references: %s unset; falling back to PERSON_REFS_STORE=memory",
                ", ".join(missing),
            )
            mode = "memory"
    if mode == "memory":
        return mode, InMemoryPersonRefsStore()
    return mode, BigQueryPersonRefsStore(table_name(env))


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
