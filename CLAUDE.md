# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Code Standards

**Always refer to [CODE_STANDARDS.md](./CODE_STANDARDS.md) when writing code or making
environment changes.** It is the authoritative source for packaging (`uv`), linting/
formatting (`ruff`), type checking (`ty`), testing (`pytest`), and commit conventions
(e.g. never add `Co-Authored-By` trailers). See also the `modern-python` skill.

## Project Overview

Trend Trawler is a multi-agent system that automates trend-to-creative ad generation. It identifies culturally relevant Google Search trends, conducts web research, and generates candidate ad copy and visual concepts for a given brand/campaign. Built with Google's ADK (Agent Development Kit), deployed to Vertex AI Agent Engine, and orchestrated via Cloud Run Functions with PubSub triggers.

**Naming:** Agent Engine = *Agent Runtime* (2026 rebrand, Gemini Enterprise Agent Platform). The code uses the AgentPlatform SDK (`agentplatform.Client().runtimes`); the root env gets it from google-cloud-aiplatform 2.x (no override needed: the `eval` group lists google-adk's eval extra itself, see `pyproject.toml`). (Full note in README.)

## Commands

`make help` lists shortcuts for the commands below.

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
# TRUST_CLIENT_USER_ID=1 is required locally: the default per-user authz mode is enforce
# (401 without a proxy-asserted X-TT-User), and the app refuses the flag on Cloud Run.
# SESSION_SERVICE_URI=memory:// sidesteps ADK's per-agent local SQLite path check, which
# rejects the agents/ symlinks (pre-existing 400 "resolves outside base directory").
TRUST_CLIENT_USER_ID=1 SESSION_SERVICE_URI=memory:// ALLOW_ORIGINS=http://localhost:3000 uv run uvicorn deployment.async_app:app --port 8000
cd frontend && npm install && npm run dev   # http://localhost:3000

# Deploy agent to Agent Engine (the per-agent packages bundled into the engine
# come from AGENT_EXTRA_PACKAGES in deploy_agent.py — a hand-kept single source of
# truth that tests/test_deploy_import_graph.py checks against each agent's real
# import graph, so cross-package deps like creative_eval/agent_common can't be forgotten)
python deployment/deploy_agent.py --version=v1 --agent=trend_scout --create
python deployment/deploy_agent.py --version=v1 --agent=creative_agent --create
python deployment/deploy_agent.py --version=v1 --agent=interactive_creative --create
# add --enable_tracing to opt a new engine into Cloud Trace
# (GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true + ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false)
# Or deploy from Cloud Build (fresh clone of _REF, pinned Python 3.13, env only from the
# committed deployment/deploy.env, engines in parallel, never writes .env; new IDs in
# engine_ids.json -> `deployment/engine_handoff.py apply --update-worker`: .env + the CRF
# worker's CREATIVE_AGENT_ENGINE_ID + its pinned traffic moved to the new revision (verified),
# refused while a batch row is PROCESSING): deployment/README.md → Deploying engines with Cloud Build
gcloud builds submit --no-source --region=us-central1 --config=deployment/cloudbuild.agents.yaml \
  --substitutions=_CREATIVE_VERSION=v37,_INTERACTIVE_VERSION=v33,_SCOUT_VERSION=v18

# List/delete Agent Engine instances
python deployment/deploy_agent.py --list
python deployment/deploy_agent.py --resource_id=<ID> --delete

# Test deployed agents
export USER_ID='test_user'
python deployment/test_deployment.py --agent=trend_scout --user_id=$USER_ID
python deployment/test_deployment.py --agent=creative_agent --user_id=$USER_ID
```

Formatting and linting use ruff (`uv run ruff format .` / `uv run ruff check .`; config in `pyproject.toml`, rule set pinned to `E,F,I,UP,B`); type checking uses `uv run ty check`.

### Testing

```bash
# Frontend tests (Vitest + React Testing Library)
cd frontend && npm test            # single run
cd frontend && npm run test:watch  # watch mode

# Python tests (pytest) — no GCP credentials needed, but GOOGLE_CLOUD_PROJECT must be set
# (any dummy value, e.g. test-project; the repo .env normally provides it) because
# genai.Client(vertexai=True) construction (e.g. the lazily built creative_eval judge
# client, exercised by tests) resolves the project eagerly
uv run pytest tests/ -v
uv run pytest tests/ -q -n 4   # parallel via pytest-xdist (~2x faster than serial; the CI setting)
uv run pytest tests/ -q -n 16  # local tip: fastest on a 32-core box (42s vs 50s for -n auto, 53s for -n 4; each worker re-imports the agents, so more is not always better). CI keeps -n 4 on its 4-vCPU runner
uv run pytest tests/ -q -n 6 -m "not slow"   # fast local loop (skips JAX-compile/golden/image-staging tests; CI runs all)

# ADK evals — end-to-end agent evaluation with LLM-as-judge (real API calls, ~5 min per case)
PYTHONPATH="$PWD" uv run adk eval trend_scout tests/eval/evalsets/trend_scout_evalset.json \
  --config_file_path=tests/eval/eval_config.json --print_detailed_results

# creative_agent eval — needs PYTHONPATH (adk eval's file-spec loader doesn't put the
# repo root on sys.path, so creative_agent's sibling import `creative_eval` would fail),
# and uses its own creative-specific rubric config.
PYTHONPATH="$PWD" uv run adk eval creative_agent tests/eval/evalsets/creative_agent_evalset.json \
  --config_file_path=tests/eval/creative_eval_config.json --print_detailed_results
```

- Test inventories (which test file covers what, frontend and Python) and the full CI workflow details: [docs/reference/testing.md](docs/reference/testing.md); per-file table in [tests/README.md](tests/README.md).
- ADK evals: `tests/eval/` (one evalset + rubric config per agent; `creative_agent` needs `PYTHONPATH="$PWD"`). Integration: `deployment/integration_test.py` (live GCP, deployed agents).
- CI (path-gated, PR-only + `workflow_dispatch`): `python-ci.yml` (`uv sync --locked`, requirements drift, ruff, ty, `pytest -n 4`), `crf-deps.yml` (CRF tests in the function's own venv), `frontend-tests.yml` (lint, test, `next build`), `adk-eval.yml` (nightly, not on PRs; `tests/eval/efficiency_gate.py` is the real pass/fail signal).

```bash
# Integration tests (requires deployed agents + GCP credentials)
python deployment/integration_test.py --check health                          # verify agents reachable
python deployment/integration_test.py --check session --agent trend_scout   # session lifecycle
python deployment/integration_test.py --check smoke --agent creative_agent    # full end-to-end (asserts finalize_done + eval report / research PDF URIs)
python deployment/integration_test.py --check all                             # everything
```

## Architecture

**Flat package layout (deliberate):** agent packages live flat at the repo root, not under an
`agents/`/`src/` parent. Agent Engine's `extra_packages` staging preserves each package's relative path
as its import path (`tarfile.add(path)` → arcname), so nesting would break every bare
`from creative_agent …` import. Do not "tidy" this into a nested tree.

### Two-Phase Agent Pipeline

- **Phase 1 — `trend_scout/`**: gathers the top 25 Google Search trends, digests them (`trend_digest`: one-line summaries + sensitivity flags, cached per trend list in BigQuery `trend_digest`; flagged terms are never auto-picked), researches cultural context, picks the 3 most campaign-relevant trends and saves them to BigQuery.
- **Phase 2 — `creative_agent/`**: one trend + campaign → parallel research → structured `CreativeBrief` → gated ad copy and visual concepts → rendered images with post-render QA → LLM-judge evaluation → research PDF, HTML gallery and eval report in GCS. The root makes ONE `creative_pipeline` Workflow call after `memorize`.
- **Phase 2 (interactive) — `interactive_creative/`**: the same pipelines (imported from `creative_agent`'s public facade, `creative_agent/__init__.py`), pausing at 3 `LongRunningFunctionTool` checkpoints (brief, ad copies, visual concepts); resumable App. Checkpoint details: [docs/reference/agent-composition.md](docs/reference/agent-composition.md).
- **Evaluation — `creative_eval/`**: `gemini-3.1-pro-preview` judge, 12 advisory dimensions + binary gates; a creative passes only with score ≥ 0.7 AND every blocking gate passed. Every report is stamped with `creative_eval.JUDGE_VERSION`: bump it (to the change's date, `.N` for a second same-day change) whenever gates, gate/dimension wording, scoring/pass rules or the judge model change; calibration counts current-version ratings only. Gates, judge integrity, early ad-copy judging, calibration: [docs/reference/evaluation.md](docs/reference/evaluation.md).

### Agent Composition

Compact view (full tree with every node, gate and state key: [docs/reference/agent-composition.md](docs/reference/agent-composition.md)):

```
trend_scout (root; resumable App; root history trimmed by drop_other_agent_context)
├── gather_trends_agent → trend_digest (Workflow: digest_lookup → hit | miss (describe → classify → digest_store) | disabled)
├── understand_trends_agent_resilient (RetryUntilKeyNode: searcher → synthesizer → info_gtrends)
├── pick_trends_agent → selected_gtrends; review_trends (LongRunningFunctionTool, opt-in)
└── persistence tools (BigQuery, GCS, record_research_gaps, memorize)

creative_agent (root `root_agent`; tools = creative_pipeline (NodeTool) + memorize)
└── creative_pipeline (Workflow, input_schema=PipelineRequest)
    ├── combined_research_pipeline: planners + load_brand_history + trend_situation_pipeline
    │   → research_join → refinement_gate → report composer → brief_writer → brief_gate
    │   (revise ≤ BRIEF_REVISION_ROUNDS) → save_research_pdf_node
    ├── ad_creative_pipeline: drafter (10) → critic (4) → copy_gate (revise ≤ COPY_REVISION_ROUNDS)
    ├── visual_production_pipeline: art_director → concept drafter/critic/finalizer → concept_gate
    │   (fix ≤ CONCEPT_REVISION_ROUNDS) → visual_generator_resilient (render + image QA, no LLM turn)
    ├── (fan-out sibling of the visual stage: evaluate_ad_copies_node, early ad-copy judging; joined before finalize)
    └── finalize_pipeline: evaluate_creatives_node → persist_node → finalize_ready (sets finalize_done)

interactive_creative (root; resumable App): combined_research / ad_creative / visual_generation /
  finalize pipelines + a standalone visual_generator_resilient (renders after the reviser), as NodeTools
  + review_research / review_ad_copies / review_visual_concepts checkpoints
  + ad_copy_user_reviser, visual_concept_reviser, save_draft_report_artifact, memorize
```

Key ADK patterns used: `Agent`, graph `Workflow`s (`google.adk.workflow`: fan-out + `JoinNode`, routed function nodes, truthy terminal nodes; exposed to roots as bare nodes → `NodeTool`), `RetryUntilKeyNode` (graph retry wrapper in `agent_common/`), `AgentTool` (wraps agents as tools), `LongRunningFunctionTool` (pause/resume for human-in-the-loop), and `App` + `ResumabilityConfig` (resumable sessions). The ADK Workflow migration (proposal P2, complete 2026-09-29; `docs/plans/2026-09-29-p2-adk-workflow-migration.md`) retired the deprecated `SequentialAgent`/`ParallelAgent`/`LoopAgent` containers and the `RunIfAgent`/`RetryUntilKeyAgent` wrappers; `tests/test_public_api.py` guards against their return (no references in the agent packages, and importing every agent emits no legacy-container `DeprecationWarning`).

### Invariants (read before changing these areas)

- **Build + traffic pins:** images are built by `gcloud builds submit --config cloudbuild.yaml --ignore-file deployment/cloudbuild.gcloudignore --substitutions=SHORT_SHA=$(git rev-parse --short HEAD)` (SHORT_SHA is required) and deployed with `gcloud run deploy --image <digest>`; `trend-trawler-share` runs web's image digest (no separate build). `trend-trawler-api` and `trend-trawler-web` are traffic-pinned, so a new revision lands at 0%: move traffic with `gcloud run services update-traffic --to-revisions <rev>=100`, checking first that the api has no run in flight (a revision change kills in-flight runs). The CRF worker's pin is moved by `deployment/engine_handoff.py apply --update-worker`.
- **api runtime:** deployed with `--no-cpu-throttling --min-instances 1` (detached runs need CPU; no scale-to-zero). Runbook: [deployment/README.md](deployment/README.md).
- **Public surface:** `trend-trawler-share` (`SHARE_MODE`) is the only public service. Never add `allUsers` / IAP changes to web or api. Details: [docs/reference/frontend.md](docs/reference/frontend.md).
- **Agent Engine pickles at deploy:** anything read at agent-module import (models, knobs, Model Armor, temperature profile, timeouts) comes from the deployer's env (`deployment/deploy.env` in Cloud Build). Never seed per-run state at import.
- **Locations:** gemini-3.x models only at `global` (`GOOGLE_CLOUD_LOCATION=global`, pinned in code by `agent_common.models.build_gemini`); Agent Engine SDK clients use `GCP_REGION` (default `us-central1`); BigQuery, GCS and Pub/Sub live in `us-central1` (their clients take no location). Bucket name: `GOOGLE_CLOUD_STORAGE_BUCKET` (not `GCS_BUCKET_NAME`, no `BUCKET`).
- **JAX is dev-group only:** never add `jax` to `[project] dependencies` or `requirements.txt` (api image + engines stay JAX-free); `runserver/` never imports `bandit/`. Runtime code never imports group-only packages (`tests/test_dependency_groups.py`).
- **Flat package layout** (above) and `AGENT_EXTRA_PACKAGES` in `deployment/deploy_agent.py` (checked by `tests/test_deploy_import_graph.py`).
- **Prompts:** instruction texts spliced into ADK prompts must be brace-free (`{x}` is a state token; use `[x]`). Optional rule blocks (`{timely_rules?}`, `{person_casting_rules?}`, `{rating_limits?}`, safer-angle rules) keep runs without them **byte-identical**, pinned by sha tests (`tests/test_timely_rules.py`, `tests/test_safer_angle.py`). Render and judge prompts have goldens (`tests/test_render_parity.py`, `tests/test_judge_prompt_golden.py`).
- **Completion:** creative apps are finished only at `finalize_done` (runserver auto-continue, the CRF worker and the smoke test all key on it).
- **Root history:** `agent_common.history.drop_other_agent_context` trims replayed NodeTool interiors from root prompts; keep it first in each root's `before_model_callback`.
- **BigQuery writes are idempotent:** row keys come from `agent_common.idempotency.stable_row_id(session.id, …)`, never uuid4, written via `MERGE … WHEN NOT MATCHED THEN INSERT`, so at-least-once tool execution (resumed apps, CRF retries) leaves exactly one row.
- **Privacy in logs:** never log user emails (log `stable_row_id(user_id)`), person photo URIs, whole events or `state_delta`. Person references are consent-gated (`runserver/person_refs.py`); URIs are never returned either; revoking deletes the photo and every image made with it (revoke cascade). Details: [docs/reference/configuration.md](docs/reference/configuration.md) → Person casting, Personalised variants, Person share scope + revoke cascade.
- **Package boundaries:** `interactive_creative` imports only `creative_agent`'s public facade (`creative_agent/__init__.py`), never `creative_agent.agent` / `.schemas` internals, and the facade never re-exports `config` (it would shadow the `creative_agent.config` submodule). `agent_common` holds no per-agent business logic and no cloud function imports it. The shared tool name must stay `memorize`. Campaign state keys are seeded via `createSession` initialState, never via the user message.
- **Python ↔ TypeScript mirrors:** eval dimensions, rating reasons, consent text, person slugs and timely expiry exist on both sides with drift tests or shared goldens; change both in the same PR.
- **Commits and PRs:** never add `Co-Authored-By` trailers (see CODE_STANDARDS.md).

### Frontend — `frontend/`

Next.js 16 app: async-job polling run page, results contact sheet, ratings, shares, people, experiments; IAP-gated, per-user authz via the `/api/adk` proxy + `runserver/authz.py`; the public share viewer is the `SHARE_MODE` service. Details: [docs/reference/frontend.md](docs/reference/frontend.md).

### Bandit experiments — `bandit/`, `bandit_serving/`, `bandit_traffic/`

JAX LinTS endpoint (CPR, one worker, one replica) + synthetic traffic job + `/experiments` UI; [docs/bandit/contracts.md](docs/bandit/contracts.md) is the interface source of truth (change it in the same PR as any interface change). Details: [docs/reference/bandit.md](docs/reference/bandit.md).

### Event-Driven Orchestration — `cloud_functions/`

Pub/Sub orchestrator → Cloud Tasks queue `creative-worker-queue` → worker running the `creative_agent` engine (its own `CREATIVE_AGENT_ENGINE_ID`, set by `deployment/engine_handoff.py apply --update-worker`), with fenced BigQuery row state. Details: [docs/reference/crf.md](docs/reference/crf.md).

### Configuration

`agent_common/` building blocks (config, retry, `RetryUntilKeyNode`, `FailSoftNode`, timeouts, rate limit, history trim, clock, clients, idempotency, safety, models, observability), model lineup, image QA, trend digest / situation / timely slot, safer angle, brand history, rating learning, session state keys, person casting, variants, share scope + revoke cascade, visual-intent keys. Details: [docs/reference/configuration.md](docs/reference/configuration.md).

### Agent Definition Pattern

Agents use `before_agent_callback` to initialize session state, `before_model_callback` for rate limiting, and `output_key` to store results in state for downstream agents. Instructions use context variables like `{brand}`, `{target_product}`, `{target_audience}`.

Agent `instruction=` strings live in the package's `prompts.py` (as `<AGENT_VAR>_INSTR` constants), not inline in `agent.py`.

Agent `output_schema=` Pydantic models live in the package's `schemas.py` (re-imported into `agent.py`, which keeps them importable from the agent module), not inline in `agent.py`.

Image-prompt guidance (`IMAGE_PROMPT_GUIDE`, style shortlist, motif/product/brand-cue guard, concept gate): [docs/reference/configuration.md](docs/reference/configuration.md) → Image-generation prompt guidance.

### Data Flow

BigQuery tables, Cloud Storage layout, Pub/Sub + Cloud Tasks. Details: [docs/reference/data-flow.md](docs/reference/data-flow.md).

## Key Files

Where each piece lives: [docs/reference/key-files.md](docs/reference/key-files.md).

## Requirements

- Python >=3.13
- `google-adk[otel-gcp]>=2.10.0,<3.0.0` (`otel-gcp` adds the GenAI SDK instrumentor, so `generate_content` spans appear when tracing is on; content capture stays off by default)
- google-cloud-aiplatform 2.x (`[agent-engines]` at runtime). `[project] dependencies` is the slim runtime set that ships to Agent Engine (requirements.txt) and the api image (`uv sync --no-dev`); dependency groups hold the rest: `eval` (google-adk's eval extra listed package by package, since `google-adk[eval]` caps aiplatform <2; incl. aiplatform `[evaluation]`; used by the nightly eval CI), `experiments` (plotly/kaleido/matplotlib), and `dev` (includes both, plus the CRF's functions-framework/cloudevents/pubsub, JAX, pytest, ruff, ty; the CRF iterates BigQuery rows, so neither it nor its tests need pandas; deploy_agent.py needs only the runtime set, so the Cloud Build deploy uses `uv sync --no-dev`). Runtime code must not import group-only packages (`tests/test_dependency_groups.py`)
- Node.js >=22.13 (for frontend)
- GCP project with BigQuery, Cloud Storage, Pub/Sub, Eventarc, Cloud Tasks, and Agent Engine enabled
- `.env` file populated from `.env.example`
