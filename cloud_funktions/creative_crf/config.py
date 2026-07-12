# config.py


class AppConfig:

    # gcp project
    GOOGLE_CLOUD_PROJECT = "hybrid-vertex"
    # Agent Engine is a *regional* resource — this is us-central1, NOT the
    # `global` model location used for the gemini-3.x endpoints.
    GCP_REGION = "us-central1"
    GOOGLE_CLOUD_PROJECT_NUMBER = 934903580331
    CREATIVE_WORKER_TOPIC_NAME = "creative-worker-queue-topic"


config = AppConfig()
