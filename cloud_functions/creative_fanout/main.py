"""Offline, batch workflow for trend insights and candidate ad creatives

This file contains two separate Cloud Run Function deployments:

1. Agent Orchestrator Deployment: executes the `crf_entrypoint` function
2. Agent Worker Deployment: executes the `agent_worker_entrypoint` function

Why?
* When deploying a service triggered by a Pub/Sub topic, you must
  specify exactly one entry point function to be executed when a message
  arrives on that topic
* Therefore, you must deploy the code twice, with each deployment
  configured to listen to its unique trigger topic and execute the
  appropriate handler function.

Objectives:
1. Agent Orchestrator: Checks the `trend_scout` agent's
   recommendations in the `BQ_PROJECT_ID.BQ_DATASET_ID.BQ_TABLE_TARGETS`
   BQ table and dispatches a PubSub message for each new row in the table
2. Agent Worker: Processes a single PubSub message dispatched by the
   Orchestrator, invoking the Agent Engine Runtime to generate
   ad copy and creatives for a single (trend + campaign) pair (i.e., a
   single row in the BigQuery table)


Example PubSub msg format:
message = {
    "bq_dataset": "trend_trawler",
    "bq_table": "target_trends_crf",
    "agent_resource_id": "<AGENT_ENGINE_ID>",
    "max_rows": 1,  # optional; clamped to CRF_MAX_ROWS_PER_RUN (default 3)
}
"""

import asyncio
import base64
import inspect
import json
import logging
import re
import sys
import threading
import time
from datetime import UTC, date, datetime

import agentplatform
import functions_framework
from cloudevents.http import CloudEvent
from google.cloud import bigquery, pubsub_v1

from .completion import RunMonitor, RunOutcome
from .config import config
from .session import agent_session


# --- logging ---
class CloudRunJsonFormatter(logging.Formatter):
    """One JSON object per line with ``severity`` + ``message`` (+ ``time``).

    Cloud Run parses JSON lines on stdout/stderr into structured log entries:
    ``severity`` becomes the entry's level and ``message`` its text
    (https://docs.cloud.google.com/run/docs/logging). A record's optional
    ``json_fields`` dict (``logger.info(..., extra={"json_fields": {...}})``)
    is merged in as extra top-level keys (queryable as ``jsonPayload.<key>``);
    it never overrides ``severity`` / ``message`` / ``time``.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"
        entry = {}
        fields = getattr(record, "json_fields", None)
        if isinstance(fields, dict):
            entry.update(fields)
        entry.update(
            severity=record.levelname,
            message=message,
            time=datetime.fromtimestamp(record.created, UTC).isoformat(),
            logger=record.name,
        )
        return json.dumps(entry, default=str)


def _json_log_handler() -> logging.Handler:
    """A stdout handler writing ``CloudRunJsonFormatter`` lines."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CloudRunJsonFormatter())
    return handler


# Like the old text basicConfig: a no-op if the root logger already has
# handlers (functions-framework adds none in this image at import time).
logging.basicConfig(level=logging.INFO, handlers=[_json_log_handler()])
# Module logger for new code (propagates to the root handler configured above).
logger = logging.getLogger(__name__)

_USER_ID = config.AGENT_WORKER_USER_ID
_LOCATION = config.GCP_REGION

# Indirection so tests drive the completion check's polling without waiting.
_clock = time.monotonic
_sleep = asyncio.sleep


def _worker_topic_path() -> str:
    """Fully-qualified worker topic, resolved at use time (the project is a
    required env var, so resolving it at import would break credential-free
    imports)."""
    return (
        f"projects/{config.GOOGLE_CLOUD_PROJECT_NUMBER}"
        f"/topics/{config.CREATIVE_WORKER_TOPIC_NAME}"
    )


# ==============================
# clients
# ==============================
# One BigQuery client and one Pub/Sub publisher per instance, built on first
# use (so importing needs no credentials) and reused by every invocation:
# functions-framework serves requests on many threads (the orchestrator runs
# at concurrency 100), so creation is guarded by a lock, and a per-invocation
# client would leak its HTTP/gRPC transport (they were never closed). The Agent
# Runtime client is NOT cached: it is bound to one event loop (see
# `_new_vertex_client`).
_clients_lock = threading.Lock()
_bigquery_client: bigquery.Client | None = None
_pubsub_publisher: pubsub_v1.PublisherClient | None = None


def _get_bigquery_client() -> bigquery.Client:
    """The instance's shared BigQuery client (lazy, thread-safe)."""
    global _bigquery_client
    if _bigquery_client is None:
        with _clients_lock:
            if _bigquery_client is None:
                _bigquery_client = bigquery.Client(project=config.GOOGLE_CLOUD_PROJECT)
    return _bigquery_client


def _get_pubsub_client() -> pubsub_v1.PublisherClient:
    """The instance's shared Pub/Sub publisher (lazy, thread-safe)."""
    global _pubsub_publisher
    if _pubsub_publisher is None:
        with _clients_lock:
            if _pubsub_publisher is None:
                _pubsub_publisher = pubsub_v1.PublisherClient()
    return _pubsub_publisher


def _new_vertex_client():
    """A new Agent Runtime client (``agentplatform.Client``) for ONE invocation.

    Deliberately not cached: the worker drives each invocation with its own
    ``asyncio.run`` (a new event loop), and without aiohttp (not in this image)
    google-genai's async path reuses a single ``httpx.AsyncClient`` bound to
    the loop of its first request, so a cached client made the 2nd invocation
    on a warm instance fail with ``RuntimeError: Event loop is closed``.
    Creating one is cheap (no network call); ``create_agent_run`` closes it
    (``aio.aclose()``) before its loop ends. Lazy, so importing this module
    needs no credentials.
    """
    return agentplatform.Client(
        project=config.GOOGLE_CLOUD_PROJECT,
        location=config.GCP_REGION,
    )  # pyright: ignore[reportCallIssue]


async def _aclose_vertex_client(client) -> None:
    """Release the client's async HTTP transport on the current loop (best
    effort: a close failure is logged, never fails the run)."""
    try:
        result = client.aio.aclose()
        if inspect.isawaitable(result):
            await result
    except Exception as e:  # noqa: BLE001 — cleanup only
        logger.warning(f"Closing the Agent Runtime client failed: {e!r}")


# ==============================
# helper functions
# ==============================
def pretty_print_event(event):
    """Pretty prints an event with truncation for long content."""
    if not event.get("content"):
        logging.info(f"[{event.get('author', 'unknown')}]: {event}")
        return

    author = event.get("author", "unknown")
    parts = event["content"].get("parts") or []

    for part in parts:
        # Deployed AdkApp engines stream snake_case dicts (nulls dropped); accept
        # camelCase too. Inline mirror of deployment/test_deployment.py (the CRF
        # package can't import deployment/).
        func_call = part.get("function_call") or part.get("functionCall")
        func_response = part.get("function_response") or part.get("functionResponse")
        if part.get("text"):
            text = part["text"]
            logging.info(f"[{author}]: {text}")
        elif func_call:
            logging.info(
                f"[{author}]: Function call: {func_call.get('name', 'unknown')}"
            )
            # Truncate args if too long
            args = json.dumps(func_call.get("args", {}))
            if len(args) > 100:
                args = args[:97] + "..."
            logging.info(f"  Args: {args}")
        elif func_response:
            logging.info(
                f"[{author}]: Function response: {func_response.get('name', 'unknown')}"
            )
            # Truncate response if too long
            response = json.dumps(func_response.get("response", {}))
            if len(response) > 100:
                response = response[:97] + "..."
            logging.info(f"  Response: {response}")


# ==============================
# message parsing
# ==============================
class MalformedMessageError(ValueError):
    """A Pub/Sub message that can never be processed, however often it is
    redelivered: the entry points log it as an error and ACK (return) instead
    of raising, which would NACK it into a redelivery loop."""


# Reasoning Engine IDs are numeric; allow the usual id characters, but nothing
# that could change the resource path it is spliced into.
_AGENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TRIGGER_FIELDS = ("bq_dataset", "bq_table", "agent_resource_id")
_WORKER_FIELDS = (*_TRIGGER_FIELDS, "row_data")
# Every row_data key the worker indexes (build_row_dict always sends them; a
# campaign value may be null).
_ROW_FIELDS = (
    "index",
    "entry_timestamp",
    "brand",
    "target_product",
    "key_selling_point",
    "target_audience",
    "target_search_trend",
)


def _decode_pubsub_json(cloud_event) -> dict:
    """The CloudEvent's base64 Pub/Sub ``message.data`` as a JSON object.

    Raises MalformedMessageError for anything else (no data, bad base64 or
    UTF-8, invalid JSON, a non-object payload).
    """
    try:
        raw = base64.b64decode(cloud_event.data["message"]["data"]).decode("utf-8")
        payload = json.loads(raw)
    except (KeyError, TypeError, ValueError) as e:
        # binascii.Error, UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise MalformedMessageError(
            f"undecodable Pub/Sub message ({type(e).__name__}: {e})"
        ) from e
    if not isinstance(payload, dict):
        raise MalformedMessageError(
            f"payload is a JSON {type(payload).__name__}, not an object"
        )
    return payload


def _require_fields(mapping: dict, fields, what: str) -> None:
    missing = [f for f in fields if f not in mapping]
    if missing:
        raise MalformedMessageError(f"{what} missing {', '.join(missing)}")


def _validate_agent_id(agent_id) -> None:
    if not isinstance(agent_id, str) or not _AGENT_ID_RE.fullmatch(agent_id):
        raise MalformedMessageError(f"invalid agent_resource_id {agent_id!r}")


def _parse_worker_message(cloud_event) -> tuple[str, str, str, dict]:
    """``(dataset, table, agent_resource_id, row_data)`` from a worker message.

    Raises ValueError (MalformedMessageError, or the identifier / timestamp
    validators' ValueError) for a message that can never be processed.
    """
    payload = _decode_pubsub_json(cloud_event)
    _require_fields(payload, _WORKER_FIELDS, "worker message")
    row_data = payload["row_data"]
    if not isinstance(row_data, dict):
        raise MalformedMessageError("row_data is not an object")
    _require_fields(row_data, _ROW_FIELDS, "row_data")
    index = row_data["index"]
    if isinstance(index, bool) or not isinstance(index, int):
        raise MalformedMessageError(f"row_data.index {index!r} is not an integer")
    _validate_agent_id(payload["agent_resource_id"])
    # dataset/table stay in the worker message (compatible with messages already
    # in flight across a deploy) but must be allow-listed; the timestamp must
    # parse (the lock builder would raise on it otherwise).
    _validate_target(payload["bq_dataset"], payload["bq_table"])
    _to_utc_datetime(row_data["entry_timestamp"])
    return (
        payload["bq_dataset"],
        payload["bq_table"],
        payload["agent_resource_id"],
        row_data,
    )


# ==============================
# SQL builders
# ==============================
# BigQuery can't parameterize identifiers, and the dataset/table arrive in the
# Pub/Sub payload, so identifiers are validated against a config allow-list and
# backtick-quoted; every *value* goes through a query parameter.
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


def _resolve_max_rows(requested, cap):
    """The trigger message's optional ``max_rows``, clamped to ``[1, cap]``.

    Unset or invalid (non-integer, non-positive, bool) falls back to ``cap``.
    """
    if requested is None or isinstance(requested, bool):
        return cap
    try:
        value = int(requested)
    except (TypeError, ValueError):
        logger.warning(f"Ignoring invalid max_rows={requested!r}; using {cap}.")
        return cap
    if value < 1:
        logger.warning(f"Ignoring non-positive max_rows={value}; using {cap}.")
        return cap
    return min(value, cap)


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


# Session-state key ← message key. The campaign fields plus trend_scout's
# detection date (`refresh_date` → `trend_detected_on`) and research context
# (`trend_context` → `trend_scout_context`), which the creative agents read only
# as hints to re-verify.
CAMPAIGN_STATE_KEYS = (
    ("brand", "brand"),
    ("target_product", "target_product"),
    ("key_selling_points", "key_selling_point"),
    ("target_audience", "target_audience"),
    ("target_search_trends", "target_search_trend"),
    ("trend_detected_on", "refresh_date"),
    ("trend_scout_context", "trend_context"),
)


def build_campaign_state(msg_dict: dict) -> dict[str, str]:
    """The createSession initialState for one row (blank/NULL values left out).

    Blank/NULL columns are skipped so the root memorizes the campaign fields
    from the message instead of treating a seeded "None" as present (and so an
    older row without trend_context/refresh_date seeds nothing for them).
    """
    return {
        state_key: str(msg_dict[msg_key]).strip()
        for state_key, msg_key in CAMPAIGN_STATE_KEYS
        if msg_dict.get(msg_key) is not None and str(msg_dict[msg_key]).strip()
    }


def _optional_cell(value) -> str:
    """A nullable BigQuery cell as a JSON-safe string ("" for NULL).

    DATE/TIMESTAMP cells become an ISO date (``YYYY-MM-DD``).
    """
    if value is None:
        return ""
    # datetime before date: a datetime IS a date, and its time part is dropped.
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def build_row_dict(index, row) -> dict:
    """The worker message's ``row_data`` for one BigQuery row (JSON-safe).

    ``row`` is a BigQuery ``Row`` (or mapping); a NULL campaign string stays
    ``None`` (JSON null). ``refresh_date`` / ``trend_context``
    are read with ``.get`` so a table without the trend_context column (before
    its ALTER) still dispatches.
    """
    return {
        "index": index,
        # ISO 8601 string for JSON serialization
        "entry_timestamp": row["entry_timestamp"].isoformat(),
        "target_search_trend": row["target_trend"],
        "brand": row["brand"],
        "target_audience": row["target_audience"],
        "target_product": row["target_product"],
        "key_selling_point": row["key_selling_point"],
        "refresh_date": _optional_cell(row.get("refresh_date")),
        "trend_context": _optional_cell(row.get("trend_context")),
    }


async def create_agent_run(
    agent_id: str,
    msg_dict: dict,
    user_id: str,
    deadline: float | None = None,
) -> RunOutcome:
    """Invoke agent workflow and report whether the run finished.

    Args:
        agent_id (str): resource ID for the deployed agent
        msg_dict (dict): campaign metadata for the creative agent. Has following keys:
            brand: brand to generate creatives for
            target_audience: description of target audience
            target_product: product to promote in creatives
            key_selling_point: compelling features etc. to reference
            target_search_trend: trending search topic to overlap with creative themes
            refresh_date (optional): ISO date trend_scout detected the trend
            trend_context (optional): trend_scout's compact research JSON
        user_id (str): the user ID to use for the Agent Engine session
        deadline (float): ``_clock()`` time by which the run must be over
            (default: now + the worker timeout - the safety margin)

    Returns:
        RunOutcome: ``finalized`` only when the session reached
        ``finalize_done`` (see completion.py: a clean stream end is not
        enough; a timeout/cut is polled; an idle unfinished run gets ONE
        retry message).
    """
    if deadline is None:
        deadline = _clock() + config.WORKER_TIMEOUT_SECS - config.BUDGET_SAFETY_SECS
    logging.info(f"Invoking Agent Run {msg_dict['index'] + 1}...")

    USER_QUERY = f"""Brand: {msg_dict["brand"]}
    Target Product: {msg_dict["target_product"]}
    Key Selling Point(s): {msg_dict["key_selling_point"]}
    Target Audience: {msg_dict["target_audience"]}
    Target Search Trend: {msg_dict["target_search_trend"]}
    """

    # Seed the campaign fields as session state so the inputs are deterministic
    # (creative_agent's state init setdefaults them; the root only memorizes
    # fields missing from state). The message above stays as a readable echo.
    # Blank/NULL columns are left out so the root memorizes them from the
    # message instead of treating a seeded "None" as present.
    campaign_state = build_campaign_state(msg_dict)

    # create → stream + completion check → delete, all under one user_id. The
    # delete runs only after the check (it reads the session) and on every path
    # (agent_session's finally), so a failed run never leaks a session, and it
    # can't drift onto a different user_id.
    # A fresh client (and engine handle) per invocation, closed on this loop:
    # see _new_vertex_client for why it can't be cached.
    client = _new_vertex_client()
    try:
        remote_agent = client.runtimes.get(
            name=f"projects/{config.GOOGLE_CLOUD_PROJECT_NUMBER}/locations/{_LOCATION}/reasoningEngines/{agent_id}"
        )
        async with agent_session(
            remote_agent, user_id, state=campaign_state
        ) as session:
            # long running op
            monitor = RunMonitor(
                remote_agent,
                user_id,
                session["id"],
                deadline=deadline,
                stream_timeout_secs=config.STREAM_TIMEOUT_SECS,
                poll_interval_secs=config.POLL_INTERVAL_SECS,
                idle_secs=config.IDLE_SECS,
                clock=_clock,
                sleep=_sleep,
                on_event=pretty_print_event,
            )
            return await monitor.run(USER_QUERY)
    finally:
        await _aclose_vertex_client(client)


class RunNotFinalizedError(RuntimeError):
    """The run ended (after at most one retry) without ``finalize_done``."""

    def __init__(self, outcome: RunOutcome):
        super().__init__(
            f"run not finalized (reason={outcome.reason}, retried={outcome.retried})"
        )
        self.outcome = outcome


def _log_run_duration(
    timestamp, index, status, run_start, outcome: RunOutcome | None = None
) -> None:
    """The AGENT_RUN_DURATION_SECS marker: the same text as before (existing
    log queries match ``message``) plus row/index/status/secs and the
    completion check's finalized/retried/reason as JSON keys."""
    secs = round(_clock() - run_start, 1)
    fields = {"row": timestamp, "index": index, "status": status, "secs": secs}
    fields["finalized"] = outcome.finalized if outcome else False
    fields["retried"] = outcome.retried if outcome else False
    fields["reason"] = outcome.reason if outcome else "error"
    logger.info(
        f"AGENT_RUN_DURATION_SECS row={timestamp} index={index} "
        f"status={status} secs={secs:.1f}",
        extra={"json_fields": fields},
    )


# --- Helper to encapsulate the single-row logic ---
async def _execute_agent_and_update_status(
    trend_dict, agent_id, bq_client, dataset, table
):
    """Handles the async agent run for a single row and updates its status."""

    # Extract data
    timestamp = trend_dict["entry_timestamp"]

    # 1. ATTEMPT TO ACQUIRE ATOMIC LOCK
    if not acquire_processing_lock(bq_client, dataset, table, timestamp):
        # Lock failed. Another worker got it, or the row is already PROCESSED/FAILED.
        # RETURN here so the Pub/Sub message is ACKed and no more processing attempts are made.
        return

    # --- ONLY ONE WORKER PROCEEDS PAST THIS POINT ---

    # Wall-clock timer for the end-to-end agent run. Emitted as a structured log
    # marker (AGENT_RUN_DURATION_SECS) on both the success and failure paths so we
    # can compute p50/p95 pipeline duration from Cloud Logging — the key input to
    # the Ambient-Agent trigger-endpoint decision (10-min ceiling). See
    # docs/notes/ambient-agents-vs-cloud-functions.md.
    run_start = _clock()
    outcome: RunOutcome | None = None
    try:
        # 2. Run the Agent (The heavy async part) and check it finished
        # (finalize_done; at most one retry). The row status is now 'PROCESSING'.
        user_id = f"{_USER_ID}_{trend_dict['index']}"
        outcome = await create_agent_run(
            agent_id=agent_id,
            msg_dict=trend_dict,
            user_id=user_id,
            deadline=run_start + config.WORKER_TIMEOUT_SECS - config.BUDGET_SAFETY_SECS,
        )
        if not outcome.finalized:
            raise RunNotFinalizedError(outcome)

        # 3. Update status to PROCESSED
        # We MUST change the status away from PROCESSING now
        update_rows_status(
            bq_client=bq_client,
            dataset=dataset,
            table=table,
            timestamps=[timestamp],
            status="PROCESSED",
        )
        _log_run_duration(
            timestamp, trend_dict["index"], "PROCESSED", run_start, outcome
        )
        logging.info(f"Successfully processed and marked row {timestamp} as PROCESSED.")

    except Exception as e:  # noqa: BLE001 — recorded as FAILED below, then ACKed
        # The agent run (or the PROCESSED write) failed: record FAILED, then
        # RETURN so the worker message is ACKed. Re-raising would only buy a
        # no-op retry: the redelivery's QUEUED->PROCESSING lock can't match a
        # FAILED row, so it would just log "Lock failed" and ACK anyway.
        _log_run_duration(timestamp, trend_dict["index"], "FAILED", run_start, outcome)
        logger.error(
            f"Failed processing row {timestamp} (index={trend_dict['index']}): "
            f"{type(e).__name__}: {e}",
            extra={
                "json_fields": {
                    "row": timestamp,
                    "status": "FAILED",
                    "reason": (
                        e.outcome.reason
                        if isinstance(e, RunNotFinalizedError)
                        else type(e).__name__
                    ),
                    "retried": outcome.retried if outcome else False,
                    "secs": round(_clock() - run_start, 1),
                }
            },
        )

        # 4. Update status PROCESSING -> FAILED. If THIS write raises, it
        # propagates (failed invocation / NACK) so the error is loud. The row is
        # left in PROCESSING, which the orchestrator's stale-PROCESSING reaper
        # re-queues (or fails, over the attempt cap) after
        # REAP_STALE_PROCESSING_MINUTES.
        update_rows_status(
            bq_client=bq_client,
            dataset=dataset,
            table=table,
            timestamps=[timestamp],
            status="FAILED",
        )
        logger.error(
            f"Marked row {timestamp} as FAILED; ACKing the worker message "
            "(no retry: a FAILED row can't be re-locked)."
        )
        return


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


# ==================================================
# 1. Orchestrator Entry Point
# Triggered by: Trigger Topic $CREATIVE_TRIGGER_NAME
# Executes: Queries BQ, publishes N worker messages
# ==================================================
@functions_framework.cloud_event
def crf_entrypoint(cloud_event: CloudEvent) -> None:
    """Checks BigQuery table and processes any new rows

    Each row in the BQ table includes a trend and campaign metadata.
    These are used as inputs when invoking an Agent Engine instance to
    generate ad copy and creative candidates. Each row (trend & campaign
    metadata) should be processed once.

    This function performs the following steps:
    1. Consumes PubSub message.
    2. Checks BigQuery table for new rows.
    3. Executes query updating the status of each new row as 'PROCESSING'.
    4. Executes concurrent Agent Engine calls, one for each new row.
    5. If Agent Engine run succeeds, updates row status as 'PROCESSED';
       if fails updates with 'FAILED' status.

    Args:
      request: json dictionary with the following keys:
        bq_dataset: BQ dataset hosting tables for monitoring the trawler
        bq_table: BQ table to monitor
        agent_resource_id: resource ID for agent deployed to Agent Engine

    Returns:
         None: output is written to Cloud Logging
    """
    # A trigger without data or without `bq_dataset` is not work: log and ACK.
    data = cloud_event.data
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, dict) or "data" not in message:
        logging.info("No data found in the Pub/Sub message.")
        return
    try:
        message_payload = _decode_pubsub_json(cloud_event)
    except MalformedMessageError as e:
        logger.error(f"Rejecting trigger message: {e}")
        return
    logging.info(f"Parsed message payload: {message_payload}")
    if "bq_dataset" not in message_payload:
        logging.info("Trigger message has no 'bq_dataset'; nothing to process.")
        return

    # The trigger message names the dataset/table; they become SQL identifiers,
    # so they must be on the config allow-list. A missing field, a bad
    # identifier or agent id can never become valid on redelivery, so log and
    # ACK rather than NACK into a redelivery loop.
    try:
        _require_fields(message_payload, _TRIGGER_FIELDS, "trigger message")
        dataset = message_payload["bq_dataset"]
        table = message_payload["bq_table"]
        agent_resource_id = message_payload["agent_resource_id"]
        _validate_agent_id(agent_resource_id)
        _validate_target(dataset, table)
    except ValueError as e:
        logger.error(f"Rejecting trigger message: {e}")
        return

    bq_client = _get_bigquery_client()
    pubsub_publisher = _get_pubsub_client()

    # 0. Reap stale PROCESSING rows: a worker that hard-crashes AFTER acquiring
    # the lock strands its row in PROCESSING forever (a clean failure self-reports
    # FAILED). Re-queue those older than the staleness bound under the attempt cap
    # so the re-query below re-dispatches them (or fail them over the cap).
    reap_stale_processing_rows(bq_client, dataset, table)

    # 1. Fetch unprocessed rows: brand-new (processed_status IS NULL) AND rows
    # orphaned in QUEUED by a prior run that set them QUEUED then crashed or only
    # partially published (a failed publish leaves the row QUEUED), plus rows the
    # reap step above just re-queued. Re-dispatch is safe: a row a worker already
    # grabbed is PROCESSING/PROCESSED (not selected here), and a genuinely-stuck
    # QUEUED row is re-sent — the worker's atomic QUEUED->PROCESSING lock
    # (acquire_processing_lock) dedups any double delivery.
    # Capped at CRF_MAX_ROWS_PER_RUN (oldest first) so one trigger can't fan
    # out the whole backlog; rows past the limit stay unclaimed for next time.
    max_rows = _resolve_max_rows(
        message_payload.get("max_rows"), config.CRF_MAX_ROWS_PER_RUN
    )
    select_sql, select_params = _build_select_unprocessed_sql(
        bq_client.project, dataset, table, max_rows
    )
    logging.info(
        f"Selecting up to {max_rows} unprocessed rows "
        "(trends flagged sensitive in trend_context are excluded)."
    )
    try:
        # Iterate the rows directly: the CRF image ships without pandas.
        rows = list(_run_query(bq_client, select_sql, select_params))
    except Exception as e:
        logging.error(f"Error querying BQ: {e}")
        raise  # Re-raise to signal failure to Pub/Sub

    if not rows:
        logging.info(
            "No new rows found to process (all are marked PROCESSED or table is empty)."
        )
    else:
        # 2. Extract data into a list of dictionaries
        row_list = []
        for index, row in enumerate(rows):
            row_list.append(build_row_dict(index, row))
        logging.info(f"Loaded {len(row_list)} UNPROCESSED records for agent execution.")

        # 3. Get timestamps of rows to be processed
        timestamps_to_process = [d["entry_timestamp"] for d in row_list]

        # --- LOCK STEP ---
        # 4. Mark the batch as 'QUEUED' before starting any costly work.
        update_rows_status(
            bq_client=bq_client,
            dataset=dataset,
            table=table,
            timestamps=timestamps_to_process,
            status="QUEUED",
        )
        # If the function fails here, the trigger message NACKs and retries,
        # but the next successful run will see these rows are now QUEUED,
        # preventing duplication (assuming the next step succeeds).

        # 5. Dispatch Step: Publish one message per row to the worker topic, then
        # confirm each publish resolved. A publish that fails silently would leave
        # its row QUEUED forever under the old IS-NULL-only re-query; now such a row
        # is recovered by the broadened re-query on the next orchestrator run, so we
        # only need to surface the failure (don't crash the whole batch over one).
        worker_topic = _worker_topic_path()
        publish_futures = []
        for row_dict in row_list:
            # Create a dedicated payload for the worker
            worker_payload = {
                "bq_dataset": dataset,
                "bq_table": table,
                "agent_resource_id": agent_resource_id,
                "row_data": row_dict,  # Pass the necessary row info
            }

            data_str = json.dumps(worker_payload)
            data_bytes = data_str.encode("utf-8")
            publish_futures.append(pubsub_publisher.publish(worker_topic, data_bytes))

        dispatched_count = 0
        failed_count = 0
        for future in publish_futures:
            try:
                future.result()
                dispatched_count += 1
            except Exception as e:  # noqa: BLE001 — one bad publish shouldn't sink the batch
                failed_count += 1
                logging.error(f"Failed to publish a worker message: {e}")

        if failed_count:
            logging.warning(
                f"{failed_count}/{len(row_list)} worker publishes failed; those rows "
                "remain QUEUED and will be recovered on the next orchestrator run."
            )
        logging.info(
            f"Successfully dispatched {dispatched_count} tasks to the worker queue."
        )

        # Since the orchestration succeeded, ACK the original trigger message.
        # The heavy lifting and final status updates are handled by the workers.

    # The Orchestrator should exit cleanly (ACKing the original message)
    # as soon as all worker messages are successfully published.

    return


# ==================================================
# 2. Worker Entry Point
# Triggered by: Worker Queue Topic
# Executes: Agent run, updates single BQ row status
# ==================================================
@functions_framework.cloud_event
def agent_worker_entrypoint(cloud_event: CloudEvent) -> None:
    """Entry point for the worker, processes data for a single row."""
    # A malformed message (undecodable, missing fields, a bad identifier, agent
    # id or entry_timestamp) never becomes valid on redelivery: log and ACK.
    try:
        dataset, table, agent_resource_id, row_data = _parse_worker_message(cloud_event)
    except ValueError as e:
        logger.error(f"Rejecting worker message: {e}")
        return

    try:
        bq_client = _get_bigquery_client()
        # Since the core agent logic is async, we run it here
        asyncio.run(
            _execute_agent_and_update_status(
                trend_dict=row_data,
                agent_id=agent_resource_id,
                bq_client=bq_client,
                dataset=dataset,
                table=table,
            )
        )

    except Exception as e:
        logging.error(f"Fatal error in worker entrypoint: {e}")
        raise  # NACK the worker message
