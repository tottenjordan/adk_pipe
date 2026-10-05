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

**Deploy lease** (``deploy_lease_until`` / ``deploy_lease_owner``): only the holder
of an unexpired lease may run a deploy for an experiment, so several api instances
(or an old revision kept alive by a traffic tag) can't each start one. It is taken
with a conditional ``UPDATE ... WHERE lease is NULL or expired`` whose
``num_dml_affected_rows`` says who won (BigQuery serializes concurrent mutating DML
on a table), renewed by the holder's heartbeat and released when the deploy ends.
The lease columns are only ever written by those statements, never by ``upsert``.

**Rolling-deploy safety:** a row fetched from a table migrated by a newer revision
can carry columns this code doesn't know. A partial ``upsert`` drops (and logs once)
unknown columns it isn't writing; only writing an unknown column raises.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import logging
import os
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from google.cloud import bigquery

from agent_common.clients import get_bigquery_client

log = logging.getLogger(__name__)

ACTIVE_STATUSES = ("deploying", "ready", "running_traffic", "stopping")
# The endpoint policy: the only one the traffic job logs to ``bandit_events``.
SERIES_POLICY = "linear_ts"
SERIES_WINDOWS = 20

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
    "scenario_overrides": "STRING",
    # Deploy lease (2026-10-05): written only by the lease UPDATEs, never by upsert.
    "deploy_lease_until": "TIMESTAMP",
    "deploy_lease_owner": "STRING",
    # The endpoint's LinTS discount γ (2026-10-05); written only when < 1 (drift).
    "policy_discount": "FLOAT",
}
JSON_COLUMNS = ("arms", "progress", "scenario_overrides")
# Never rewritten by an update: the key and the creation time.
_IMMUTABLE = ("experiment_id", "created_at")
LIST_LIMIT = 100
_WARNED_UNKNOWN: set[str] = set()


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

    async def acquire_deploy_lease(
        self, experiment_id: str, owner: str, ttl_seconds: int
    ) -> bool:
        """Take the deploy lease of a ``deploying`` row if it is free or expired."""
        ...

    async def renew_deploy_lease(
        self, experiment_id: str, owner: str, ttl_seconds: int
    ) -> bool:
        """Extend ``owner``'s lease; False when ``owner`` no longer holds it."""
        ...

    async def release_deploy_lease(self, experiment_id: str, owner: str) -> None:
        """Clear the lease if ``owner`` still holds it."""
        ...

    async def creative_series_rows(self, experiment_id: str) -> dict[str, list[dict]]:
        """Raw ``bandit_events`` aggregates for contracts §8: ``{"series": ...,
        "segments": ..., "true_ctr": ..., "creative_segments": ...}`` (the four
        builders' row shapes)."""
        ...


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


def _known_columns(row: dict, fields: list[str] | None) -> dict:
    """``row`` minus unknown columns that aren't being written (logged once each).

    A full write (``fields is None``) or an explicitly written unknown column raises
    KeyError; an unknown column merely present in a fetched row (a newer revision
    migrated the table) is dropped, so rolling deploys don't break partial updates."""
    written = set(row if fields is None else fields)
    out = {}
    for col, value in row.items():
        if col in EXPERIMENT_COLUMN_TYPES:
            out[col] = value
        elif col in written:
            raise KeyError(f"unknown bandit_experiments column {col!r}")
        elif col not in _WARNED_UNKNOWN:
            _WARNED_UNKNOWN.add(col)
            log.warning("bandit_experiments: ignoring unknown column %r", col)
    return out


def build_upsert_sql(
    table: str, row: dict, fields: Iterable[str] | None = None
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    """MERGE one experiment row on ``experiment_id`` (pure).

    WHEN NOT MATCHED inserts every (known) column in ``row``; WHEN MATCHED sets only
    ``fields`` (default: every column but the key and ``created_at``). Raises
    KeyError for a written column missing from ``EXPERIMENT_COLUMN_TYPES``; unknown
    columns that aren't written are ignored (``_known_columns``)."""
    fields = None if fields is None else list(fields)
    enc = encode_row(_known_columns(row, fields))
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


def _lease_params(
    experiment_id: str, owner: str, ttl_seconds: int | None = None
) -> list[bigquery.ScalarQueryParameter]:
    params = [
        bigquery.ScalarQueryParameter("experiment_id", "STRING", experiment_id),
        bigquery.ScalarQueryParameter("owner", "STRING", owner),
    ]
    if ttl_seconds is not None:
        params.append(
            bigquery.ScalarQueryParameter("ttl_seconds", "INT64", int(ttl_seconds))
        )
    return params


def build_acquire_lease_sql(
    table: str, experiment_id: str, owner: str, ttl_seconds: int
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    """Conditional lease grab: one affected row means ``owner`` holds it (pure).
    Server-side ``CURRENT_TIMESTAMP()``, so instance clock skew doesn't matter."""
    sql = f"""
        UPDATE `{table}`
        SET deploy_lease_until =
                TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL @ttl_seconds SECOND),
            deploy_lease_owner = @owner
        WHERE experiment_id = @experiment_id
          AND status = @status
          AND (deploy_lease_until IS NULL OR deploy_lease_until < CURRENT_TIMESTAMP())
        """
    params = _lease_params(experiment_id, owner, ttl_seconds)
    params.append(bigquery.ScalarQueryParameter("status", "STRING", "deploying"))
    return sql, params


def build_renew_lease_sql(
    table: str, experiment_id: str, owner: str, ttl_seconds: int
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    sql = f"""
        UPDATE `{table}`
        SET deploy_lease_until =
                TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL @ttl_seconds SECOND)
        WHERE experiment_id = @experiment_id AND deploy_lease_owner = @owner
        """
    return sql, _lease_params(experiment_id, owner, ttl_seconds)


def build_release_lease_sql(
    table: str, experiment_id: str, owner: str
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    sql = f"""
        UPDATE `{table}`
        SET deploy_lease_until = NULL, deploy_lease_owner = NULL
        WHERE experiment_id = @experiment_id AND deploy_lease_owner = @owner
        """
    return sql, _lease_params(experiment_id, owner)


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


def _series_params(
    experiment_id: str, windows: int | None = None
) -> list[bigquery.ScalarQueryParameter]:
    params = [
        bigquery.ScalarQueryParameter("experiment_id", "STRING", experiment_id),
        bigquery.ScalarQueryParameter("policy", "STRING", SERIES_POLICY),
    ]
    if windows is not None:
        params.append(bigquery.ScalarQueryParameter("windows", "INT64", windows))
    return params


def build_creative_series_sql(
    table: str, experiment_id: str, windows: int = SERIES_WINDOWS
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    """Per (arm, episode, window) impressions / clicks over ``bandit_events`` (pure).

    ``horizon = MAX(round) + 1`` and ``nw = LEAST(@windows, horizon)`` equal round
    windows; ``win = DIV(round * nw, horizon)`` (exact integer floor, clamped to
    ``nw - 1``), so window ``w`` is ``[ceil(w*H/nw), ceil((w+1)*H/nw))``. Every row
    carries ``horizon`` and ``n_windows``; the cross-episode means are computed in
    Python (``runserver.experiments_series``)."""
    if windows < 1:
        raise ValueError("windows must be >= 1")
    sql = f"""
        WITH ev AS (
            SELECT arm, episode, round, clicked
            FROM `{table}`
            WHERE experiment_id = @experiment_id AND policy = @policy
        ),
        h AS (
            SELECT MAX(round) + 1 AS horizon,
                   LEAST(@windows, MAX(round) + 1) AS nw
            FROM ev
        )
        SELECT
            ev.arm AS arm,
            ev.episode AS episode,
            LEAST(DIV(ev.round * h.nw, h.horizon), h.nw - 1) AS win,
            COUNT(*) AS impressions,
            SUM(IFNULL(ev.clicked, 0)) AS clicks,
            ANY_VALUE(h.horizon) AS horizon,
            ANY_VALUE(h.nw) AS n_windows
        FROM ev CROSS JOIN h
        GROUP BY arm, episode, win
        ORDER BY arm, episode, win
        """
    return sql, _series_params(experiment_id, windows)


def build_segment_winners_sql(
    table: str, experiment_id: str
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    """``optimal_arm`` frequency per segment (the argmax is picked in Python)."""
    sql = f"""
        SELECT segment, optimal_arm, COUNT(*) AS n
        FROM `{table}`
        WHERE experiment_id = @experiment_id AND policy = @policy
        GROUP BY segment, optimal_arm
        """
    return sql, _series_params(experiment_id)


def build_true_ctr_sql(
    table: str, experiment_id: str
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    """Simulator-truth CTR per arm: the mean ``p_chosen`` when it was chosen."""
    sql = f"""
        SELECT arm, AVG(p_chosen) AS true_ctr
        FROM `{table}`
        WHERE experiment_id = @experiment_id AND policy = @policy
        GROUP BY arm
        """
    return sql, _series_params(experiment_id)


def build_creative_segments_sql(
    table: str, experiment_id: str
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    """Per (arm, segment) totals over all episodes (pure).

    Sums rather than means, so Python can derive per-segment CTR / true CTR and
    per-creative missed clicks / engaged seconds: ``p_sum / p_n`` is the mean
    ``p_chosen`` (``COUNT`` skips NULLs), ``regret_sum`` is divided by the episode
    count, ``dwell_sum`` by impressions. ``clicked`` is INT64 0/1, so ``SUM``."""
    sql = f"""
        SELECT
            arm,
            segment,
            COUNT(*) AS impressions,
            SUM(IFNULL(clicked, 0)) AS clicks,
            SUM(p_chosen) AS p_sum,
            COUNT(p_chosen) AS p_n,
            SUM(IFNULL(regret, 0)) AS regret_sum,
            SUM(IFNULL(dwell_s, 0)) AS dwell_sum
        FROM `{table}`
        WHERE experiment_id = @experiment_id AND policy = @policy
        GROUP BY arm, segment
        ORDER BY arm, segment
        """
    return sql, _series_params(experiment_id)


def series_rows_from_events(
    events: Iterable[dict], windows: int = SERIES_WINDOWS
) -> dict[str, list[dict]]:
    """The four §8 queries evaluated over in-memory ``bandit_events`` rows (same
    semantics as the SQL builders; used by ``InMemoryExperimentStore``)."""
    evs = [e for e in events if e.get("policy") == SERIES_POLICY]
    if not evs:
        return {"series": [], "segments": [], "true_ctr": [], "creative_segments": []}
    horizon = max(int(e["round"]) for e in evs) + 1
    nw = min(windows, horizon)
    cells: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    seg: Counter = Counter()
    p_sum: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
    by_seg: dict[tuple, dict[str, Any]] = defaultdict(
        lambda: {
            "impressions": 0,
            "clicks": 0,
            "p_sum": None,  # SQL SUM over all-NULL p_chosen is NULL
            "p_n": 0,
            "regret_sum": 0.0,
            "dwell_sum": 0.0,
        }
    )
    for e in evs:
        w = min(int(e["round"]) * nw // horizon, nw - 1)
        cell = cells[(e["arm"], int(e["episode"]), w)]
        cell[0] += 1
        cell[1] += int(e.get("clicked") or 0)
        seg[(e.get("segment"), e.get("optimal_arm"))] += 1
        if e.get("p_chosen") is not None:
            acc = p_sum[e["arm"]]
            acc[0] += float(e["p_chosen"])
            acc[1] += 1
        cs = by_seg[(e["arm"], e.get("segment"))]
        cs["impressions"] += 1
        cs["clicks"] += int(e.get("clicked") or 0)
        if e.get("p_chosen") is not None:
            cs["p_sum"] = (cs["p_sum"] or 0.0) + float(e["p_chosen"])
            cs["p_n"] += 1
        cs["regret_sum"] += float(e.get("regret") or 0.0)
        cs["dwell_sum"] += float(e.get("dwell_s") or 0.0)
    return {
        "series": [
            {
                "arm": arm,
                "episode": ep,
                "win": w,
                "impressions": imps,
                "clicks": clicks,
                "horizon": horizon,
                "n_windows": nw,
            }
            for (arm, ep, w), (imps, clicks) in sorted(cells.items())
        ],
        "segments": [
            {"segment": s, "optimal_arm": a, "n": n} for (s, a), n in seg.items()
        ],
        "true_ctr": [
            {"arm": arm, "true_ctr": total / n if n else None}
            for arm, (total, n) in p_sum.items()
        ],
        "creative_segments": [
            {"arm": arm, "segment": segment, **cell}
            for (arm, segment), cell in by_seg.items()
        ],
    }


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

    def _dml(self, sql: str, params: list) -> int:
        if self._client is None:
            self._client = self._client_factory()
        job = self._client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=params)
        )
        job.result()
        return int(job.num_dml_affected_rows or 0)

    async def _lease_dml(self, built: tuple[str, list]) -> int | None:
        """Affected rows, or None when the table predates the lease columns (the
        lease then degrades to the old unguarded behaviour, loudly)."""
        from google.api_core import exceptions as gexc

        sql, params = built
        try:
            return await asyncio.to_thread(self._dml, sql, params)
        except gexc.BadRequest as exc:
            if "deploy_lease" not in str(exc):
                raise
            log.error(
                "bandit_experiments has no deploy_lease_* columns: deploy lease "
                "disabled until the migration runs (deployment/README.md)"
            )
            return None

    async def acquire_deploy_lease(
        self, experiment_id: str, owner: str, ttl_seconds: int
    ) -> bool:
        table = self.tables["experiments"]
        n = await self._lease_dml(
            build_acquire_lease_sql(table, experiment_id, owner, ttl_seconds)
        )
        return n is None or n > 0

    async def renew_deploy_lease(
        self, experiment_id: str, owner: str, ttl_seconds: int
    ) -> bool:
        table = self.tables["experiments"]
        n = await self._lease_dml(
            build_renew_lease_sql(table, experiment_id, owner, ttl_seconds)
        )
        return n is None or n > 0

    async def release_deploy_lease(self, experiment_id: str, owner: str) -> None:
        table = self.tables["experiments"]
        await self._lease_dml(build_release_lease_sql(table, experiment_id, owner))

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

    async def creative_series_rows(self, experiment_id: str) -> dict[str, list[dict]]:
        table = self.tables["events"]
        series, segments, true_ctr, creative_segments = await asyncio.gather(
            self._run(build_creative_series_sql(table, experiment_id)),
            self._run(build_segment_winners_sql(table, experiment_id)),
            self._run(build_true_ctr_sql(table, experiment_id)),
            self._run(build_creative_segments_sql(table, experiment_id)),
        )
        return {
            "series": series,
            "segments": segments,
            "true_ctr": true_ctr,
            "creative_segments": creative_segments,
        }


class InMemoryExperimentStore:
    """Process-local store for tests and ``BANDIT_DEPLOY_MODE=fake`` local dev."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.metrics: dict[str, list[dict]] = {}
        self.events: dict[str, list[dict]] = {}

    def add_events(self, experiment_id: str, events: Iterable[dict]) -> None:
        """Append ``bandit_events`` rows (contracts §3 columns) for tests/local dev."""
        self.events.setdefault(experiment_id, []).extend(
            copy.deepcopy(e) for e in events
        )

    async def upsert(self, row: dict, fields: Iterable[str] | None = None) -> None:
        fields = None if fields is None else list(fields)
        row = _known_columns(row, fields)  # same KeyError contract as the SQL builder
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

    # Lease: same semantics as the BigQuery UPDATEs; atomic because there is no
    # await between the check and the write.
    async def acquire_deploy_lease(
        self, experiment_id: str, owner: str, ttl_seconds: int
    ) -> bool:
        row = self.rows.get(experiment_id)
        if row is None or row.get("status") != "deploying":
            return False
        now = dt.datetime.now(dt.UTC)
        until = row.get("deploy_lease_until")
        if until is not None and until >= now:
            return False
        row["deploy_lease_until"] = now + dt.timedelta(seconds=ttl_seconds)
        row["deploy_lease_owner"] = owner
        return True

    async def renew_deploy_lease(
        self, experiment_id: str, owner: str, ttl_seconds: int
    ) -> bool:
        row = self.rows.get(experiment_id)
        if row is None or row.get("deploy_lease_owner") != owner:
            return False
        row["deploy_lease_until"] = dt.datetime.now(dt.UTC) + dt.timedelta(
            seconds=ttl_seconds
        )
        return True

    async def release_deploy_lease(self, experiment_id: str, owner: str) -> None:
        row = self.rows.get(experiment_id)
        if row is not None and row.get("deploy_lease_owner") == owner:
            row["deploy_lease_until"] = None
            row["deploy_lease_owner"] = None

    async def creative_series_rows(self, experiment_id: str) -> dict[str, list[dict]]:
        return series_rows_from_events(self.events.get(experiment_id, []))
