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
1. Agent Orchestrator: Checks the `trend_trawler` agent's 
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
    "agent_resource_id": "47239417575768064",
}
"""

import os

os.environ["PYTHONUNBUFFERED"] = "1"

import json
import base64
import asyncio
import logging
import warnings

import vertexai
import functions_framework
from google.cloud import bigquery
from google.cloud import pubsub_v1
from cloudevents.http import CloudEvent

from .config import config


# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")

_USER_ID = "Ima_CloudRun_jr"
_PROJECT_NUMBER = config.GOOGLE_CLOUD_PROJECT_NUMBER
_LOCATION = config.GOOGLE_CLOUD_LOCATION
_WORKER_TOPIC_NAME = (
    f"projects/{_PROJECT_NUMBER}/topics/{config.CREATIVE_WORKER_TOPIC_NAME}"
) # Configuration for the worker topic


# ==============================
# clients
# ==============================
def _get_bigquery_client() -> bigquery.Client:
    """Get a configured BigQuery client."""
    return bigquery.Client(project=config.GOOGLE_CLOUD_PROJECT)


def _get_pubsub_client():
    """Get a configured Pub/Sub client."""
    return pubsub_v1.PublisherClient()


client = vertexai.Client(
    project=config.GOOGLE_CLOUD_PROJECT,
    location=config.GOOGLE_CLOUD_LOCATION,
)  # pyright: ignore[reportCallIssue]


# ==============================
# helper functions
# ==============================
def pretty_print_event(event):
    """Pretty prints an event with truncation for long content."""
    if "content" not in event:
        logging.info(f"[{event.get('author', 'unknown')}]: {event}")
        return

    author = event.get("author", "unknown")
    parts = event["content"].get("parts", [])

    for part in parts:
        if "text" in part:
            text = part["text"]
            logging.info(f"[{author}]: {text}")
        elif "functionCall" in part:
            func_call = part["functionCall"]
            logging.info(
                f"[{author}]: Function call: {func_call.get('name', 'unknown')}"
            )
            # Truncate args if too long
            args = json.dumps(func_call.get("args", {}))
            if len(args) > 100:
                args = args[:97] + "..."
            logging.info(f"  Args: {args}")
        elif "functionResponse" in part:
            func_response = part["functionResponse"]
            logging.info(
                f"[{author}]: Function response: {func_response.get('name', 'unknown')}"
            )
            # Truncate response if too long
            response = json.dumps(func_response.get("response", {}))
            if len(response) > 100:
                response = response[:97] + "..."
            logging.info(f"  Response: {response}")


def update_rows_status(bq_client, dataset, table, timestamps, status="PROCESSED"):
    """Updates the processing status of multiple rows atomically."""

    if not timestamps:
        logging.info("No rows to update status for.")
        return

    # Create a list of timestamp strings for the WHERE IN clause
    ts_list = [f"TIMESTAMP('{t}')" for t in timestamps]
    ts_string = ", ".join(ts_list)

    update_query = f"""
        UPDATE `{bq_client.project}.{dataset}.{table}`
        SET processed_status = '{status}'
        WHERE entry_timestamp IN ({ts_string})
    """

    # Execute the update
    try:
        query_job = bq_client.query(update_query)
        query_job.result()
        logging.info(
            f"Successfully updated status to {status} for {len(timestamps)} rows."
        )
    except Exception as e:
        # Crucial: Log the error if the update fails (like the BQ syntax error)
        logging.error(f"Failed to update rows status to {status}: {e}")
        # Reraise the exception to ensure the function fails and the Pub/Sub message retries
        raise


async def my_delete_task(remote_agent, session):
    logging.info(f"Delete task starting with agent: {remote_agent}...")
    await remote_agent.async_delete_session(user_id=_USER_ID, session_id=session["id"])
    logging.info(f"Deleted session for user ID: {_USER_ID}")


# function to interact with remote agent
async def async_send_message(remote_agent, user_id, session, user_query) -> None:
    """Send a message to the deployed agent."""

    # Clear events for each new query
    events = []
    try:
        async for event in remote_agent.async_stream_query(
            user_id=user_id,
            session_id=session["id"],
            message=user_query,  # user_input
        ):
            events.append(event)
            pretty_print_event(event)

    except Exception as e:
        logging.error(f"Error during streaming: {type(e).__name__}: {e}")


async def create_agent_run(
    agent_id: str,
    msg_dict: dict,
    user_id: str,
):
    """Invoke agent workflow

    Args:
        agent_id (str): resource ID for the deployed agent
        msg_dict (dict): campaign metadata for the creative agent. Has following keys:
            brand: brand to generate creatives for
            target_audience: description of target audience
            target_product: product to promote in creatives
            key_selling_point: compelling features etc. to reference
            target_search_trend: trending search topic to overlap with creative themes
        user_id (str): the user ID to use for the Agent Engine session

    """
    logging.info(f"Invoking Agent Run {msg_dict['index'] + 1}...")

    remote_agent = client.agent_engines.get(
        name=f"projects/{_PROJECT_NUMBER}/locations/{_LOCATION}/reasoningEngines/{agent_id}"
    )

    session = await remote_agent.async_create_session(user_id=user_id)
    logging.info(f"\n\nCreated session for user ID: {user_id}\n\n")

    USER_QUERY = f"""Brand: {msg_dict['brand']} 
    Target Product: {msg_dict['target_product']} 
    Key Selling Point(s): {msg_dict['key_selling_point']} 
    Target Audience: {msg_dict['target_audience']} 
    Target Search Trend: {msg_dict['target_search_trend']} 
    """

    # long running op
    await async_send_message(
        remote_agent=remote_agent,
        user_id=user_id,
        session=session,
        user_query=USER_QUERY,
    )
    await my_delete_task(remote_agent=remote_agent, session=session)


# --- Helper to encapsulate the single-row logic ---
async def _execute_agent_and_update_status(
    trend_dict, agent_id, bq_client, dataset, table
):
    """Handles the async agent run for a single row and updates its status."""

    # Extract data
    timestamp = trend_dict["entry_timestamp"]

    try:
        # 1. Run the Agent (The heavy async part)
        user_id = f"{_USER_ID}_{trend_dict['index']}"
        await create_agent_run(
            agent_id=agent_id,
            msg_dict=trend_dict,
            user_id=user_id,
        )

        # 2. Update status to PROCESSED
        update_rows_status(
            bq_client=bq_client,
            dataset=dataset,
            table=table,
            timestamps=[timestamp],
            status="PROCESSED",
        )
        logging.info(f"Successfully processed and marked row {timestamp} as PROCESSED.")

    except Exception as e:
        logging.error(f"Failed processing row {timestamp}: {e}")
        # 3. Update status to FAILED
        update_rows_status(
            bq_client=bq_client,
            dataset=dataset,
            table=table,
            timestamps=[timestamp],
            status="FAILED",
            # Note: If this status update fails, the function will raise an error
            # and the worker Pub/Sub message will retry, which is acceptable.
        )
        # We raise here so the Worker Pub/Sub message retries,
        # but the BQ row status indicates the specific failure.
        raise  # Reraise to trigger NACK/retry for the Pub/Sub worker message


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
    bq_client = _get_bigquery_client()
    pubsub_publisher = _get_pubsub_client()

    # Get the Pub/Sub message data
    pubsub_message = cloud_event.data

    # Decode the base64 encoded message data
    if "message" in pubsub_message and "data" in pubsub_message["message"]:
        data = base64.b64decode(pubsub_message["message"]["data"]).decode("utf-8")
        logging.info(f"Received Pub/Sub message data:\n\n{data}\n\n")

        # You can further parse the data if it's JSON, for example
        try:
            message_payload = json.loads(data)
            logging.info(f"Parsed message payload: {message_payload}")
        except json.JSONDecodeError:
            logging.exception("Message data is not valid JSON.")
    else:
        logging.info("No data found in the Pub/Sub message.")

    if message_payload and "bq_dataset" in message_payload:
        dataset = message_payload["bq_dataset"]
        table = message_payload["bq_table"]
        agent_resource_id = message_payload["agent_resource_id"]
        # The triggering message should pass the necessary config,
        # OR the orchestrator uses a hardcoded worker topic name.

        # 1. Fetch ALL unprocessed rows (processed_status is NULL)
        # optionally fetch FAILED rows
        rows_to_process_query = f"""
            SELECT * FROM `{bq_client.project}.{dataset}.{table}`
            WHERE processed_status IS NULL 
            ORDER BY entry_timestamp ASC
        """
        try:
            df = bq_client.query(rows_to_process_query).to_dataframe()
        except Exception as e:
            logging.error(f"Error querying BQ: {e}")
            raise  # Re-raise to signal failure to Pub/Sub

    if df.empty:
        logging.info(
            "No new rows found to process (all are marked PROCESSED or table is empty)."
        )
    else:
        # 2. Extract data into a list of dictionaries
        row_list = []
        for index, row in df.iterrows():
            # Ensure the timestamp is converted to a string format (ISO 8601)
            timestamp_str = row["entry_timestamp"].isoformat()

            row_dict = {
                "index": index,
                # Use the string version for JSON serialization
                "entry_timestamp": timestamp_str,
                "target_search_trend": row["target_trend"],
                "brand": row["brand"],
                "target_audience": row["target_audience"],
                "target_product": row["target_product"],
                "key_selling_point": row["key_selling_point"],
            }
            row_list.append(row_dict)
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

        # 5. Dispatch Step: Publish one message per row to the worker topic
        dispatched_count = 0
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

            # Publish the message
            # Note: We don't await the publish result here, fire-and-forget is usually fine,
            # but you might want to handle publishing errors.
            pubsub_publisher.publish(_WORKER_TOPIC_NAME, data_bytes)
            dispatched_count += 1

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
    bq_client = _get_bigquery_client()

    try:
        # Decode worker message payload (This contains the single row data)
        pubsub_message = cloud_event.data
        data = base64.b64decode(pubsub_message["message"]["data"]).decode("utf-8")
        worker_payload = json.loads(data)

        # Ensure all required metadata is present
        dataset = worker_payload["bq_dataset"]
        table = worker_payload["bq_table"]
        agent_resource_id = worker_payload["agent_resource_id"]
        row_data = worker_payload["row_data"]

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
