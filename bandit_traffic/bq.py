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
import random
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from bandit_traffic.endpoint_client import call_with_retries

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
    *,
    rewards_rejected: int = 0,
) -> tuple[str, list[QueryParam]]:
    """Column-level UPDATE of ``progress`` + ``updated_at`` only (pure).

    The api's MERGE upserts SET only the columns it changed, and this SETs only
    the two the job owns, so neither side clobbers the other. ``progress`` also
    carries ``rewards_rejected`` (the run's rewards the endpoint answered
    ``accepted: false``, e.g. ``unknown request_id`` after a mid-run endpoint
    restart), so that case is visible on the row."""
    progress = json.dumps(
        {
            "episodes_done": int(episodes_done),
            "episodes_total": int(episodes_total),
            "rewards_rejected": int(rewards_rejected),
        },
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
        self,
        experiment_id: str,
        episodes_done: int,
        episodes_total: int,
        *,
        rewards_rejected: int = 0,
    ) -> None: ...


class BigQueryWriteError(RuntimeError):
    pass


#: Bounds on each streaming insert / progress UPDATE wait (the client defaults
#: are ``None``, i.e. no bound), so a hung call fails instead of eating the
#: job's 1 h task timeout.
INSERT_TIMEOUT_SECONDS = 30.0
QUERY_TIMEOUT_SECONDS = 60.0
#: First backoff (seconds) between insert retries of the failed rows, doubled
#: per attempt with jitter (each sleep is 50-100% of the nominal delay).
INSERT_RETRY_BASE_DELAY = 1.0
#: First backoff between progress UPDATE attempts (same doubling and jitter).
PROGRESS_RETRY_BASE_DELAY = 1.0

#: insertAll row-error reasons a retry cannot fix (schema mismatch, bad value,
#: missing table, no access). ``stopped`` marks the request's other rows, which
#: were rejected only because of an ``invalid`` one.
PERMANENT_INSERT_REASONS = frozenset(
    {"invalid", "invalidQuery", "notFound", "accessDenied"}
)


def is_permanent_insert_error(errors: Sequence[Any]) -> bool:
    """True when ``insert_rows_json``'s row errors include a reason a retry
    can't fix (``PERMANENT_INSERT_REASONS``); plain-string reasons count too."""
    for entry in errors:
        details = entry.get("errors", []) if isinstance(entry, Mapping) else []
        for err in details:
            reason = err.get("reason") if isinstance(err, Mapping) else err
            if reason in PERMANENT_INSERT_REASONS:
                return True
    return False


def failed_row_indices(errors: Sequence[Any], n: int) -> list[int] | None:
    """The positions (in the request just sent) of the rows ``errors`` names,
    sorted; ``None`` when an entry has no usable ``index`` (then the caller
    re-sends the whole request)."""
    out: set[int] = set()
    for entry in errors:
        index = entry.get("index") if isinstance(entry, Mapping) else None
        if isinstance(index, bool) or not isinstance(index, int):
            return None
        if not 0 <= index < n:
            return None
        out.add(index)
    return sorted(out)


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
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.tables = dict(tables or table_names())
        self._client_factory = client_factory
        self._job_config_factory = job_config_factory or default_job_config
        self._client: Any = None
        self.batch_rows = batch_rows
        self.attempts = attempts
        self.now = now
        self.sleep = sleep

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
        """Stream ``rows``; on transient row errors, retry only the rows the
        errors name (by index, with their own insert ids) after a jittered
        exponential backoff. The other rows were inserted, and re-sending them
        would lean on best-effort ``insertId`` dedupe (the series SQL counts
        rows without deduping)."""
        rows, row_ids = list(rows), list(row_ids)
        for attempt in range(1, self.attempts + 1):
            errors = self._bq().insert_rows_json(
                table, rows, row_ids=row_ids, timeout=INSERT_TIMEOUT_SECONDS
            )
            if not errors:
                return
            if is_permanent_insert_error(errors):
                log.error(
                    "insert into %s: %d row errors are permanent (schema mismatch "
                    "or bad value; none of the rows are inserted), not retrying: %s",
                    table,
                    len(errors),
                    str(errors[:3])[:500],
                )
                raise BigQueryWriteError(
                    f"insert into {table} failed with permanent row errors"
                )
            log.warning(
                "insert into %s: %d row errors (attempt %d/%d): %s",
                table,
                len(errors),
                attempt,
                self.attempts,
                str(errors[:3])[:500],
            )
            if attempt == self.attempts:
                break
            failed = failed_row_indices(errors, len(rows))
            if failed is not None:
                rows = [rows[i] for i in failed]
                row_ids = [row_ids[i] for i in failed]
            delay = INSERT_RETRY_BASE_DELAY * 2 ** (attempt - 1)
            self.sleep(delay * (0.5 + random.random() / 2))
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
        self,
        experiment_id: str,
        episodes_done: int,
        episodes_total: int,
        *,
        rewards_rejected: int = 0,
    ) -> None:
        """The progress UPDATE, retried with backoff on any error (5xx,
        timeouts, a serialization conflict with the api's MERGE on the same
        row). Progress is advisory and the next episode rewrites it, so a final
        failure is a warning, never fatal."""
        sql, params = build_progress_update_sql(
            self.tables["experiments"],
            experiment_id,
            episodes_done,
            episodes_total,
            self.now(),
            rewards_rejected=rewards_rejected,
        )

        def run() -> None:
            self._bq().query(sql, job_config=self._job_config_factory(params)).result(
                timeout=QUERY_TIMEOUT_SECONDS
            )

        try:
            call_with_retries(
                run,
                attempts=self.attempts,
                base_delay=PROGRESS_RETRY_BASE_DELAY,
                sleep=self.sleep,
                what="progress update",
                retryable=lambda exc: True,
            )
        except Exception as exc:
            log.warning(
                "progress update failed after %d attempts (%s: %s); continuing, "
                "the next episode rewrites it",
                self.attempts,
                type(exc).__name__,
                str(exc)[:300],
            )


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
        self,
        experiment_id: str,
        episodes_done: int,
        episodes_total: int,
        *,
        rewards_rejected: int = 0,
    ) -> None:
        sql, params = build_progress_update_sql(
            self.tables["experiments"],
            experiment_id,
            episodes_done,
            episodes_total,
            self.now(),
            rewards_rejected=rewards_rejected,
        )
        self._append(
            "progress",
            [{"sql": sql, "params": {name: value for name, _, value in params}}],
        )
