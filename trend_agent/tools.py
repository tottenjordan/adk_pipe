import os
import logging

logging.basicConfig(level=logging.INFO)

import datetime
from pathlib import Path

from google.cloud import storage
from google.cloud import bigquery
from google.adk.tools import ToolContext


BQ_PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
bq_client = bigquery.Client(project=BQ_PROJECT)


# ==============================
# Google Search Trends (context)
# =============================
def get_gtrends_max_date() -> str:
    query = f"""
        SELECT 
         MAX(refresh_date) as max_date
        FROM `bigquery-public-data.google_trends.top_terms`
    """
    max_date = bq_client.query(query).to_dataframe()
    return max_date.iloc[0][0].strftime("%m/%d/%Y")


max_date = get_gtrends_max_date()


def get_daily_gtrends(tool_context: ToolContext, today_date: str = max_date) -> dict:
    """
    Retrieves the top 25 Google Search Trends (term, rank, refresh_date).

    Args:
        today_date: Today's date in the format 'MM/DD/YYYY'. Use the default value provided.

    Returns:
        dict: key is the latest date for the trends, the value is a markdown table containing the Google Search Trends.
             The table includes columns for 'term', 'rank', and 'refresh_date'.
             Returns 25 terms ordered by their rank (ascending order) for the current week.
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
        df_t = bq_client.query(query).to_dataframe()
        df_t.index += 1
        df_t["rank"] = df_t.index
        df_t = df_t.drop("x", axis=1)
        new_order = ["term", "rank", "refresh_date"]
        df_t = df_t[new_order]

        markdown_string = df_t.to_markdown(index=True)
        logging.info(f"\n\nmarkdown_string: {markdown_string}\n\n")

        # tool_context.state["start_gtrends"] = df_t.to_dict()
        tool_context.state["raw_gtrends"] = df_t['term'].to_list()
    except Exception as e:
        return {"status": "error", "error_message": str(e)}

    return {
        "status": "ok",
        f"markdown_table": markdown_string,
    }


def write_to_file(content: str, tool_context: ToolContext) -> dict:
    """
    Writes the given content to a timestamped markdown file.

    Args:
        content (str): Full markdown content as a string to be saved to disk.
        tool_context (ToolContext): The tool context.

    Returns:
        dict: A dictionary containing the status and generated filename.
    """

    local_dir = "output"
    gcs_folder = tool_context.state["gcs_folder"]

    # Example: "250611_142317"
    timestamp = datetime.datetime.now().strftime("%y%m%d_%H%M%S")

    # Construct the output filename using the timestamp.
    # Example: "output/250611_142317_selected_trends.md"
    artifact_key = f"{timestamp}_selected_trends.md"
    local_file = f"{local_dir}/{artifact_key}"

    # Ensure the "output" directory exists. If it doesn’t, create it.
    # `exist_ok=True` prevents an error if the directory already exists.
    Path(local_dir).mkdir(exist_ok=True)

    # Write the markdown content to the constructed file.
    # `encoding='utf-8'` ensures proper character encoding.
    Path(local_file).write_text(content) # , encoding="utf-8"

    # save to GCS 
    storage_client = storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    gcs_bucket = os.environ.get("BUCKET", "tmp")
    gcs_bucket = gcs_bucket.replace("gs://", "")
    bucket = storage_client.bucket(gcs_bucket)
    blob = bucket.blob(os.path.join(gcs_folder, local_file))
    blob.upload_from_filename(local_file)

    # Return a dictionary indicating success, and the artifact_key that was written.
    return {
        "status": "success",
        "gcs_bucket": gcs_bucket,
        "gcs_folder": gcs_folder,
        "file": local_file
    }


def save_search_trends_to_session_state(trend_term: str, tool_context: ToolContext) -> dict:
    """
    Tool to save `trend_term` to the 'final_select_ad_copies' state key.
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