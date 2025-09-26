import logging
import markdown
import os, string

logging.basicConfig(level=logging.INFO)

import shutil
from PIL import Image
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


def save_select_visual_concept(
    select_vis_concept_dict: dict, tool_context: ToolContext
) -> dict:
    """
    Tool to save `select_vis_concept_dict` to the 'final_select_vis_concepts' state key.
    Use this tool after creating visual concepts with the `visual_generation_pipeline` tool.

    Args:
        select_vis_concept_dict (dict): A dict representing a visual concept for ad generation. Use the `tool_context` to extract the following schema:
            name (str): An intuitive name of the visual concept.
            headline (str): The attention-grabbing headline.
            caption (str): The candidate social media caption proposed for the visual concept.
            creative_explain (str): A brief explanation of the visual concept.
            trend (str): The trend(s) referenced by this creative.
            trend_reference (str): How the visual concept relates to the `target_search_trends`
            audience_appeal (str): A brief explanation for the target audience appeal.
            markets_product (str): A brief explanation of how this markets the target product.
            rationale_perf (str): A brief rationale explaining why this ad copy will perform well.
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


async def save_img_artifact_key(
    artifact_key_dict: dict,
    tool_context: ToolContext,
) -> dict:
    """
    Tool to save image artifact details to the session state.
    Use this tool after generating an image with the `generate_image` tool.

    Args:
        artifact_key_dict (dict): A dict representing a generated image artifact. Use the `tool_context` to extract the following schema:
            artifact_key (str): The filename used to identify the image artifact; the value of `artifact_key` returned in the `generate_image` tool response.
            img_prompt (str): The prompt used to generate the image artifact.
            concept_explained (str): A brief explanation of the visual concept used to generate this artifact.
            headline (str): The attention-grabbing headline proposed for the artifact's ad-copy.
            caption (str): The candidate social media caption proposed for the artifact's ad-copy.
            trend (str): The trend(s) referenced by this creative.
            rationale_perf (str): A brief rationale explaining why this ad copy will perform well.
            audience_appeal (str): A brief explanation for the target audience appeal.
            markets_product (str): A brief explanation of how this markets the target product.
        tool_context (ToolContext) The tool context.

    Returns:
        dict: the status of this functions overall outcome.
    """
    existing_img_artifact_keys = tool_context.state.get(
        "img_artifact_keys", {"img_artifact_keys": []}
    )
    existing_img_artifact_keys["img_artifact_keys"].append(artifact_key_dict)
    tool_context.state["img_artifact_keys"] = existing_img_artifact_keys
    return {"status": "ok"}


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

    # get artifact details
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

    try:
        # creatives
        CONNECTED_GALLERY_STRING = ""
        for index, entry in enumerate(final_visual_concepts_list):
            ARTIFACT_NAME = (
                entry["name"].translate(REMOVE_PUNCTUATION).replace(" ", "_")
            )
            ARTIFACT_KEY = f"{ARTIFACT_NAME}.png"
            AUTH_GCS_URL = f"https://storage.mtls.cloud.google.com/{config.GCS_BUCKET_NAME}/{gcs_folder}/{gcs_subdir}/{ARTIFACT_KEY}?authuser=3"

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
                                <div class="hover-snippet snippet-bottom-left"><strong>How it markets Target Product:</strong>{entry['markets_product'].replace('"', "'")}</div>
                                <div class="hover-snippet snippet-bottom-right"><strong>Target audience appeal:</strong>{entry['audience_appeal'].replace('"', "'")}</div>
                            </div>
                        </div>
                        <p class="caption">{entry['caption']}</p>
                    </div>
            """
            CONNECTED_GALLERY_STRING += GALLERY_IMAGE_BLOCK

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
                    margin-bottom: 20px;
                }

                /* Sub-header styles */
                .sub-header-container {
                    max-width: 1000px;
                    margin: 30px auto 20px;
                    display: flex;
                    justify-content: center;
                    gap: 40px;
                    list-style: none;
                    padding: 0;
                }

                .sub-header-container h3 {
                    margin: 0;
                    font-weight: 500;
                    color: #555;
                    cursor: pointer;
                    transition: color 0.2s ease;
                }

                .sub-header-container h3:hover {
                    color: #007bff;
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

                /* NEW: Image Title Style */
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

                /* 3. Styling for the caption */
                .caption {
                    margin: 0;
                    padding: 15px;
                    font-size: 1.15em;
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
                    color: #a0d8ff; /* A slightly different color for the label */
                    font-weight: 600;
                }

                .snippet-top-left {
                    /* Place this snippet in the top-left corner of its grid cell */
                    justify-self: start; /* Horizontal alignment */
                    align-self: start;   /* Vertical alignment */
                }

                /* -- NEW RULE FOR THE THIRD SNIPPET -- */
                .snippet-bottom-left {
                    justify-self: start;
                    align-self: end;
                    grid-row: 2 / 3;
                    grid-column: 1 / 2;
                }

                .snippet-bottom-right {
                    /* Place this snippet in the bottom-right corner of its grid cell */
                    justify-self: end; /* Horizontal alignment */
                    align-self: end;   /* Vertical alignment */
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
                    box-shadow: 0 5px 20px rgba(0,0,0,0.5);
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
            </style>
        </head>
        <body>
        """

        HTML_BODY = f"""

            <h1>{brand} Ad Creatives for {target_product}</h1>

            <!-- NEW: Sub-headers -->
            <div class="sub-header-container">
                <h3>Key Selling Point: {key_selling_points}</h3>
                <h3>Target Audience: {target_audience}</h3>
            </div>

            <h1>search trend: '{target_search_trends}'</h1>

            <div class="gallery-container">
        """

        HTML_END = """
            </div>

            <!-- NEW: Lightbox HTML Structure -->
            <div id="lightbox" class="lightbox-overlay">
                <span class="lightbox-close">&times;</span>
                <img class="lightbox-content" id="lightbox-img">
            </div>

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

        FINAL_HTML = HTML_TEMPLATE + HTML_BODY + CONNECTED_GALLERY_STRING + HTML_END

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

    # Construct a client side representation of a blob.
    # Note `Bucket.blob` differs from `Bucket.get_blob` as it doesn't retrieve
    # any content from Google Cloud Storage. As we don't need additional data,
    # using `Bucket.blob` is preferred here.
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
    NEW_BLOB_NAME = f"{gcs_folder}/{gcs_subdir}/resized/{XL_LOCAL_FILENAME}"
    new_blob = bucket.blob(NEW_BLOB_NAME)
    new_blob.upload_from_filename(XL_LOCAL_FILENAME)

    # rm local file
    os.remove(LOCAL_FILENAME)
    os.remove(XL_LOCAL_FILENAME)
    high_res_auth_gcs_uri = f"https://storage.mtls.cloud.google.com/{config.GCS_BUCKET_NAME}/{NEW_BLOB_NAME}?authuser=3"
    return high_res_auth_gcs_uri
