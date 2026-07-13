<div align="center">

<img src="imgs/trend_trawler_banner.png" alt="Trend Trawler — a trawler casting a wide net at golden hour" width="480" />

<h1 align="center">🌊 Trend Trawler 🎣</h1>

> Turn trending Google Search terms into campaign-ready ad creatives — a multi-agent system built with Google's **ADK**, deployed to **Vertex AI Agent Engine**, and fanned out via **Cloud Run Functions + Pub/Sub**.

![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![uv](https://img.shields.io/badge/packaging-uv-DE5FE9?logo=uv&logoColor=white)
![Ruff](https://img.shields.io/badge/lint-ruff-261230?logo=ruff&logoColor=white)
![ty](https://img.shields.io/badge/types-ty-261230?logo=astral&logoColor=white)
![Google ADK](https://img.shields.io/badge/Google%20ADK-2.4-4285F4?logo=google&logoColor=white)
![Vertex AI](https://img.shields.io/badge/Vertex%20AI-Agent%20Engine-4285F4?logo=googlecloud&logoColor=white)
![Gemini](https://img.shields.io/badge/Gemini-886FBF?logo=googlegemini&logoColor=white)
![Next.js](https://img.shields.io/badge/Next.js-16-000000?logo=nextdotjs&logoColor=white)

</div>

**Trend Trawler** runs a two-phase, event-driven pipeline: first it finds culturally relevant Google Search trends for a campaign, then it researches each trend and generates, evaluates, and exports candidate ad copy and visual concepts. It can run headless (offline, event-triggered) or interactively through a custom web UI.

| Stage | Agent | What it does |
| :---: | --- | --- |
| 1 | 🔦 **`trend_scout`** | Gathers the top 25 Google Search trends, researches cultural context, and filters to the 3 most campaign-relevant |
| 2 | 🎨 **`creative_agent`** | Researches a `<trend, campaign>` pair and generates candidate ad copy + visual concepts, rendering an image for each |
| 2 | ⚖️ **`creative_eval`** | Scores every ad copy and visual concept via LLM-as-judge across 12 quality dimensions |
| 2 | 🧑‍💻 **`interactive_creative`** | Same pipeline as `creative_agent`, with human-in-the-loop review checkpoints after research, ad copies, and visual concepts |

<details>
  <summary>casting a wide net — how the pipeline flows</summary>

<br />

Trend Trawler works like its namesake: it drops a **wide net** over the day's Search trends, then hauls in only the catch worth keeping.

1. **Cast** — `trend_scout` pulls the top 25 Google Search trends and researches each for cultural context.
2. **Haul in** — it filters to the 3 trends most relevant to your campaign and writes them to BigQuery.
3. **Work the catch** — for each `<trend, campaign>` pair, `creative_agent` runs parallel web research, synthesizes a strategic brief, and generates candidate ad copy plus a rendered image per visual concept.
4. **Grade it** — `creative_eval` scores every ad copy and visual concept with an LLM-as-judge across 12 quality dimensions (passing threshold 0.7).
5. **Land it** — the research PDF, HTML gallery, and evaluation report are exported to Cloud Storage.

Prefer to stay hands-on? `interactive_creative` runs the same flow but pauses for your review after the research report, the ad copies, and the visual concepts.

</details>


## Table of Contents
- [Architecture](#architecture)
- [Quickstart](#quickstart)
- [Usage](#usage)
- [Evaluation](#evaluation)
- [Example Outputs](#example-outputs)
- [Frontend UI](#frontend-ui)
- [Deployment](#deployment)
- [Testing](#testing)
- [Repo Structure](#repo-structure)
- [TODO](#todo)


## Architecture

Trend Trawler is a two-phase agent pipeline built on Google's [ADK](https://google.github.io/adk-docs/get-started/):

- **Phase 1 — `trend_scout`** gathers the top 25 Google Search trends, researches cultural context via web search, filters to the 3 most campaign-relevant trends, and writes them to BigQuery.
- **Phase 2 — `creative_agent`** takes a single `<trend, campaign>` pair, runs parallel web research (campaign + trend researchers), synthesizes a strategic brief, generates ad copy and visual concepts, evaluates every creative with `creative_eval`, and exports a research PDF, an HTML gallery, and an evaluation report to Cloud Storage. `interactive_creative` is the same pipeline with human-in-the-loop checkpoints.

Deployed agents run on **Vertex AI Agent Engine**; batch runs fan out one creative job per trend via **Cloud Run Functions + Pub/Sub**.

<p align="center">
  <img src="docs/architecture/agent-engine-pipeline.png" alt="creative_agent pipeline on Agent Engine" width="720">
</p>

**Helpful references**
* [Overview of prompting strategies](https://cloud.google.com/vertex-ai/generative-ai/docs/learn/prompts/prompt-design-strategies#best-practices)
* [ADK documentation](https://google.github.io/adk-docs/get-started/)
* [Sample Agents](https://github.com/google/adk-samples/tree/main/python/agents)
* [adk-python SDK samples](https://github.com/google/adk-python/tree/main/contributing/samples)


## Quickstart

**1. Clone and authenticate**

```bash
git clone https://github.com/tottenjordan/adk_pipe.git
cd adk_pipe

export GOOGLE_CLOUD_PROJECT=$(gcloud config get-value project)
export GOOGLE_CLOUD_PROJECT_NUMBER=$(gcloud projects describe $GOOGLE_CLOUD_PROJECT --format="value(projectNumber)")

gcloud config set project $GOOGLE_CLOUD_PROJECT
gcloud auth application-default login
```

**2. Configure `.env`** — copy [.env.example](./.env.example) and fill in your project values, then `source .env`.

```bash
cp .env.example .env
# edit .env ...
source .env
```

<details>
  <summary>key <code>.env</code> values</summary>

```bash
GOOGLE_GENAI_USE_VERTEXAI=1
GOOGLE_CLOUD_PROJECT=this-my-project-id
# gemini-3.x models are only served from the `global` Vertex location;
# regional resources (BigQuery, GCS, PubSub, Agent Engine) use us-central1.
GOOGLE_CLOUD_LOCATION=global
GCP_REGION=us-central1
GOOGLE_CLOUD_PROJECT_NUMBER=12345678910


# Cloud Storage
GOOGLE_CLOUD_STORAGE_BUCKET=this-my-bucket-name
BUCKET=gs://this-my-bucket-name


# PubSub
CREATIVE_TOPIC_NAME=creative-eventarc-topic
CREATIVE_WORKER_TOPIC_NAME=creative-worker-queue-topic


# Cloud Run Functions
BASE_IMAGE=python313

CREATIVE_CRF_NAME=creative-trawler-crf
CRF_ENTRYPOINT=crf_entrypoint
CREATIVE_TRIGGER_NAME=creative-eventarc-trigger

CREATIVE_WORKER_CRF_NAME=creative-worker-crf
CREATIVE_WORKER_ENTRYPOINT=agent_worker_entrypoint
CREATIVE_WORKER_TRIGGER_NAME=creative-worker-starter-trigger


# BigQuery 
BQ_PROJECT_ID='this-my-project-bq-id'
BQ_DATASET_ID='trend_trawler'
BQ_TABLE_TARGETS='target_trends_crf'
BQ_TABLE_CREATIVES='trend_creatives'
BQ_TABLE_ALL_TRENDS='all_trends'


# Agent Engine (leave blank)
CREATIVE_AGENT_ENGINE_ID=""
TRAWLER_AGENT_ENGINE_ID=""


# campaign metadata
BRAND="Paul Reed Smith (PRS)"
TARGET_AUDIENCE="millennials who follow jam bands (e.g., Widespread Panic and Phish), respond positively to nostalgic messages"
TARGET_PRODUCT="PRS SE CE24 Electric Guitar"
KEY_SELLING_POINT="The 85/15 S Humbucker pickups deliver a wide tonal range, from thick humbucker tones to clear single-coil sounds, making the guitar suitable for various genres."
TARGET_SEARCH_TREND="tswift engaged"
```

</details>

**3. Install dependencies**

```bash
uv sync
```

**4. Create the BigQuery dataset and tables**

```bash
bq --location=US mk --dataset $BQ_PROJECT_ID:$BQ_DATASET_ID
```

<details>
  <summary>create the target-trends and creatives tables</summary>

```bash
# selected search trends
bq mk \
 -t \
 $BQ_PROJECT_ID:$BQ_DATASET_ID.$BQ_TABLE_TARGETS \
 uuid:STRING,processed_status:STRING,target_trend:STRING,refresh_date:DATE,trawler_date:DATE,entry_timestamp:TIMESTAMP,trawler_gcs:STRING,brand:STRING,target_audience:STRING,target_product:STRING,key_selling_point:STRING

# target-trend creatives
bq mk \
 -t \
 $BQ_PROJECT_ID:$BQ_DATASET_ID.$BQ_TABLE_CREATIVES \
 uuid:STRING,target_trend:STRING,datetime:DATETIME,creative_gcs:STRING,brand:STRING,target_audience:STRING,target_product:STRING,key_selling_point:STRING
```
</details>

**5. Run an agent locally**

```bash
uv run adk web .
```

Open the dev UI, pick an agent from the top-left drop-down, and provide your campaign metadata — see [Usage](#usage).


## Usage

Define your `campaign metadata` — these are the inputs to `trend_scout` and `creative_agent`.

```bash
# example campaign metadata
Brand Name: 'Paul Reed Smith (PRS)'
Target Audience: 'millennials who follow jam bands (e.g., Widespread Panic and Phish), respond positively to nostalgic messages, and love surreal memes'
Target Product: 'SE CE24 Electric Guitar'
Key Selling Points: 'The 85/15 S Humbucker pickups deliver a wide tonal range, from thick humbucker tones to clear single-coil sounds, making the guitar suitable for various genres.'
```

<details>
  <summary>guidance on what works well here</summary>

**Target Audience:** 
* who are they? what do they want? 
* go beyond typical demographics with...
  * **psychographics:** *people who are frustrated with...* 
  * **lisfestyle:** *frequent travelers; spending most income on concert experiences.*
  * **hobbies, interests, humor**: *music lovers, attend lots of jam band concerts. love surreal memes*
  * **lifestage**: *recent empty-nesters*

**Key Selling Points**

This will be the `{target_products}` 's flavor in the messaging and visual concepts
*can be used multiple ways. here are some...*

* What is the `{target_audience}` 's benefit? what will make them really care?
* external factors e.g., if selling sweaters: `it's cold outside`
* don't have to choose a single benefit. if there are several, explain them (experiment with this). However, can hyper-focused on one benefit as well...

  * *"Advanced Night Repair - Ideal for visible age prevention with double action to fight visible effects of free radical damage"*
  * *"Call Screen - Goodbye, spam calls. With Call Screen, Pixel can now detect and filter out even more spam calls. For other calls, it can tell you who’s calling and why before you pick up. Detect and decline spam calls without distracting you."*
  * *"Best Take - Group pics, perfected. Pixel’s Best Take combines similar photos into one fantastic picture where everyone looks their best. AI is able to blend multiple still images to give everyone their best look"*

</details>

### Running an Agent

Start the local dev UI:

```bash
uv run adk web .
```

**[a] choose `trend_scout` from the drop-down menu (top left)...**

```bash
user: Brand Name: "YOUR BRAND OF CHOICE"
      Target Audience: "YOUR TARGET AUDIENCE OF CHOICE"
      Target Product: "YOU TARGET PRODUCT OF CHOICE"
      Key Selling Points: "YOU KEY SELLING POINT(S)"

agent: `[end-to-end workflow >> recommended subset of trends]` 
```

**[b] choose `creative agent` in the top-left drop-down menu...**

```bash
user: Brand Name: "YOUR BRAND OF CHOICE"
      Target Audience: "YOUR TARGET AUDIENCE OF CHOICE"
      Target Product: "YOU TARGET PRODUCT OF CHOICE"
      Key Selling Points: "YOU KEY SELLING POINT(S)"
      target_search_trend: "YOUR_SEARCH_TREND_OF_CHOICE"

agent: `[end-to-end workflow >> candidate creatives]`
```

**[c] choose `interactive_creative` for human-in-the-loop mode...**

Same inputs as the `creative_agent`, but the pipeline pauses at 3 checkpoints for human review:

1. **After research** — review the research report, approve or request changes
2. **After ad copies** — review generated ad copies before visual concept generation
3. **After visual concepts** — review visual concepts and image prompts before image generation

At each checkpoint the UI displays a review panel where you can approve and continue, or provide feedback. Uses ADK's `LongRunningFunctionTool` for pause/resume.


## Evaluation

The `creative_eval` module runs automatically as part of both the `creative_agent` and `interactive_creative` pipelines. It evaluates every generated ad copy and visual concept using an LLM-as-judge approach (`gemini-3.1-pro-preview`). Each creative is scored by an independent judge call, run concurrently in a thread pool. Because the judge is a gemini-3 model, its client uses the `global` Vertex location.

**Ad Copy Dimensions (6):** strategic alignment, trend authenticity, platform viability, copy quality, audience fit, CTA strength

**Visual Concept Dimensions (6):** trend-visual connection, brand representation, audience appeal, prompt technical quality, stopping power, concept coherence

Each dimension is scored 1–10. Scores are normalized to 0.0–1.0 with a **0.7 passing threshold**. The evaluation report includes per-dimension verdicts with rationale, strengths, suggested improvements, and a summary with pass rates and weakest dimensions. The report is saved as JSON to GCS.


## Example Outputs

**1. the `creative_agent` conducts web research to inform the creative process. a PDF of this web research is saved for humans:**

<p align="center" width="100%">
    <img src="imgs/tt_prs_research_overview_p050_15fps.gif">
</p>


**2. the agents final step produces an HTML display of all generated ad creatives:**

<p align="center" width="100%">
    <img src="imgs/tt_prs_html_overview_p050_15fps.gif">
</p>

<details>
  <summary>some details on the HTML report</summary>

#### see campaign metadata at the top:

* brand
* target product
* key selling point
* target audience

#### each creative has a headline (title) and a caption

![trend trawler creative outputs](imgs/gallery_sample_prs.png)

#### hovering over a creative will display:

* how it references the search trend
* how it markets the target product
* why the target audience will find it appealing

![trend trawler creative outputs](imgs/its_complicated.png)


*remember: these are ad candidates to start the ideation process. the prompts are saved so you can easily tweak the creative*

</details>


## Frontend UI

A custom React frontend (Next.js + Tailwind CSS + shadcn/ui) for running agents and viewing results: campaign input form with agent selector, a live SSE event stream, pipeline-state widgets, interactive-mode review checkpoints, and a results page with the artifact gallery, HTML portfolio, and evaluation report.

```bash
# terminal 1 — ADK API server (backend)
uv run adk api_server . --allow_origins=http://localhost:3000

# terminal 2 — frontend (Node.js >= 18)
cd frontend && npm install && npm run dev   # http://localhost:3000
```

**→ See [frontend/README.md](frontend/README.md)** for the full component tree and details.


## Deployment

Trend Trawler deploys in two layers:

| Layer | What | Where |
| --- | --- | --- |
| **Agents** | `trend_scout`, `creative_agent`, `interactive_creative` | Vertex AI Agent Engine (one instance each) |
| **Fan-out** | orchestrator (`crf_entrypoint`) + worker (`agent_worker_entrypoint`) | Cloud Run Functions + Pub/Sub |

<p align="center">
  <img src="docs/architecture/crf-fanout-orchestration.png" alt="Cloud Run Functions fan-out orchestration" width="720">
</p>

Deploy an agent to Agent Engine:

```bash
python deployment/deploy_agent.py --version=v1 --agent=creative_agent --create
```

**→ Full instructions** — IAM, Pub/Sub topics, eventarc triggers, invoking the fan-out, testing deployed
agents, and the Cloud Run alternative — **live in [deployment/README.md](deployment/README.md).**


## Testing

```bash
# Frontend tests (Vitest + React Testing Library + jsdom)
cd frontend && npm test              # single run; npm run test:watch for watch mode

# Python tests (pytest) — requires GCP credentials
uv run pytest tests/ -v

# Creative evaluation test (real Gemini API calls, ~2 min)
uv run python -m creative_eval.run_eval_test

# ADK evals — end-to-end LLM-as-judge (real API calls, ~5 min per case)
uv run adk eval trend_scout tests/eval/evalsets/trend_scout_evalset.json \
  --config_file_path=tests/eval/eval_config.json --print_detailed_results

# Integration tests — requires deployed agents + GCP credentials
python deployment/integration_test.py --check all
```

The `creative_agent` eval must run with `PYTHONPATH="$PWD"` and its own rubric config — see [CLAUDE.md](CLAUDE.md) for the exact invocation and per-agent details.

**→ See [tests/README.md](tests/README.md)** for the full test-suite layout and what each test file covers.

**CI:** GitHub Actions runs frontend tests on push/PR to `main` when `frontend/**` files change (`.github/workflows/frontend-tests.yml`).


## Repo Structure

> The agent packages are kept **flat** at the repo root (not grouped under an `agents/` or `src/`
> parent) on purpose: Agent Engine's `extra_packages` staging preserves each package's relative path
> as its import path, so nesting them would break every bare `from creative_agent …` import. Flat is a
> deploy constraint, not an oversight.

```bash
.
├── trend_scout/                # Phase 1 — trend discovery agent
│   ├── __init__.py
│   ├── agent.py
│   ├── callbacks.py              # state init, rate limiting, citation processing
│   ├── config.py
│   └── tools.py
├── creative_agent/               # Phase 2 — creative generation agent
│   ├── __init__.py
│   ├── agent.py
│   ├── callbacks.py
│   ├── config.py
│   ├── prompts.py
│   ├── tools.py
│   └── sub_agents/
│       ├── __init__.py
│       ├── campaign_researcher/
│       │   ├── __init__.py
│       │   └── agent.py
│       └── trend_researcher/
│           ├── __init__.py
│           └── agent.py
├── interactive_creative/         # Phase 2 — human-in-the-loop variant
│   ├── __init__.py
│   ├── agent.py
│   └── review_tools.py           # LongRunningFunctionTool review checkpoints
├── creative_eval/                # LLM-as-judge evaluation module
│   ├── __init__.py
│   ├── agent.py
│   ├── config.py
│   ├── evaluate.py               # concurrent per-creative scoring
│   ├── prompts.py
│   ├── run_eval_test.py
│   └── schemas.py
├── agent_common/                 # ADK-free shared config (BaseAgentConfiguration, retry, build_gemini)
│   ├── __init__.py
│   ├── config.py
│   ├── locations.py              # MODEL_LOCATION (pins gemini-3.x to `global`)
│   ├── models.py                 # build_gemini(name)
│   └── retry.py                  # build_infra_retry()
├── cloud_functions/              # event-driven fan-out (orchestrator + worker)
│   ├── creative_fanout/
│   │   ├── config.py
│   │   ├── main.py               # crf_entrypoint + agent_worker_entrypoint
│   │   └── requirements.txt
│   └── trawler_scheduler/
│       ├── config.py
│       ├── main.py
│       └── requirements.txt
├── deployment/
│   ├── README.md                 # full deploy guide (Agent Engine, CRF fan-out, Cloud Run)
│   ├── deploy_agent.py           # deploy / list / delete Agent Engine instances
│   ├── headless_run.py           # run creative_agent via a local ADK Runner
│   ├── integration_test.py       # live GCP checks (health, session, smoke)
│   └── test_deployment.py        # invoke deployed agents
├── frontend/                     # Next.js + Tailwind + shadcn/ui web app
│   ├── src/
│   │   ├── app/                  # routes: campaign form, run (SSE), results + API proxies
│   │   ├── components/           # event log, gallery, GCS/trend widgets, ui/ primitives
│   │   ├── lib/                  # api client (session CRUD, SSE), presets, types, utils
│   │   └── __tests__/            # Vitest unit tests
│   ├── next.config.ts
│   ├── package.json
│   └── vitest.config.ts
├── tests/                        # pytest suite + ADK evals — see tests/README.md
├── docs/
│   ├── architecture/             # pipeline + CRF fan-out diagrams
│   ├── baselines/
│   ├── notes/                    # hard-won session notes
│   └── plans/                    # implementation plans (historical)
├── imgs/                         # README media
├── .github/workflows/
│   └── frontend-tests.yml
├── deploy-to-agent-engine.ipynb
├── .env.example
├── CLAUDE.md
├── CODE_STANDARDS.md
├── pyproject.toml
├── requirements.txt
├── uv.lock
└── README.md
```


## TODO

* ~~deployment script for Vertex AI Agent Engine~~
* ~~event-based triggers~~
* ~~creative evaluation (LLM-as-judge)~~
* ~~interactive mode (human-in-the-loop review checkpoints)~~
* scheduled runs
* email / notification
* easy export to ~*live editor tool* to nano-banana
