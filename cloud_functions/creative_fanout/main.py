"""Offline, batch workflow for trend insights and candidate ad creatives.

This module holds the Cloud Run function entry points. One image
(``cloud_functions/creative_fanout/Dockerfile``) runs as two Cloud Run
services; each service's ``FUNCTION_TARGET`` (+ ``FUNCTION_SIGNATURE_TYPE``,
default ``cloudevent``) picks its entry point:

1. Orchestrator (``crf_entrypoint``): finds the unprocessed rows of the
   `trend_scout` agent's recommendations table
   (`BQ_PROJECT_ID.BQ_DATASET_ID.BQ_TABLE_TARGETS`) and sends one worker
   message per row (``orchestrator.dispatch``) over ``CRF_WORKER_TRANSPORT``:
   a Cloud Tasks HTTP task (``tasks``) or a worker-topic message (``pubsub``).
2. Worker (``agent_worker_task``, HTTP, for Cloud Tasks; or
   ``agent_worker_entrypoint``, CloudEvent, for the Pub/Sub rollback path,
   one shared handler ``worker.handle_worker_message``): processes one worker
   message: locks the row, runs the creative agent on Agent Engine for that
   (trend + campaign) pair and records the row's final status (``worker.process_row``).
   The engine is the worker's ``CREATIVE_AGENT_ENGINE_ID`` env var (set by
   ``deployment/engine_handoff.py apply --update-worker``), not the message.

Example trigger message:
message = {
    "bq_dataset": "trend_trawler",
    "bq_table": "target_trends_crf",
    "max_rows": 1,  # optional; clamped to CRF_MAX_ROWS_PER_RUN (default 3)
}
"""

import logging

import flask
import functions_framework
from cloudevents.http import CloudEvent

# `logs` configures the root JSON log handler on import.
from . import clients, logs, orchestrator, worker  # noqa: F401
from .messages import (
    parse_task_request,
    parse_trigger_message,
    parse_worker_message,
)

# Module logger (propagates to the root JSON handler `logs` configures).
logger = logging.getLogger(__name__)


# ==================================================
# 1. Orchestrator Entry Point
# Triggered by: Trigger Topic $CREATIVE_TRIGGER_NAME
# Executes: Queries BQ, publishes N worker messages
# ==================================================
@functions_framework.cloud_event
def crf_entrypoint(cloud_event: CloudEvent) -> None:
    """Dispatch the unprocessed rows of the trends table to the worker.

    Steps (``orchestrator.dispatch``): re-queue stale PROCESSING rows (the
    reaper), select up to ``CRF_MAX_ROWS_PER_RUN`` unprocessed rows (oldest
    first; sensitive trends skipped), mark them QUEUED, then publish one
    worker message per row. The workers run the agent and write
    PROCESSED/FAILED; this function never calls Agent Engine.

    Args:
      cloud_event: the Pub/Sub trigger. Its JSON payload has:
        bq_dataset: BQ dataset hosting the status-tracking table
        bq_table: BQ table to monitor (allow-listed)
        max_rows (optional): can only lower ``CRF_MAX_ROWS_PER_RUN``
        (a legacy ``agent_resource_id`` is ignored: the worker's env names
        the engine)

    Returns normally (ACK) for a message that is not work or is malformed;
    a BigQuery error raises (NACK, Pub/Sub redelivers).
    """
    # A trigger without data or without `bq_dataset` is not work (None): ACK.
    # A malformed one can never become valid on redelivery: log and ACK rather
    # than NACK into a redelivery loop.
    try:
        payload = parse_trigger_message(cloud_event)
    except ValueError as e:
        logger.error(f"Rejecting trigger message: {e}")
        return
    if payload is None:
        return

    # The heavy lifting and final status updates are handled by the workers;
    # the orchestrator exits cleanly (ACKing the trigger) once the worker
    # messages are published.
    orchestrator.dispatch(payload)


# ==================================================
# 2. Worker Entry Points
# Triggered by: a Cloud Tasks HTTP task (agent_worker_task) or, on the
# Pub/Sub rollback path, the worker topic (agent_worker_entrypoint)
# Executes: Agent run, updates single BQ row status
# ==================================================
# Header -> JSON log field for the Cloud Tasks request headers worth logging.
_TASK_HEADER_FIELDS = (
    ("X-CloudTasks-TaskName", "cloud_tasks_task_name"),
    ("X-CloudTasks-TaskRetryCount", "cloud_tasks_retry_count"),
    ("X-CloudTasks-TaskExecutionCount", "cloud_tasks_execution_count"),
    ("X-CloudTasks-QueueName", "cloud_tasks_queue"),
)


def _task_log_fields(headers) -> dict:
    """The task's Cloud Tasks headers as JSON log fields (counts as ints)."""
    fields = {}
    for header, field in _TASK_HEADER_FIELDS:
        value = headers.get(header)
        if value is None:
            continue
        fields[field] = (
            int(value) if field.endswith("_count") and value.isdigit() else value
        )
    return fields


@functions_framework.http
def agent_worker_task(request: flask.Request) -> tuple[str, int]:
    """Cloud Tasks entry point: process one row from the task's JSON body.

    Cloud Tasks retries any non-2xx response, so the status code is the
    ACK/NACK: 200 for a body that can never run (malformed, not allow-listed),
    a missing engine and every recorded outcome (lost lock, PROCESSED,
    FAILED, re-queued); 503 for an error before the status write (what the
    Pub/Sub entry NACKs), retried under the queue's retry policy. The run
    deadline is taken here, at request start (``worker.run_deadline``).
    """
    deadline = worker.run_deadline(worker._clock())
    fields = _task_log_fields(request.headers)
    logger.info(
        f"Worker task received: {fields.get('cloud_tasks_task_name', '?')} "
        f"(retry {fields.get('cloud_tasks_retry_count', '?')}).",
        extra={"json_fields": fields},
    )

    try:
        dataset, table, row_data = parse_task_request(request.get_data())
    except ValueError as e:
        logger.error(f"Rejecting worker task: {e}", extra={"json_fields": fields})
        return "rejected: malformed task", 200

    outcome = worker.handle_worker_message(dataset, table, row_data, deadline=deadline)
    if outcome is worker.Outcome.RETRY:
        return "retry", 503
    return "ok", 200


@functions_framework.cloud_event
def agent_worker_entrypoint(cloud_event: CloudEvent) -> None:
    """Pub/Sub entry point (the rollback transport): process one row.

    Same handler as ``agent_worker_task``; the run deadline is taken here, at
    request start (Cloud Run's request timeout counts from there, not from the
    row lock): ``worker.run_deadline`` = start + ``CRF_WORKER_TIMEOUT_SECS`` -
    ``CRF_BUDGET_SAFETY_SECS``, passed down to every bounded wait.

    The engine is ``config.CREATIVE_AGENT_ENGINE_ID`` (the worker's env var);
    while it is unset or invalid every message is logged and ACKed before the
    lock, so its row stays QUEUED.

    Returns normally (ACK) for a malformed message, a missing engine, a lost
    lock or a recorded status; an unexpected error before the status write
    raises (NACK).
    """
    deadline = worker.run_deadline(worker._clock())

    # A malformed message (undecodable, missing fields, a bad identifier or
    # entry_timestamp) never becomes valid on redelivery: log and ACK.
    try:
        dataset, table, row_data = parse_worker_message(cloud_event)
    except ValueError as e:
        logger.error(f"Rejecting worker message: {e}")
        return

    outcome = worker.handle_worker_message(dataset, table, row_data, deadline=deadline)
    if outcome is worker.Outcome.RETRY:
        # NACK the worker message (the error is logged by the handler).
        raise RuntimeError("Worker run failed before its status was recorded")
