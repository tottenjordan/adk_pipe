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
import logging

import functions_framework
from cloudevents.http import CloudEvent

# `logs` configures the root JSON log handler on import.
from . import clients, logs, orchestrator, worker  # noqa: F401
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
    """Entry point for the worker, processes data for a single row."""
    # A malformed message (undecodable, missing fields, a bad identifier, agent
    # id or entry_timestamp) never becomes valid on redelivery: log and ACK.
    try:
        dataset, table, agent_resource_id, row_data = parse_worker_message(cloud_event)
    except ValueError as e:
        logger.error(f"Rejecting worker message: {e}")
        return

    try:
        bq_client = clients.get_bigquery_client()
        # Since the core agent logic is async, we run it here
        asyncio.run(
            worker.process_row(
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
