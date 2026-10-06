# Agent Architecture Diagrams

Per-agent ADK architecture diagrams generated with the PaperBanana MCP pipeline
(`gemini-3.1-flash-image`), in the style of official Google Cloud documentation.
Each shows the agent/graph composition (ADK 2 graph `Workflow`s exposed as
`NodeTool`s, `AgentTool` wrapping, `RetryUntilKeyNode` retry wrappers) and the
per-agent tooling. The `trend_scout` and `creative_agent` diagrams were regenerated
2026-09-29 for the P2 graph-Workflow migration (no more Sequential/Parallel/RunIf agents);
the `interactive_creative` diagram was added 2026-09-29.

| Diagram | Agent | Highlights |
|---|---|---|
| ![trend_scout](trend_scout_architecture.png) | `trend_scout/` | Root `LlmAgent` (resumable `App`) → `AgentTool` gather/pick sub-agents + `understand_trends_agent_resilient` (`NodeTool`: `RetryUntilKeyNode`, max 3, over a START → searcher → synthesizer graph `Workflow`) → opt-in `review_trends` checkpoint → flat persistence tools; shared session state; BigQuery (idempotent `MERGE`) + GCS sinks |
| ![creative_agent](creative_agent_architecture.png) | `creative_agent/` | Three graph-`Workflow` `NodeTool`s: research (START fan-out to trend + campaign planners, each a `RetryUntilKeyNode` searcher→synthesizer → `research_join` `JoinNode` → barrier → `merge_planners` → `refinement_gate` routing `refine`/`skip` → composer); ad copy (drafter → critic); visual (art_director → drafter → critic → finalizer → `RetryUntilKeyNode` image gen); `creative_eval_agent` `AgentTool` LLM-judge; all sinks |
| ![interactive_creative](interactive_creative_architecture.png) | `interactive_creative/` | Resumable root (`App` + `ResumabilityConfig`) reusing `creative_agent`'s research / ad-copy / visual graph-`Workflow` `NodeTool`s, with three `LongRunningFunctionTool` checkpoints (`review_research` → `review_ad_copies` → `review_visual_concepts`, human approve/feedback/edits → resume) → `visual_concept_reviser` `AgentTool` (applies `visual_revision_notes`, skipped when none) → `visual_generator_resilient` (`RetryUntilKeyNode`, max 6) → `creative_eval_agent`; same GCS + BigQuery sinks |
| ![creative_eval](creative_eval_architecture.png) | `creative_eval/` | LLM-as-judge: `evaluate_all_creatives` → `ThreadPoolExecutor` concurrent fan-out to N Gemini judges → 6 ad-copy + 6 visual dims → `EvaluationSummary` → `CreativeEvaluationReport` → GCS JSON + BigQuery `creative_evals` (join on `creative_uuid`) |

## Agent Workflow Diagrams

Companion diagrams to the architecture set above, also generated with PaperBanana
(2026-10-01). Where the architecture diagrams show *how an agent is composed*, these
show *the order a run executes in*: phases, branches, human-review pauses, retries,
and where results are persisted.

| Diagram | Agent | Run order |
|---|---|---|
| ![trend_scout workflow](trend_scout_workflow.png) | `trend_scout/` | Input → gather top 25 trends → branch on `interactive_trend_pick` (pause at `review_trends` for a human pick, or autonomous pick of the 3 most relevant) → research via `understand_trends_agent_resilient` → persist (`record_research_gaps`, BigQuery `target_trends_crf`, `selected_trends.txt`, session state to GCS) |
| ![creative_agent workflow](creative_agent_workflow.png) | `creative_agent/` | Parallel trend + campaign research joined at `research_join` → `refinement_gate` (refine only when degraded) → report + research PDF → ad copy drafter → critic → art director → concept drafter/critic/finalizer → retry-wrapped image rendering → `creative_eval_agent` → GCS + BigQuery persistence |
| ![interactive_creative workflow](interactive_creative_workflow.png) | `interactive_creative/` | The `creative_agent` pipeline with three `LongRunningFunctionTool` pauses (research, ad copies, editable visual concepts), `visual_concept_reviser` applying checkpoint-3 notes before rendering, then eval + persistence |
| ![creative_eval workflow](creative_eval_workflow.png) | `creative_eval/` | `creative_eval_agent` → `evaluate_all_creatives` reads session state → independent Gemini judge per creative (max 2 concurrent, 6 dimensions each; visuals judged on concept text + image prompt) → score = mean/10, pass at 0.7 → `CreativeEvaluationReport` back to the root, which saves it to GCS + BigQuery `creative_evals` |

## Infrastructure Diagrams

Event-driven orchestration diagrams for the Cloud Run functions + Eventarc
triggers (`cloud_functions/creative_fanout/`) and how they interact with the
Vertex AI Agent Engine.

| Diagram | Scope | Highlights |
|---|---|---|
| ![crf fan-out](crf_fanout_system_architecture.png) | System (breadth) | Pub/Sub trigger → **Eventarc** → Orchestrator (`crf_entrypoint`, concurrency=100) reaps stale `PROCESSING` rows (>45 min → re-queue, or `FAILED` after 3 attempts), claims up to `max_rows` oldest `NULL`/orphaned-`QUEUED` trends (`CRF_MAX_ROWS_PER_RUN`, default 3) + marks `QUEUED` → **fans out** one worker message per trend → **Eventarc** → serialized Worker (`agent_worker_entrypoint`, concurrency=1 / max-instances=1 for project-wide Gemini quota) → atomic `QUEUED→PROCESSING` lock (`processing_attempts + 1`) → **Vertex AI Agent Engine** (`creative_agent`, AgentPlatform SDK `client.runtimes.get`) → `PROCESSED`/`FAILED` → BigQuery (`target_trends_crf`, `trend_creatives`, `creative_evals`) + GCS. Redrawn 2026-09-29 for legibility (two bands, large type; the full step detail lives in this row and `cloud_functions/creative_fanout/main.py`) |
| ![crf worker](crf_worker_reliability_deepdive.png) | Worker (depth) | How one worker turns Pub/Sub **at-least-once** delivery into **exactly-once** processing: atomic BigQuery lock (`NULL→QUEUED→PROCESSING→PROCESSED/FAILED`), duplicate-redelivery short-circuit (return + ACK), the `agent_session` create→stream→delete triad (same `user_id`, delete always in `finally`), and the ACK-success / NACK-retry semantics |

## Frontend Diagrams

The Next.js web app (`frontend/`), how it connects to the ADK backend, and how it is served.

| Diagram | Scope | Highlights |
|---|---|---|
| ![frontend arch](frontend_architecture.png) | App architecture + request flow | Next.js 16 App Router client (React 19, Tailwind 4, shadcn/ui) — form `/`, live run view `/run/[sessionId]` that **polls** the async-job `/runs` API, results `/results/[sessionId]` — talks only to same-origin Route Handlers: `/api/adk/[...path]` reverse-proxies REST session CRUD and the async-job `/runs` kick-off/poll/resume endpoints to the backend launcher `deployment/async_app.py` (serving `trend_scout`, `creative_agent`, `interactive_creative`); `/api/gcs` uses **ADC** to proxy Cloud Storage artifacts. Same-origin boundary avoids CORS + Cloud Workstations port-auth |
| ![frontend deploy](frontend_cloudrun_deployment.png) | Serving + Cloud Run deployment | **Current (dev):** one Cloud Workstations VM runs `next dev` (:3000) + the `deployment/async_app.py` launcher under uvicorn (:8000) side by side, bridged by the same-origin proxy. **Target (Cloud Run, implemented):** two Cloud Run services — a containerized Next.js frontend (`trend-trawler-web`) whose `ADK_API_BASE` points at a private backend (`trend-trawler-api`, the `async_app` launcher), reached via a metadata-server ID token (IAM `run.invoker`); `/api/gcs` uses ADC; shared GCS + BigQuery in `us-central1`. The backend runs `--no-cpu-throttling --min-instances 1` so detached runs keep CPU. Runbook in [`deployment/README.md`](../../deployment/README.md#frontend--api_server-on-cloud-run). The frontend is **IAP-gated** (domain-restricted). Includes a cross-reference to the batch fan-out (CRF) diagrams |

### Live Cloud Run Deployment

The as-built two-service Cloud Run deployment (project `<PROJECT_ID>`, `us-central1`),
captured from three angles. Complements `frontend_cloudrun_deployment.png` above (which
contrasts the dev workstation vs. the Cloud Run target) with the detail of the live system.

| Diagram | Scope | Highlights |
|---|---|---|
| ![cloudrun topology](frontend_cloudrun_topology.png) | Deployment topology (what runs where) | **IAP-gated** public `trend-trawler-web` (Next.js standalone, SA `tt-web-sa`) → `roles/run.invoker` → private `trend-trawler-api` (the `deployment/async_app.py` launcher, SA `tt-api-sa`, `--no-cpu-throttling --min-instances 1`); the backend calls **Vertex AI** (Gemini, `global`), **BigQuery** (dataset `trend_trawler`), and **Cloud Storage** (`trend-trawler-deploy-ae`) in-process, all in `us-central1`; sessions persist in a dedicated Agent Engine (`SESSION_SERVICE_URI`) |
| ![cloudrun auth flow](frontend_cloudrun_auth_flow.png) | Request & auth flow | IAP admits the browser to `trend-trawler-web`; same-origin calls → the `/api/adk` proxy mints a metadata-server **ID token** (audience = backend URL) and forwards `Bearer`-authed HTTPS to the private backend, which serves the async-job `/runs` **poll** responses (detached `asyncio` run survives client disconnect); `/api/gcs` uses an **OAuth access token** to stream artifacts; unauthenticated backend calls get `403` |
| ![cloudrun build pipeline](frontend_cloudrun_build_pipeline.png) | Build & deploy pipeline | `gcloud run deploy --source` uploads source (filtered by **`.gcloudignore`** — excludes `.git`/`node_modules`/`tests`/`docs`, keeps `pyproject.toml`+`uv.lock`) → **Cloud Build** builds each image (`frontend/Dockerfile` → Next.js standalone; root `Dockerfile` → `uv` + the `deployment/async_app.py` launcher under uvicorn) → **Artifact Registry** → new 100%-traffic **Cloud Run** revision, with per-service deploy-time env vars |

Runbook: [`deployment/README.md` → Frontend + api_server on Cloud Run](../../deployment/README.md#frontend--api_server-on-cloud-run).

## Bandit creative experiments

| Diagram | Scope | Highlights |
|---|---|---|
| ![bandit experiments architecture](bandit_experiments_architecture.png) | Bandit experiments + scripted behaviour shifts | IAP-gated `trend-trawler-web` (Deploy panel, Behaviour shifts editor, run selector) → `/api/adk` proxy → `trend-trawler-api` `/experiments`, which writes `experiment.json` / `runs/N.json` to Cloud Storage, uploads + deploys the **CPR `BanditPredictor`** (JAX linear Thompson sampling, 1 replica) on Agent Platform, and starts **run N** of the synthetic-traffic **Cloud Run Job** (`SHIFTS_JSON`, `TRAFFIC_RUN`, `FORGET`). The job simulates readers with the scheduled shifts, calls the endpoint `:predict` in batches of 100, replays the baselines and the unshifted **ghost** Linear TS on the same random draws, and writes run-numbered events and episode metrics (`traffic_run`, `shift_response`, `regimes`) to **BigQuery**, which the api aggregates into per-shift responses and the paired shift cost |

Guide: [`docs/bandit/README.md`](../bandit/README.md).

## Regenerating

Diagrams are generated one at a time (respecting the shared 2 RPM
`gemini-3.1-flash-image` cap) via the `paperbanana-figures` skill. To tweak a
label without a full regenerate, use `continue_diagram(run_id=..., feedback=...)`.
