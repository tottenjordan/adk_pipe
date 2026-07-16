"""Cloud Storage tools: uploads/downloads, PDF + eval-report persistence, hi-res."""

import os
import json
import string
import shutil
import asyncio
import logging
import functools

from PIL import Image
from markdown_pdf import MarkdownPdf, Section

from google.genai import types
from google.cloud import storage
from google.adk.tools import ToolContext

from .config import config


# Create a translation table to map punctuation characters to None (removal).
# Single source of truth shared by image_tools.generate_image and
# tools.save_creative_gallery_html (both derive the same artifact key).
REMOVE_PUNCTUATION = str.maketrans("", "", string.punctuation)


def artifact_key_for(concept_name: str) -> str:
    """Derive the deterministic ``<name>.png`` artifact key from a concept name.

    Byte-identical to the historical inline derivation: strip punctuation, then
    replace spaces with underscores and append ``.png``.
    """
    return concept_name.translate(REMOVE_PUNCTUATION).replace(" ", "_") + ".png"


@functools.cache
def _get_gcs_client() -> storage.Client:
    """Get a configured GCS client (cached; built lazily on first use)."""
    return storage.Client(project=config.PROJECT_ID)


def _download_blob(bucket_name, source_blob_name):
    """
    Downloads a blob from the bucket.
    Args:
        bucket_name (str): The ID of your GCS bucket
        source_blob_name (str): The ID of your GCS object
    Returns:
        Blob content as bytes.
    """
    # storage_client = storage.Client()
    storage_client = _get_gcs_client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(source_blob_name)
    return blob.download_as_bytes()


def _save_to_gcs(
    tool_context: ToolContext,
    image_bytes: bytes,
    filename: str,
):
    # --- Save to GCS ---
    storage_client = _get_gcs_client()
    gcs_bucket = config.GCS_BUCKET_NAME
    bucket = storage_client.bucket(gcs_bucket)

    gcs_folder = tool_context.state["gcs_folder"]
    gcs_subdir = tool_context.state["agent_output_dir"]
    gcs_blob_name = f"{gcs_folder}/{gcs_subdir}/{filename}"

    blob = bucket.blob(gcs_blob_name)

    try:
        blob.upload_from_string(image_bytes, content_type="image/png")
        gcs_uri = f"gs://{gcs_bucket}/{gcs_blob_name}"

        return gcs_uri

    except Exception as e_gcs:
        # Propagate so ADK 2.0 RetryConfig can retry transient infra failures.
        logging.error(f"GCS upload failed for '{filename}': {e_gcs}")
        raise


def _upload_blob_to_gcs(
    source_file_name: str,
    destination_blob_name: str,
    # gcs_bucket: str,
) -> str:
    """
    Uploads a blob to a GCS bucket.
    Args:
        source_file_name (str): The path to the file to upload.
            e.g., "local/path/to/file" (file to upload)
        destination_blob_name (str): The desired folder path in gcs
            e.g., "folder/paths-to/storage-object-name"
    Returns:
        str: The GCS URI of the uploaded file.
    """
    storage_client = _get_gcs_client()
    gcs_bucket = config.GCS_BUCKET_NAME
    bucket = storage_client.bucket(gcs_bucket)
    blob = bucket.blob(destination_blob_name)
    blob.upload_from_filename(source_file_name)
    return f"File {source_file_name} uploaded to {destination_blob_name}."


def _get_high_res_img(gcs_folder: str, gcs_subdir: str, artifact_key: str):
    """
    gets existing img artifact, increases size, and  uploads to Cloud Storage

    Args:
        gcs_folder (str): folder within cloud storage bucket
        gcs_subdir (str): subfolder within Cloud Storage bucket
        artifact_key (str): name of the existing image artifact

    Returns:
        Authenticated Cloud Storage URI of the resized image
    """

    # get existing img artifact
    storage_client = _get_gcs_client()
    bucket = storage_client.bucket(config.GCS_BUCKET_NAME)
    blob = bucket.blob(f"{gcs_folder}/{gcs_subdir}/{artifact_key}")
    LOCAL_FILENAME = f"local_{artifact_key}"
    XL_LOCAL_FILENAME = f"XL_{LOCAL_FILENAME}"
    img = None

    try:
        with open(LOCAL_FILENAME, "wb") as file_obj:
            # Download the blob contents to the opened file object
            blob.download_to_file(file_obj)

        # convert to higher res
        img = Image.open(LOCAL_FILENAME)
        current_w, current_h = img.size
        new_w = int(current_w * 1.5)
        new_h = int(current_h * 1.5)
        resized_image = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        resized_image.save(XL_LOCAL_FILENAME)

        # upload to gcs
        mTLS_GCS_PREFIX = "https://storage.mtls.cloud.google.com"
        NEW_BLOB_NAME = f"{gcs_folder}/{gcs_subdir}/resized/{XL_LOCAL_FILENAME}"
        new_blob = bucket.blob(NEW_BLOB_NAME)
        new_blob.upload_from_filename(XL_LOCAL_FILENAME)

        high_res_auth_gcs_uri = (
            f"{mTLS_GCS_PREFIX}/{config.GCS_BUCKET_NAME}/{NEW_BLOB_NAME}?authuser=3"
        )
        return high_res_auth_gcs_uri

    finally:
        # Always release the PIL handle and remove both temp files, even on a
        # mid-function raise (download/resize/upload), so we never leak them.
        if img is not None:
            img.close()
        for tmp in (LOCAL_FILENAME, XL_LOCAL_FILENAME):
            try:
                os.remove(tmp)
            except OSError:
                pass


async def save_draft_report_artifact(tool_context: ToolContext) -> dict:
    """
    Saves generated PDF report bytes as an artifact.

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: Status and the location of the generated PDF artifact.
    """
    # get vars
    processed_report = tool_context.state["final_report_with_citations"]
    gcs_bucket = config.GCS_BUCKET_NAME
    gcs_folder = tool_context.state["gcs_folder"]
    gcs_subdir = tool_context.state["agent_output_dir"]
    artifact_key = "research_report_with_citations.pdf"
    gcs_blob_name = f"{gcs_folder}/{gcs_subdir}/{artifact_key}"

    try:
        DIR = "report_creatives"
        local_filepath = f"{DIR}/{artifact_key}"

        def _render_pdf() -> bytes:
            """Blocking: build the PDF on disk and return its bytes."""
            if not os.path.exists(DIR):
                os.makedirs(DIR)
            # create markdown PDF object
            pdf = MarkdownPdf(toc_level=4)
            pdf.add_section(Section(f" {processed_report}\n"))
            pdf.meta["title"] = "[Draft] Trend & Campaign Research Report"
            pdf.save(local_filepath)
            # open pdf and read bytes for types.Part() object
            with open(local_filepath, "rb") as f:
                return f.read()

        # PDF render + read is multi-second blocking work — run off the event loop.
        document_bytes = await asyncio.to_thread(_render_pdf)

        document_part = types.Part(
            inline_data=types.Blob(data=document_bytes, mime_type="application/pdf")
        )
        version = await tool_context.save_artifact(
            filename=artifact_key, artifact=document_part
        )
        # save to gcs (blocking network I/O — off the loop)
        await asyncio.to_thread(
            _upload_blob_to_gcs,
            source_file_name=local_filepath,
            destination_blob_name=gcs_blob_name,
        )
        # save to session state (must stay on the loop)
        gcs_uri = f"gs://{gcs_bucket}/{gcs_blob_name}"
        tool_context.state["research_report_gcs_uri"] = gcs_uri
        logging.info(
            f"\n\nSaved artifact doc '{artifact_key}', version {version}, to: '{gcs_uri}' \n\n"
        )
        # clean up (blocking filesystem work — off the loop)
        await asyncio.to_thread(shutil.rmtree, DIR)
        logging.info(f"Directory '{DIR}' and its contents removed successfully")

        return {
            "status": "success",
            "gcs_uri": gcs_uri,
        }

    except Exception as e:
        # Propagate so ADK 2.0 RetryConfig can retry transient infra failures.
        logging.exception(f"Error saving artifact: {e}")
        raise


def save_eval_report_to_gcs(tool_context: ToolContext) -> dict:
    """
    Saves the creative evaluation report JSON to Cloud Storage.

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: Status and the GCS URI of the saved evaluation report.
    """
    report_data = tool_context.state.get("creative_evaluation_report")
    if not report_data:
        return {
            "status": "error",
            "message": "No creative_evaluation_report found in session state.",
        }

    gcs_folder = tool_context.state["gcs_folder"]
    gcs_subdir = tool_context.state["agent_output_dir"]
    filename = "creative_eval_report.json"
    gcs_blob_name = f"{gcs_folder}/{gcs_subdir}/{filename}"
    gcs_uri = f"gs://{config.GCS_BUCKET_NAME}/{gcs_blob_name}"

    try:
        report_json = json.dumps(report_data, indent=2, default=str)

        storage_client = _get_gcs_client()
        bucket = storage_client.bucket(config.GCS_BUCKET_NAME)
        blob = bucket.blob(gcs_blob_name)
        blob.upload_from_string(report_json, content_type="application/json")

        tool_context.state["eval_report_gcs_uri"] = gcs_uri
        logging.info(f"Saved creative eval report to: '{gcs_uri}'")

        return {"status": "success", "gcs_uri": gcs_uri}

    except Exception as e:
        # Propagate so ADK 2.0 RetryConfig can retry transient infra failures.
        logging.exception(f"Error saving eval report to GCS: {e}")
        raise
