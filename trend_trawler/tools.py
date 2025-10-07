import os
import logging
import datetime
import json, shutil, uuid
from pathlib import Path
from google.cloud import storage
from google.cloud import bigquery
from google.adk.tools import ToolContext

from .config import config


# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


# ==============================
# clients
# =============================
def _get_gcs_client() -> storage.Client:
    """Get a configured GCS client."""
    return storage.Client(project=config.PROJECT_ID)


def _get_bigquery_client() -> bigquery.Client:
    """Get a configured BigQuery client."""
    return bigquery.Client(project=config.BQ_PROJECT_ID)


# =============================
# tools
# =============================
def memorize(key: str, value: str, tool_context: ToolContext):
    """
    Memorize pieces of information, one key-value pair at a time.

    Args:
        key: the label indexing the memory to store the value.
        value: the information to be stored.
        tool_context: The ADK tool context.

    Returns:
        A status message.
    """
    mem_dict = tool_context.state
    mem_dict[key] = value
    return {"status": f'Stored "{key}": "{value}"'}


# ==============================
# Google Search Trends (context)
# =============================
def _get_gtrends_max_date() -> str:
    query = f"""
        SELECT 
         MAX(refresh_date) as max_date
        FROM `bigquery-public-data.google_trends.top_terms`
    """
    bq_client = _get_bigquery_client()
    max_date_df = bq_client.query(query).to_dataframe()
    return max_date_df.max_date.iloc[0].strftime("%m/%d/%Y")


max_date = _get_gtrends_max_date()


def get_daily_gtrends(tool_context: ToolContext, today_date: str = max_date) -> str:
    """
    Retrieves the top 25 Google Search Trends (term, rank, refresh_date).

    Args:
        today_date: Today's date in the format 'MM/DD/YYYY'. Use the default value provided.

    Returns:
        str: A markdown-formatted string listing the Google Search Trends and their corresponding
             rank, or an error message if the query fails.
             The table includes columns for 'term', 'rank', and 'refresh_date'.
             Returns 25 rows of results.
    """

    # get latest refresh date
    # max_date = _get_gtrends_max_date()
    logging.info(f"\n\nmax_date in trends_assistant: {max_date}\n\n")

    query = f"""
        SELECT
          term,
          refresh_date,
          ARRAY_AGG(STRUCT(rank,week) ORDER BY week DESC LIMIT 1) x
        FROM `bigquery-public-data.google_trends.top_terms`
        WHERE refresh_date = PARSE_DATE('%m/%d/%Y',  '{max_date}')
        GROUP BY term, refresh_date
        ORDER BY (SELECT rank FROM UNNEST(x))
        """
    try:
        bq_client = _get_bigquery_client()

        # Setup the query for BigQuery
        query_job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("parameter1", "STRING", max_date)
            ]
        )
        # Execute the BigQuery Query and retrieve results to local dataframe
        results = bq_client.query(query, job_config=query_job_config).to_dataframe()

        # Prepare dataframe
        results.index += 1
        results["rank"] = results.index
        results = results.drop("x", axis=1)
        new_order = ["term", "rank", "refresh_date"]
        results = results[new_order]

        # Update state
        tool_context.state["raw_gtrends"] = results["term"].to_list()

        # Convert the dataframe to Markdown
        results = results.to_markdown(index=True)

        return results
    except Exception as e:
        logging.exception(f"Failed to gather daily trends: {e}")
        return str(e)


def write_to_file(content: str, tool_context: ToolContext) -> dict:
    """
    Writes the given content to a markdown file. Saves the file to Google Cloud Storage.

    Args:
        content (str): Full markdown content as a string to be saved to disk.
        tool_context (ToolContext): The tool context.

    Returns:
        dict: A dictionary containing the status and the markdown file's Cloud Storage URI (gcs_uri).
    """

    LOCAL_DIR = tool_context.state["agent_output_dir"]
    gcs_folder = tool_context.state["gcs_folder"]

    # Construct the output filename e.g., "trawler_output/selected_trends.md"
    artifact_key = "selected_trends.txt"
    local_file = f"{LOCAL_DIR}/{artifact_key}"

    # Ensure the "trawler_output" directory exists. If it doesn’t, create it.
    # `exist_ok=True` prevents an error if the directory already exists.
    Path(LOCAL_DIR).mkdir(exist_ok=True)

    # Write the markdown content to the constructed file.
    # `encoding='utf-8'` ensures proper character encoding.
    Path(local_file).write_text(content)

    # save to GCS
    storage_client = _get_gcs_client()
    gcs_bucket = config.GCS_BUCKET_NAME
    bucket = storage_client.bucket(gcs_bucket)
    blob = bucket.blob(os.path.join(gcs_folder, local_file))
    blob.upload_from_filename(local_file)

    # save to session state
    gcs_blob_name = f"{gcs_folder}/{LOCAL_DIR}/{artifact_key}"
    gcs_uri = f"gs://{gcs_bucket}/{gcs_blob_name}"
    tool_context.state["select_trends_markdown_gcs_uri"] = gcs_uri

    try:
        shutil.rmtree(LOCAL_DIR)
        logging.info(f"Directory '{LOCAL_DIR}' and its contents removed successfully")
    except FileNotFoundError:
        logging.exception(f"Directory '{LOCAL_DIR}' not found")

    # Return a dictionary indicating success, and the artifact_key that was written.
    return {
        "status": "success",
        "gcs_uri": gcs_uri,
    }


def save_search_trends_to_session_state(
    trend_term: str, tool_context: ToolContext
) -> dict:
    """
    Tool to save `trend_term` to the 'target_search_trends' state key.
    Use this tool once the subset of trends have been selected.

    Args:
        trend_term (str): the selected trending search term.
        tool_context: The tool context.

    Returns:
        A status message.
    """
    existing_target_search_trends = tool_context.state.get("target_search_trends")

    if existing_target_search_trends is not {"target_search_trends": []}:
        existing_target_search_trends["target_search_trends"].append(trend_term)

    tool_context.state["target_search_trends"] = existing_target_search_trends
    return {"status": "ok"}


def save_session_state_to_gcs(tool_context: ToolContext) -> dict:
    """
    Writes the session state to JSON. Saves the JSON file to Cloud Storage.

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: A dictionary containing the status and the json file's Cloud Storage URI (gcs_uri).
    """

    session_state = tool_context.state.to_dict()
    LOCAL_DIR = session_state["agent_output_dir"]
    gcs_folder = session_state["gcs_folder"]

    filename = f"trawler_session_state.json"
    local_file = f"{LOCAL_DIR}/{filename}"

    # Ensure the `LOCAL_DIR` directory exists. If it doesn’t, create it.
    # `exist_ok=True` prevents an error if the directory already exists.
    Path(LOCAL_DIR).mkdir(exist_ok=True)

    # Write to local file
    with open(local_file, "w") as f:
        json.dump(session_state, f, indent=4)

    # save to GCS
    storage_client = _get_gcs_client()
    gcs_bucket = config.GCS_BUCKET_NAME
    bucket = storage_client.bucket(gcs_bucket)
    blob = bucket.blob(os.path.join(gcs_folder, local_file))
    blob.upload_from_filename(local_file)

    # save to session state
    gcs_blob_name = f"{gcs_folder}/{LOCAL_DIR}/{filename}"
    gcs_uri = f"gs://{gcs_bucket}/{gcs_blob_name}"

    try:
        shutil.rmtree(LOCAL_DIR)
        logging.info(f"Directory '{LOCAL_DIR}' and its contents removed successfully")
    except FileNotFoundError:
        logging.exception(f"Directory '{LOCAL_DIR}' not found")

    # Return a dictionary indicating status and the Cloud Storage URI.
    return {
        "status": "success",
        "gcs_uri": gcs_uri,
    }


def write_trends_to_bq(tool_context: ToolContext, refresh_date: str = max_date) -> dict:
    """
    Writes selected trends to a BigQuery Table.

    Args:
        tool_context (ToolContext): The tool context.
        refresh_date: Latest refresh date from the trends table in the format 'MM/DD/YYYY'. Use the default value provided.

    Returns:
        dict: A dictionary containing a 'status' key ('success' or 'error').
              On success, status is 'success' and includes a 'trends' key with the inserted terms
              On failure, status is 'error' and includes an 'error_message'.
    """
    bq_client = _get_bigquery_client()

    # values to insert
    unique_id = f"{str(uuid.uuid4())[:8]}"
    current_date = datetime.datetime.now().strftime("%m/%d/%Y")

    gcs_url_prefix = "https://console.cloud.google.com/storage/browser"
    gcs_folder = tool_context.state["gcs_folder"]
    gcs_dir = tool_context.state["agent_output_dir"]
    trawler_gcs = f"{gcs_url_prefix}/{config.GCS_BUCKET_NAME}/{gcs_folder}/{gcs_dir}"

    try:
        # insert a row for each selected target search trend
        target_trends = tool_context.state.get("target_search_trends")
        for trend in target_trends["target_search_trends"]:
            # write SQL
            sql_query = f"""
            INSERT INTO 
              `{config.BQ_PROJECT_ID}.{config.BQ_DATASET_ID}.{config.BQ_TABLE_TARGETS}` (uuid, 
                target_trend,
                refresh_date,
                trawler_date,
                trawler_gcs,
                brand,
                target_audience,
                target_product,
                key_selling_point)
            VALUES 
            (
                '{unique_id}', 
                '{trend}',
                PARSE_DATE('%m/%d/%Y', '{refresh_date}'), 
                PARSE_DATE('%m/%d/%Y', '{current_date}'),
                '{trawler_gcs}',
                '{tool_context.state["brand"]}',
                '{tool_context.state["target_audience"]}',
                '{tool_context.state["target_product"]}',
                '{tool_context.state["key_selling_points"]}'
            );
            """
            # make API request
            job = bq_client.query(sql_query)
            job.result()  # wait for job to complete
            if job.errors:
                logging.error(
                    f"DML INSERT job for trend: '{trend}' failed: {job.errors}"
                )
            else:
                logging.info(
                    f"DML INSERT job {job.job_id} for trend: '{trend}' completed; added {job.num_dml_affected_rows} rows."
                )
        return {
            "status": "success",
            "trends": ", ".join(target_trends["target_search_trends"]),
        }
    except Exception as e:
        logging.exception(f"Failed to insert rows to bq: {e}")
        return {
            "status": "error",
            "error_message": f"Error inserting rows to bq: {e}",
        }
