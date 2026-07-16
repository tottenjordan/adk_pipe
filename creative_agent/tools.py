import os
import asyncio
import logging
import warnings

from google.adk.tools import ToolContext

from agent_common import collect_degradation_warnings
from .config import config
from . import gallery_template as gt

# Backward-compatible re-exports: keep the public ``creative_agent.tools`` import
# surface unchanged after the implementation moved into sibling modules. Some of
# these (``_get_high_res_img``, ``_upload_blob_to_gcs``) are also used by
# ``save_creative_gallery_html`` below.
from .image_tools import (  # noqa: F401
    generate_image,
    _generate_image_with_backoff,
    _is_retryable_genai_error,
    _IMAGE_GEN_MAX_ATTEMPTS,
    _IMAGE_GEN_BASE_DELAY_SECS,
    _IMAGE_GEN_MAX_DELAY_SECS,
)
from .bq_tools import (  # noqa: F401
    build_eval_bq_row,
    write_trends_to_bq,
    write_eval_report_to_bq,
    _get_bigquery_client,
)
from .gcs_tools import (  # noqa: F401
    save_draft_report_artifact,
    save_eval_report_to_gcs,
    artifact_key_for,
    _get_gcs_client,
    _download_blob,
    _save_to_gcs,
    _upload_blob_to_gcs,
    _get_high_res_img,
)


# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")


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
            ARTIFACT_KEY = artifact_key_for(entry["concept_name"])
            GCS_BLOB_PATH = f"{gcs_folder}/{gcs_subdir}/{ARTIFACT_KEY}"
            AUTH_GCS_URL = f"https://storage.mtls.cloud.google.com/{config.GCS_BUCKET_NAME}/{GCS_BLOB_PATH}?authuser=3"

            # get high-res image (fall back to standard-res if missing).
            # _get_high_res_img does blocking download/resize/upload — off the loop.
            try:
                HIGH_RES_AUTH_GCS_URL = await asyncio.to_thread(
                    _get_high_res_img,
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

        # Save the HTML to a new file (blocking file + network I/O — off the loop)
        REPORT_NAME = "creative_portfolio_gallery.html"

        def _write_html() -> None:
            with open(REPORT_NAME, "w", encoding="utf-8") as html_file:
                html_file.write(FINAL_HTML)

        await asyncio.to_thread(_write_html)

        # save HTML file to cloud storage
        gcs_blob_name = f"{gcs_folder}/{gcs_subdir}/{REPORT_NAME}"
        gcs_uri = f"gs://{config.GCS_BUCKET_NAME}/{gcs_blob_name}"
        await asyncio.to_thread(
            _upload_blob_to_gcs,
            source_file_name=REPORT_NAME,
            destination_blob_name=gcs_blob_name,
        )
        await asyncio.to_thread(os.remove, REPORT_NAME)

        return {
            "status": "success",
            "gcs_uri": gcs_uri,
        }

    except Exception as e:
        # Propagate so ADK 2.0 RetryConfig can retry transient infra failures.
        logging.exception(f"Error saving artifact: {e}")
        raise
