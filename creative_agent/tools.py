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
    # get constants
    gcs_folder = tool_context.state["gcs_folder"]
    gcs_subdir = tool_context.state["agent_output_dir"]

    # get artifact details
    final_visual_concepts_dict = tool_context.state.get("final_select_vis_concepts")
    final_visual_concepts_list = final_visual_concepts_dict["final_select_vis_concepts"]

    artifact_keys_list = []
    for entry in final_visual_concepts_list:
        # logging.info(entry)

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
    final_visual_concepts_list = final_visual_concepts_dict[
        "final_select_vis_concepts"
    ]

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

        # save HTML file to cloud storage
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
