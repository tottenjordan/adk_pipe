"""BigQuery rows for the traffic job (contracts §3), plus writers.

Pure row builders and SQL builders (no client), an injectable batched BigQuery
writer, and a JSONL writer for ``--dry-run``. Column-type maps mirror
``deployment/create_bq_tables.sh`` exactly (the ``creative_agent/bq_tools.py``
``EVAL_COLUMN_TYPES`` style); JSON payload columns are serialized to strings.

``bandit_experiments`` is owned by the api; the job only touches ``progress`` and
``updated_at`` with a column-level ``UPDATE`` (never a row rewrite), the same
"only SET what changed" rule as ``runserver/experiments_store.build_upsert_sql``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

EVENT_COLUMN_TYPES: dict[str, str] = {
    "experiment_id": "STRING",
    "episode": "INT64",
    "round": "INT64",
    "batch": "INT64",
    "request_id": "STRING",
    "ts": "TIMESTAMP",
    "policy": "STRING",
    "segment": "STRING",
    "context": "STRING",
    "arm": "STRING",
    "propensity": "FLOAT64",
    "reward": "FLOAT64",
    "clicked": "INT64",
    "dwell_s": "FLOAT64",
    "p_chosen": "FLOAT64",
    "p_optimal": "FLOAT64",
    "optimal_arm": "STRING",
    "regret": "FLOAT64",
    "model_version": "STRING",
    "latency_ms": "FLOAT64",
    "traffic_run": "INT64",
}
EVENT_JSON_COLUMNS = ("context",)

METRICS_COLUMN_TYPES: dict[str, str] = {
    "experiment_id": "STRING",
    "episode": "INT64",
    "policy": "STRING",
    "horizon": "INT64",
    "total_reward": "FLOAT64",
    "total_clicks": "INT64",
    "cumulative_regret": "FLOAT64",
    "pct_optimal": "FLOAT64",
    "steps_to_converge": "INT64",
    "curve": "STRING",
    "arm_share": "STRING",
    "per_segment": "STRING",
    "arm_stats": "STRING",
    "created_at": "TIMESTAMP",
    "traffic_run": "INT64",
    "shift_response": "STRING",
    "regimes": "STRING",
}
METRICS_JSON_COLUMNS = (
    "curve",
    "arm_share",
    "per_segment",
    "arm_stats",
    "shift_response",
    "regimes",
)

DEFAULT_TABLES = {
    "experiments": "bandit_experiments",
    "events": "bandit_events",
    "metrics": "bandit_episode_metrics",
}
INSERT_BATCH_ROWS = 500


def table_names(env: Mapping[str, str] = os.environ) -> dict[str, str]:
    """Fully-qualified ``project.dataset.table`` names from the BQ env vars (the
    same variables and defaults as ``runserver/experiments_store.table_names``)."""
    prefix = f"{env.get('BQ_PROJECT_ID', '')}.{env.get('BQ_DATASET_ID', '')}"
    return {
        "experiments": f"{prefix}."
        + env.get("BQ_TABLE_BANDIT_EXPERIMENTS", DEFAULT_TABLES["experiments"]),
        "events": f"{prefix}."
        + env.get("BQ_TABLE_BANDIT_EVENTS", DEFAULT_TABLES["events"]),
        "metrics": f"{prefix}."
        + env.get("BQ_TABLE_BANDIT_METRICS", DEFAULT_TABLES["metrics"]),
    }


def iso_ts(value: dt.datetime | str) -> str:
    """A TIMESTAMP value for ``insert_rows_json`` (RFC 3339, UTC)."""
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _json_str(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _coerce(col: str, value: Any, types: Mapping[str, str]) -> Any:
    if value is None:
        return None
    kind = types[col]
    if kind == "INT64":
        return int(value)
    if kind == "FLOAT64":
        f = float(value)
        return f if math.isfinite(f) else None
    if kind == "TIMESTAMP":
        return iso_ts(value)
    return str(value)


def build_event_row(
    *,
    experiment_id: str,
    episode: int,
    round: int,
    batch: int,
    request_id: str,
    ts: dt.datetime | str,
    segment: str,
    context: Mapping[str, Any] | str,
    arm: str,
    propensity: float | None,
    reward: float,
    clicked: int,
    dwell_s: float | None,
    p_chosen: float,
    p_optimal: float,
    optimal_arm: str,
    regret: float,
    model_version: str | None,
    latency_ms: float | None,
    policy: str = "linear_ts",
    traffic_run: int = 1,
) -> dict[str, Any]:
    """One ``bandit_events`` row (§3), keys exactly ``EVENT_COLUMN_TYPES``."""
    raw = {
        "experiment_id": experiment_id,
        "episode": episode,
        "round": round,
        "batch": batch,
        "request_id": request_id,
        "ts": ts,
        "policy": policy,
        "segment": segment,
        "context": _json_str(context),
        "arm": arm,
        "propensity": propensity,
        "reward": reward,
        "clicked": clicked,
        "dwell_s": dwell_s,
        "p_chosen": p_chosen,
        "p_optimal": p_optimal,
        "optimal_arm": optimal_arm,
        "regret": regret,
        "model_version": model_version,
        "latency_ms": latency_ms,
        "traffic_run": traffic_run,
    }
    return {c: _coerce(c, raw[c], EVENT_COLUMN_TYPES) for c in EVENT_COLUMN_TYPES}


def build_episode_metrics_row(
    metrics: Mapping[str, Any],
    *,
    experiment_id: str,
    created_at: dt.datetime | str,
    traffic_run: int = 1,
) -> dict[str, Any]:
    """One ``bandit_episode_metrics`` row (§3) from a ``bandit.metrics.episode_metrics``
    dict (plus the optional ``shift_response`` / ``regimes`` payloads of a run
    with shifts, §10). Extra keys (``realized_regret``, ``suboptimal_pulls``, ...)
    are dropped; the JSON payload columns are serialized (absent -> NULL)."""
    raw = dict(metrics)
    raw["experiment_id"] = experiment_id
    raw["created_at"] = created_at
    raw["traffic_run"] = traffic_run
    for col in METRICS_JSON_COLUMNS:
        raw[col] = _json_str(raw.get(col))
    return {
        c: _coerce(c, raw.get(c), METRICS_COLUMN_TYPES) for c in METRICS_COLUMN_TYPES
    }


def metrics_row_id(row: Mapping[str, Any]) -> str:
    """Best-effort streaming dedupe key for a metrics row:
    ``{experiment_id}-r{traffic_run}-e{episode}-{policy}`` (a NULL run counts
    as run 1), so a second traffic run never dedupes against the first."""
    run = row.get("traffic_run") or 1
    return f"{row['experiment_id']}-r{run}-e{row['episode']}-{row['policy']}"


# (name, BigQuery type, value): converted to ScalarQueryParameter by the writer so
# this module stays importable (and testable) without the BigQuery SDK.
QueryParam = tuple[str, str, Any]


def build_progress_update_sql(
    table: str,
    experiment_id: str,
    episodes_done: int,
    episodes_total: int,
    now: dt.datetime | str,
) -> tuple[str, list[QueryParam]]:
    """Column-level UPDATE of ``progress`` + ``updated_at`` only (pure).

    The api's MERGE upserts SET only the columns it changed, and this SETs only
    the two the job owns, so neither side clobbers the other."""
    progress = json.dumps(
        {"episodes_done": int(episodes_done), "episodes_total": int(episodes_total)},
        separators=(",", ":"),
    )
    sql = (
        f"UPDATE `{table}` SET progress = @progress, updated_at = @updated_at "
        "WHERE experiment_id = @experiment_id"
    )
    return sql, [
        ("progress", "STRING", progress),
        ("updated_at", "TIMESTAMP", iso_ts(now)),
        ("experiment_id", "STRING", experiment_id),
    ]


def chunks(rows: Sequence[dict], size: int) -> Iterable[Sequence[dict]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


class RowWriter(Protocol):
    def write_events(self, rows: Sequence[dict]) -> None: ...

    def write_metrics(self, rows: Sequence[dict]) -> None: ...

    def update_progress(
        self, experiment_id: str, episodes_done: int, episodes_total: int
    ) -> None: ...


class BigQueryWriteError(RuntimeError):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def default_job_config(params: list[QueryParam]) -> Any:
    from google.cloud import bigquery

    return bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter(*p) for p in params]
    )


class BigQueryWriter:
    """Batched streaming inserts (``insert_rows_json``) + the progress UPDATE.

    ``client_factory`` returns a ``google.cloud.bigquery.Client``-like object
    (``insert_rows_json``, ``query``); ``job_config_factory`` turns the
    ``(name, type, value)`` params into the ``query(job_config=...)`` argument
    (default: a ``QueryJobConfig`` of ``ScalarQueryParameter``s). Both are
    injectable for tests."""

    def __init__(
        self,
        tables: Mapping[str, str] | None = None,
        *,
        client_factory: Callable[[], Any] | None = None,
        job_config_factory: Callable[[list[QueryParam]], Any] | None = None,
        batch_rows: int = INSERT_BATCH_ROWS,
        attempts: int = 3,
        now: Callable[[], dt.datetime] = _utcnow,
    ):
        self.tables = dict(tables or table_names())
        self._client_factory = client_factory
        self._job_config_factory = job_config_factory or default_job_config
        self._client: Any = None
        self.batch_rows = batch_rows
        self.attempts = attempts
        self.now = now

    def _bq(self) -> Any:
        if self._client is None:
            if self._client_factory is None:
                from google.cloud import bigquery

                self._client = bigquery.Client(
                    project=os.environ.get("BQ_PROJECT_ID") or None
                )
            else:
                self._client = self._client_factory()
        return self._client

    def _insert(self, table: str, rows: Sequence[dict], row_ids: list[str]) -> None:
        for attempt in range(1, self.attempts + 1):
            errors = self._bq().insert_rows_json(table, list(rows), row_ids=row_ids)
            if not errors:
                return
            log.warning(
                "insert into %s: %d row errors (attempt %d/%d): %s",
                table,
                len(errors),
                attempt,
                self.attempts,
                str(errors[:3])[:500],
            )
        raise BigQueryWriteError(f"insert into {table} failed after retries")

    def write_events(self, rows: Sequence[dict]) -> None:
        for part in chunks(rows, self.batch_rows):
            self._insert(
                self.tables["events"], part, [str(r["request_id"]) for r in part]
            )

    def write_metrics(self, rows: Sequence[dict]) -> None:
        for part in chunks(rows, self.batch_rows):
            self._insert(
                self.tables["metrics"], part, [metrics_row_id(r) for r in part]
            )

    def update_progress(
        self, experiment_id: str, episodes_done: int, episodes_total: int
    ) -> None:
        sql, params = build_progress_update_sql(
            self.tables["experiments"],
            experiment_id,
            episodes_done,
            episodes_total,
            self.now(),
        )
        self._bq().query(sql, job_config=self._job_config_factory(params)).result()


class JsonlWriter:
    """``--dry-run``: appends rows to ``events.jsonl`` / ``episode_metrics.jsonl``
    and progress updates (with the SQL that would run) to ``progress.jsonl``."""

    def __init__(
        self,
        out_dir: str | Path,
        tables: Mapping[str, str] | None = None,
        now: Callable[[], dt.datetime] = _utcnow,
    ):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.tables = dict(tables or table_names())
        self.now = now
        self.paths = {
            "events": self.out_dir / "events.jsonl",
            "metrics": self.out_dir / "episode_metrics.jsonl",
            "progress": self.out_dir / "progress.jsonl",
        }
        for p in self.paths.values():
            p.write_text("")

    def _append(self, kind: str, rows: Iterable[dict]) -> None:
        with self.paths[kind].open("a") as fh:
            for row in rows:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")

    def write_events(self, rows: Sequence[dict]) -> None:
        self._append("events", rows)

    def write_metrics(self, rows: Sequence[dict]) -> None:
        self._append("metrics", rows)

    def update_progress(
        self, experiment_id: str, episodes_done: int, episodes_total: int
    ) -> None:
        sql, params = build_progress_update_sql(
            self.tables["experiments"],
            experiment_id,
            episodes_done,
            episodes_total,
            self.now(),
        )
        self._append(
            "progress",
            [{"sql": sql, "params": {name: value for name, _, value in params}}],
        )
