"""Cloud Storage tools: uploads/downloads, PDF + eval-report persistence."""

import asyncio
import functools
import json
import logging
import os
import string
import tempfile

from google.adk.tools import ToolContext
from google.genai import types
from markdown_pdf import MarkdownPdf, Section

from agent_common.clients import get_gcs_client

from .brief_render import insert_brief_into_report
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


# Shared lazy getter (agent_common.clients), cached here so creative_agent keeps
# reusing one client (built lazily on first use). Bound to the historical private
# name so call sites + test monkeypatch points are unchanged.
_get_gcs_client = functools.cache(get_gcs_client)


def _download_blob(bucket_name, source_blob_name):
    """
    Downloads a blob from the bucket.
    Args:
        bucket_name (str): The ID of your GCS bucket
        source_blob_name (str): The ID of your GCS object
    Returns:
        Blob content as bytes.
    """
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


async def save_draft_report_artifact(tool_context: ToolContext) -> dict:
    """
    Saves generated PDF report bytes as an artifact.

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: Status and the location of the generated PDF artifact.
    """
    # get vars
    # The structured creative brief (when one was written) leads the report body.
    processed_report = insert_brief_into_report(
        tool_context.state["final_report_with_citations"],
        tool_context.state.get("creative_brief"),
    )
    gcs_bucket = config.GCS_BUCKET_NAME
    gcs_folder = tool_context.state["gcs_folder"]
    gcs_subdir = tool_context.state["agent_output_dir"]
    artifact_key = "research_report_with_citations.pdf"
    gcs_blob_name = f"{gcs_folder}/{gcs_subdir}/{artifact_key}"

    try:
        # Per-invocation temp dir: concurrent runs share this process's CWD, so a
        # bare relative scratch path let one run's cleanup race another's write
        # (issue #104 — [Errno 2] under N=5). TemporaryDirectory is unique per call
        # and auto-removed on context exit, even on a mid-function raise.
        with tempfile.TemporaryDirectory() as tmpdir:
            local_filepath = os.path.join(tmpdir, artifact_key)

            def _render_pdf() -> bytes:
                """Blocking: build the PDF on disk and return its bytes."""
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
