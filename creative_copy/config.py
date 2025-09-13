import os
from dotenv import load_dotenv
from dataclasses import dataclass

# Load environment variables from a .env file
load_dotenv()


@dataclass
class ResearchConfiguration:
    """Configuration for research-related models and parameters.

    Attributes:
        state_init (str): a key indicating the state dict is initialized
        critic_model (str): Model for evaluation tasks.
        worker_model (str): Model for working/generation tasks.
        video_analysis_model (str): Model for video understanding.
        image_gen_model (str): Model for generating images.
        video_gen_model (str): Model for generating video.
        max_results_yt_trends (int): The value to set for `max_results` with the YouTube API
                                i.e., the number of video results to return.
        rate_limit_seconds (int): total duration to calculate the rate at which the agent queries the LLM API.
        rpm_quota (int): requests per minute threshold for agent LLM API rate limiter
        GCS_BUCKET (str): The Cloud Storage bucket used to save artifacts.
        GCS_BUCKET_NAME (str): Name of the Cloud Storage bucket, without the `gs://` prefix.
        PROJECT_ID (str): Name of GCP project.
        PROJECT_NUMBER (int): ID of GCP project.
        LOCATION (str): Region used for GCP assets.
    """

    state_init = "_state_init"
    critic_model: str = "gemini-2.5-pro"
    worker_model: str = "gemini-2.5-flash"
    video_analysis_model: str = "gemini-2.5-pro"
    lite_planner_model: str = "gemini-2.0-flash-001"  # "gemini-2.5-flash-lite"
    image_gen_model: str = (
        "imagen-4.0-ultra-generate-preview-06-06"  # "imagen-4.0-fast-generate-preview-06-06"
    )
    video_gen_model: str = "veo-3.0-generate-001"
    max_results_yt_trends: int = 45

    # Adjust these values to limit the rate at which the agent queries the LLM API.
    rate_limit_seconds: int = 60
    rpm_quota: int = 1000

    # env vars
    GCS_BUCKET = os.environ.get("BUCKET")
    GCS_BUCKET_NAME = os.environ.get("GOOGLE_CLOUD_STORAGE_BUCKET")
    PROJECT_ID=os.environ.get("GOOGLE_CLOUD_PROJECT")
    PROJECT_NUMBER=os.environ.get("GOOGLE_CLOUD_PROJECT_NUMBER")
    LOCATION=os.environ.get("GOOGLE_CLOUD_LOCATION")


config = ResearchConfiguration()