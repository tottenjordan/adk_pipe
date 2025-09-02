# Trend Trawler

> TODO

## Deploy ADK Agent to Cloud Run

**1. Set project and authenticate**

```bash
export GOOGLE_CLOUD_PROJECT=$(gcloud config get-value project)
export GOOGLE_CLOUD_PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")

gcloud auth login
gcloud config set project $GOOGLE_CLOUD_PROJECT
```

**2. Create requirements.txt file**

```bash
poetry export --without-hashes --format=requirements.txt > ./requirements.txt
```

**3. Set environment variables...**

```bash
export GOOGLE_GENAI_USE_VERTEXAI=True

# Set your Google Cloud Project ID
export GOOGLE_CLOUD_PROJECT="hybrid-vertex"

# Set your desired Google Cloud Location
export GOOGLE_CLOUD_LOCATION="us-central1"

# Set the path to your agent code directory
export AGENT_PATH="trend_agent/"

# Set a name for your Cloud Run service (optional)
export SERVICE_NAME="trend-agent-service-v4"

# Set an application name (optional)
export APP_NAME="trend-agent-app"

# name of your cloud storage bucket
export BUCKET=gs://tia-adk-media

# example campaign json
export SESSION_STATE_JSON_PATH=example_estee_anr.json
```

**4. Deploy agent to cloud run...**

```bash
adk deploy cloud_run \
  --project=$GOOGLE_CLOUD_PROJECT \
  --region=$GOOGLE_CLOUD_LOCATION \
  --port 8000 \
  --service_name=$SERVICE_NAME \
  --with_ui \
  $AGENT_PATH
```

> TODO