import logging
import markdown
import os, string

logging.basicConfig(level=logging.INFO)

import json, shutil
from PIL import Image
from pathlib import Path
from markdown_pdf import MarkdownPdf, Section

from google import genai
from google.genai import types
from google.cloud import storage
from google.adk.tools import ToolContext

from .config import config


# ==============================
# clients
# =============================
def get_gcs_client() -> storage.Client:
    """Get a configured GCS client."""
    return storage.Client(project=config.PROJECT_ID)


client = genai.Client(
    vertexai=True,
    project=config.PROJECT_ID,
    location=config.LOCATION,
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


def save_select_ad_copy(select_ad_copy_dict: dict, tool_context: ToolContext) -> dict:
    """
    Tool to save `select_ad_copy_dict` to the 'final_select_ad_copies' state key.
    Use this tool after creating ad copies with the `ad_creative_pipeline` tool.

    Args:
        select_ad_copy_dict (dict): A dict representing an ad copy finalized for ad generation. Use the `tool_context` to extract the following schema:
            headline (str): A concise, attention-grabbing phrase.
            body_text (str): The main body of the ad copy. Should be compelling.
            caption (str): The candidate social media caption proposed for the ad copy.
            call_to_action (str): A catchy, action-oriented phrase intended for the target audience.
            trend_reference (str): How it relates to the trending topic: {target_search_trends}
            audience_appeal (str): A brief rationale for target audience appeal
            performance_rationale (str): A brief rationale explaining why this ad copy will perform well.
        tool_context: The tool context.

    Returns:
        A status message.
    """
    # name (str): An intuitive name of the ad copy concept.

    existing_ad_copies = tool_context.state.get(
        "final_select_ad_copies", {"final_select_ad_copies": []}
    )
    existing_ad_copies["final_select_ad_copies"].append(select_ad_copy_dict)
    tool_context.state["final_select_ad_copies"] = existing_ad_copies
    return {"status": "ok"}


def save_select_visual_concept(
    select_vis_concept_dict: dict, tool_context: ToolContext
) -> dict:
    """
    Tool to save `select_vis_concept_dict` to the 'final_select_vis_concepts' state key.
    Use this tool after creating visual concepts with the `visual_generation_pipeline` tool.

    Args:
        select_vis_concept_dict (dict): A dict representing a visual concept for ad generation. Use the `tool_context` to extract the following schema:
            name (str): An intuitive name of the visual concept.
            trend (str): The trend(s) referenced by this creative.
            headline (str): The attention-grabbing headline.
            caption (str): The candidate social media caption proposed for the visual concept.
            creative_explain (str): A brief explanation of the visual concept.
            trend_reference (str): How the visual concept relates to the `target_search_trends`
            audience_appeal (str): A brief explanation for the target audience appeal.
            markets_product (str): A brief explanation of how this markets the target product.
            rationale_perf (str): A brief rationale explaining why this visual concept will perform well.
            prompt (str): The suggested prompt to generate this creative.
        tool_context: The tool context.

    Returns:
        dict: the status of this functions overall outcome.
    """
    existing_vis_concepts = tool_context.state.get(
        "final_select_vis_concepts", {"final_select_vis_concepts": []}
    )
    existing_vis_concepts["final_select_vis_concepts"].append(select_vis_concept_dict)
    tool_context.state["final_select_vis_concepts"] = existing_vis_concepts
    return {"status": "ok"}


async def generate_image(
    tool_context: ToolContext,
):
    f"""Generates an image based on the prompt for {config.image_gen_model}

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: Status and the artifact_key of the generated image.
    """
    # get constants
    gcs_folder = tool_context.state["gcs_folder"]
    gcs_subdir = tool_context.state["agent_output_dir"]

    # get artifact details
    final_visual_concepts_dict = tool_context.state.get("final_select_vis_concepts")
    final_visual_concepts_list = final_visual_concepts_dict["final_select_vis_concepts"]

    artifact_keys_list = []
    for entry in final_visual_concepts_list:

        try:

            response = client.models.generate_images(
                model=config.image_gen_model,
                prompt=entry["prompt"],
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    enhance_prompt=False,
                ),
            )

            if (
                response.generated_images is not None
                and response.generated_images[0].image is not None
                and response.generated_images[0].image.image_bytes is not None
            ):
                # extract bytes && define artifact key
                image_bytes = response.generated_images[0].image.image_bytes
                ARTIFACT_NAME = (
                    entry["name"].translate(REMOVE_PUNCTUATION).replace(" ", "_")
                )
                artifact_key = f"{ARTIFACT_NAME}.png"

                # save img to Cloud Storage
                img_gcs_uri = _save_to_gcs(
                    tool_context=tool_context,
                    image_bytes=image_bytes,
                    filename=artifact_key,
                )

                # save ADK artifact
                img_artifact = types.Part.from_bytes(
                    data=image_bytes, mime_type="image/png"
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
            logging.exception(f"No images generated. {e}")
            return {"status": "error", "error_message": "No images generated. {e}"}

    return {
        "status": "success",
        "message": f"Saved img artifacts: {artifact_keys_list} to `gs://{config.GCS_BUCKET_NAME}/{gcs_folder}/{gcs_subdir}`",
    }


async def save_creatives_html_report(tool_context: ToolContext) -> dict:
    """
    Saves generated HTML report to Cloud Storage.

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: Status and the location of the HTML artifact file.
    """
    processed_report = tool_context.state["final_report_with_citations"]
    gcs_folder = tool_context.state["gcs_folder"]
    gcs_subdir = tool_context.state["agent_output_dir"]

    # get visual concept details
    final_visual_concepts_dict = tool_context.state.get("final_select_vis_concepts")
    final_visual_concepts_list = final_visual_concepts_dict["final_select_vis_concepts"]

    try:

        # creatives
        IMG_CREATIVE_STRING = ""
        for entry in final_visual_concepts_list:
            ARTIFACT_NAME = (
                entry["name"].translate(REMOVE_PUNCTUATION).replace(" ", "_")
            )
            ARTIFACT_KEY = f"{ARTIFACT_NAME}.png"
            AUTH_GCS_URL = f"https://storage.mtls.cloud.google.com/{config.GCS_BUCKET_NAME}/{gcs_folder}/{gcs_subdir}/{ARTIFACT_KEY}?authuser=3"
            IMG_HTML_STR = f"""<img src={AUTH_GCS_URL} alt ='authenticated URL' width='600' class='center'>
            """

            str_1 = f"# {entry['headline']}\n"
            str_2 = f"{IMG_HTML_STR}\n\n"
            str_3 = f"**{entry['caption']}**\n\n"
            str_4 = f"**Trend:** {entry['trend']}\n\n"
            str_5 = f"**Visual Concept:** {entry['creative_explain']}\n\n"
            str_6 = f"**How it references trend:** {entry['trend_reference']}\n\n"
            str_7 = f"**How it markets target product:** {entry['markets_product']}\n\n"
            str_8 = f"**Target audience appeal:** {entry['audience_appeal']}\n\n"
            str_9 = f"**Why this will perform well:** {entry['rationale_perf']}\n\n"
            str_10 = f"**Prompt:** {entry['prompt']}\n\n"
            result = (
                str_1
                + " "
                + str_2
                + " "
                + str_3
                + " "
                + str_4
                + " "
                + str_5
                + " "
                + str_6
                + " "
                + str_7
                + " "
                + str_8
                + " "
                + str_9
                + " "
                + str_10
            )

            IMG_CREATIVE_STRING += result

        markdown_string = (
            f"{processed_report}\n\n# Ad Creatives\n\n{IMG_CREATIVE_STRING}\n\n"
        )
        html_fragment = markdown.markdown(markdown_string)

        # Wrap the fragment in a complete HTML document with a meta charset tag
        full_html_document = f"""<!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>Trend Creative Report</title>
        </head>
        <body>
        {html_fragment}
        </body>
        </html>
        """

        # Save the HTML to a new file
        REPORT_NAME = "creative_report.html"
        with open(REPORT_NAME, "w", encoding="utf-8") as html_file:
            html_file.write(full_html_document)

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
        logging.exception(f"Error saving artifact: {e}")
        return {"status": "failed", "error": str(e)}


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
    final_visual_concepts_dict = tool_context.state.get("final_select_vis_concepts")
    final_visual_concepts_list = final_visual_concepts_dict["final_select_vis_concepts"]

    # get ad copy details
    final_ad_copy_dict = tool_context.state.get("final_select_ad_copies")
    final_ad_copy_list = final_ad_copy_dict["final_select_ad_copies"]

    try:

        # =========================== #
        # CSS formatting for HTML
        # =========================== #

        HTML_TEMPLATE = """<!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Trend Creative Report</title>
            <style>
                /* Basic body styling for better presentation */
                body {
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                    background-color: #f0f2f5;
                    margin: 0;
                    padding: 20px;
                }

                h1 {
                    text-align: center;
                    color: #333;
                    font-size: 3rem; /* You can adjust this value to your liking */ 
                    margin-bottom: 40px; /* Increased for better spacing */
                }

                /* Sub-header styles */
                .sub-header-container {
                    max-width: 1600px; /* Step 1: Match the gallery's width for perfect alignment */
                    margin: 0 auto 40px;
                    display: flex;
                    gap: 40px; /* You can adjust this gap to control the spacing between the columns */
                    list-style: none;
                    padding: 0;
                }

                .sub-header-container h3 {
                    flex: 1; /* make each h3 take up an equal amount of space */
                    margin: 0;
                    font-weight: 500;
                    color: #555;
                    cursor: pointer;
                    transition: all 0.3s ease;
                    text-align: center;
                    
                    /* Optional but nice: Add some padding and a background to visualize the equal dimensions */
                    background-color: #fff;
                    padding: 20px;
                    border-radius: 8px;
                    box-shadow: 0 4px 8px rgba(0,0,0,0.05);
                }

                /* this rule styles the label to match the hover-text style */
                .sub-header-container h3 strong {
                    display: block; /* This makes the label appear on its own line */
                    color: #a0d8ff; /* This is the light blue color from the hover-snippet */
                    font-weight: 600;
                    margin-bottom: 4px; /* Adds a little space between the label and the text */
                }

                .sub-header-container h3:hover {
                    transform: scale(1.05);
                    box-shadow: 0 8px 16px rgba(0,0,0,0.1); /* Enhance the shadow for a "lift" effect */
                    background-color: #f9f9f9;
                }
                .sub-header-container h3:hover,
                .sub-header-container h3:hover strong {
                    color: #007bff;
                }

                /* --- CSS RULE TO ENLARGE MIDDLE HEADER'S TEXT --- */
                .sub-header-container h3:nth-child(2) .enlarged-text {
                    font-size: 1.5em;
                    font-weight: 600;
                }

                /* --- THIS IS THE CRITICAL RULE --- */
                .gallery-container {
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(550px, 1fr));
                    gap: 20px;
                    max-width: 1600px;
                    margin: 0 auto;
                }

                /* Gallery Item Card */
                .gallery-item {
                    display: flex;
                    flex-direction: column;
                    overflow: hidden;
                    border-radius: 8px;
                    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
                    background-color: #fff;
                    transition: transform 0.3s ease, box-shadow 0.3s ease;
                }

                .gallery-item:hover {
                    transform: translateY(-5px);
                    box-shadow: 0 8px 16px rgba(0, 0, 0, 0.2);
                }

                .image-title {
                    margin: 0;
                    padding: 15px;
                    font-size: 1.5em;
                    font-weight: 600;
                    text-align: center;
                    color: #333;
                    background-color: #f9f9f9;
                    border-bottom: 1px solid #eee;
                }

                .image-container {
                    position: relative;
                    overflow: hidden;
                }

                .image-container img {
                    width: 100%;
                    height: auto;
                    display: block;
                    transition: transform 0.3s ease;
                    cursor: pointer;
                }

                /* The zoom effect is now triggered by hovering the image container */
                .gallery-item:hover .image-container img {
                    transform: scale(1.30);
                }

                /* Styling for the caption */
                .caption {
                    margin: 0;
                    padding: 15px;
                    font-size: 1.20em;
                    font-weight: normal;
                    text-align: left;
                    color: #444;
                    border-top: 1px solid #eee;
                }

                /* 4. Styling for the hover text */
                .hover-text {
                    position: absolute;
                    top: 0;
                    left: 0;
                    width: 100%;
                    height: 100%;
                    opacity: 0;
                    visibility: hidden;
                    background-color: rgba(0, 0, 0, 0.7);
                    color: white;
                    padding: 20px;
                    box-sizing: border-box;
                    transition: opacity 0.3s ease, visibility 0.3s ease;

                    /* Make the overlay a grid container */
                    display: grid;
                    grid-template-columns: 1fr 1fr;
                    grid-template-rows: 1fr 1fr;

                    /* The overlay will now ignore mouse clicks. */
                    pointer-events: none;
                }

                .gallery-item:hover .hover-text {
                    opacity: 1;
                    visibility: visible;
                }

                /* --- NEW RULES --- */
                .hover-snippet {
                    font-size: 1.1em;
                    line-height: 1.4;
                }

                .hover-snippet strong {
                    display: block;
                    color: #a0d8ff;
                    font-weight: 600;
                }

                .snippet-top-left {
                    justify-self: start; /* Horizontal alignment */
                    align-self: start;  /* Vertical alignment */
                }

                .snippet-top-right {
                    justify-self: end; /* Align horizontally to the end (right) of the grid cell */
                    align-self: start; /* Align vertically to the start (top) of the grid cell */
                    text-align: right; /* Ensure the text itself is right-aligned */
                    
                    /* Explicitly place this in the top-right grid cell (row 1, column 2) */
                    grid-row: 1 / 2;
                    grid-column: 2 / 3;
                }

                /* -- NEW RULE FOR THE THIRD SNIPPET -- */
                .snippet-bottom-left {
                    justify-self: start;
                    align-self: end;
                    grid-row: 2 / 3;
                    grid-column: 1 / 2;
                }

                .snippet-bottom-right {
                    justify-self: end; /* Horizontal alignment */
                    align-self: end; /* Vertical alignment */
                    text-align: right;

                    /* Put this snippet in the bottom-right cell of our 2x2 grid */
                    grid-column: 2 / 3;
                    grid-row: 2 / 3;
                }

                /* Lightbox styles - CORRECTED */
                .lightbox-overlay {
                    position: fixed;
                    top: 0;
                    left: 0;
                    width: 100%;
                    height: 100%;
                    background-color: rgba(0, 0, 0, 0.85);
                    z-index: 1000;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    opacity: 0;
                    visibility: hidden;
                    transition: opacity 0.3s ease, visibility 0.3s ease;
                }

                .lightbox-overlay.visible {
                    opacity: 1;
                    visibility: visible;
                }

                .lightbox-content {
                    max-width: 95vw;
                    max-height: 90vh;
                    display: block;
                    border-radius: 5px;
                    box-shadow: 0 5px 20px rgba(0, 0, 0, 0.5);
                }

                .lightbox-close {
                    position: absolute;
                    top: 20px;
                    right: 30px;
                    color: white;
                    font-size: 40px;
                    font-weight: bold;
                    cursor: pointer;
                    transition: color 0.2s ease;
                }

                .lightbox-close:hover {
                    color: #ccc;
                }

                /* --- NEW CSS FOR AD COPY & VISUAL CONCEPTS SECTIONS --- */
                .content-section {
                    /* padding: 40px 20px; */
                    max-width: 1600px;
                    margin: 15px auto 0; /* Adds space above the section and centers it */
                }

                /* --- NEW STYLES FOR COLLAPSIBLE BEHAVIOR --- */
                .content-section details {
                    background-color: #fff;
                    border-radius: 8px;
                    box-shadow: 0 4px 8px rgba(0,0,0,0.1);
                    transition: all 0.3s ease;
                }

                .content-section summary {
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    padding: 20px 25px;
                    cursor: pointer;
                    list-style: none; /* Hide default triangle */
                }

                .content-section summary::-webkit-details-marker {
                    display: none; /* Hide default triangle in Webkit */
                }

                .content-section summary h2 {
                    font-size: 2em;
                    color: #333;
                    margin: 0; /* Remove default margin */
                }

                .content-section summary::after {
                    content: '+';
                    font-size: 2.5rem;
                    font-weight: 300;
                    color: #007bff;
                    transition: transform 0.2s ease;
                }

                .content-section details[open] > summary::after {
                    content: '−';
                    transform: rotate(180deg);
                }

                /* This grid style now applies to BOTH sections */
                .card-grid {
                    display: grid;
                    grid-template-columns: repeat(2, 1fr);
                    gap: 40px; /* increase from 25px for more space */
                    padding: 0 25px 25px 25px;
                }

                /* This card style now applies to ALL cards in BOTH sections */
                .content-card {
                    background-color: #fff;
                    border-radius: 8px;
                    padding: 25px;
                    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
                    transition: transform 0.3s ease, box-shadow 0.3s ease; /* Smooth transition for the hover effect */
                    font-size: 1.1rem; /* Sets the base font size for everything inside the card */
                }

                .content-card:hover {
                    transform: scale(1.03); /* A subtle scale effect that won't run off the page */
                    box-shadow: 0 8px 20px rgba(0, 0, 0, 0.15);
                }

                /* The DL/DT/DD styles are specific to the card content */
                .content-card dl {
                    display: grid;
                    grid-template-columns: auto 1fr; /* Create two columns: one for the label, one for the text */
                    row-gap: 16px;
                    column-gap: 10px;
                    margin: 0; /* Remove default margins from the <dl> */
                }

                .content-card dt {
                    font-weight: bold; /* bold labeling styling */
                    color: #d9534f; /* A nice, readable red */
                }

                .content-card dd {
                    margin-left: 0; /* Resets browser default indentation */
                    color: #555;
                    line-height: 1.5;
                }
            </style>
        </head>
        <body>
        """

        HTML_BODY = f"""

            <h1>{brand} {target_product}</h1>

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
                entry["name"].translate(REMOVE_PUNCTUATION).replace(" ", "_")
            )
            ARTIFACT_KEY = f"{ARTIFACT_NAME}.png"
            GCS_BLOB_PATH = f"{gcs_folder}/{gcs_subdir}/{ARTIFACT_KEY}"
            AUTH_GCS_URL = f"https://storage.mtls.cloud.google.com/{config.GCS_BUCKET_NAME}/{GCS_BLOB_PATH}?authuser=3"

            # get high-res image
            HIGH_RES_AUTH_GCS_URL = _get_high_res_img(
                gcs_folder=tool_context.state["gcs_folder"],
                gcs_subdir=tool_context.state["agent_output_dir"],
                artifact_key=ARTIFACT_KEY,
            )

            # generate HTML block for gallery images
            GALLERY_IMAGE_BLOCK = f"""
                <!-- Image {index+1} -->
                <div class="gallery-item">
                    <h4 class="image-title">{entry['headline']}</h4>
                    <div class="image-container">
                        <img src="{AUTH_GCS_URL}" 
                                data-high-res-src="{HIGH_RES_AUTH_GCS_URL}"
                                alt="{entry['creative_explain'].replace('"', "'")}" 
                                title="{entry['headline']}">
                        <div class="hover-text">
                            <div class="hover-snippet snippet-top-left"><strong>Trend Reference:</strong>{entry['trend_reference'].replace('"', "'")}</div>
                            <div class="hover-snippet snippet-top-right"><strong>Visual Concept Name:</strong>{entry['name']}</div>
                            <div class="hover-snippet snippet-bottom-left"><strong>How it markets Target Product:</strong>{entry['markets_product'].replace('"', "'")}</div>
                            <div class="hover-snippet snippet-bottom-right"><strong>Target audience appeal:</strong>{entry['audience_appeal'].replace('"', "'")}</div>
                        </div>
                    </div>
                    <p class="caption">{entry['caption']}</p>
                </div>
            """
            CONNECTED_GALLERY_STRING += GALLERY_IMAGE_BLOCK

        HTML_POST_GALLERY = """
        </div>

        <!-- NEW: Lightbox HTML Structure -->
        <div id="lightbox" class="lightbox-overlay">
            <span class="lightbox-close">&times;</span>
            <img class="lightbox-content" id="lightbox-img">
        </div>
        """

        # =========================== #
        # visual concepts HTML chunks
        # =========================== #

        HTML_PRE_VS = """
        <!-- --- NEW HTML FOR VISUAL CONCEPTS SECTION --- -->
        <section class="content-section">
            <details>
                <summary>
                    <h2>Visual Concepts</h2>
                </summary>
                <div class="card-grid">
        """

        CONNECTED_VS_STRING = ""
        for index, entry in enumerate(final_visual_concepts_list):
            # generate HTML block for visual concepts
            VISUAL_CONCEPT_BLOCK = f"""
                    <!-- Visual Concept {index+1} -->
                    <div class="content-card">
                        <dl>
                            <dt>Name:</dt> <dd>{entry['name']}</dd>
                            <dt>Trend:</dt> <dd>{entry['trend']}</dd>
                            <dt>Creative Concept Explained:</dt> <dd>{entry['creative_explain']}</dd>
                            <dt>Why this will perform well:</dt> <dd>{entry['rationale_perf']}</dd>
                            <dt>prompt</dt> <dd>{entry['prompt']}</dd>
                        </dl>
                    </div>
            """
            CONNECTED_VS_STRING += VISUAL_CONCEPT_BLOCK

        HTML_POST_VS = """
                </div>
            </details>
        </section>
        """

        # =========================== #
        # ad copy HTML chunks
        # =========================== #

        HTML_PRE_AD_COPY = """
        <!-- --- NEW HTML FOR AD COPY SECTION --- -->
        <section class="content-section">
            <details>
                <summary>
                    <h2>Ad Copy Ideas</h2>
                </summary>
                <div class="card-grid">

        """

        CONNECTED_AD_COPY_STRING = ""
        for index, entry in enumerate(final_ad_copy_list):
            # generate HTML block for ad copies
            AD_COPY_BLOCK = f"""
                    <!-- Ad Copy {index+1} -->
                    <div class="content-card">
                        <dl>
                            <dt>Headline:</dt> <dd>{entry['headline']}</dd>
                            <dt>Body Text:</dt> <dd>{entry['body_text']}</dd>
                            <dt>Social Media Caption:</dt> <dd>{entry['caption']}</dd>
                            <dt>Call-to-Action:</dt> <dd>{entry['call_to_action']}</dd>
                            <dt>Trend-Reference:</dt> <dd>{entry['trend_reference']}</dd>
                            <dt>Audience Appeal:</dt> <dd>{entry['audience_appeal']}</dd>
                            <dt>Performance Rationale:</dt> <dd>{entry['performance_rationale']}</dd>
                        </dl>
                    </div>
            """
            CONNECTED_AD_COPY_STRING += AD_COPY_BLOCK

        HTML_POST_AD_COPY = """
                </div>
            </details>
        </section>
        <!-- --- END OF NEW HTML --- -->
        """

        HTML_END_JAVASCRIPT = """
            <!-- NEW: JavaScript for Lightbox functionality -->
            <script>
                document.addEventListener('DOMContentLoaded', () => {
                    const galleryImages = document.querySelectorAll('.image-container img');
                    const lightbox = document.getElementById('lightbox');
                    const lightboxImg = document.getElementById('lightbox-img');
                    const closeBtn = document.querySelector('.lightbox-close');

                    galleryImages.forEach(image => {
                        image.addEventListener('click', () => {
                            // lightboxImg.src = image.src;
                            // Use the 'data-high-res-src' for the lightbox image
                            lightboxImg.src = image.dataset.highResSrc;
                            lightbox.classList.add('visible');
                        });
                    });

                    const closeLightbox = () => lightbox.classList.remove('visible');
                    closeBtn.addEventListener('click', closeLightbox);
                    lightbox.addEventListener('click', e => (e.target === lightbox) && closeLightbox());
                    document.addEventListener('keydown', e => (e.key === 'Escape') && closeLightbox());
                });
            </script>

        </body>
        </html>
        """

        # concat all strings to form HTML doc
        FINAL_HTML = (
            HTML_TEMPLATE
            + HTML_BODY
            + CONNECTED_GALLERY_STRING
            + HTML_POST_GALLERY
            + HTML_PRE_VS
            + CONNECTED_VS_STRING
            + HTML_POST_VS
            + HTML_PRE_AD_COPY
            + CONNECTED_AD_COPY_STRING
            + HTML_POST_AD_COPY
            + HTML_END_JAVASCRIPT
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
        logging.exception(f"Error saving artifact: {e}")
        return {"status": "failed", "error": str(e)}


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
        DIR = f"report_creatives"
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
        logging.exception(f"Error saving artifact: {e}")
        return {"status": "failed", "error": str(e)}


def save_session_state_to_gcs(tool_context: ToolContext) -> dict:
    """
    Writes the session state to JSON. Saves the JSON file to Cloud Storage.

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: A dictionary containing the status and the json file's Cloud Storage URI (gcs_uri).
    """

    session_state = tool_context.state.to_dict()
    gcs_bucket = session_state["gcs_bucket"]
    gcs_folder = session_state["gcs_folder"]
    gcs_subdir = session_state["agent_output_dir"]

    # create new dict to save
    data = {}

    # gcs location
    data["gcs_bucket"] = gcs_bucket
    data["gcs_folder"] = gcs_folder
    data["agent_output_dir"] = gcs_subdir

    # campaign metadata
    data["brand"] = session_state["brand"]
    data["target_product"] = session_state["target_product"]
    data["target_audience"] = session_state["target_audience"]
    data["key_selling_points"] = session_state["key_selling_points"]

    # creatives
    data["final_select_ad_copies"] = tool_context.state.get("final_select_ad_copies")
    data["final_select_vis_concepts"] = tool_context.state.get(
        "final_select_vis_concepts"
    )

    # web research
    data["final_report_with_citations"] = session_state["final_report_with_citations"]

    # save local json
    filename = f"creative_session_state.json"
    local_file = f"{gcs_subdir}/{filename}"
    Path(gcs_subdir).mkdir(exist_ok=True)

    # Write to local file
    with open(local_file, "w") as f:
        json.dump(data, f, indent=4)

    # save json to GCS
    storage_client = get_gcs_client()
    gcs_bucket = config.GCS_BUCKET_NAME
    bucket = storage_client.bucket(gcs_bucket)
    blob = bucket.blob(os.path.join(gcs_folder, local_file))
    blob.upload_from_filename(local_file)

    # return values
    gcs_blob_name = f"{gcs_folder}/{gcs_subdir}/{filename}"
    gcs_uri = f"gs://{gcs_bucket}/{gcs_blob_name}"

    try:
        shutil.rmtree(gcs_subdir)
        logging.info(f"Directory '{gcs_subdir}' and its contents removed successfully")
    except FileNotFoundError:
        logging.exception(f"Directory '{gcs_subdir}' not found")

    # Return a dictionary indicating status and the Cloud Storage URI.
    return {
        "status": "success",
        "gcs_uri": gcs_uri,
    }


# =============================
# utils
# =============================
def download_blob(bucket_name, source_blob_name):
    """
    Downloads a blob from the bucket.
    Args:
        bucket_name (str): The ID of your GCS bucket
        source_blob_name (str): The ID of your GCS object
    Returns:
        Blob content as bytes.
    """
    # storage_client = storage.Client()
    storage_client = get_gcs_client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(source_blob_name)
    return blob.download_as_bytes()


def _save_to_gcs(
    tool_context: ToolContext,
    image_bytes: bytes,
    filename: str,
):
    # --- Save to GCS ---
    storage_client = get_gcs_client()
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
        return {
            "status": "error",
            "message": f"Image generated but failed to upload to GCS: {e_gcs}",
        }


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
    storage_client = get_gcs_client()
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
    storage_client = get_gcs_client()
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
