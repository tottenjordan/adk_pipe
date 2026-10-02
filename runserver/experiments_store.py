"""Persistence for bandit experiments (``bandit_experiments`` / ``bandit_episode_metrics``).

Rows are plain dicts keyed by the contracts §3 column names. In Python the JSON
columns are objects (``arms``: list of §5 arm dicts, ``progress``: dict or None) and
timestamps are tz-aware ``datetime``s; ``BigQueryExperimentStore`` encodes/decodes
them at the boundary.

``BigQueryExperimentStore`` upserts with ``MERGE`` on ``experiment_id``, binding every
value as a typed ``@named`` parameter (the ``creative_agent/bq_tools.py`` pattern).
An update only SETs the columns the caller changed (``fields``), so the api never
clobbers ``progress`` written concurrently by the traffic job. The SQL builders are
pure; the blocking client calls run in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import os
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from google.cloud import bigquery

from agent_common.clients import get_bigquery_client

ACTIVE_STATUSES = ("deploying", "ready", "running_traffic", "stopping")

# bandit_experiments column types (mirrors deployment/create_bq_tables.sh).
EXPERIMENT_COLUMN_TYPES = {
    "experiment_id": "STRING",
    "user_id": "STRING",
    "session_id": "STRING",
    "app_name": "STRING",
    "created_at": "TIMESTAMP",
    "updated_at": "TIMESTAMP",
    "status": "STRING",
    "scenario": "STRING",
    "ctr_mode": "STRING",
    "reward_mode": "STRING",
    "arms": "STRING",
    "config_uri": "STRING",
    "model_resource": "STRING",
    "endpoint_id": "STRING",
    "deployed_model_id": "STRING",
    "ttl_expires_at": "TIMESTAMP",
    "stopped_at": "TIMESTAMP",
    "traffic_execution": "STRING",
    "progress": "STRING",
    "error": "STRING",
}
JSON_COLUMNS = ("arms", "progress")
# Never rewritten by an update: the key and the creation time.
_IMMUTABLE = ("experiment_id", "created_at")
LIST_LIMIT = 100


class ExperimentStore(Protocol):
    async def upsert(self, row: dict, fields: Iterable[str] | None = None) -> None:
        """Insert ``row``, or (when it exists) SET only ``fields`` (default: all)."""
        ...

    async def get(self, experiment_id: str) -> dict | None: ...

    async def list_for_user(self, user_id: str) -> list[dict]:
        """The user's experiments, newest first."""
        ...

    async def list_active(self) -> list[dict]:
        """Every experiment in an ``ACTIVE_STATUSES`` status (the reaper's scan)."""
        ...

    async def metrics_rows(self, experiment_id: str) -> list[dict]: ...


def table_names(env=os.environ) -> dict[str, str]:
    """Fully-qualified ``project.dataset.table`` names from the BQ env vars."""
    prefix = f"{env.get('BQ_PROJECT_ID', '')}.{env.get('BQ_DATASET_ID', '')}"
    return {
        "experiments": f"{prefix}."
        + env.get("BQ_TABLE_BANDIT_EXPERIMENTS", "bandit_experiments"),
        "events": f"{prefix}." + env.get("BQ_TABLE_BANDIT_EVENTS", "bandit_events"),
        "metrics": f"{prefix}."
        + env.get("BQ_TABLE_BANDIT_METRICS", "bandit_episode_metrics"),
    }


def encode_row(row: dict) -> dict:
    """Python row -> BigQuery-typed values (JSON columns serialized)."""
    out = {}
    for col, value in row.items():
        if col in JSON_COLUMNS and value is not None and not isinstance(value, str):
            value = json.dumps(value)
        out[col] = value
    return out


def decode_row(row: dict) -> dict:
    """BigQuery row -> Python row (JSON columns parsed)."""
    out = dict(row)
    for col in JSON_COLUMNS:
        value = out.get(col)
        if isinstance(value, str) and value:
            try:
                out[col] = json.loads(value)
            except ValueError:
                out[col] = None
        elif value == "":
            out[col] = None
    if out.get("arms") is None:
        out["arms"] = []
    return out


def _param(col: str, value: Any) -> bigquery.ScalarQueryParameter:
    bq_type = EXPERIMENT_COLUMN_TYPES[col]
    if bq_type == "TIMESTAMP" and isinstance(value, str):
        value = dt.datetime.fromisoformat(value)
    return bigquery.ScalarQueryParameter(col, bq_type, value)


def build_upsert_sql(
    table: str, row: dict, fields: Iterable[str] | None = None
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    """MERGE one experiment row on ``experiment_id`` (pure).

    WHEN NOT MATCHED inserts every column in ``row``; WHEN MATCHED sets only
    ``fields`` (default: every column but the key and ``created_at``). Raises
    KeyError for a column missing from ``EXPERIMENT_COLUMN_TYPES``."""
    enc = encode_row(row)
    cols = list(enc)
    params = [_param(c, enc[c]) for c in cols]
    update = [c for c in (cols if fields is None else fields) if c not in _IMMUTABLE]
    for c in update:
        if c not in enc:
            raise KeyError(f"update field {c!r} not in row")
    select_list = ",\n                ".join(f"@{c} AS {c}" for c in cols)
    matched = (
        "WHEN MATCHED THEN\n            UPDATE SET "
        + ", ".join(f"{c} = S.{c}" for c in update)
        if update
        else ""
    )
    sql = f"""
        MERGE `{table}` T
        USING (
            SELECT
                {select_list}
        ) S
        ON T.experiment_id = S.experiment_id
        {matched}
        WHEN NOT MATCHED THEN
            INSERT ({", ".join(cols)})
            VALUES ({", ".join(f"S.{c}" for c in cols)});
        """
    return sql, params


def build_get_sql(
    table: str, experiment_id: str
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    sql = f"SELECT * FROM `{table}` WHERE experiment_id = @experiment_id LIMIT 1"
    return sql, [
        bigquery.ScalarQueryParameter("experiment_id", "STRING", experiment_id)
    ]


def build_list_sql(
    table: str, user_id: str
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    sql = (
        f"SELECT * FROM `{table}` WHERE user_id = @user_id "
        f"ORDER BY created_at DESC LIMIT {LIST_LIMIT}"
    )
    return sql, [bigquery.ScalarQueryParameter("user_id", "STRING", user_id)]


def build_list_active_sql(
    table: str,
) -> tuple[str, list[bigquery.ArrayQueryParameter]]:
    sql = f"SELECT * FROM `{table}` WHERE status IN UNNEST(@statuses)"
    return sql, [
        bigquery.ArrayQueryParameter("statuses", "STRING", list(ACTIVE_STATUSES))
    ]


def build_metrics_sql(
    table: str, experiment_id: str
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    sql = (
        f"SELECT * FROM `{table}` WHERE experiment_id = @experiment_id "
        "ORDER BY episode, policy"
    )
    return sql, [
        bigquery.ScalarQueryParameter("experiment_id", "STRING", experiment_id)
    ]


class BigQueryExperimentStore:
    def __init__(
        self,
        tables: dict[str, str] | None = None,
        client_factory: Callable[[], Any] = get_bigquery_client,
    ):
        self.tables = tables or table_names()
        self._client_factory = client_factory
        self._client = None

    def _query(self, sql: str, params: list) -> list[dict]:
        if self._client is None:
            self._client = self._client_factory()
        job = self._client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=params)
        )
        return [dict(r.items()) for r in job.result()]

    async def _run(self, built: tuple[str, list]) -> list[dict]:
        sql, params = built
        return await asyncio.to_thread(self._query, sql, params)

    async def upsert(self, row: dict, fields: Iterable[str] | None = None) -> None:
        await self._run(build_upsert_sql(self.tables["experiments"], row, fields))

    async def get(self, experiment_id: str) -> dict | None:
        rows = await self._run(build_get_sql(self.tables["experiments"], experiment_id))
        return decode_row(rows[0]) if rows else None

    async def list_for_user(self, user_id: str) -> list[dict]:
        rows = await self._run(build_list_sql(self.tables["experiments"], user_id))
        return [decode_row(r) for r in rows]

    async def list_active(self) -> list[dict]:
        rows = await self._run(build_list_active_sql(self.tables["experiments"]))
        return [decode_row(r) for r in rows]

    async def metrics_rows(self, experiment_id: str) -> list[dict]:
        return await self._run(build_metrics_sql(self.tables["metrics"], experiment_id))


class InMemoryExperimentStore:
    """Process-local store for tests and ``BANDIT_DEPLOY_MODE=fake`` local dev."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.metrics: dict[str, list[dict]] = {}

    async def upsert(self, row: dict, fields: Iterable[str] | None = None) -> None:
        for col in row:
            EXPERIMENT_COLUMN_TYPES[col]  # same KeyError contract as the SQL builder
        current = self.rows.get(row["experiment_id"])
        if current is None:
            self.rows[row["experiment_id"]] = copy.deepcopy(row)
            return
        for col in row if fields is None else fields:
            if col not in _IMMUTABLE:
                current[col] = copy.deepcopy(row[col])

    async def get(self, experiment_id: str) -> dict | None:
        row = self.rows.get(experiment_id)
        return copy.deepcopy(row) if row else None

    async def list_for_user(self, user_id: str) -> list[dict]:
        rows = [copy.deepcopy(r) for r in self.rows.values() if r["user_id"] == user_id]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows[:LIST_LIMIT]

    async def list_active(self) -> list[dict]:
        return [
            copy.deepcopy(r)
            for r in self.rows.values()
            if r["status"] in ACTIVE_STATUSES
        ]

    async def metrics_rows(self, experiment_id: str) -> list[dict]:
        return copy.deepcopy(self.metrics.get(experiment_id, []))
