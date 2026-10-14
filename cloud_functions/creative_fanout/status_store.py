"""The trend table's ``processed_status`` state in BigQuery.

SQL builders plus the statements the orchestrator and worker run against the
target table: select unprocessed rows, mark rows QUEUED, the worker's
compare-and-set QUEUED->PROCESSING lock, the re-read of the locked row, the
worker's fenced PROCESSED / FAILED / requeue writes and the stale-PROCESSING
reaper. Every status change is one row of ``TRANSITIONS``, and every UPDATE
carries that row's from-state predicate.

Rows are matched by trend_scout's MERGE key ``(uuid, target_trend)``
(``RowKey``); ``entry_timestamp`` is only for ordering and logs (and the
one-time match for a legacy worker message without the key).

BigQuery can't parameterize identifiers, and the dataset/table arrive in the
trigger / worker message payload, so identifiers are validated against a config
allow-list and backtick-quoted; every *value* goes through a query parameter.
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from google.cloud import bigquery

from .config import config

logger = logging.getLogger(__name__)

_FINAL_STATUSES = frozenset({"PROCESSED", "FAILED"})
_PROJECT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]*")

# The sensitive-trend predicate (true for a row that is NOT flagged):
# trend_scout's classifier sets `"sensitive": true` in a flagged row's
# trend_context. ``JSON_VALUE`` on a STRING yields NULL for a NULL, empty or
# malformed ``trend_context`` (and the key is absent on non-sensitive rows),
# which ``COALESCE`` treats as not sensitive. (BigQuery rejects the ``SAFE.``
# prefix on ``JSON_VALUE``; it isn't needed.)
_NOT_SENSITIVE_SQL = (
    "COALESCE(JSON_VALUE(trend_context, '$.sensitive'), 'false') != 'true'"
)


@dataclass(frozen=True)
class Transition:
    """One allowed ``processed_status`` change.

    ``guard`` is the from-state predicate the statement's WHERE clause carries:
    every UPDATE checks where the row is now, so a stale writer can never
    overwrite a newer state. ``source`` / ``target`` / ``actor`` document it.
    """

    source: str
    target: str
    actor: str
    guard: str


_FENCE = "processed_status = 'PROCESSING' AND processing_attempts = @attempt"

# The ONLY processed_status transitions (deployment/README.md §3 mirrors this
# table). The lock's new attempt count is the fencing token: the worker's
# later writes match only the attempt it locked, so a worker that outlived the
# reaper (its row re-queued and locked again as attempt n+1) changes nothing.
TRANSITIONS: dict[str, Transition] = {
    "mark_queued": Transition(
        source="NULL or QUEUED",
        target="QUEUED",
        actor="orchestrator, before publishing",
        guard="(processed_status IS NULL OR processed_status = 'QUEUED')",
    ),
    "lock": Transition(
        source="QUEUED with processing_attempts = n",
        target="PROCESSING with processing_attempts = n + 1",
        actor="worker (compare-and-set on n)",
        guard=(
            "processed_status = 'QUEUED' "
            "AND COALESCE(processing_attempts, 0) = @prev_attempt"
        ),
    ),
    "mark_final": Transition(
        source="PROCESSING at the worker's attempt",
        target="PROCESSED or FAILED",
        actor="worker (fenced)",
        guard=_FENCE,
    ),
    "requeue": Transition(
        source="PROCESSING at the worker's attempt",
        target="QUEUED, or FAILED at MAX_PROCESSING_ATTEMPTS",
        actor="worker, transient error before a session existed (fenced)",
        guard=_FENCE,
    ),
    "reap": Transition(
        source="PROCESSING older than REAP_STALE_PROCESSING_MINUTES",
        target="QUEUED, or FAILED at MAX_PROCESSING_ATTEMPTS",
        actor="orchestrator, before selecting",
        guard=(
            "processed_status = 'PROCESSING' AND processing_started_at < "
            "TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @stale_minutes MINUTE)"
        ),
    ),
}


class AmbiguousRowKeyError(RuntimeError):
    """A single-row statement matched more than one row (two rows share the
    key): an error, never a lost lock or a lost fence."""


@dataclass(frozen=True)
class RowKey:
    """One trend row's identity: trend_scout's MERGE key ``(uuid, target_trend)``.

    ``entry_timestamp`` is kept for ordering and logs. It is the match only for
    a *legacy* key (a worker message published before the key was added, in
    flight across the deploy): the worker locks and re-reads such a row by
    timestamp once, then switches to the real key it read.
    """

    uuid: str | None
    target_trend: str | None
    entry_timestamp: object = None

    @classmethod
    def from_mapping(cls, mapping) -> "RowKey":
        """From a worker message's ``row_data`` or a BigQuery row."""
        return cls(
            mapping.get("uuid"),
            mapping.get("target_trend"),
            mapping.get("entry_timestamp"),
        )

    @property
    def legacy(self) -> bool:
        return not (self.uuid and self.target_trend)

    def __str__(self) -> str:
        if self.legacy:
            return f"entry_timestamp={self.entry_timestamp}"
        return f"uuid={self.uuid} target_trend={self.target_trend!r}"

    def log_fields(self) -> dict:
        ts = self.entry_timestamp
        return {
            "uuid": self.uuid,
            "target_trend": self.target_trend,
            "row": ts.isoformat() if isinstance(ts, datetime) else ts,
        }


def _validate_target(dataset, table):
    """Raise ValueError unless dataset/table are on the config allow-lists."""
    if not isinstance(dataset, str) or dataset not in config.ALLOWED_BQ_DATASETS:
        raise ValueError(
            f"BigQuery dataset {dataset!r} not allowed "
            f"(allowed: {sorted(config.ALLOWED_BQ_DATASETS)})"
        )
    if not isinstance(table, str) or table not in config.ALLOWED_BQ_TABLES:
        raise ValueError(
            f"BigQuery table {table!r} not allowed "
            f"(allowed: {sorted(config.ALLOWED_BQ_TABLES)})"
        )


def _table_ref(project, dataset, table):
    """Validated, backtick-quoted `project.dataset.table` reference."""
    if not isinstance(project, str) or not _PROJECT_ID_RE.fullmatch(project):
        raise ValueError(f"Invalid BigQuery project id {project!r}")
    _validate_target(dataset, table)
    return f"`{project}.{dataset}.{table}`"


def _to_utc_datetime(ts):
    """Parse an ISO-8601 timestamp string for a TIMESTAMP query parameter.

    Naive values are treated as UTC, matching the old `TIMESTAMP('<naive>')`
    SQL; aware values are normalized to UTC (same instant, microseconds kept).
    Raises ValueError on anything that isn't a timestamp.
    """
    try:
        dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)
    except TypeError as e:  # e.g. None / int from a malformed payload
        raise ValueError(f"Invalid entry_timestamp {ts!r}: {e}") from e
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _build_select_unprocessed_sql(project, dataset, table, max_rows):
    """Up to ``max_rows`` oldest rows not yet claimed: brand-new (NULL) or
    orphaned in QUEUED.

    Rows whose ``trend_context`` JSON carries ``"sensitive": true`` (set by
    trend_scout's sensitivity classifier) are never selected, so batch runs
    never make ads about them (``_NOT_SENSITIVE_SQL``; the worker re-checks it
    when it re-reads the locked row).

    Returns ``(sql, query_parameters)``.
    """
    sql = f"""
        SELECT * FROM {_table_ref(project, dataset, table)}
        WHERE {TRANSITIONS["mark_queued"].guard}
          AND {_NOT_SENSITIVE_SQL}
        ORDER BY entry_timestamp ASC
        LIMIT @max_rows
    """
    return sql, [bigquery.ScalarQueryParameter("max_rows", "INT64", int(max_rows))]


def _run_query(bq_client, sql, params):
    """Run a parameterized query, wait (bounded) and return its ``RowIterator``.

    ``query_and_wait`` gives up after ``wait_timeout`` (raising
    ``concurrent.futures.TimeoutError`` and trying to cancel the job), and the
    job's ``job_timeout_ms`` bounds it server-side too, so a stuck query fails
    the invocation instead of hanging until the Cloud Run timeout. The
    iterator carries ``num_dml_affected_rows`` for DML (both the fast
    ``jobs.query`` path and the job fallback set it).
    """
    timeout = config.BQ_QUERY_TIMEOUT_SECS
    return bq_client.query_and_wait(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=params, job_timeout_ms=int(timeout * 1000)
        ),
        wait_timeout=timeout,
    )


def select_unprocessed(bq_client, dataset, table, max_rows) -> list:
    """Up to ``max_rows`` unprocessed rows, oldest first (see
    ``_build_select_unprocessed_sql``). A query error is logged and re-raised."""
    select_sql, select_params = _build_select_unprocessed_sql(
        bq_client.project, dataset, table, max_rows
    )
    logging.info(
        f"Selecting up to {max_rows} unprocessed rows "
        "(trends flagged sensitive in trend_context are excluded)."
    )
    try:
        # Iterate the rows directly: the CRF image ships without pandas.
        return list(_run_query(bq_client, select_sql, select_params))
    except Exception as e:
        logging.error(f"Error querying BQ: {e}")
        raise  # Re-raise to signal failure to Pub/Sub


def _key_match(key: RowKey) -> tuple[str, list]:
    """The WHERE fragment (and its parameters) matching one row by key.

    A legacy key (no uuid/target_trend) matches on ``entry_timestamp``.
    """
    if key.legacy:
        return "entry_timestamp = @entry_timestamp", [
            bigquery.ScalarQueryParameter(
                "entry_timestamp", "TIMESTAMP", _to_utc_datetime(key.entry_timestamp)
            )
        ]
    return "uuid = @uuid AND target_trend = @target_trend", [
        bigquery.ScalarQueryParameter("uuid", "STRING", key.uuid),
        bigquery.ScalarQueryParameter("target_trend", "STRING", key.target_trend),
    ]


def _single_row_count(result, what: str, key: RowKey) -> int:
    """``num_dml_affected_rows`` of a single-row statement; > 1 raises."""
    n = result.num_dml_affected_rows or 0
    if n > 1:
        raise AmbiguousRowKeyError(f"{what} for {key} matched {n} rows, expected 1")
    return n


def _build_mark_queued_sql(project, dataset, table, keys):
    """NULL/QUEUED -> QUEUED for the listed rows (``TRANSITIONS['mark_queued']``).

    Rows are matched by ``(uuid, target_trend)``; a key without them (a row
    trend_scout wrote without a uuid) falls back to its ``entry_timestamp``.

    Returns ``(sql, query_parameters)``.
    """
    matches, params = [], []
    real = [k for k in keys if not k.legacy]
    legacy = [k for k in keys if k.legacy]
    if real:
        matches.append("STRUCT(uuid, target_trend) IN UNNEST(@row_keys)")
        params.append(
            bigquery.ArrayQueryParameter(
                "row_keys",
                "STRUCT",
                [
                    bigquery.StructQueryParameter(
                        None,
                        bigquery.ScalarQueryParameter("uuid", "STRING", k.uuid),
                        bigquery.ScalarQueryParameter(
                            "target_trend", "STRING", k.target_trend
                        ),
                    )
                    for k in real
                ],
            )
        )
    if legacy:
        matches.append("entry_timestamp IN UNNEST(@timestamps)")
        params.append(
            bigquery.ArrayQueryParameter(
                "timestamps",
                "TIMESTAMP",
                [_to_utc_datetime(k.entry_timestamp) for k in legacy],
            )
        )
    sql = f"""
        UPDATE {_table_ref(project, dataset, table)}
        SET processed_status = 'QUEUED'
        WHERE ({" OR ".join(matches)})
          AND {TRANSITIONS["mark_queued"].guard}
    """
    return sql, params


def mark_queued(bq_client, dataset, table, keys: list[RowKey]) -> int:
    """Mark the rows about to be published QUEUED; returns the rows changed.

    Only a NULL or QUEUED row changes: a row a worker locked or finished since
    the SELECT keeps its state (its message is then a no-op: the worker's lock
    needs QUEUED). A query error is logged and re-raised.
    """
    if not keys:
        logger.info("No rows to mark QUEUED.")
        return 0
    sql, params = _build_mark_queued_sql(bq_client.project, dataset, table, keys)
    try:
        n = _run_query(bq_client, sql, params).num_dml_affected_rows or 0
    except Exception as e:
        logger.error(f"Failed to mark {len(keys)} row(s) QUEUED: {e}")
        raise
    if n < len(keys):
        logger.warning(
            f"Marked {n}/{len(keys)} rows QUEUED; the others were locked or "
            "finished since the SELECT (their messages will be no-ops)."
        )
    else:
        logger.info(f"Marked {n} row(s) QUEUED.")
    return n


def _build_lock_read_sql(project, dataset, table, key):
    """The row's current status and attempt count, read before the lock.

    Returns ``(sql, query_parameters)``.
    """
    match, params = _key_match(key)
    sql = f"""
        SELECT processed_status, processing_attempts
        FROM {_table_ref(project, dataset, table)}
        WHERE {match}
    """
    return sql, params


def _build_lock_sql(project, dataset, table, key, prev_attempt):
    """Compare-and-set QUEUED -> PROCESSING lock UPDATE for one row
    (``TRANSITIONS['lock']``).

    Sets ``processing_attempts`` to ``prev_attempt + 1`` only while the row is
    still QUEUED with ``prev_attempt`` attempts, so exactly one worker wins and
    knows its attempt number (the fencing token for its later writes). Also
    stamps ``processing_started_at`` so the reaper can age out a worker that
    hard-crashes while holding the lock.

    Returns ``(sql, query_parameters)``.
    """
    match, params = _key_match(key)
    sql = f"""
        UPDATE {_table_ref(project, dataset, table)}
        SET processed_status = 'PROCESSING',
            processing_started_at = CURRENT_TIMESTAMP(),
            processing_attempts = @attempt
        WHERE {match}
          AND {TRANSITIONS["lock"].guard}
    """
    params += [
        bigquery.ScalarQueryParameter("prev_attempt", "INT64", int(prev_attempt)),
        bigquery.ScalarQueryParameter("attempt", "INT64", int(prev_attempt) + 1),
    ]
    return sql, params


def acquire_processing_lock(bq_client, dataset, table, key: RowKey) -> int | None:
    """Lock one row QUEUED -> PROCESSING; returns the new ``processing_attempts``
    (the fencing token), or None when the lock was lost.

    Lost: the row is gone, is not QUEUED (another worker has it, or it is
    PROCESSED / FAILED), or another worker locked it between the read and the
    compare-and-set. A key that matches more than one row raises
    ``AmbiguousRowKeyError``; a BigQuery error is logged and re-raised (the
    message is redelivered).
    """
    project = bq_client.project
    read_sql, read_params = _build_lock_read_sql(project, dataset, table, key)
    try:
        rows = list(_run_query(bq_client, read_sql, read_params))
        if len(rows) > 1:
            raise AmbiguousRowKeyError(
                f"lock read for {key} matched {len(rows)} rows, expected 1"
            )
        if not rows:
            logger.warning(f"Lock failed for row {key}: no such row.")
            return None
        status = rows[0]["processed_status"]
        if status != "QUEUED":
            logger.info(f"Lock failed for row {key}: status is {status}, not QUEUED.")
            return None
        prev_attempt = rows[0]["processing_attempts"] or 0
        lock_sql, lock_params = _build_lock_sql(
            project, dataset, table, key, prev_attempt
        )
        result = _run_query(bq_client, lock_sql, lock_params)
        if _single_row_count(result, "lock", key) != 1:
            logger.info(f"Lock failed for row {key}: another worker locked it first.")
            return None
    except Exception as e:
        logger.error(f"Failed to acquire lock for row {key}: {e}")
        raise
    attempt = prev_attempt + 1
    logger.info(f"Acquired lock for row {key} (attempt {attempt}).")
    return attempt


def _build_read_locked_row_sql(project, dataset, table, key, attempt):
    """The locked row (PROCESSING at ``attempt``) plus ``crf_sensitive``, the
    orchestrator SELECT's sensitive-trend predicate re-evaluated now.

    Returns ``(sql, query_parameters)``.
    """
    match, params = _key_match(key)
    sql = f"""
        SELECT *, NOT ({_NOT_SENSITIVE_SQL}) AS crf_sensitive
        FROM {_table_ref(project, dataset, table)}
        WHERE {match}
          AND {_FENCE}
    """
    params.append(bigquery.ScalarQueryParameter("attempt", "INT64", int(attempt)))
    return sql, params


def read_locked_row(bq_client, dataset, table, key: RowKey, attempt: int):
    """Re-read the row this worker locked, so the run is built from BigQuery,
    not from the message.

    Returns the BigQuery row (with a boolean ``crf_sensitive`` column: the row
    was flagged sensitive after it was dispatched, or the message was never
    the orchestrator's), or None when no row at that key holds this attempt's
    lock. More than one row raises ``AmbiguousRowKeyError``.
    """
    sql, params = _build_read_locked_row_sql(
        bq_client.project, dataset, table, key, attempt
    )
    rows = list(_run_query(bq_client, sql, params))
    if len(rows) > 1:
        raise AmbiguousRowKeyError(
            f"re-read of {key} matched {len(rows)} rows, expected 1"
        )
    return rows[0] if rows else None


def _build_fenced_update_sql(project, dataset, table, key, status, attempt, transition):
    """PROCESSING at ``attempt`` -> ``status`` for one row, guarded by
    ``TRANSITIONS[transition]`` (``mark_final`` or ``requeue``).

    Returns ``(sql, query_parameters)``.
    """
    match, params = _key_match(key)
    sql = f"""
        UPDATE {_table_ref(project, dataset, table)}
        SET processed_status = @status
        WHERE {match}
          AND {TRANSITIONS[transition].guard}
    """
    params += [
        bigquery.ScalarQueryParameter("status", "STRING", status),
        bigquery.ScalarQueryParameter("attempt", "INT64", int(attempt)),
    ]
    return sql, params


def _fenced_update(bq_client, dataset, table, key, status, attempt, transition):
    sql, params = _build_fenced_update_sql(
        bq_client.project, dataset, table, key, status, attempt, transition
    )
    result = _run_query(bq_client, sql, params)
    if _single_row_count(result, transition, key) == 1:
        logger.info(f"Row {key} (attempt {attempt}) -> {status}.")
        return True
    # The row is no longer this worker's: the reaper re-queued it (and maybe a
    # newer attempt holds it), or it was changed by hand. Change nothing.
    logger.warning(
        f"Lost fence: {transition} {status} for row {key} (attempt {attempt}) "
        "matched no row; leaving it as is.",
        extra={
            "json_fields": {
                **key.log_fields(),
                "reason": "lost_fence",
                "status": status,
                "attempt": attempt,
            }
        },
    )
    return False


def mark_final(bq_client, dataset, table, key: RowKey, status: str, attempt: int):
    """PROCESSING at ``attempt`` -> PROCESSED or FAILED (``TRANSITIONS
    ['mark_final']``). Returns whether the row matched (False = lost fence,
    logged, nothing changed). A BigQuery error raises."""
    if status not in _FINAL_STATUSES:
        raise ValueError(f"Invalid final processed_status {status!r}")
    return _fenced_update(bq_client, dataset, table, key, status, attempt, "mark_final")


def requeue(bq_client, dataset, table, key: RowKey, attempt: int) -> str | None:
    """Put a row this worker locked back, after an error before any session
    existed (``TRANSITIONS['requeue']``): QUEUED, or FAILED once ``attempt``
    reached ``MAX_PROCESSING_ATTEMPTS`` (the lock already counted it).

    Returns the status written, or None for a lost fence. A BigQuery error
    raises.
    """
    status = "FAILED" if attempt >= config.MAX_PROCESSING_ATTEMPTS else "QUEUED"
    if _fenced_update(bq_client, dataset, table, key, status, attempt, "requeue"):
        return status
    return None


def _build_reap_sql(project, dataset, table, stale_minutes, max_attempts):
    """Reclaim rows stranded in PROCESSING by a hard-crashed worker.

    A row still legitimately running holds PROCESSING for at most the worker's
    Cloud Run timeout, so only rows older than `stale_minutes` are reaped. Rows
    already attempted `max_attempts` times go FAILED (poison-pill guard); the
    rest go back to QUEUED to be re-dispatched.

    NULL `processing_started_at` is excluded by the `<` comparison (SQL NULL
    semantics) — pre-migration stranded rows are handled by the one-time cleanup
    in the deploy runbook, not by this recurring reaper.

    Returns ``(sql, query_parameters)``.
    """
    sql = f"""
        UPDATE {_table_ref(project, dataset, table)}
        SET processed_status = CASE
                WHEN COALESCE(processing_attempts, 0) >= @max_attempts THEN 'FAILED'
                ELSE 'QUEUED'
            END
        WHERE {TRANSITIONS["reap"].guard}
    """
    params = [
        bigquery.ScalarQueryParameter("max_attempts", "INT64", int(max_attempts)),
        bigquery.ScalarQueryParameter("stale_minutes", "INT64", int(stale_minutes)),
    ]
    return sql, params


def reap_stale_processing_rows(bq_client, dataset, table):
    """Reclaim rows stranded in PROCESSING by a hard-crashed worker.

    Re-queues stale rows under the attempt cap (re-dispatched by the re-query
    that follows) and fails them over it. Returns the number of rows reclaimed.
    Best-effort: a failure here is logged and swallowed so it never crashes the
    orchestrator's normal dispatch.
    """
    sql, params = _build_reap_sql(
        bq_client.project,
        dataset,
        table,
        config.REAP_STALE_PROCESSING_MINUTES,
        config.MAX_PROCESSING_ATTEMPTS,
    )
    try:
        n = _run_query(bq_client, sql, params).num_dml_affected_rows or 0
        if n:
            logging.warning(
                f"Reaped {n} stale PROCESSING row(s) "
                f"(>{config.REAP_STALE_PROCESSING_MINUTES}min): re-queued under "
                f"{config.MAX_PROCESSING_ATTEMPTS} attempts, else FAILED."
            )
        return n
    except Exception as e:
        logging.error(f"Reaper failed (continuing without reaping): {e}")
        return 0
