# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Code Standards

**Always refer to [CODE_STANDARDS.md](./CODE_STANDARDS.md) when writing code or making
environment changes.** It is the authoritative source for packaging (`uv`), linting/
formatting (`ruff`), type checking (`ty`), testing (`pytest`), and commit conventions
(e.g. never add `Co-Authored-By` trailers). See also the `modern-python` skill.

## Project Overview

Trend Trawler is a multi-agent system that automates trend-to-creative ad generation. It identifies culturally relevant Google Search trends, conducts web research, and generates candidate ad copy and visual concepts for a given brand/campaign. Built with Google's ADK (Agent Development Kit), deployed to Vertex AI Agent Engine, and orchestrated via Cloud Run Functions with PubSub triggers.

## Commands

```bash
# Install dependencies
uv sync

# Local development (ADK web UI)
uv run adk web .

# Local development (custom frontend + backend)
# Run the async_app launcher (NOT bare `adk api_server`): the frontend's run page
# polls the async-job `/runs` endpoints, which only the launcher mounts. It also
# serves all the canned ADK CRUD/getSession/artifact endpoints, so this is a
# superset of `adk api_server`.
ALLOW_ORIGINS=http://localhost:3000 uv run uvicorn deployment.async_app:app --port 8000
cd frontend && npm install && npm run dev   # http://localhost:3000

# Deploy agent to Agent Engine (the per-agent packages bundled into the engine
# are derived from AGENT_EXTRA_PACKAGES in deploy_agent.py — a single source of
# truth from the import graph, so cross-package deps like creative_eval/agent_common
# can't be forgotten)
python deployment/deploy_agent.py --version=v1 --agent=trend_scout --create
python deployment/deploy_agent.py --version=v1 --agent=creative_agent --create
python deployment/deploy_agent.py --version=v1 --agent=interactive_creative --create

# List/delete Agent Engine instances
python deployment/deploy_agent.py --list
python deployment/deploy_agent.py --resource_id=<ID> --delete

# Test deployed agents
export USER_ID='test_user'
python deployment/test_deployment.py --agent=trend_scout --user_id=$USER_ID
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

# ADK evals — end-to-end agent evaluation with LLM-as-judge (real API calls, ~5 min per case)
uv run adk eval trend_scout tests/eval/evalsets/trend_scout_evalset.json \
  --config_file_path=tests/eval/eval_config.json --print_detailed_results

# creative_agent eval — needs PYTHONPATH (adk eval's file-spec loader doesn't put the
# repo root on sys.path, so creative_agent's sibling import `creative_eval` would fail),
# and uses its own creative-specific rubric config.
PYTHONPATH="$PWD" uv run adk eval creative_agent tests/eval/evalsets/creative_agent_evalset.json \
  --config_file_path=tests/eval/creative_eval_config.json --print_detailed_results
```

- Frontend: `frontend/src/__tests__/` — pure logic tests (async-job poll client in `poll-run.test.ts`, form validation, GCS URI building, widget layouts, trend markdown parsing, extractItems, interactive mode pause/resume)
- Python: `tests/` — Pydantic schema validation, agent pipeline structure, tool functions, callbacks (citation regex, state init, rate limiting), async-job run helpers (`test_async_runs.py`), deployment utilities, cloud function logic. See [tests/README.md](tests/README.md) for the per-file breakdown.
- ADK Evals: `tests/eval/` — end-to-end agent evaluation using `adk eval` CLI with rubric-based LLM-as-judge scoring (response quality + tool use quality). Runs against real APIs. One evalset + rubric config per agent: `evalsets/trend_scout_evalset.json` + `eval_config.json`; `evalsets/creative_agent_evalset.json` + `creative_eval_config.json`. The `creative_agent` eval must be run with `PYTHONPATH="$PWD"` (see command above).
- Integration: `deployment/integration_test.py` — live GCP checks (health, session lifecycle, smoke tests). Requires deployed agents.
- CI: `.github/workflows/frontend-tests.yml` — runs frontend tests on push/PR to `main` when `frontend/**` changes

```bash
# Integration tests (requires deployed agents + GCP credentials)
python deployment/integration_test.py --check health                          # verify agents reachable
python deployment/integration_test.py --check session --agent trend_scout   # session lifecycle
python deployment/integration_test.py --check smoke --agent creative_agent    # full end-to-end
python deployment/integration_test.py --check all                             # everything
```

## Architecture

**Flat package layout (deliberate):** agent packages live flat at the repo root, not under an
`agents/`/`src/` parent. Agent Engine's `extra_packages` staging preserves each package's relative path
as its import path (`tarfile.add(path)` → arcname), so nesting would break every bare
`from creative_agent …` import. Do not "tidy" this into a nested tree.

### Two-Phase Agent Pipeline

**Phase 1 — `trend_scout/`**: Gathers top 25 Google Search trends, researches cultural context via web search, filters to 3 most campaign-relevant trends, saves to BigQuery.

**Phase 2 — `creative_agent/`**: Takes a single trend + campaign metadata, runs parallel web research (campaign researcher + trend researcher as sub-agents), synthesizes a strategic brief, generates ad copy and visual concepts, evaluates all creatives, and exports research PDF, HTML gallery, and evaluation report to GCS.

**Phase 2 (interactive) — `interactive_creative/`**: Same pipeline as `creative_agent`, but pauses at 3 checkpoints for human review via ADK's `LongRunningFunctionTool`: (1) after research report, (2) after ad copies, (3) after visual concepts. Uses `ResumabilityConfig(is_resumable=True)`.

**Evaluation — `creative_eval/`**: LLM-as-judge module that scores each ad copy and visual concept across 6 dimensions (12 total). Uses `gemini-3.1-pro-preview` (served from the `global` Vertex location) with structured output; each creative is judged by an independent, concurrent call. Scores normalized 0.0–1.0, passing threshold 0.7. Produces `CreativeEvaluationReport` saved as JSON to GCS.

### Agent Composition

```
trend_scout (root Agent)
├── gather_trends_agent (get_daily_gtrends tool)
├── understand_trends_agent (google_search tool)
├── pick_trends_agent (strategic filtering)
└── Persistence tools (BigQuery, GCS)

creative_agent (root Agent)
├── combined_research_pipeline (SequentialAgent)
│   ├── parallel_planner_agent (ParallelAgent)
│   │   ├── gs_sequential_planner (trend_researcher sub-agent)
│   │   └── ca_sequential_planner (campaign_researcher sub-agent)
│   └── merge_planners (synthesizes insights)
├── ad_creative_pipeline (SequentialAgent)
├── visual_generation_pipeline (SequentialAgent)
├── visual_generator (image generation)
├── creative_eval_agent (LLM-as-judge scoring)
└── Persistence tools (GCS, BigQuery, HTML gallery)

interactive_creative (root Agent, resumable)
├── [same sub-agents as creative_agent]
├── review_research (LongRunningFunctionTool checkpoint)
├── review_ad_copies (LongRunningFunctionTool checkpoint)
└── review_visual_concepts (LongRunningFunctionTool checkpoint)
```

Key ADK patterns used: `Agent`, `SequentialAgent`, `ParallelAgent`, `AgentTool` (wraps agents as tools), `LongRunningFunctionTool` (pause/resume for human-in-the-loop).

### Frontend — `frontend/`

Next.js 16 (App Router) + TypeScript + Tailwind CSS + shadcn/ui. Light theme with Sora font. Consumes the backend REST endpoints at `localhost:8000` — ADK's canned session/artifact CRUD plus the async-job `/runs` kick-off/poll/resume endpoints (served together by `deployment/async_app.py`).

**Deployment:** the frontend now ships to Cloud Run as two services — `trend-trawler-web` (Next.js standalone) and `trend-trawler-api`. The backend runs the **custom launcher `deployment/async_app.py`** under uvicorn (entrypoint `deployment/backend_entrypoint.sh`): it mounts ADK's canned FastAPI app (session/artifact CRUD, `getSession`, `list-apps`) **plus** the async-job `/runs` router from the flat `runserver/` package — both sharing one `VertexAiSessionService`. It **must** be deployed with `--no-cpu-throttling --min-instances 1` so detached runs keep CPU and aren't killed by scale-to-zero (see the async-run runbook). The backend is private; the same-origin `/api/adk` proxy reaches it with a metadata-server ID token (`roles/run.invoker`). The frontend is **IAP-gated** (domain-restricted to `jordantotten.altostrat.com` via Cloud Run direct IAP), and the backend uses **persistent Agent Engine sessions** via `SESSION_SERVICE_URI` (a dedicated `trend-trawler-sessions` Reasoning Engine). Runbook: [deployment/README.md → Frontend + api_server on Cloud Run](deployment/README.md#frontend--api_server-on-cloud-run).

**Pages:**
- `/` — Campaign input form (brand, audience, product, selling points, agent selector: `trend_scout`, `creative_agent`, `interactive_creative`)
- `/run/[sessionId]` — Live run view: the page **polls** the async-job run (fire-and-forget kick-off + `GET /runs/.../{session}?since=N`) and renders new events into a timeline, pipeline state widgets (modal overlays), and a campaign metadata sidebar. Because progress is read from the persistent session log (not a browser-held SSE stream), a run **survives disconnect/reload/IAP re-auth** — reloading re-polls from `since=0` and replays. Interactive mode adds pause/resume review panels at each checkpoint.
- `/results/[sessionId]` — Artifacts gallery, research PDF viewer, evaluation report, session state inspector

**Key files:**
- `frontend/src/app/layout.tsx` — Root layout, fonts (Sora + JetBrains Mono), glass header
- `frontend/src/app/page.tsx` — Campaign input form
- `frontend/src/app/run/[sessionId]/page.tsx` — async-job polling (`pollRun`), pipeline widgets, status tracking, stall-timeout
- `frontend/src/app/results/[sessionId]/page.tsx` — Results viewer with artifact tabs
- `frontend/src/lib/api.ts` — API client (session CRUD, async-job `startRun`/`pollRun`/`resumeRun`, artifact fetching)
- `frontend/src/app/api/gcs/route.ts` — Authenticated GCS proxy for serving artifacts

### Event-Driven Orchestration — `cloud_functions/`

Fan-out pattern using two Cloud Run Function deployments from the same source (`cloud_functions/creative_fanout/`):
- **Orchestrator** (`crf_entrypoint`): Triggered by `CREATIVE_TOPIC_NAME` PubSub topic, queries BigQuery for unprocessed trends, dispatches one message per trend to worker topic. Concurrency=100.
- **Worker** (`agent_worker_entrypoint`): Triggered by `CREATIVE_WORKER_TOPIC_NAME`, processes a single trend row by invoking Agent Engine. Concurrency=1 (prevents duplicate processing). Timeout=900s.

### Configuration

Shared building blocks live in **`agent_common/`** (a lightweight package bundled into every deployed engine; it depends on `google-adk` but is deliberately free of any per-agent business logic, and no cloud function imports it):
- `agent_common/config.py` — `BaseAgentConfiguration`, the single source of truth for the model names, rate-limit knobs, and GCP/BigQuery env vars. Each agent's `config.py` subclasses it (`ResearchConfiguration(BaseAgentConfiguration)`) and adds only its genuine differences (e.g. `trend_scout`'s `SetupConfiguration`), which is why the two agent configs no longer drift.
- `agent_common/retry.py` — `build_infra_retry(extra_exceptions=(), max_attempts=3)`, the one place the ADK `RetryConfig` transient-exception list is defined (`creative_agent` passes the genai `ServerError`).
- `agent_common/retry_agent.py` — `RetryUntilKeyAgent`, the retry-on-empty producer wrapper (re-runs a flaky `google_search`+thinking producer until its `output_key` is populated, bounded; degrades observably on exhaustion). Shared here so both `creative_agent` and `trend_scout` wrap producers without cross-importing each other's package.
- `agent_common/locations.py` + `agent_common/models.py` — `MODEL_LOCATION` (default `global`) and `build_gemini(name)`, which pin every gemini-3.x call's serving location in code (Agent Engine *reserves* `GOOGLE_CLOUD_LOCATION`, so it can't be forced via deploy env vars).
- `agent_common/observability.py` — the shared debugging callbacks used by every agent: `log_run_start` (run→session correlation line), `log_empty_turn_finish_reason` (`after_model_callback` that warns only on empty/abnormal producer turns), `make_final_state_summary(label, keys)` (factory → `after_agent_callback` logging load-bearing state keys + `*__retry_exhausted` markers), and `collect_degradation_warnings(state)` — the single source of truth that turns retry-exhaustion markers into the notes surfaced on the eval report (`warnings`), the `creative_evals.research_gaps` BQ column, and the HTML gallery banner. Snapshots `state.to_dict()` before scanning (an ADK `State` isn't directly iterable).

The bucket name comes from `GOOGLE_CLOUD_STORAGE_BUCKET` (the var deploy actually ships) — not the local-only `GCS_BUCKET_NAME`. Key settings:
- **Models**: `gemini-3.5-flash` (worker), `gemini-3.1-pro-preview` (critic + `creative_eval` judge), `gemini-3.1-flash-lite` (lite planner), `gemini-3.1-flash-image` (image gen), `veo-3.1-generate-001` (video gen)
- **Model location**: gemini-3.x models are only served from the `global` Vertex location — set `GOOGLE_CLOUD_LOCATION=global`. Regional resources (BigQuery, GCS, PubSub, Agent Engine) stay in `us-central1`.
  - **Agent Engine region (`GCP_REGION`):** Agent Engine / Reasoning Engine is a *regional* resource, so its Vertex AI SDK clients read `GCP_REGION` (default `us-central1`), decoupled from `GOOGLE_CLOUD_LOCATION=global`. Wired through `deployment/deploy_agent.py`, `deployment/test_deployment.py`, `deployment/integration_test.py`, and the `cloud_functions/*/config.py` constants (`config.GCP_REGION`). The `global` model location is used only by the genai model clients (`creative_agent/tools.py`, `creative_eval/evaluate.py`); BigQuery and GCS clients take no location.
- **Rate limiting**: `before_model_callback` enforces rpm_quota (1000) over 60s intervals
- **Session state keys**: `brand`, `target_product`, `target_audience`, `key_selling_points`, `target_search_trends`

### Agent Definition Pattern

Agents use `before_agent_callback` to initialize session state, `before_model_callback` for rate limiting, and `output_key` to store results in state for downstream agents. Instructions use context variables like `{brand}`, `{target_product}`, `{target_audience}`.

### Data Flow

- **BigQuery**: Stores trend recommendations (`target_trends_crf`), creative results (`trend_creatives`), all trends (`all_trends`), per-run evaluation summaries (`creative_evals` — one row per run, joins `trend_creatives` via `creative_uuid`, links to the full report JSON in GCS)
- **Cloud Storage**: Research PDFs, HTML galleries, session state JSONs
- **PubSub**: Event-driven dispatch between orchestrator and workers

## Key Files

- `*/agent.py` — Agent definitions (root and sub-agents)
- `*/tools.py` — Custom tool functions for each agent
- `*/callbacks.py` — State initialization, rate limiting, citation processing
- `*/prompts.py` — Agent instruction templates
- `*/config.py` — Per-agent config; subclasses `agent_common.BaseAgentConfiguration`
- `agent_common/config.py` — `BaseAgentConfiguration` shared config source-of-truth
- `agent_common/retry.py` — `build_infra_retry()` shared ADK `RetryConfig` factory
- `agent_common/models.py` / `agent_common/locations.py` — `build_gemini()` + `MODEL_LOCATION` (pins gemini-3.x to `global`)
- `interactive_creative/review_tools.py` — `LongRunningFunctionTool` pause tools for human-in-the-loop checkpoints
- `creative_eval/evaluate.py` — Core LLM-as-judge evaluation logic
- `creative_eval/schemas.py` — Pydantic models for evaluation reports
- `tests/eval/eval_config.json` — ADK eval criteria config (rubric-based scoring)
- `tests/eval/evalsets/` — ADK eval cases per agent
- `deployment/deploy_agent.py` — Agent Engine deploy/list/delete CLI; `AGENT_EXTRA_PACKAGES`/`AGENT_DEPLOY_SPECS` maps are the single source of truth for what each agent bundles
- `deployment/test_deployment.py` — Invoke deployed agents for testing
- `runserver/async_runs.py` — async-job run model: `/runs` FastAPI router + pure helpers. Kicks off a **detached `asyncio` task** driving `Runner.run_async` to completion decoupled from the HTTP request, appends a terminal `__run_status` marker event on done/error, and serves poll (`GET ?since=N`) + resume endpoints. Replaces browser-held SSE so runs survive client disconnect.
- `deployment/async_app.py` — launcher that mounts the `/runs` router on ADK's canned FastAPI app, sharing one `VertexAiSessionService`; run under uvicorn by `deployment/backend_entrypoint.sh`
- `cloud_functions/creative_fanout/main.py` — Orchestrator and worker entry points
- `cloud_functions/creative_fanout/session.py` — `agent_session` async context manager (create→query→delete under one `user_id`, delete-on-error)

## Requirements

- Python >=3.13
- `google-adk[eval]>=1.28.0`
- Node.js >=18 (for frontend)
- GCP project with BigQuery, Cloud Storage, PubSub, and Agent Engine enabled
- `.env` file populated from `.env.example`
