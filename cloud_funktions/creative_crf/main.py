"""Offline, batch workflow for trend insights and candidate Ad creatives

Checks the `trend_trawler` agent's recommendations in the
`BQ_PROJECT_ID.BQ_DATASET_ID.BQ_TABLE_TARGETS` BQ table.
Generates ad creatives for any news recs

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
from cloudevents.http import CloudEvent

# from google.cloud.exceptions import NotFound

from .config import config


# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")

_USER_ID = "Ima_CloudRun_jr"
_PROJECT_NUMBER = config.GOOGLE_CLOUD_PROJECT_NUMBER
_LOCATION = config.GOOGLE_CLOUD_LOCATION


# ==============================
# clients
# ==============================
def _get_bigquery_client() -> bigquery.Client:
    """Get a configured BigQuery client."""
    return bigquery.Client(project=config.GOOGLE_CLOUD_PROJECT)


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


# NO BQ CLIENT OR STATUS UPDATE LOGIC IN THIS FUNCTION NOW
async def _run_single_agent_task(trend_dict, agent_id):
    """Handles the async agent run for a single row."""

    # 1. Run the Agent
    user_id = f"{_USER_ID}_{trend_dict['index']}"
    await create_agent_run(
        agent_id=agent_id,
        msg_dict=trend_dict,
        user_id=user_id,
    )

    # Return the timestamp of the successful run
    return trend_dict["entry_timestamp"]


# make multiple agent calls, async
async def _run_multiple_agents(agent_id, row_list):
    """Runs agent tasks concurrently and collects successful timestamps."""
    logging.info(f"Preparing to run {len(row_list)} concurrent agent tasks.")

    tasks = [_run_single_agent_task(trend_dict, agent_id) for trend_dict in row_list]

    # Execute all tasks concurrently
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Check for exceptions and report
    successful_timestamps = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]

    if failures:
        logging.error(f"{len(failures)} agent runs failed: {failures}")

    logging.info("All agent runs completed (or failed).")
    return successful_timestamps


# This should be the entrypoint for your Cloud Function
# Triggered from a message on a Cloud Pub/Sub topic.
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

        # 1. Fetch ALL unprocessed rows (processed_status is NULL)
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
            row_dict = {
                "index": index,
                "entry_timestamp": row[
                    "entry_timestamp"
                ],  # Include timestamp for BQ update
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
        # 4. Mark the batch as 'PROCESSING' before starting any costly work.
        update_rows_status(
            bq_client=bq_client,
            dataset=dataset,
            table=table,
            timestamps=timestamps_to_process,
            status="PROCESSING",
        )
        # If this update fails, the function fails, Pub/Sub retries,
        # and the next attempt will fetch the same rows as 'NEW' (processed_status is NULL).

        # --- AGENT RUN STEP ---
        # 5. Run Agents Concurrently (collects timestamps of successful runs)
        successful_timestamps = asyncio.run(
            _run_multiple_agents(agent_id=agent_resource_id, row_list=row_list)
        )

        # --- FINAL UPDATE STEP ---
        # 6. Mark successful rows as 'PROCESSED'
        if successful_timestamps:
            update_rows_status(
                bq_client=bq_client,
                dataset=dataset,
                table=table,
                timestamps=successful_timestamps,
                status="PROCESSED",
            )

        # 7. Handle rows that failed during the run (optional but recommended)
        failed_timestamps = [
            ts for ts in timestamps_to_process if ts not in successful_timestamps
        ]
        if failed_timestamps:
            update_rows_status(
                bq_client=bq_client,
                dataset=dataset,
                table=table,
                timestamps=failed_timestamps,
                status="FAILED",  # Or set back to `NULL` for automatic retry
            )

        # If the function crashes after step 4 but before step 6, the
        # data is locked in 'PROCESSING' and requires manual cleanup or
        # a dedicated monitor service.

        logging.info(
            f"Total rows successfully processed and status updated: {len(successful_timestamps)}"
        )
