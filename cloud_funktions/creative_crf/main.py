"""Offline, batch workflow for trend insights and candidate Ad creatives

Checking the `trend_trawler` agent's recommendations in the `hybrid-vertex.trend_trawler.target_trends` BQ table
Generating ad creatives for any news recs

Example PubSub msg format:

message = {
    "bq_dataset": "trend_trawler",
    "bq_table": "target_trends",
    "agent_resource_id": "47239417575768064",
}
"""

import os

os.environ["PYTHONUNBUFFERED"] = "1"

import json
import time
import base64
import asyncio
import logging
import warnings

import vertexai
import functions_framework
from google.cloud import bigquery
from cloudevents.http import CloudEvent
from google.cloud.exceptions import NotFound

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
def insert_bq_data(table_id, num_rows):
    rows_to_insert = [{"num_rows_last_check": num_rows, "last_check_time": time.time()}]
    bq_client = _get_bigquery_client()
    errors = bq_client.insert_rows_json(table_id, rows_to_insert)
    if errors == []:
        logging.info("New rows have been added.")
    else:
        logging.error(f"Encountered errors while inserting rows: {errors}")


def create_count_table(table_id, num_rows):
    bq_client = _get_bigquery_client()
    schema = [
        bigquery.SchemaField("num_rows_last_check", "INTEGER", mode="REQUIRED"),
        bigquery.SchemaField("last_check_time", "TIMESTAMP", mode="REQUIRED"),
    ]
    table = bigquery.Table(table_id, schema=schema)
    table = bq_client.create_table(table)
    logging.info(f"Created table {table.project}.{table.dataset_id}.{table.table_id}")

    insert_bq_data(table_id, num_rows)


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

    AGENT_QUERY = f"""Brand: {msg_dict['brand']} 
    Target Product: {msg_dict['target_product']} 
    Key Selling Point(s): {msg_dict['key_selling_point']} 
    Target Audience: {msg_dict['target_audience']} 
    Target Search Trend: {msg_dict['target_search_trend']} 
    """

    events = []
    async for event in remote_agent.async_stream_query(
        user_id=user_id, session_id=session["id"], message=AGENT_QUERY
    ):

        events.append(event)
        logging.info(event)  # full event stream i.e., agent's thought process

        # Extract just the final text response
        final_text_responses = [
            e
            for e in events
            if e.get("content", {}).get("parts", [{}])[0].get("text")
            and not e.get("content", {}).get("parts", [{}])[0].get("function_call")
        ]
        if final_text_responses:
            logging.info("\n\n--- Final Response ---\n\n")
            logging.info(final_text_responses[0]["content"]["parts"][0]["text"])

    await remote_agent.async_delete_session(user_id=user_id, session_id=session["id"])
    logging.info(f"Deleted session for user ID: {user_id}")


# This should be the entrypoint for your Cloud Function
# Triggered from a message on a Cloud Pub/Sub topic.
@functions_framework.cloud_event
def crf_entrypoint(cloud_event: CloudEvent) -> None:
    """Checks size of BigQuery table

    1. Consumes PubSub message
    2. Checks BigQuery table for new trend

    Args:
        request: json dictionary with the following keys:
            bq_dataset: BigQuery dataset hosting tables for monitoring the trawler
            bq_table: BigQuery table to monitor
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

        data_table = bq_client.get_table(f"{bq_client.project}.{dataset}.{table}")
        current_rows = data_table.num_rows
        logging.info(f"{table} table has {current_rows} rows")

        # See if `count` table exists in dataset
        try:
            count_table = bq_client.get_table(f"{bq_client.project}.{dataset}.count")
            logging.info(
                "`count` table exists, querying to see how many rows at last checkpoint"
            )
        except NotFound:
            logging.exception("No `count` table found, creating one...")
            create_count_table(f"{bq_client.project}.{dataset}.count", current_rows)

        # get new rows, if any
        query_job = bq_client.query(
            f"""
            SELECT num_rows_last_check FROM `{bq_client.project}.{dataset}.count`
            ORDER BY last_check_time DESC
            LIMIT 1"""
        )
        results = query_job.result()
        for i in results:
            last_retrain_count = i[0]

        if current_rows:
            rows_added_since_last_trawl_run = current_rows - last_retrain_count
            logging.info(
                f"{rows_added_since_last_trawl_run} rows added since last trawl"
            )

        if rows_added_since_last_trawl_run > 0:

            # do creative-agent runs on campaign+trend in new rows
            query = f"""
                SELECT * FROM `{bq_client.project}.{dataset}.target_trends`
                ORDER BY entry_timestamp DESC
                LIMIT 1""" # {last_retrain_count}

            df = bq_client.query(query).to_dataframe()

            # Iterate over rows using iterrows()
            row_list = []
            for index, row in df.iterrows():
                row_dict = {}
                row_dict["index"] = index
                row_dict["target_search_trend"] = row["target_trend"]
                row_dict["brand"] = row["brand"]
                row_dict["target_audience"] = row["target_audience"]
                row_dict["target_product"] = row["target_product"]
                row_dict["key_selling_point"] = row["key_selling_point"]
                row_list.append(row_dict)

            # for trend_dict in row_list:
            #     response = asyncio.run(
            #         create_agent_run(
            #             agent_id=agent_resource_id,
            #             msg_dict=trend_dict,
            #             user_id=f"{_USER_ID}_{row_dict['index']}",
            #         )
            #     )
            #     logging.info(response)

            row_dict = {}
            row_dict["index"] = index
            row_dict["target_search_trend"] = row_list[0]["target_search_trend"]
            row_dict["brand"] = row_list[0]["brand"]
            row_dict["target_audience"] = row_list[0]["target_audience"]
            row_dict["target_product"] = row_list[0]["target_product"]
            row_dict["key_selling_point"] = row_list[0]["key_selling_point"]

            response = asyncio.run(
                create_agent_run(
                    agent_id=agent_resource_id,
                    msg_dict=row_dict,
                    user_id=f"{_USER_ID}_{row_dict['index']}",
                )
            )
            logging.info(response)
            insert_bq_data(f"{bq_client.project}.{dataset}.count", current_rows)

    else:
        logging.info("No BigQuery info provided")
        return
