import asyncio
import html
import logging
import os
import tempfile

from google.adk.tools import ToolContext

from agent_common import collect_degradation_warnings
from agent_common.state import memorize  # noqa: F401  (ADK tool; re-exported)

from . import gallery_template as gt
from .bq_tools import (  # noqa: F401
    _get_bigquery_client,
    build_eval_bq_row,
    write_eval_report_to_bq,
    write_trends_to_bq,
)
from .brief_render import render_brief_summary_html
from .config import config
from .gcs_tools import (  # noqa: F401
    _download_blob,
    _get_gcs_client,
    _save_to_gcs,
    _upload_blob_to_gcs,
    artifact_key_for,
    save_draft_report_artifact,
    save_eval_report_to_gcs,
)

# Backward-compatible re-exports: keep the public ``creative_agent.tools`` import
# surface unchanged after the implementation moved into sibling modules. Some of
# these (e.g. ``_upload_blob_to_gcs``) are also used by
# ``save_creative_gallery_html`` below.
from .image_tools import (  # noqa: F401
    _IMAGE_GEN_BASE_DELAY_SECS,
    _IMAGE_GEN_MAX_ATTEMPTS,
    _IMAGE_GEN_MAX_DELAY_SECS,
    _generate_image_with_backoff,
    _is_retryable_genai_error,
    generate_image,
)

# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def _build_research_warning_banner(warnings: list[str]) -> str:
    """Render a degradation banner for the HTML gallery, or "" when research was clean.

    Pure (no client/state) so it is unit-testable. `warnings` come from
    `collect_degradation_warnings(state)` — the single source of truth shared with
    the eval report and the `research_gaps` BigQuery column. Notes can quote
    model text (e.g. `<key>__issues` examples), so each is HTML-escaped. The
    heading says "Run notes": the notes cover every degraded step, not only
    research.
    """
    if not warnings:
        return ""
    items = "".join(f"<li>{html.escape(note, quote=False)}</li>" for note in warnings)
    return f"""
            <div class="research-warning">
                <strong>⚠️ Run notes:</strong>
                <ul>{items}</ul>
            </div>
    """


def _build_image_check_line(record: dict | None) -> str:
    """The gallery's per-image "Image check" line, or "" with no QA verdict.

    Pure. ``record`` is ``generated_images[concept]`` (written by
    ``generate_image``); ``qa`` is None when image QA was disabled or
    unavailable, which renders nothing. Issues are model text → escaped.
    """
    qa = (record or {}).get("qa")
    if not isinstance(qa, dict):
        return ""
    if qa.get("passed"):
        text = "Image check: passed"
    else:
        failures = "; ".join(str(f) for f in qa.get("failures") or [])
        text = (
            f"Image check: issues — {failures}" if failures else "Image check: issues"
        )
    rerenders = int((record or {}).get("attempts") or 1) - 1
    if rerenders == 1:
        text += " (re-rendered once)"
    elif rerenders > 1:
        text += f" (re-rendered {rerenders} times)"
    return f'<p class="image-check">{html.escape(text, quote=False)}</p>'


def _esc(value: object) -> str:
    """HTML-escape model/user text for a text node (quotes left as-is)."""
    return html.escape(str(value), quote=False)


def _esc_attr(value: object) -> str:
    """HTML-escape model/user text for a double-quoted attribute value."""
    return html.escape(str(value), quote=True)


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
    # Small proposition + trend-fit card from the structured brief ("" if none).
    brief_summary = render_brief_summary_html(tool_context.state.get("creative_brief"))
    generated_images = tool_context.state.get("generated_images") or {}

    try:
        # =========================== #
        # CSS formatting for HTML
        # =========================== #

        HTML_BODY = f"""

            <h1>{_esc(brand)} {_esc(target_product)}</h1>
            {research_warning_banner}
            {brief_summary}
            <!-- Sub-headers -->
            <div class="sub-header-container">
                <h3><strong>key selling point(s):</strong>  {_esc(key_selling_points)}</h3>
                <h3><strong>search trend:</strong> <span class="enlarged-text">'{_esc(target_search_trends)}'</span></h3>
                <h3><strong>target audience:</strong>  {_esc(target_audience)}</h3>
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
            # The lightbox links the original 2K render: the old 1.5x Lanczos
            # upscale re-upload added no detail and cost seconds per image.

            # generate HTML block for gallery images
            GALLERY_IMAGE_BLOCK = f"""
                <!-- Image {index + 1} -->
                <div class="gallery-item">
                    <h4 class="image-title">{_esc(entry["headline"])}</h4>
                    <div class="image-container">
                        <img src="{_esc_attr(AUTH_GCS_URL)}" 
                                data-high-res-src="{_esc_attr(AUTH_GCS_URL)}"
                                alt="{_esc_attr(entry["concept_summary"])}" 
                                title="{_esc_attr(entry["headline"])}">
                        <div class="hover-text">
                            <div class="hover-snippet snippet-top-left"><strong>Trend Reference:</strong>{_esc(entry["trend_reference"])}</div>
                            <div class="hover-snippet snippet-top-right"><strong>Visual Concept Name:</strong>{_esc(entry["concept_name"])}</div>
                            <div class="hover-snippet snippet-bottom-left"><strong>How it markets Target Product:</strong>{_esc(entry["markets_product"])}</div>
                            <div class="hover-snippet snippet-bottom-right"><strong>Target audience appeal:</strong>{_esc(entry["audience_appeal"])}</div>
                        </div>
                    </div>
                    <p class="caption">{_esc(entry["social_caption"])}</p>
                    {_build_image_check_line(generated_images.get(entry["concept_name"]))}
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
                            <dt>Name:</dt> <dd>{_esc(entry["concept_name"])}</dd>
                            <dt>Style:</dt> <dd>{_esc(entry.get("visual_style", ""))}</dd>
                            <dt>Trend:</dt> <dd>{_esc(entry["trend"])}</dd>
                            <dt>Creative Concept Explained:</dt> <dd>{_esc(entry["concept_summary"])}</dd>
                            <dt>Why this will perform well:</dt> <dd>{_esc(entry["selection_rationale"])}</dd>
                            <dt>prompt</dt> <dd>{_esc(entry["image_generation_prompt"])}</dd>
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
                            <dt>Headline:</dt> <dd>{_esc(entry["headline"])}</dd>
                            <dt>Body Text:</dt> <dd>{_esc(entry["body_text"])}</dd>
                            <dt>Social Media Caption:</dt> <dd>{_esc(entry["social_caption"])}</dd>
                            <dt>Call-to-Action:</dt> <dd>{_esc(entry["call_to_action"])}</dd>
                            <dt>Trend-Reference:</dt> <dd>{_esc(entry["trend_connection"])}</dd>
                            <dt>Audience Appeal:</dt> <dd>{_esc(entry["audience_appeal_rationale"])}</dd>
                            <dt>Performance Rationale:</dt> <dd>{_esc(entry["detailed_performance_rationale"])}</dd>
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

        # Save the HTML to a temp file then upload (blocking file + network I/O —
        # off the loop). A per-invocation temp dir keeps concurrent runs from racing
        # on a shared CWD filename (issue #104): the bare 'creative_portfolio_...html'
        # let one run's os.remove delete the file another run was still uploading.
        # TemporaryDirectory is unique per call and auto-cleaned on context exit.
        REPORT_NAME = "creative_portfolio_gallery.html"
        gcs_blob_name = f"{gcs_folder}/{gcs_subdir}/{REPORT_NAME}"
        gcs_uri = f"gs://{config.GCS_BUCKET_NAME}/{gcs_blob_name}"

        def _write_and_upload() -> None:
            with tempfile.TemporaryDirectory() as td:
                path = os.path.join(td, REPORT_NAME)
                with open(path, "w", encoding="utf-8") as html_file:
                    html_file.write(FINAL_HTML)
                _upload_blob_to_gcs(
                    source_file_name=path,
                    destination_blob_name=gcs_blob_name,
                )

        await asyncio.to_thread(_write_and_upload)

        return {
            "status": "success",
            "gcs_uri": gcs_uri,
        }

    except Exception as e:
        # Propagate so ADK 2.0 RetryConfig can retry transient infra failures.
        logging.exception(f"Error saving artifact: {e}")
        raise
