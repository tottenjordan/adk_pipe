# Trend Trawler

> *[trends-2-creatives](https://github.com/tottenjordan/zghost/tree/main) in offline, beast mode*

<details>
  <summary>scoop 'n score</summary>

> Given a campaign, the trawler sifts through the top 25 trending Search terms, returning a subset of trends

<p align="center">
  <img src='imgs/trend_trawler_banner.png' width="500"/>
</p>

> TODO: Given a campaign and a Search trend, the creative agent conducts web research (for context) and generates candidate ad creatives

</details>

## About

> TODO

**TODOs**
* event-based triggers
* scheduled runs
* orchestration
* accesible outputs
* email / notification
* easy export to ~*live editor tool* to nano-banana


## example report

> See web research and ad creatives from the `creative_agent`

[![demo](https://img.youtube.com/vi/0628QG8J9Mc/hqdefault.jpg)](https://www.youtube.com/watch?v=0628QG8J9Mc)


## Deploy ADK Agent to Cloud Run

**1. Set project and authenticate**

```bash
git clone https://github.com/tottenjordan/adk_pipe.git


export GOOGLE_CLOUD_PROJECT=$(gcloud config get-value project)
export GOOGLE_CLOUD_PROJECT_NUMBER=$(gcloud projects describe $GOOGLE_CLOUD_PROJECT --format="value(projectNumber)")

gcloud config set project $GOOGLE_CLOUD_PROJECT
gcloud auth application-default login

touch .env
echo "GOOGLE_GENAI_USE_VERTEXAI=1" >> .env
echo "GOOGLE_CLOUD_PROJECT=your-project-id" >> .env
echo "GOOGLE_CLOUD_PROJECT_NUMBER=1234789" >> .env
echo "GOOGLE_CLOUD_LOCATION=us-central1" >> .env
echo "BUCKET=gs://your-bucket-name" >> .env
```

update the `campaign_metadata` in your `.env` file like this:

```bash
# campaign metadata
BRAND="Paul Reed Smith (PRS)"
TARGET_AUDIENCE="millennials who follow jam bands (e.g., Widespread Panic and Phish), respond positively to nostalgic messages"
TARGET_PRODUCT="PRS SE CE24 Electric Guitar"
KEY_SELLING_POINT="The 85/15 S Humbucker pickups deliver a wide tonal range, from thick humbucker tones to clear single-coil sounds, making the guitar suitable for various genres."
```

then copy `.env` file to both agent directories (deployed separately)

```bash
cp .env trend_trawler/.env
cp .env creative_agent/.env

source .env
```

**2. poetry install && create requirements.txt file**

```bash
poetry install

poetry export --without-hashes --format=requirements.txt > ./creative_agent/requirements.txt
poetry export --without-hashes --format=requirements.txt > ./trend_trawler/requirements.txt
```

**3. local deployment / testing**

start local dev UI...

```bash
poetry run adk web .
```

**[3.a] choose `trend_trawler` from drop-down menu (top left)...**

```bash
user: start

agent: `[end-to-end workflow >> recommended subset of trends]` 
```

**[3.b] choose `creative agent` in top-left drop-down menu...**

```bash
user: `target_search_trend: `YOUR_SEARCH_TREND_O_CHOICE`

agent: `[end-to-end workflow >> candidate creatives (img/vid)]` 
```

**4. Deploy `trend trawler agent` to Cloud Run...**

```bash
export AGENT_DIR_NAME=trend_trawler

# Set the path to your agent code directory
export AGENT_PATH=$AGENT_DIR_NAME/

# avoid permission issues in Cloud Run
chmod -R 777 $AGENT_PATH

# Set a name for your Cloud Run service (optional)
export SERVICE_NAME="trend-trawler-prs-beastmode"

adk deploy cloud_run \
  --project=$GOOGLE_CLOUD_PROJECT \
  --region=$GOOGLE_CLOUD_LOCATION \
  --port 8000 \
  --service_name=$SERVICE_NAME \
  --with_ui \
  $AGENT_PATH
```

*if prompted with the following, select `y`...*

`Allow unauthenticated invocations to [your-service-name] (y/N)?.`

**4. Deploy `creative agent` to Cloud Run...**

```bash
export AGENT_DIR_NAME=creative_agent

# Set the path to your agent code directory
export AGENT_PATH=$AGENT_DIR_NAME/

# avoid permission issues in Cloud Run
chmod -R 777 $AGENT_PATH

# Set a name for your Cloud Run service (optional)
export SERVICE_NAME="trend-creative-prs-beastmode"

adk deploy cloud_run \
  --project=$GOOGLE_CLOUD_PROJECT \
  --region=$GOOGLE_CLOUD_LOCATION \
  --port 8000 \
  --service_name=$SERVICE_NAME \
  --with_ui \
  $AGENT_PATH
```

*if prompted with the following, select `y`...*

`Allow unauthenticated invocations to [your-service-name] (y/N)?.`


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