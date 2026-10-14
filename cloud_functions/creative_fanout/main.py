"""Offline, batch workflow for trend insights and candidate ad creatives.

This module holds the two Cloud Run function entry points. One image
(``cloud_functions/creative_fanout/Dockerfile``) runs as two Cloud Run
services, each triggered by its own Pub/Sub topic; the service's
``FUNCTION_TARGET`` picks the entry point (a Pub/Sub-triggered service runs
exactly one handler):

1. Orchestrator (``crf_entrypoint``): finds the unprocessed rows of the
   `trend_scout` agent's recommendations table
   (`BQ_PROJECT_ID.BQ_DATASET_ID.BQ_TABLE_TARGETS`) and publishes one worker
   message per row (``orchestrator.dispatch``).
2. Worker (``agent_worker_entrypoint``): processes one worker message: locks
   the row, runs the creative agent on Agent Engine for that (trend +
   campaign) pair and records the row's final status (``worker.process_row``).

Example trigger message:
message = {
    "bq_dataset": "trend_trawler",
    "bq_table": "target_trends_crf",
    "agent_resource_id": "<AGENT_ENGINE_ID>",
    "max_rows": 1,  # optional; clamped to CRF_MAX_ROWS_PER_RUN (default 3)
}
"""

import asyncio
import logging

import functions_framework
from cloudevents.http import CloudEvent

# `logs` configures the root JSON log handler on import.
from . import clients, logs, orchestrator, worker  # noqa: F401
from .config import config
from .messages import parse_trigger_message, parse_worker_message

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
        agent_resource_id: resource ID of the creative agent on Agent Engine
        max_rows (optional): can only lower ``CRF_MAX_ROWS_PER_RUN``

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
# 2. Worker Entry Point
# Triggered by: Worker Queue Topic
# Executes: Agent run, updates single BQ row status
# ==================================================
@functions_framework.cloud_event
def agent_worker_entrypoint(cloud_event: CloudEvent) -> None:
    """Process one row: lock it, run the agent, record PROCESSED or FAILED.

    The run deadline is taken here, at request start (Cloud Run's request
    timeout counts from there, not from the row lock):
    ``worker.run_deadline`` = start + ``CRF_WORKER_TIMEOUT_SECS`` -
    ``CRF_BUDGET_SAFETY_SECS``, passed down to every bounded wait.

    Returns normally (ACK) for a malformed message, a lost lock or a recorded
    status; an unexpected error before the status write raises (NACK).
    """
    deadline = worker.run_deadline(worker._clock())

    # A malformed message (undecodable, missing fields, a bad identifier, agent
    # id or entry_timestamp) never becomes valid on redelivery: log and ACK.
    try:
        dataset, table, row_data = parse_worker_message(cloud_event)
    except ValueError as e:
        logger.error(f"Rejecting worker message: {e}")
        return

    try:
        bq_client = clients.get_bigquery_client()
        # Since the core agent logic is async, we run it here
        asyncio.run(
            worker.process_row(
                trend_dict=row_data,
                agent_id=config.CREATIVE_AGENT_ENGINE_ID,
                bq_client=bq_client,
                dataset=dataset,
                table=table,
                deadline=deadline,
            )
        )

    except Exception as e:
        logger.error(f"Fatal error in worker entrypoint: {e}")
        raise  # NACK the worker message
