"""The trend table's ``processed_status`` state in BigQuery.

SQL builders plus the statements the orchestrator and worker run against the
target table: select unprocessed rows, mark rows QUEUED / PROCESSED / FAILED,
the worker's atomic QUEUED->PROCESSING lock and the stale-PROCESSING reaper.

BigQuery can't parameterize identifiers, and the dataset/table arrive in the
Pub/Sub payload, so identifiers are validated against a config allow-list and
backtick-quoted; every *value* goes through a query parameter.
"""

import logging
import re
from datetime import UTC, datetime

from google.cloud import bigquery

from .config import config

_VALID_STATUSES = frozenset({"QUEUED", "PROCESSING", "PROCESSED", "FAILED"})
_PROJECT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]*")


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


def _build_update_status_sql(project, dataset, table, timestamps, status):
    """Set `processed_status` for the rows whose entry_timestamp is listed.

    Returns ``(sql, query_parameters)``.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(f"Invalid processed_status {status!r}")
    sql = f"""
        UPDATE {_table_ref(project, dataset, table)}
        SET processed_status = @status
        WHERE entry_timestamp IN UNNEST(@timestamps)
    """
    params = [
        bigquery.ScalarQueryParameter("status", "STRING", status),
        bigquery.ArrayQueryParameter(
            "timestamps", "TIMESTAMP", [_to_utc_datetime(t) for t in timestamps]
        ),
    ]
    return sql, params


def _build_select_unprocessed_sql(project, dataset, table, max_rows):
    """Up to ``max_rows`` oldest rows not yet claimed: brand-new (NULL) or
    orphaned in QUEUED.

    Rows whose ``trend_context`` JSON carries ``"sensitive": true`` (set by
    trend_scout's sensitivity classifier) are never selected, so batch runs
    never make ads about them. ``JSON_VALUE`` on a STRING yields NULL for a NULL,
    empty or malformed ``trend_context`` (and the key is absent on non-sensitive
    rows), which ``COALESCE`` treats as not sensitive. (BigQuery rejects the
    ``SAFE.`` prefix on ``JSON_VALUE``; it isn't needed.)

    Returns ``(sql, query_parameters)``.
    """
    sql = f"""
        SELECT * FROM {_table_ref(project, dataset, table)}
        WHERE (processed_status IS NULL OR processed_status = 'QUEUED')
          AND COALESCE(JSON_VALUE(trend_context, '$.sensitive'), 'false') != 'true'
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


def update_rows_status(bq_client, dataset, table, timestamps, status="PROCESSED"):
    """Updates the processing status of multiple rows atomically.

    Raises ValueError for a non-allow-listed dataset/table or unknown status.
    """

    if not timestamps:
        logging.info("No rows to update status for.")
        return

    sql, params = _build_update_status_sql(
        bq_client.project, dataset, table, timestamps, status
    )

    # Execute the update
    try:
        _run_query(bq_client, sql, params)
        logging.info(
            f"Successfully updated status to {status} for {len(timestamps)} rows."
        )
    except Exception as e:
        # Crucial: Log the error if the update fails (like the BQ syntax error)
        logging.error(f"Failed to update rows status to {status}: {e}")
        # Reraise the exception to ensure the function fails and the Pub/Sub message retries
        raise


def _build_lock_sql(project, dataset, table, timestamp):
    """Atomic QUEUED->PROCESSING lock UPDATE for a single row.

    Also stamps `processing_started_at` (so a worker that hard-crashes while
    holding the lock can be aged out by the reaper) and increments
    `processing_attempts` (bounded-retry / poison-pill guard). Gating on
    `processed_status = 'QUEUED'` preserves the exactly-once lock semantics.

    Returns ``(sql, query_parameters)``.
    """
    sql = f"""
        UPDATE {_table_ref(project, dataset, table)}
        SET processed_status = 'PROCESSING',
            processing_started_at = CURRENT_TIMESTAMP(),
            processing_attempts = COALESCE(processing_attempts, 0) + 1
        WHERE
            entry_timestamp = @entry_timestamp
            AND processed_status = 'QUEUED'
    """
    params = [
        bigquery.ScalarQueryParameter(
            "entry_timestamp", "TIMESTAMP", _to_utc_datetime(timestamp)
        )
    ]
    return sql, params


def acquire_processing_lock(bq_client, dataset, table, timestamp):
    """
    Atomically changes status from 'QUEUED' to 'PROCESSING' for a single row.
    Returns True if the lock was acquired (1 row updated), False otherwise.
    """
    lock_sql, lock_params = _build_lock_sql(
        bq_client.project, dataset, table, timestamp
    )

    # Execute the update
    try:
        # Wait (bounded) for the UPDATE and read the rows-affected count
        result = _run_query(bq_client, lock_sql, lock_params)
        rows_updated = result.num_dml_affected_rows

        if rows_updated == 1:
            logging.info(
                f"Successfully acquired lock (status set to PROCESSING) for row {timestamp}."
            )
            return True
        else:
            # If 0 rows were updated, it means another worker was faster
            # and already changed the status (to PROCESSING, PROCESSED, or FAILED).
            logging.info(
                f"Lock failed for row {timestamp}. Status was not 'QUEUED' (rows updated: {rows_updated})."
            )
            return False

    except Exception as e:
        # Crucial: Log the error if the update fails (e.g., BQ error)
        logging.error(f"Failed to acquire lock for row {timestamp}: {e}")
        # Re-raise to ensure the Pub/Sub message retries in case of system error
        raise


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
        WHERE processed_status = 'PROCESSING'
            AND processing_started_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @stale_minutes MINUTE)
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
