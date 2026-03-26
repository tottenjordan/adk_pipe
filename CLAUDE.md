# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Trend Trawler is a multi-agent system that automates trend-to-creative ad generation. It identifies culturally relevant Google Search trends, conducts web research, and generates candidate ad copy and visual concepts for a given brand/campaign. Built with Google's ADK (Agent Development Kit), deployed to Vertex AI Agent Engine, and orchestrated via Cloud Run Functions with PubSub triggers.

## Commands

```bash
# Install dependencies
uv sync

# Local development (ADK web UI)
uv run adk web .

# Local development (custom frontend + ADK api_server)
uv run adk api_server . --allow_origins=http://localhost:3000
cd frontend && npm install && npm run dev   # http://localhost:3000

# Deploy agent to Agent Engine
python deployment/deploy_agent.py --version=v1 --agent=trend_trawler --create
python deployment/deploy_agent.py --version=v1 --agent=creative_agent --create

# List/delete Agent Engine instances
python deployment/deploy_agent.py --list
python deployment/deploy_agent.py --resource_id=<ID> --delete

# Test deployed agents
export USER_ID='test_user'
python deployment/test_deployment.py --agent=trend_trawler --user_id=$USER_ID
python deployment/test_deployment.py --agent=creative_agent --user_id=$USER_ID
```

Code formatting uses Black (via VSCode).

### Testing

```bash
# Frontend tests (Vitest + React Testing Library)
cd frontend && npm test            # single run
cd frontend && npm run test:watch  # watch mode

# Python tests (pytest) — requires GCP credentials (module-level genai.Client)
uv run pytest tests/ -v
```

- Frontend: `frontend/src/__tests__/` — pure logic tests (SSE parsing, form validation, GCS URI building, widget layouts, trend markdown parsing, extractItems)
- Python: `tests/` — Pydantic schema validation, agent pipeline structure, tool functions, callbacks (citation regex, state init, rate limiting), deployment utilities, cloud function logic
- Integration: `deployment/integration_test.py` — live GCP checks (health, session lifecycle, smoke tests). Requires deployed agents.
- CI: `.github/workflows/frontend-tests.yml` — runs frontend tests on push/PR to `main` when `frontend/**` changes

```bash
# Integration tests (requires deployed agents + GCP credentials)
python deployment/integration_test.py --check health                          # verify agents reachable
python deployment/integration_test.py --check session --agent trend_trawler   # session lifecycle
python deployment/integration_test.py --check smoke --agent creative_agent    # full end-to-end
python deployment/integration_test.py --check all                             # everything
```

## Architecture

### Two-Phase Agent Pipeline

**Phase 1 — `trend_trawler/`**: Gathers top 25 Google Search trends, researches cultural context via web search, filters to 3 most campaign-relevant trends, saves to BigQuery.

**Phase 2 — `creative_agent/`**: Takes a single trend + campaign metadata, runs parallel web research (campaign researcher + trend researcher as sub-agents), synthesizes a strategic brief, generates ad copy and HTML gallery, exports research PDF and artifacts to GCS.

### Agent Composition

```
trend_trawler (root Agent)
├── gather_trends_agent (get_daily_gtrends tool)
├── understand_trends_agent (google_search tool)
├── pick_trends_agent (strategic filtering)
└── Persistence tools (BigQuery, GCS)

creative_agent (root Agent)
├── merge_parallel_insights (SequentialAgent)
│   ├── parallel_planner_agent (ParallelAgent)
│   │   ├── gs_sequential_planner (trend_researcher sub-agent)
│   │   └── ca_sequential_planner (campaign_researcher sub-agent)
│   └── merge_planners (synthesizes insights)
├── combined_web_evaluator
├── enhanced_combined_searcher
└── combined_report_composer (HTML/PDF output)
```

Key ADK patterns used: `Agent`, `SequentialAgent`, `ParallelAgent`, `AgentTool` (wraps agents as tools).

### Frontend — `frontend/`

Next.js 16 (App Router) + TypeScript + Tailwind CSS + shadcn/ui. Light theme with Sora font. Consumes the ADK `api_server` REST + SSE endpoints at `localhost:8000`.

**Pages:**
- `/` — Campaign input form (brand, audience, product, selling points, agent selector)
- `/run/[sessionId]` — Live SSE event stream with timeline, pipeline state widgets (modal overlays), campaign metadata sidebar
- `/results/[sessionId]` — Artifacts gallery, research PDF viewer, session state inspector

**Key files:**
- `frontend/src/app/layout.tsx` — Root layout, fonts (Sora + JetBrains Mono), glass header
- `frontend/src/app/page.tsx` — Campaign input form
- `frontend/src/app/run/[sessionId]/page.tsx` — SSE streaming, pipeline widgets, status tracking
- `frontend/src/app/results/[sessionId]/page.tsx` — Results viewer with artifact tabs
- `frontend/src/lib/api.ts` — API client (session CRUD, SSE streaming, artifact fetching)
- `frontend/src/app/api/gcs/route.ts` — Authenticated GCS proxy for serving artifacts

### Event-Driven Orchestration — `cloud_funktions/`

Fan-out pattern using two Cloud Run Function deployments from the same source (`cloud_funktions/creative_crf/`):
- **Orchestrator** (`crf_entrypoint`): Triggered by `CREATIVE_TOPIC_NAME` PubSub topic, queries BigQuery for unprocessed trends, dispatches one message per trend to worker topic. Concurrency=100.
- **Worker** (`agent_worker_entrypoint`): Triggered by `CREATIVE_WORKER_TOPIC_NAME`, processes a single trend row by invoking Agent Engine. Concurrency=1 (prevents duplicate processing). Timeout=900s.

### Configuration

Each agent has its own `config.py` importing from a shared `.env` file (see `.env.example`). Key settings:
- **Models**: `gemini-2.5-flash` (worker), `gemini-2.5-pro` (critic), `imagen-4.0-ultra-generate-preview-06-06` (image gen), `veo-3.0-generate-001` (video gen)
- **Rate limiting**: `before_model_callback` enforces rpm_quota (1000) over 60s intervals
- **Session state keys**: `brand`, `target_product`, `target_audience`, `key_selling_points`, `target_search_trends`

### Agent Definition Pattern

Agents use `before_agent_callback` to initialize session state, `before_model_callback` for rate limiting, and `output_key` to store results in state for downstream agents. Instructions use context variables like `{brand}`, `{target_product}`, `{target_audience}`.

### Data Flow

- **BigQuery**: Stores trend recommendations (`target_trends_crf`), creative results (`trend_creatives`), all trends (`all_trends`)
- **Cloud Storage**: Research PDFs, HTML galleries, session state JSONs
- **PubSub**: Event-driven dispatch between orchestrator and workers

## Key Files

- `*/agent.py` — Agent definitions (root and sub-agents)
- `*/tools.py` — Custom tool functions for each agent
- `*/callbacks.py` — State initialization, rate limiting, citation processing
- `*/prompts.py` — Agent instruction templates
- `*/config.py` — Model selection, env vars, rate limit settings
- `deployment/deploy_agent.py` — Agent Engine deploy/list/delete CLI
- `deployment/test_deployment.py` — Invoke deployed agents for testing
- `cloud_funktions/creative_crf/main.py` — Orchestrator and worker entry points

## Requirements

- Python >=3.13
- `google-adk>=1.27.3`
- Node.js >=18 (for frontend)
- GCP project with BigQuery, Cloud Storage, PubSub, and Agent Engine enabled
- `.env` file populated from `.env.example`
