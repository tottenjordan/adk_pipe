import os
import warnings
from dotenv import load_dotenv
from dataclasses import dataclass

from google.adk.workflow import RetryConfig
from google.api_core import exceptions as api_exceptions
from google.genai import errors as genai_errors

warnings.filterwarnings("ignore")

# See note in trend_trawler/config.py: ADK matches retry exceptions by exact class
# name, so we list the concrete transient classes (genai 5xx + Google API 5xx/429 +
# transport). creative_agent calls genai directly (image gen), hence ServerError.
INFRA_RETRY = RetryConfig(
    max_attempts=3,
    exceptions=[
        genai_errors.ServerError,            # genai 5xx
        api_exceptions.ServiceUnavailable,   # 503
        api_exceptions.InternalServerError,  # 500
        api_exceptions.GatewayTimeout,       # 504
        api_exceptions.TooManyRequests,      # 429
        api_exceptions.DeadlineExceeded,
        ConnectionError,
        TimeoutError,
    ],
)

# Load environment variables from a .env file
ENV_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
load_dotenv(dotenv_path=ENV_FILE_PATH)


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
    critic_model: str = "gemini-3.1-pro-preview"  # gemini-3-pro-preview
    worker_model: str = "gemini-3.5-flash"  # gemini-3-flash-preview
    video_analysis_model: str = "gemini-3.1-pro-preview"  # gemini-3-pro-preview
    lite_planner_model: str = "gemini-3.1-flash-lite"  # gemini-3-flash-preview
    image_gen_model: str = "gemini-3.1-flash-image"
    video_gen_model: str = "veo-3.1-generate-001"  # "veo-3.0-generate-001"
    max_results_yt_trends: int = 45

    # Adjust these values to limit the rate at which the agent queries the LLM API.
    rate_limit_seconds: int = 60
    rpm_quota: int = 1000

    # env vars
    GCS_BUCKET = os.environ.get("BUCKET")
    GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME")
    PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
    PROJECT_NUMBER = os.environ.get("GOOGLE_CLOUD_PROJECT_NUMBER")
    LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION")

    # TODO: update doc string
    BQ_PROJECT_ID = os.environ.get("BQ_PROJECT_ID")
    BQ_DATASET_ID = os.environ.get("BQ_DATASET_ID")
    BQ_TABLE_TARGETS = os.environ.get("BQ_TABLE_TARGETS")
    BQ_TABLE_CREATIVES = os.environ.get("BQ_TABLE_CREATIVES")
    BQ_TABLE_ALL_TRENDS = os.environ.get("BQ_TABLE_ALL_TRENDS")


config = ResearchConfiguration()
