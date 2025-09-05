import os
import logging

logging.basicConfig(level=logging.INFO)

from pathlib import Path
import datetime, json, shutil
from dotenv import load_dotenv
from google.cloud import storage
from google.cloud import bigquery
from google.adk.tools import ToolContext


# ==============================
# Load environment variables
# =============================
root_dir = Path(__file__).parent.parent
dotenv_path = root_dir / ".env"
load_dotenv(dotenv_path=dotenv_path)
logging.info(f"root_dir: {root_dir}")

try:
    # replaced `os.getenv()`
    GCS_BUCKET = os.environ.get("BUCKET")
    BRAND = os.environ.get("BRAND")
    TARGET_PRODUCT = os.environ.get("TARGET_PRODUCT")
    TARGET_AUDIENCE = os.environ.get("TARGET_AUDIENCE")
    KEY_SELLING_POINT = os.environ.get("KEY_SELLING_POINT")
except KeyError:
    raise Exception("environment variables not set")

logging.info(f"BRAND: {BRAND}")
logging.info(f"TARGET_PRODUCT: {TARGET_PRODUCT}")
logging.info(f"TARGET_AUDIENCE: {TARGET_AUDIENCE}")
logging.info(f"KEY_SELLING_POINT: {KEY_SELLING_POINT}")


# ==============================
# clients
# =============================
def get_gcs_client() -> storage.Client:
    """Get a configured GCS client."""
    return storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"))


def get_bigquery_client() -> bigquery.Client:
    """Get a configured BigQuery client."""
    return bigquery.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"))


# ==============================
# Google Search Trends (context)
# =============================
def get_gtrends_max_date() -> str:
    query = f"""
        SELECT 
         MAX(refresh_date) as max_date
        FROM `bigquery-public-data.google_trends.top_terms`
    """
    bq_client = get_bigquery_client()
    max_date = bq_client.query(query).to_dataframe()
    return max_date.iloc[0][0].strftime("%m/%d/%Y")


max_date = get_gtrends_max_date()


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
    max_date = get_gtrends_max_date()
    # max_date = "07/15/2025"
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
        bq_client = get_bigquery_client()

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

    except Exception as e:
        # return {"status": "error", "error_message": str(e)}
        return str(e)

    # return {
    #     "status": "ok",
    #     f"markdown_table": markdown_string,
    # }
    return results


def write_to_file(content: str, tool_context: ToolContext) -> dict:
    """
    Writes the given content to a timestamped markdown file.

    Args:
        content (str): Full markdown content as a string to be saved to disk.
        tool_context (ToolContext): The tool context.

    Returns:
        dict: A dictionary containing the status and generated filename.
    """

    LOCAL_DIR = "output"
    gcs_folder = tool_context.state["gcs_folder"]

    # Example: "250611_142317"
    timestamp = datetime.datetime.now().strftime("%y%m%d_%H%M%S")

    # Construct the output filename using the timestamp.
    # Example: "output/250611_142317_selected_trends.md"
    artifact_key = f"{timestamp}_selected_trends.md"
    local_file = f"{LOCAL_DIR}/{artifact_key}"

    # Ensure the "output" directory exists. If it doesn’t, create it.
    # `exist_ok=True` prevents an error if the directory already exists.
    Path(LOCAL_DIR).mkdir(exist_ok=True)

    # Write the markdown content to the constructed file.
    # `encoding='utf-8'` ensures proper character encoding.
    Path(local_file).write_text(content)  # , encoding="utf-8"

    # save to GCS
    # storage_client = storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    storage_client = get_gcs_client()
    gcs_bucket = os.environ.get("BUCKET", "tmp")
    gcs_bucket = gcs_bucket.replace("gs://", "")
    bucket = storage_client.bucket(gcs_bucket)
    blob = bucket.blob(os.path.join(gcs_folder, local_file))
    blob.upload_from_filename(local_file)

    try:
        shutil.rmtree(LOCAL_DIR)
        logging.info(f"Directory '{LOCAL_DIR}' and its contents removed successfully")
    except FileNotFoundError:
        logging.exception(f"Directory '{LOCAL_DIR}' not found")

    # Return a dictionary indicating success, and the artifact_key that was written.
    return {
        "status": "success",
        "gcs_bucket": gcs_bucket,
        "gcs_folder": gcs_folder,
        "file": local_file,
    }


def write_to_json(tool_context: ToolContext) -> dict:
    """
    Writes the selected trends to JSON.

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: A dictionary containing the selected trend terms.
    """

    LOCAL_DIR = "output"
    gcs_folder = tool_context.state["gcs_folder"]
    selected_trends_list = tool_context.state["target_search_trends"][
        "target_search_trends"
    ]
    data = {"selected trends": selected_trends_list}

    # Construct the output filename using the timestamp.
    # Example: "output/selected_trends.json"
    artifact_key = f"selected_trends.json"
    local_file = f"{LOCAL_DIR}/{artifact_key}"

    # Ensure the "output" directory exists. If it doesn’t, create it.
    # `exist_ok=True` prevents an error if the directory already exists.
    Path(LOCAL_DIR).mkdir(exist_ok=True)

    # Write to local file
    with open(local_file, "w") as f:
        json.dump(data, f, indent=4)

    # save to GCS
    storage_client = get_gcs_client()
    gcs_bucket = os.environ.get("BUCKET", "tmp")
    gcs_bucket = gcs_bucket.replace("gs://", "")
    bucket = storage_client.bucket(gcs_bucket)
    blob = bucket.blob(os.path.join(gcs_folder, local_file))
    blob.upload_from_filename(local_file)

    try:
        shutil.rmtree(LOCAL_DIR)
        logging.info(f"Directory '{LOCAL_DIR}' and its contents removed successfully")
    except FileNotFoundError:
        logging.exception(f"Directory '{LOCAL_DIR}' not found")

    # Return a dictionary indicating success, and the artifact_key that was written.
    return {
        "status": "success",
        "gcs_bucket": gcs_bucket,
        "gcs_folder": gcs_folder,
        "file": local_file,
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
