import logging
import markdown
import uuid, os, string

logging.basicConfig(level=logging.INFO)

import shutil
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
            trend_reference (str): How the visual concept relates to the `target_search_trends`
            audience_appeal (str): A brief explanation for the target audience appeal.
            markets_product (str): A brief explanation of how this markets the target product.
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
    prompt: str,
    tool_context: ToolContext,
    concept_name: str,
):
    f"""Generates an image based on the prompt for {config.image_gen_model}

    Args:
        prompt (str): The prompt to generate the image from.
        tool_context (ToolContext): The tool context.
        concept_name (str, optional): The visual concept's name.

    Returns:
        dict: Status and the artifact_key of the generated image.
    """
    # Create output filename
    if concept_name:
        filename_prefix = concept_name.translate(REMOVE_PUNCTUATION).replace(" ", "_")
    else:
        filename_prefix = f"{str(uuid.uuid4())[:8]}"

    # genai client
    response = client.models.generate_images(
        model=config.image_gen_model,
        prompt=prompt,
        config=types.GenerateImagesConfig(
            number_of_images=1,
            enhance_prompt=False,
        ),
    )

    if not response.generated_images:
        return {
            "status": "error",
            "error_message": f"No images generated. Response: {str(response)}",
        }

    for index, image_results in enumerate(response.generated_images):
        if (
            image_results.image is not None
            and image_results.image.image_bytes is not None
        ):
            image_bytes = image_results.image.image_bytes
            artifact_key = f"{filename_prefix}_{index}.png"

            img_gcs_uri = save_to_gcs(
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
            logging.info(f"Saved image artifact, '{artifact_key}', to '{img_gcs_uri}'")
            return {
                # "status": "success",
                "artifact_key": artifact_key,
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

    try:

        # ==================== #
        # get image creatives
        # ==================== #

        # get artifact details
        img_artifact_state_dict = tool_context.state.get("img_artifact_keys")
        img_artifact_list = img_artifact_state_dict["img_artifact_keys"]

        IMG_CREATIVE_STRING = ""
        for entry in img_artifact_list:
            logging.info(entry)
            ARTIFACT_FILENAME = entry["artifact_key"]
            AUTH_GCS_URL = f"https://storage.mtls.cloud.google.com/{config.GCS_BUCKET_NAME}/{gcs_folder}/{gcs_subdir}/{ARTIFACT_FILENAME}?authuser=3"
            IMG_HTML_STR = f"""<img src={AUTH_GCS_URL} alt ='authenticated URL' width='600' class='center'>
            """

            str_1 = f"# {entry['headline']}\n"
            str_2 = f"{IMG_HTML_STR}\n\n"
            str_3 = f"**{entry['caption']}**\n\n"
            str_4 = f"**Trend:** {entry['trend']}\n\n"
            str_5 = f"**Visual Concept:** {entry['concept_explained']}\n\n"
            str_6 = f"**How it markets target product:** {entry['markets_product']}\n\n"
            str_7 = f"**Target audience appeal:** {entry['audience_appeal']}\n\n"
            str_8 = f"**Why this will perform well:** {entry['rationale_perf']}\n\n"
            str_9 = f"**Prompt:** {entry['img_prompt']}\n\n"
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
            )

            IMG_CREATIVE_STRING += result

        markdown_string = (
            f"{processed_report}\n\n# Ad Creatives\n\n{IMG_CREATIVE_STRING}\n\n"
        )
        html_fragment = markdown.markdown(markdown_string)  # encoding="utf-8"

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

        gcs_blob_name = f"{gcs_folder}/{gcs_subdir}/{REPORT_NAME}"
        gcs_uri = f"gs://{config.GCS_BUCKET_NAME}/{gcs_blob_name}"

        upload_blob_to_gcs(
            source_file_name=REPORT_NAME,
            destination_blob_name=gcs_blob_name,
        )
        os.remove(REPORT_NAME)

        return {
            "status": "success",
            "gcs_uri": gcs_uri,
        }

    except Exception as e:
        logging.error(f"Error saving artifact: {e}")
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
        upload_blob_to_gcs(
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
        logging.error(f"Error saving artifact: {e}")
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


def save_to_gcs(
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


def upload_blob_to_gcs(
    source_file_name: str,
    destination_blob_name: str,
    # gcs_bucket: str,
) -> str:
    """
    Uploads a blob to a GCS bucket.
    Args:
        source_file_name (str): The path to the file to upload.
        destination_blob_name (str): The desired folder path in gcs
    Returns:
        str: The GCS URI of the uploaded file.
    """
    # bucket_name = "your-bucket-name" (no 'gs://')
    # source_file_name = "local/path/to/file" (file to upload)
    # destination_blob_name = "folder/paths-to/storage-object-name"
    # storage_client = storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    storage_client = get_gcs_client()
    gcs_bucket = config.GCS_BUCKET_NAME
    bucket = storage_client.bucket(gcs_bucket)
    blob = bucket.blob(destination_blob_name)
    blob.upload_from_filename(source_file_name)
    return f"File {source_file_name} uploaded to {destination_blob_name}."
