# Trend Trawler

> ADK agent workflows for finding culturally significant trends

<p align="center">
  <img src='imgs/trend_trawler_banner.png' width="700"/>
</p>

Given a campaign, the trawler sifts through the top 25 trending Search terms, returning a subset of trends

Given a campaign and a Search trend, the creative agent conducts web research (for context) and generates candidate ad creatives

## example report

> See web research and ad creatives from the `creative_agent`

[![demo](https://img.youtube.com/vi/xcx60kjNo8Y/hqdefault.jpg)](https://www.youtube.com/watch?v=xcx60kjNo8Y)


## Deploy ADK Agent to Cloud Run

**1. Set project and authenticate**

```bash
export GOOGLE_CLOUD_PROJECT=$(gcloud config get-value project)
export GOOGLE_CLOUD_PROJECT_NUMBER=$(gcloud projects describe $GOOGLE_CLOUD_PROJECT --format="value(projectNumber)")

gcloud auth login
gcloud config set project $GOOGLE_CLOUD_PROJECT
```

**2. Create requirements.txt file**

```bash
poetry export --without-hashes --format=requirements.txt > ./creative_agent/requirements.txt
poetry export --without-hashes --format=requirements.txt > ./trend_trawler/requirements.txt
```

**3. Deploy `trend trawler agent` to Cloud Run...**

```bash
export AGENT_DIR_NAME=trend_trawler

cp .env $AGENT_DIR_NAME/.env

# Set the path to your agent code directory
export AGENT_PATH=$AGENT_DIR_NAME/

# Set a name for your Cloud Run service (optional)
export SERVICE_NAME="trend-trawler-prs-beastmode"

# avoid permission issues in Cloud Run
chmod -R 777 $AGENT_PATH

adk deploy cloud_run \
  --project=$GOOGLE_CLOUD_PROJECT \
  --region=$GOOGLE_CLOUD_LOCATION \
  --port 8000 \
  --service_name=$SERVICE_NAME \
  --with_ui \
  $AGENT_PATH
```

**4. Deploy `creative agent` to Cloud Run...**

```bash
export AGENT_DIR_NAME=creative_agent

cp .env $AGENT_DIR_NAME/.env

# Set the path to your agent code directory
export AGENT_PATH=$AGENT_DIR_NAME/

# Set a name for your Cloud Run service (optional)
export SERVICE_NAME="trend-creative-prs-beastmode"

chmod -R 777 $AGENT_PATH

adk deploy cloud_run \
  --project=$GOOGLE_CLOUD_PROJECT \
  --region=$GOOGLE_CLOUD_LOCATION \
  --port 8000 \
  --service_name=$SERVICE_NAME \
  --with_ui \
  $AGENT_PATH
```

## Folder Structure

```bash
.
├── creative_agent
│   ├── agent.py
│   ├── callbacks.py
│   ├── config.py
│   ├── __init__.py
│   ├── prompts.py
│   ├── requirements.txt
│   ├── sub_agents
│   │   ├── campaign_researcher
│   │   │   ├── agent.py
│   │   │   └── __init__.py
│   │   ├── __init__.py
│   │   └── trend_researcher
│   │       ├── agent.py
│   │       └── __init__.py
│   └── tools.py
├── poetry.lock
├── pyproject.toml
├── README.md
└── trend_trawler
    ├── agent.py
    ├── callbacks.py
    ├── config.py
    ├── __init__.py
    ├── requirements.txt
    └── tools.py
```