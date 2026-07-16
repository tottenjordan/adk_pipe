import os
import uuid
import string
import random
import asyncio
import logging
import warnings
import json
import datetime
from zoneinfo import ZoneInfo
import shutil
from PIL import Image
from markdown_pdf import MarkdownPdf, Section

from google import genai
from google.genai import types
from google.genai import errors as genai_errors
from google.cloud import storage
from google.cloud import bigquery
from google.adk.tools import ToolContext

from agent_common.locations import MODEL_LOCATION
from agent_common import collect_degradation_warnings
from .config import config
from . import gallery_template as gt


# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")


# ==============================
# clients
# ==============================
def _get_gcs_client() -> storage.Client:
    """Get a configured GCS client."""
    return storage.Client(project=config.PROJECT_ID)


def _get_bigquery_client() -> bigquery.Client:
    """Get a configured BigQuery client."""
    return bigquery.Client(project=config.BQ_PROJECT_ID)


# This client serves the image-gen model (gemini-3.1-flash-image), a gemini-3.x
# model served only from `global` — hence MODEL_LOCATION, not config.LOCATION
# (which is the injected regional value inside a deployed Agent Engine).
client = genai.Client(
    vertexai=True,
    project=config.PROJECT_ID,
    location=MODEL_LOCATION,
)

# Create a translation table to map punctuation characters to None (removal)
REMOVE_PUNCTUATION = str.maketrans("", "", string.punctuation)


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


# The image model (gemini-3.1-flash-image) is capped at ~2 RPM on the `global`
# endpoint (project-wide, shared), and this direct genai call is NOT wrapped by
# ADK's workflow RetryConfig (that only retries Agent *model* calls, not tool
# functions). A concurrent burst reliably trips 503 UNAVAILABLE / 429
# RESOURCE_EXHAUSTED, so we retry here with exponential backoff + jitter to pace
# under quota. See docs/notes/ambient-agents-vs-cloud-functions.md.
_IMAGE_GEN_MAX_ATTEMPTS = 5
_IMAGE_GEN_BASE_DELAY_SECS = 20.0
_IMAGE_GEN_MAX_DELAY_SECS = 90.0


def _is_retryable_genai_error(exc: Exception) -> bool:
    """True for transient/quota-paced genai errors: 5xx (ServerError) and 429."""
    if isinstance(exc, genai_errors.ServerError):  # 5xx incl. 503 UNAVAILABLE
        return True
    if isinstance(exc, genai_errors.ClientError) and getattr(exc, "code", None) == 429:
        return True
    return False


async def _generate_image_with_backoff(**kwargs):
    """Invoke the image model, retrying transient 503/429 with backoff + jitter.

    Non-retryable errors and the final attempt propagate unchanged so the caller
    (and ADK) still see genuine failures.
    """
    for attempt in range(_IMAGE_GEN_MAX_ATTEMPTS):
        try:
            return client.models.generate_content(**kwargs)
        except Exception as exc:
            if (
                not _is_retryable_genai_error(exc)
                or attempt == _IMAGE_GEN_MAX_ATTEMPTS - 1
            ):
                raise
            delay = min(
                _IMAGE_GEN_MAX_DELAY_SECS, _IMAGE_GEN_BASE_DELAY_SECS * 2**attempt
            )
            delay += random.uniform(0, delay * 0.25)  # jitter to de-sync workers
            logging.warning(
                f"Image gen transient error "
                f"(attempt {attempt + 1}/{_IMAGE_GEN_MAX_ATTEMPTS}): {exc}. "
                f"Retrying in {delay:.1f}s"
            )
            await asyncio.sleep(delay)


async def generate_image(
    tool_context: ToolContext,
):
    f"""Generates an image based on the prompt for {config.image_gen_model}

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: Status and the artifact_key of the generated image.
    """
    # Idempotency guard: skip if images were already generated
    if tool_context.state.get("_images_generated"):
        existing_keys = tool_context.state.get("_generated_artifact_keys", [])
        return {
            "status": "success",
            "message": f"Images already generated: {existing_keys}",
        }

    # get constants
    gcs_folder = tool_context.state["gcs_folder"]
    gcs_subdir = tool_context.state["agent_output_dir"]

    # get artifact details
    final_visual_concepts_dict = tool_context.state.get("final_visual_concepts")
    final_visual_concepts_list = final_visual_concepts_dict["visual_concepts"]

    artifact_keys_list = []
    for entry in final_visual_concepts_list:
        try:
            response = await _generate_image_with_backoff(
                model=config.image_gen_model,
                contents=entry["image_generation_prompt"],
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                ),
            )

            # Gemini image models return the image as inline data on a content part,
            # unlike Imagen's generate_images (which returns response.generated_images).
            image_bytes = None
            image_mime_type = "image/png"
            candidates = response.candidates or []
            if candidates and candidates[0].content and candidates[0].content.parts:
                for part in candidates[0].content.parts:
                    if part.inline_data is not None and part.inline_data.data:
                        image_bytes = part.inline_data.data
                        image_mime_type = part.inline_data.mime_type or image_mime_type
                        break

            if image_bytes is not None:
                # define artifact key
                ARTIFACT_NAME = (
                    entry["concept_name"]
                    .translate(REMOVE_PUNCTUATION)
                    .replace(" ", "_")
                )
                artifact_key = f"{ARTIFACT_NAME}.png"

                # save img to Cloud Storage
                img_gcs_uri = _save_to_gcs(
                    tool_context=tool_context,
                    image_bytes=image_bytes,
                    filename=artifact_key,
                )
                if (
                    isinstance(img_gcs_uri, dict)
                    and img_gcs_uri.get("status") == "error"
                ):
                    logging.error(
                        f"GCS upload failed for '{artifact_key}': {img_gcs_uri.get('message')}"
                    )

                # save ADK artifact
                img_artifact = types.Part.from_bytes(
                    data=image_bytes, mime_type=image_mime_type
                )
                await tool_context.save_artifact(
                    filename=artifact_key, artifact=img_artifact
                )
                logging.info(
                    f"Saved image artifact, '{artifact_key}', to '{img_gcs_uri}'"
                )
                artifact_keys_list.append(artifact_key)

            else:
                logging.error(f"Error with image generation response: {str(response)}")

        except Exception as e:
            # Propagate so ADK 2.0 RetryConfig can retry transient infra failures.
            logging.exception(f"No images generated. {e}")
            raise

    # Mark as done so subsequent calls are idempotent
    tool_context.state["_images_generated"] = True
    tool_context.state["_generated_artifact_keys"] = artifact_keys_list

    return {
        "status": "success",
        "message": f"Saved img artifacts: {artifact_keys_list} to `gs://{config.GCS_BUCKET_NAME}/{gcs_folder}/{gcs_subdir}`",
    }


def _build_research_warning_banner(warnings: list[str]) -> str:
    """Render a degradation banner for the HTML gallery, or "" when research was clean.

    Pure (no client/state) so it is unit-testable. `warnings` come from
    `collect_degradation_warnings(state)` — the single source of truth shared with
    the eval report and the `research_gaps` BigQuery column.
    """
    if not warnings:
        return ""
    items = "".join(f"<li>{note}</li>" for note in warnings)
    return f"""
            <div class="research-warning">
                <strong>⚠️ Research notes:</strong>
                <ul>{items}</ul>
            </div>
    """


async def save_creative_gallery_html(tool_context: ToolContext) -> dict:
    """
    Saves generated HTML report to Cloud Storage.

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: Status and the location of the HTML artifact file.
    """
    brand = tool_context.state["brand"]
    target_product = tool_context.state["target_product"]
    key_selling_points = tool_context.state["key_selling_points"]
    target_audience = tool_context.state["target_audience"]
    target_search_trends = tool_context.state["target_search_trends"]
    gcs_folder = tool_context.state["gcs_folder"]
    gcs_subdir = tool_context.state["agent_output_dir"]

    # get artifact details
    final_visual_concepts_dict = tool_context.state.get("final_visual_concepts")
    final_visual_concepts_list = final_visual_concepts_dict["visual_concepts"]

    # get ad copy details
    final_ad_copy_dict = tool_context.state.get("ad_copy_critique")
    final_ad_copy_list = final_ad_copy_dict["ad_copies"]

    # Degradation banner: if any research producer exhausted its retries, surface
    # it on the deliverable (single source of truth shared with the eval report /
    # BQ research_gaps). Empty string renders nothing on the happy path.
    research_warning_banner = _build_research_warning_banner(
        collect_degradation_warnings(tool_context.state)
    )

    try:
        # =========================== #
        # CSS formatting for HTML
        # =========================== #

        HTML_BODY = f"""

            <h1>{brand} {target_product}</h1>
            {research_warning_banner}
            <!-- Sub-headers -->
            <div class="sub-header-container">
                <h3><strong>key selling point(s):</strong>  {key_selling_points}</h3>
                <h3><strong>search trend:</strong> <span class="enlarged-text">'{target_search_trends}'</span></h3>
                <h3><strong>target audience:</strong>  {target_audience}</h3>
            </div>

            <h1>Ad Creatives</h1>

            <div class="gallery-container">
        """

        # =========================== #
        # ad creatives HTML chunks
        # =========================== #

        CONNECTED_GALLERY_STRING = ""
        for index, entry in enumerate(final_visual_concepts_list):
            ARTIFACT_NAME = (
                entry["concept_name"].translate(REMOVE_PUNCTUATION).replace(" ", "_")
            )
            ARTIFACT_KEY = f"{ARTIFACT_NAME}.png"
            GCS_BLOB_PATH = f"{gcs_folder}/{gcs_subdir}/{ARTIFACT_KEY}"
            AUTH_GCS_URL = f"https://storage.mtls.cloud.google.com/{config.GCS_BUCKET_NAME}/{GCS_BLOB_PATH}?authuser=3"

            # get high-res image (fall back to standard-res if missing)
            try:
                HIGH_RES_AUTH_GCS_URL = _get_high_res_img(
                    gcs_folder=tool_context.state["gcs_folder"],
                    gcs_subdir=tool_context.state["agent_output_dir"],
                    artifact_key=ARTIFACT_KEY,
                )
            except Exception as e:
                logging.warning(
                    f"Could not create high-res image for '{ARTIFACT_KEY}', falling back to standard-res: {e}"
                )
                HIGH_RES_AUTH_GCS_URL = AUTH_GCS_URL

            # generate HTML block for gallery images
            GALLERY_IMAGE_BLOCK = f"""
                <!-- Image {index + 1} -->
                <div class="gallery-item">
                    <h4 class="image-title">{entry["headline"]}</h4>
                    <div class="image-container">
                        <img src="{AUTH_GCS_URL}" 
                                data-high-res-src="{HIGH_RES_AUTH_GCS_URL}"
                                alt="{entry["concept_summary"].replace('"', "'")}" 
                                title="{entry["headline"]}">
                        <div class="hover-text">
                            <div class="hover-snippet snippet-top-left"><strong>Trend Reference:</strong>{entry["trend_reference"].replace('"', "'")}</div>
                            <div class="hover-snippet snippet-top-right"><strong>Visual Concept Name:</strong>{entry["concept_name"]}</div>
                            <div class="hover-snippet snippet-bottom-left"><strong>How it markets Target Product:</strong>{entry["markets_product"].replace('"', "'")}</div>
                            <div class="hover-snippet snippet-bottom-right"><strong>Target audience appeal:</strong>{entry["audience_appeal"].replace('"', "'")}</div>
                        </div>
                    </div>
                    <p class="caption">{entry["social_caption"]}</p>
                </div>
            """
            CONNECTED_GALLERY_STRING += GALLERY_IMAGE_BLOCK

        # =========================== #
        # visual concepts HTML chunks
        # =========================== #

        CONNECTED_VS_STRING = ""
        for index, entry in enumerate(final_visual_concepts_list):
            # generate HTML block for visual concepts
            VISUAL_CONCEPT_BLOCK = f"""
                    <!-- Visual Concept {index + 1} -->
                    <div class="content-card">
                        <dl>
                            <dt>Name:</dt> <dd>{entry["concept_name"]}</dd>
                            <dt>Trend:</dt> <dd>{entry["trend"]}</dd>
                            <dt>Creative Concept Explained:</dt> <dd>{entry["concept_summary"]}</dd>
                            <dt>Why this will perform well:</dt> <dd>{entry["selection_rationale"]}</dd>
                            <dt>prompt</dt> <dd>{entry["image_generation_prompt"]}</dd>
                        </dl>
                    </div>
            """
            CONNECTED_VS_STRING += VISUAL_CONCEPT_BLOCK

        # =========================== #
        # ad copy HTML chunks
        # =========================== #

        CONNECTED_AD_COPY_STRING = ""
        for index, entry in enumerate(final_ad_copy_list):
            # generate HTML block for ad copies
            AD_COPY_BLOCK = f"""
                    <!-- Ad Copy {index + 1} -->
                    <div class="content-card">
                        <dl>
                            <dt>Headline:</dt> <dd>{entry["headline"]}</dd>
                            <dt>Body Text:</dt> <dd>{entry["body_text"]}</dd>
                            <dt>Social Media Caption:</dt> <dd>{entry["social_caption"]}</dd>
                            <dt>Call-to-Action:</dt> <dd>{entry["call_to_action"]}</dd>
                            <dt>Trend-Reference:</dt> <dd>{entry["trend_connection"]}</dd>
                            <dt>Audience Appeal:</dt> <dd>{entry["audience_appeal_rationale"]}</dd>
                            <dt>Performance Rationale:</dt> <dd>{entry["detailed_performance_rationale"]}</dd>
                        </dl>
                    </div>
            """
            CONNECTED_AD_COPY_STRING += AD_COPY_BLOCK

        # concat all strings to form HTML doc
        FINAL_HTML = (
            gt.HTML_TEMPLATE
            + HTML_BODY
            + CONNECTED_GALLERY_STRING
            + gt.HTML_POST_GALLERY
            + gt.HTML_PRE_VS
            + CONNECTED_VS_STRING
            + gt.HTML_POST_VS
            + gt.HTML_PRE_AD_COPY
            + CONNECTED_AD_COPY_STRING
            + gt.HTML_POST_AD_COPY
            + gt.HTML_END_JAVASCRIPT
        )

        # Save the HTML to a new file
        REPORT_NAME = "creative_portfolio_gallery.html"
        with open(REPORT_NAME, "w", encoding="utf-8") as html_file:
            html_file.write(FINAL_HTML)

        # save HTML file to cloud storage
        gcs_blob_name = f"{gcs_folder}/{gcs_subdir}/{REPORT_NAME}"
        gcs_uri = f"gs://{config.GCS_BUCKET_NAME}/{gcs_blob_name}"
        _upload_blob_to_gcs(
            source_file_name=REPORT_NAME,
            destination_blob_name=gcs_blob_name,
        )
        os.remove(REPORT_NAME)

        return {
            "status": "success",
            "gcs_uri": gcs_uri,
        }

    except Exception as e:
        # Propagate so ADK 2.0 RetryConfig can retry transient infra failures.
        logging.exception(f"Error saving artifact: {e}")
        raise


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
        if not os.path.exists(DIR):
            os.makedirs(DIR)

        local_filepath = f"{DIR}/{artifact_key}"

        # create markdown PDF object
        pdf = MarkdownPdf(toc_level=4)
        pdf.add_section(Section(f" {processed_report}\n"))
        pdf.meta["title"] = "[Draft] Trend & Campaign Research Report"
        pdf.save(local_filepath)

        # open pdf and read bytes for types.Part() object
        with open(local_filepath, "rb") as f:
            document_bytes = f.read()

        document_part = types.Part(
            inline_data=types.Blob(data=document_bytes, mime_type="application/pdf")
        )
        version = await tool_context.save_artifact(
            filename=artifact_key, artifact=document_part
        )
        # save to gcs
        _upload_blob_to_gcs(
            source_file_name=local_filepath,
            destination_blob_name=gcs_blob_name,
        )
        # save to session state
        gcs_uri = f"gs://{gcs_bucket}/{gcs_blob_name}"
        tool_context.state["research_report_gcs_uri"] = gcs_uri
        logging.info(
            f"\n\nSaved artifact doc '{artifact_key}', version {version}, to: '{gcs_uri}' \n\n"
        )
        # clean up
        shutil.rmtree(DIR)
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


def build_eval_bq_row(
    *,
    report: dict,
    eval_uuid: str,
    creative_uuid: str,
    now_datetime: str,
    target_trend: str,
    brand: str,
    target_product: str,
    eval_report_gcs_uri: str,
) -> dict:
    """Flatten a CreativeEvaluationReport dict into one BigQuery row.

    Pure (no client, no wall-clock) so it is unit-testable. Numeric fields are
    coerced because the judge's JSON round-trip can hand back stringified numbers.
    """
    summary = report.get("summary", {})
    weakest = summary.get("weakest_dimensions") or []
    warnings = report.get("warnings") or []
    return {
        "uuid": eval_uuid,
        "creative_uuid": creative_uuid,
        "datetime": now_datetime,
        "target_trend": target_trend,
        "brand": brand,
        "target_product": target_product,
        "overall_pass_rate": float(summary.get("overall_pass_rate", 0.0)),
        "total_ad_copies": int(summary.get("total_ad_copies", 0)),
        "ad_copies_passed": int(summary.get("ad_copies_passed", 0)),
        "avg_ad_copy_score": float(summary.get("avg_ad_copy_score", 0.0)),
        "total_visual_concepts": int(summary.get("total_visual_concepts", 0)),
        "visual_concepts_passed": int(summary.get("visual_concepts_passed", 0)),
        "avg_visual_score": float(summary.get("avg_visual_score", 0.0)),
        "weakest_dimensions": ",".join(weakest),
        "eval_report_gcs_uri": eval_report_gcs_uri,
        # Degradation notes (research retries exhausted, etc.) surfaced from the
        # eval report's structured `warnings`. Empty string when research was clean.
        "research_gaps": " | ".join(warnings),
    }


def write_trends_to_bq(tool_context: ToolContext) -> dict:
    """
    Writes selected trends to a BigQuery Table.

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: A dictionary containing a 'status' key ('success' or 'error').
              On success, status is 'success' and includes a 'trends' key with the inserted terms
              On failure, status is 'error' and includes an 'error_message'.
    """
    bq_client = _get_bigquery_client()

    # values to insert
    unique_id = f"{str(uuid.uuid4())[:8]}"
    tool_context.state["creative_row_uuid"] = unique_id
    gcs_url_prefix = "https://console.cloud.google.com/storage/browser"
    gcs_folder = tool_context.state["gcs_folder"]
    gcs_dir = tool_context.state["agent_output_dir"]
    creative_gcs = f"{gcs_url_prefix}/{config.GCS_BUCKET_NAME}/{gcs_folder}/{gcs_dir}"

    try:
        # insert a row for the target search trend
        target_trend = tool_context.state["target_search_trends"]
        # write SQL — parameterized (@named) so a trend/brand/field containing a
        # quote or apostrophe can't break the statement or inject SQL. The table
        # name is a config-derived identifier (not parameterizable), not user input.
        sql_query = f"""
        INSERT INTO
            `{config.BQ_PROJECT_ID}.{config.BQ_DATASET_ID}.{config.BQ_TABLE_CREATIVES}` (uuid,
            target_trend,
            datetime,
            creative_gcs,
            brand,
            target_audience,
            target_product,
            key_selling_point)
        VALUES
        (
            @unique_id,
            @target_trend,
            CURRENT_DATETIME('America/New_York'),
            @creative_gcs,
            @brand,
            @target_audience,
            @target_product,
            @key_selling_points
        );
        """
        query_params = [
            bigquery.ScalarQueryParameter("unique_id", "STRING", unique_id),
            bigquery.ScalarQueryParameter("target_trend", "STRING", target_trend),
            bigquery.ScalarQueryParameter("creative_gcs", "STRING", creative_gcs),
            bigquery.ScalarQueryParameter(
                "brand", "STRING", tool_context.state["brand"]
            ),
            bigquery.ScalarQueryParameter(
                "target_audience", "STRING", tool_context.state["target_audience"]
            ),
            bigquery.ScalarQueryParameter(
                "target_product", "STRING", tool_context.state["target_product"]
            ),
            bigquery.ScalarQueryParameter(
                "key_selling_points",
                "STRING",
                tool_context.state["key_selling_points"],
            ),
        ]
        # make API request
        job = bq_client.query(
            sql_query,
            job_config=bigquery.QueryJobConfig(query_parameters=query_params),
        )
        job.result()  # wait for job to complete
        if job.errors:
            logging.error(
                f"DML INSERT job for trend: '{target_trend}' failed: {job.errors}"
            )
        else:
            logging.info(
                f"DML INSERT job {job.job_id} for trend: '{target_trend}' completed; added {job.num_dml_affected_rows} rows."
            )
        return {
            "status": "success",
            "trend": target_trend,
        }
    except Exception as e:
        # Propagate so ADK 2.0 RetryConfig can retry transient infra failures.
        logging.exception(f"Failed to insert row to bq: {e}")
        raise


def write_eval_report_to_bq(tool_context: ToolContext) -> dict:
    """Write a one-row creative-evaluation summary to BigQuery.

    Reads the report the evaluator stored in state, flattens it via
    build_eval_bq_row, and streams it to the ``BQ_TABLE_EVALS`` table. The row
    foreign-keys to the trend_creatives row via ``creative_row_uuid`` and links
    to the full per-dimension JSON already saved in GCS.
    """
    report = tool_context.state.get("creative_evaluation_report")
    if not report:
        return {
            "status": "error",
            "message": "No creative_evaluation_report found in session state.",
        }

    now_dt = (
        datetime.datetime.now(ZoneInfo("America/New_York"))
        .replace(tzinfo=None)
        .isoformat(sep=" ", timespec="seconds")
    )

    row = build_eval_bq_row(
        report=report,
        eval_uuid=str(uuid.uuid4())[:8],
        creative_uuid=tool_context.state.get("creative_row_uuid", ""),
        now_datetime=now_dt,
        target_trend=tool_context.state.get("target_search_trends", ""),
        brand=tool_context.state.get("brand", ""),
        target_product=tool_context.state.get("target_product", ""),
        eval_report_gcs_uri=tool_context.state.get("eval_report_gcs_uri", ""),
    )

    table_id = f"{config.BQ_PROJECT_ID}.{config.BQ_DATASET_ID}.{config.BQ_TABLE_EVALS}"
    try:
        bq_client = _get_bigquery_client()
        errors = bq_client.insert_rows_json(table_id, [row])
        if errors:
            logging.error(f"Eval-row insert into {table_id} failed: {errors}")
            raise RuntimeError(f"BigQuery insert returned errors: {errors}")
        logging.info(f"Inserted eval summary row {row['uuid']} into {table_id}.")
        return {"status": "success", "eval_uuid": row["uuid"]}
    except Exception as e:
        # Propagate so ADK 2.0 RetryConfig can retry transient infra failures.
        logging.exception(f"Failed to insert eval row to bq: {e}")
        raise


# =============================
# utils
# =============================
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

    with open(LOCAL_FILENAME, "wb") as file_obj:
        # Download the blob contents to the opened file object
        blob.download_to_file(file_obj)

    # convert to higher res
    img = Image.open(LOCAL_FILENAME)
    current_w, current_h = img.size
    new_w = int(current_w * 1.5)
    new_h = int(current_h * 1.5)
    resized_image = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    XL_LOCAL_FILENAME = f"XL_{LOCAL_FILENAME}"
    resized_image.save(XL_LOCAL_FILENAME)

    # upload to gcs
    mTLS_GCS_PREFIX = "https://storage.mtls.cloud.google.com"
    NEW_BLOB_NAME = f"{gcs_folder}/{gcs_subdir}/resized/{XL_LOCAL_FILENAME}"
    new_blob = bucket.blob(NEW_BLOB_NAME)
    new_blob.upload_from_filename(XL_LOCAL_FILENAME)

    # rm local file
    os.remove(LOCAL_FILENAME)
    os.remove(XL_LOCAL_FILENAME)
    high_res_auth_gcs_uri = (
        f"{mTLS_GCS_PREFIX}/{config.GCS_BUCKET_NAME}/{NEW_BLOB_NAME}?authuser=3"
    )
    return high_res_auth_gcs_uri
