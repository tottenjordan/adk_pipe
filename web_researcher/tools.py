import os, json
import logging

logging.basicConfig(level=logging.INFO)

import shutil
from pathlib import Path
from google.cloud import storage
from google.adk.tools import ToolContext

from .config import config


# ==============================
# clients
# =============================
def get_gcs_client() -> storage.Client:
    """Get a configured GCS client."""
    return storage.Client(project=config.PROJECT_ID)


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


def write_to_file(tool_context: ToolContext) -> dict:
    """
    Writes the given content to a markdown file. Saves the file to Google Cloud Storage.

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: A dictionary containing the status and the markdown file's Cloud Storage URI (gcs_uri).
    """
    LOCAL_DIR = tool_context.state["agent_output_dir"]
    gcs_folder = tool_context.state["gcs_folder"]

    # Construct the output filename e.g., "trawler_output/selected_trends.md"
    artifact_key = "research_report_with_citations.md"
    local_file = f"{LOCAL_DIR}/{artifact_key}"

    # Ensure the "trawler_output" directory exists. If it doesn’t, create it.
    # `exist_ok=True` prevents an error if the directory already exists.
    Path(LOCAL_DIR).mkdir(exist_ok=True)

    # Write the markdown content to the constructed file.
    # `encoding='utf-8'` ensures proper character encoding.
    Path(local_file).write_text(
        tool_context.state["final_report_with_citations"], encoding="utf-8"
    )

    # save to GCS
    storage_client = get_gcs_client()
    gcs_bucket = config.GCS_BUCKET_NAME
    bucket = storage_client.bucket(gcs_bucket)
    blob = bucket.blob(os.path.join(gcs_folder, local_file))
    blob.upload_from_filename(local_file)

    # save to session state
    gcs_blob_name = f"{gcs_folder}/{LOCAL_DIR}/{artifact_key}"
    gcs_uri = f"gs://{gcs_bucket}/{gcs_blob_name}"
    tool_context.state["research_report_gcs_uri"] = gcs_uri

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

    filename = f"web_researcher_session_state.json"
    local_file = f"{LOCAL_DIR}/{filename}"

    # Ensure the `LOCAL_DIR` directory exists. If it doesn’t, create it.
    # `exist_ok=True` prevents an error if the directory already exists.
    Path(LOCAL_DIR).mkdir(exist_ok=True)

    # Write to local file
    with open(local_file, "w") as f:
        json.dump(session_state, f, indent=4)

    # save to GCS
    storage_client = get_gcs_client()
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
