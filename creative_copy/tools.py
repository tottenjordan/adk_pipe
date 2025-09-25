import logging
import markdown
import uuid, time, os

logging.basicConfig(level=logging.INFO)

from google import genai
from google.genai import types
from google.cloud import storage
from google.adk.tools import ToolContext
from google.genai.types import GenerateVideosConfig

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


async def generate_image(
    prompt: str,
    tool_context: ToolContext,
    concept_name: str,
):
    f"""Generates an image based on the prompt for {config.image_gen_model}

    Args:
        prompt (str): The prompt to generate the image from.
        tool_context (ToolContext): The tool context.
        concept_name (str, optional): The name of the visual concept.

    Returns:
        dict: Status and the artifact_key of the generated image.
    """
    # Create output filename
    if concept_name:
        filename_prefix = f"{concept_name.replace(',', '').replace(' ', '_')}"
    else:
        filename_prefix = f"{str(uuid.uuid4())[:8]}"

    # genai client
    response = client.models.generate_images(
        model=config.image_gen_model,
        prompt=prompt,
        config={
            "number_of_images": 1,
            # "output_gcs_uri": XXXXXX,
        },
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
                "status": "success",
                "message": f"Generated image ADK artifact: '{artifact_key}' and saved to '{img_gcs_uri}'",
                "artifact_key": artifact_key,
            }


async def generate_video(
    prompt: str,
    concept_name: str,
    tool_context: ToolContext,
    number_of_videos: int = 1,
    # aspect_ratio: str = "16:9",
    negative_prompt: str = "",
    existing_image_filename: str = "",
):
    f"""Generates a video based on the prompt for {config.video_gen_model}.

    Args:
        prompt (str): The prompt to generate the video from.
        concept_name (str, optional): The name of the creative/visual concept.
        tool_context (ToolContext): The tool context.
        number_of_videos (int, optional): The number of videos to generate. Defaults to 1.
        negative_prompt (str, optional): The negative prompt to use. Defaults to "".

    Returns:
        dict: Status and the `artifact_key` of the generated video.
    """
    storage_client = get_gcs_client()
    # Create output filename
    if concept_name:
        filename_prefix = f"{concept_name.replace(',', '').replace(' ', '_')}"
    else:
        filename_prefix = f"{str(uuid.uuid4())[:8]}"

    gen_config = GenerateVideosConfig(
        aspect_ratio="16:9",
        number_of_videos=number_of_videos,
        output_gcs_uri=config.GCS_BUCKET,
        negative_prompt=negative_prompt,
    )
    if existing_image_filename != "":
        gcs_location = f"{config.GCS_BUCKET}/{existing_image_filename}"
        existing_image = types.Image(gcs_uri=gcs_location, mime_type="image/png")
        operation = client.models.generate_videos(
            model=config.video_gen_model,
            prompt=prompt,
            image=existing_image,
            config=gen_config,
        )
    else:
        operation = client.models.generate_videos(
            model=config.video_gen_model, prompt=prompt, config=gen_config
        )
    while not operation.done:
        time.sleep(15)
        operation = client.operations.get(operation)
        logging.info(operation)

    if operation.error:
        return {"status": f"failed due to error: {operation.error}"}

    if operation.response:
        if (
            operation.result is not None
            and operation.result.generated_videos is not None
        ):
            for index, generated_video in enumerate(operation.result.generated_videos):
                if (
                    generated_video.video is not None
                    and generated_video.video.uri is not None
                ):
                    video_uri = generated_video.video.uri
                    artifact_key = f"{filename_prefix}_{index}.mp4"

                    BUCKET = config.GCS_BUCKET
                    if BUCKET is not None:

                        # BUCKET_NAME = config.GCS_BUCKET_NAME
                        SOURCE_BLOB = video_uri.replace(BUCKET, "")[1:]

                        video_bytes = download_blob(
                            bucket_name=config.GCS_BUCKET_NAME,
                            source_blob_name=SOURCE_BLOB,
                        )
                        logging.info(
                            f"The artifact key for this video is: {artifact_key}"
                        )
                        await tool_context.save_artifact(
                            filename=artifact_key,
                            artifact=types.Part.from_bytes(
                                data=video_bytes, mime_type="video/mp4"
                            ),
                        )

                        # save to common gcs location
                        DESTINATION_BLOB_NAME = (
                            f"{tool_context.state['gcs_folder']}/{artifact_key}"
                        )
                        bucket = storage_client.get_bucket(config.GCS_BUCKET_NAME)
                        source_blob = bucket.blob(SOURCE_BLOB)
                        destination_bucket = storage_client.get_bucket(
                            config.GCS_BUCKET_NAME
                        )
                        new_blob = bucket.copy_blob(
                            source_blob,
                            destination_bucket,
                            new_name=DESTINATION_BLOB_NAME,
                        )
                        logging.info(
                            f"Blob {source_blob} copied to {destination_bucket}/{new_blob.name}"
                        )

                    return {"status": "ok", "artifact_key": f"{artifact_key}"}


async def save_img_artifact_key(
    artifact_key_dict: dict,
    tool_context: ToolContext,
) -> dict:
    """
    Tool to save image artifact details to the session state.
    Use this tool after generating an image with the `generate_image` tool.

    Args:
        artifact_key_dict (dict): A dict representing a generated image artifact. Use the `tool_context` to extract the following schema:
            artifact_key (str): The filename used to identify the image artifact; the value returned in `generate_image` tool response.
            img_prompt (str): The prompt used to generate the image artifact.
            concept (str): A brief explanation of the visual concept used to generate this artifact.
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


async def save_vid_artifact_key(
    artifact_key_dict: dict,
    tool_context: ToolContext,
) -> dict:
    """
    Tool to save video artifact details to the session state.
    Use this tool after generating an video with the `generate_video` tool.

    Args:
        artifact_key_dict (dict): A dict representing a generated video artifact. Use the `tool_context` to extract the following schema:
            artifact_key (str): The filename used to identify the video artifact; the value returned in `generate_video` tool response.
            vid_prompt (str): The prompt used to generate the video artifact.
            concept (str): A brief explanation of the visual concept used to generate this artifact.
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
    existing_vid_artifact_keys = tool_context.state.get(
        "vid_artifact_keys", {"vid_artifact_keys": []}
    )
    existing_vid_artifact_keys["vid_artifact_keys"].append(artifact_key_dict)
    tool_context.state["vid_artifact_keys"] = existing_vid_artifact_keys
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
            AUTH_GCS_URL = f"https://storage.mtls.cloud.google.com/{config.GCS_BUCKET_NAME}/{gcs_folder}/{entry['artifact_key']}?authuser=3"
            IMG_HTML_STR = f"""<img src={AUTH_GCS_URL} alt ='authenticated URL' width='600' class='center'>
            """

            str_1 = f"# {entry['headline']}\n"
            str_2 = f"{IMG_HTML_STR}\n\n"
            # str_2 = IMG_FIGURE
            str_3 = f"**{entry['caption']}**\n\n"
            str_4 = f"**Trend:** {entry['trend']}\n"
            str_5 = f"**Visual Concept:** {entry['concept']}\n"
            str_6 = f"**How it markets target product:** {entry['markets_product']}\n"
            str_7 = f"**Target audience appeal:** {entry['audience_appeal']}\n"
            str_8 = f"**Why this will perform well:** {entry['rationale_perf']}\n"
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

        # ==================== #
        # get video creatives
        # ==================== #

        # get artifact details
        vid_artifact_state_dict = tool_context.state.get("vid_artifact_keys")
        vid_artifact_list = vid_artifact_state_dict["vid_artifact_keys"]

        VID_CREATIVE_STRING = ""
        for entry in vid_artifact_list:
            logging.info(entry)

            FILENAME = entry["artifact_key"]
            AUTH_GCS_URL = f"""<video width='800' controls>
                <source src='https://storage.mtls.cloud.google.com/{config.GCS_BUCKET_NAME}/{gcs_folder}/{FILENAME}?authuser=3' type='video/mp4'>
            Your browser does not support the video tag.
            </video>
            """

            str_1 = f"# {entry['headline']}\n"
            str_2 = f"{AUTH_GCS_URL}\n\n"
            str_3 = f"**{entry['caption']}**\n\n"
            str_4 = f"**Trend:** {entry['trend']}\n"
            str_5 = f"**Visual Concept:** {entry['concept']}\n"
            str_6 = f"**How it markets target product:** {entry['markets_product']}\n"
            str_7 = f"**Target audience appeal:** {entry['audience_appeal']}\n"
            str_8 = f"**Why this will perform well:** {entry['rationale_perf']}\n"
            str_9 = f"**Prompt:** {entry['vid_prompt']}\n\n"

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

            VID_CREATIVE_STRING += result

        markdown_string = f"{processed_report}\n\n# Ad Creatives\n\n{IMG_CREATIVE_STRING}\n\n{VID_CREATIVE_STRING}\n\n"
        html_content = markdown.markdown(markdown_string)

        # Save the HTML to a new file
        REPORT_NAME = "creative_report.html"
        with open(REPORT_NAME, "w") as html_file:
            html_file.write(html_content)

        gcs_blob_name = f"{gcs_folder}/{gcs_subdir}/{REPORT_NAME}"

        upload_blob_to_gcs(
            source_file_name=REPORT_NAME, destination_blob_name=gcs_blob_name
        )

        os.remove(REPORT_NAME)

        gcs_uri = f"gs://{config.GCS_BUCKET_NAME}/{gcs_blob_name}"

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
