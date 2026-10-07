# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Code Standards

**Always refer to [CODE_STANDARDS.md](./CODE_STANDARDS.md) when writing code or making
environment changes.** It is the authoritative source for packaging (`uv`), linting/
formatting (`ruff`), type checking (`ty`), testing (`pytest`), and commit conventions
(e.g. never add `Co-Authored-By` trailers). See also the `modern-python` skill.

## Project Overview

Trend Trawler is a multi-agent system that automates trend-to-creative ad generation. It identifies culturally relevant Google Search trends, conducts web research, and generates candidate ad copy and visual concepts for a given brand/campaign. Built with Google's ADK (Agent Development Kit), deployed to Vertex AI Agent Engine, and orchestrated via Cloud Run Functions with PubSub triggers.

**Naming:** Agent Engine = *Agent Runtime* (2026 rebrand, Gemini Enterprise Agent Platform). The code uses the AgentPlatform SDK (`agentplatform.Client().runtimes`); the root env gets it from google-cloud-aiplatform 2.x via a uv override (see `pyproject.toml` `[tool.uv]`). (Full note in README.)

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
# TRUST_CLIENT_USER_ID=1 is required locally: the default per-user authz mode is enforce
# (401 without a proxy-asserted X-TT-User), and the app refuses the flag on Cloud Run.
# SESSION_SERVICE_URI=memory:// sidesteps ADK's per-agent local SQLite path check, which
# rejects the agents/ symlinks (pre-existing 400 "resolves outside base directory").
TRUST_CLIENT_USER_ID=1 SESSION_SERVICE_URI=memory:// ALLOW_ORIGINS=http://localhost:3000 uv run uvicorn deployment.async_app:app --port 8000
cd frontend && npm install && npm run dev   # http://localhost:3000

# Deploy agent to Agent Engine (the per-agent packages bundled into the engine
# are derived from AGENT_EXTRA_PACKAGES in deploy_agent.py — a single source of
# truth from the import graph, so cross-package deps like creative_eval/agent_common
# can't be forgotten)
python deployment/deploy_agent.py --version=v1 --agent=trend_scout --create
python deployment/deploy_agent.py --version=v1 --agent=creative_agent --create
python deployment/deploy_agent.py --version=v1 --agent=interactive_creative --create
# add --enable_tracing to opt a new engine into Cloud Trace
# (GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true + ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false)

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
uv run pytest tests/ -q -n 4   # parallel via pytest-xdist (~2x faster; -n auto is SLOWER — each worker re-imports the agents)
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

- Frontend: `frontend/src/__tests__/` — pure logic tests (async-job poll client in `poll-run.test.ts`, P3 proxy identity in `iap-identity.test.ts` (IAP JWT verify, audience lookup, `resolveUser`) + `user-scoping.test.ts` (route allowlist, userId rewrite, encoded-slash/app-name rejection, query allowlist), form validation, reference images (`reference-images.test.ts`), GCS URI building, bandit experiments (`experiments.test.ts`, `chart.test.ts`, `deploy-selection.test.ts`; shifts: `shifts.test.ts`, `scenario-shifts.test.ts` golden vs `bandit`, `shift-insights.test.ts`), widget layouts, trend markdown parsing, extractItems, interactive mode pause/resume, post-render image check `image-check.test.tsx`)
- Python: `tests/` — Pydantic schema validation, agent pipeline structure, tool functions, callbacks (citation regex, state init, rate limiting), async-job run helpers (`test_async_runs.py`), deployment utilities, cloud function logic, bandit experiments (`test_bandit_*.py` core/predictor/traffic, `test_bandit_shifts.py` + `test_experiments_shifts.py` scripted shifts, `test_bandit_endpoint_parity.py` endpoint↔simulator LinTS parity, `test_scenario_preview_golden.py` frontend preview/shift goldens, `test_experiment*_*.py` api/store/metrics, `test_build_image.py`). See [tests/README.md](tests/README.md) for the per-file breakdown.
- ADK Evals: `tests/eval/` — end-to-end agent evaluation using `adk eval` CLI with rubric-based LLM-as-judge scoring (response quality + tool use quality). Runs against real APIs. One evalset + rubric config per agent: `evalsets/trend_scout_evalset.json` + `eval_config.json`; `evalsets/creative_agent_evalset.json` + `creative_eval_config.json`. The `creative_agent` eval must be run with `PYTHONPATH="$PWD"` (see command above).
- Integration: `deployment/integration_test.py` — live GCP checks (health, session lifecycle, smoke tests). Requires deployed agents.
- CI (four workflows, all with `timeout-minutes`; the first three are PR-only plus manual `workflow_dispatch` — no `push: main` re-run, since the PR run already tests the squash-merge result — and path-gated):
  - `.github/workflows/python-ci.yml` — PRs touching `**.py`/`pyproject.toml`/`uv.lock`/`requirements*.txt`/`agents/**`: `uv sync --locked`, requirements.txt-vs-`uv export` drift check, `ruff check`, `ruff format --check`, `ty check`, `pytest tests/ -n 4` (no GCP creds; dummy `GOOGLE_CLOUD_PROJECT`)
  - `.github/workflows/crf-deps.yml` — PRs touching `cloud_functions/**`/`tests/test_crf_*.py`: installs the Cloud Function's own `requirements.txt` into a clean venv and runs `tests/test_crf_*.py` there
  - `.github/workflows/frontend-tests.yml` — PRs touching `frontend/**`: `npm run lint`, `npm test`, `npm run build` (`next build` type-checks everything in tsconfig's include, tests too, so there is no separate `tsc --noEmit`)
  - `.github/workflows/adk-eval.yml` — **not on PRs**: nightly schedule (02:17 PT) + `workflow_dispatch` (`agent`: all|trend_scout|creative_agent); WIF auth (inert until repo var `EVAL_WIF_PROVIDER` is set); runs `adk eval` per agent (serialized) against the isolated `trend_trawler_eval` dataset + eval bucket, then `tests/eval/efficiency_gate.py` — the real pass/fail signal, since `adk eval` exits 0 on failed cases (fails on non-PASSED cases or token/call-count regressions vs `docs/baselines/eval_efficiency.json`; latency warn-only). Setup: deployment/README.md → Eval CI (WIF)

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

**Phase 1 — `trend_scout/`**: Gathers top 25 Google Search trends, researches cultural context via web search, filters to 3 most campaign-relevant trends, saves to BigQuery.

**Phase 2 — `creative_agent/`**: Takes a single trend + campaign metadata, runs parallel web research (campaign researcher + trend researcher as sub-agents), synthesizes a strategic brief, generates ad copy and visual concepts, evaluates all creatives, and exports research PDF, HTML gallery, and evaluation report to GCS. The research PDF and the eval + persistence tail run as deterministic graph steps (inside `combined_research_pipeline` and `finalize_pipeline`), and every stage is chained inside one `creative_pipeline` Workflow, so the root makes a single workflow call after `memorize`.

**Phase 2 (interactive) — `interactive_creative/`**: Same pipeline as `creative_agent`, but pauses at 3 checkpoints for human review via ADK's `LongRunningFunctionTool`: (1) after research report, (2) after ad copies, (3) after visual concepts. Uses `ResumabilityConfig(is_resumable=True)`. It is a thin wrapper that reuses `creative_agent`'s reusable pipelines + visual schema by importing them from `creative_agent`'s **public facade** (`creative_agent/__init__.py`, a curated `__all__` re-export surface) rather than reaching into the volatile `creative_agent.agent`/`.schemas` internals. (The `config` singleton stays on its stable `creative_agent.config` submodule — re-exporting a name `config` from the package would shadow that submodule.)

**Evaluation — `creative_eval/`**: LLM-as-judge module that scores each ad copy and visual concept across 6 dimensions (12 total). Uses `gemini-3.1-pro-preview` (served from the `global` Vertex location) with structured output; each creative is judged by an independent, concurrent call. Scores normalized 0.0–1.0, passing threshold 0.7. Produces `CreativeEvaluationReport` saved as JSON to GCS.

### Agent Composition

```
trend_scout (root Agent `trend_scout`; App + ResumabilityConfig(is_resumable=True); sub-agents via AgentTool)
├── gather_trends_agent (get_daily_gtrends tool)
├── understand_trends_agent_resilient (RetryUntilKeyNode over a searcher → synthesizer Workflow → info_gtrends; bare node → NodeTool)
├── pick_trends_agent (strategic filtering → selected_gtrends)
├── review_trends (LongRunningFunctionTool — opt-in interactive trend pick)
└── Persistence tools (BigQuery, GCS, record_research_gaps, memorize)

creative_agent (root Agent `root_agent`; non-resumable App (carries plugins); tools = creative_pipeline (bare node → NodeTool) + memorize; the root memorizes missing campaign fields, calls creative_pipeline exactly once, then writes the final text)
├── creative_pipeline (Workflow, input_schema=PipelineRequest; the root's ONE workflow call — four separate root calls let the Pro root end Agent Engine runs with empty turns before finalize)
│   START → combined_research_pipeline → ad_creative_barrier → ad_creative_pipeline → visual_production_barrier
│   → visual_production_pipeline → finalize_barrier → finalize_pipeline (terminal: finalize_ready's summary); each
│   *_barrier is a no-output node keeping the previous stage's confirmation out of the next stage's PipelineRequest input.
│   The stages (nested Workflows; also reused separately by interactive_creative):
│   ├── combined_research_pipeline (Workflow, input_schema=PipelineRequest)
│   │   START → (gs_/ca_sequential_planner: each a Workflow planner → RetryUntilKeyNode-wrapped
│   │   searcher+synthesizer Workflow) → research_join (JoinNode) → research_barrier (no output)
│   │   → merge_planners → refinement_gate ("refine" only when base research is degraded:
│   │   evaluator → RetryUntilKeyNode-wrapped refined search; else "skip")
│   │   → combined_report_composer → brief_writer_failsoft (FailSoftNode → RetryUntilKeyNode →
│   │   brief_writer, CreativeBrief → creative_brief) → brief_gate (deterministic brief_check.py;
│   │   "revise" → brief_reviser_failsoft → back to brief_gate, at most BRIEF_REVISION_ROUNDS
│   │   passes; residuals → creative_brief__issues; every exit writes creative_brief_md, the
│   │   compact Markdown the creative prompts read; "ok") → save_research_pdf_node (research PDF
│   │   artifact + GCS → research_report_gcs_uri; skipped without a report; failure →
│   │   research_report_gcs_uri__issues) → research_report_ready (truthy terminal)
│   ├── ad_creative_pipeline (Workflow: drafter (10 copies spread across the brief's angles, self-rated
│   │   typicality) → critic (final 4 cover ≥3 angles; per-copy brief_checks checklist) → copy_gate
│   │   (deterministic copy_gate.py: product named, CTA ≤8 words, headline/caption length, brief avoid
│   │   terms, plus the critic's failed proposition/mandatories checks (other self-reports advisory);
│   │   "revise" → ad_copy_reviser_failsoft (rewrites ONLY flagged copies;
│   │   unflagged edits reverted by restore_unflagged) → back to copy_gate, at most COPY_REVISION_ROUNDS
│   │   passes; deterministic residuals only → ad_copy_critique__issues; "ok") → ad_copies_ready)
│   ├── visual_production_pipeline (Workflow)
│   │   visual_generation_pipeline (Workflow: art_director (resets the concept-fix state) → concept
│   │   drafter/critic/finalizer (each concept: brand_cue from the brief's distinctive assets / brand
│   │   colours, in-image text quoted from the paired copy's headline/CTA, brief avoid + fit_mode;
│   │   finalizer carries angle_id) → concept_gate (deterministic concept_guard.concept_issues:
│   │   cue-preceded quoted text ≠ paired headline/CTA/brand/product (meme/comic style exempt),
│   │   empty trend_motif, >2 headline/CTA text concepts, >1 centred hero; "revise" → visual_concept_fixer_failsoft (rewrites ONLY flagged concepts;
│   │   unflagged edits reverted by restore_unflagged_concepts, then the motif/product/brand_cue
│   │   guard) → back to concept_gate, at most CONCEPT_REVISION_ROUNDS passes; residuals →
│   │   final_visual_concepts__issues; "ok") → visual_concepts_ready) → render_barrier → visual_generator_resilient
│   │   (RetryUntilKeyNode → visual_generator, generate_image: render → image QA vision check
│   │   (creative_agent/image_qa.py) → at most IMAGE_QA_MAX_RERENDERS targeted re-renders, keep the
│   │   attempt with fewer failures, upload only it → generated_images / image_qa__issues)
│   │   → images_ready (truthy terminal)
│   └── finalize_pipeline (Workflow, creative_agent/finalize.py: evaluate_creatives_node (creative_eval
│       judge off the event loop → creative_evaluation_report; none → __retry_exhausted, stale report
│       cleared) → persist_node (fixed order: eval report JSON → HTML gallery → trend_creatives row →
│       creative_evals row (upsert); transient 5xx/429/timeouts retried ×3, then fail-soft →
│       <key>__issues) → finalize_ready (sets finalize_done; truthy summary: scores, URIs, failed steps))
└── memorize

interactive_creative (root Agent `root_agent`; App + ResumabilityConfig(is_resumable=True); reviser via AgentTool)
├── combined_research_pipeline / ad_creative_pipeline / visual_generation_pipeline / finalize_pipeline (reused from creative_agent, incl. their gates; bare nodes → NodeTool)
├── review_research / review_ad_copies / review_visual_concepts (LongRunningFunctionTool checkpoints 1–3)
├── visual_concept_reviser (applies checkpoint-3 revision notes → final_visual_concepts; guarded by ensure_trend_and_product_callback, then recheck_concept_issues_callback re-records final_visual_concepts__issues — no fix loop; a direct-edit resume clears the stale marker)
├── visual_generator_resilient (reused; render after the reviser) → finalize_pipeline
└── save_draft_report_artifact (only to re-save the PDF after a checkpoint-1 report edit) + memorize
```

Key ADK patterns used: `Agent`, graph `Workflow`s (`google.adk.workflow`: fan-out + `JoinNode`, routed function nodes, truthy terminal nodes; exposed to roots as bare nodes → `NodeTool`), `RetryUntilKeyNode` (graph retry wrapper in `agent_common/`), `AgentTool` (wraps agents as tools), `LongRunningFunctionTool` (pause/resume for human-in-the-loop), and `App` + `ResumabilityConfig` (resumable sessions). The ADK Workflow migration (proposal P2, complete 2026-09-29; `docs/plans/2026-09-29-p2-adk-workflow-migration.md`) retired the deprecated `SequentialAgent`/`ParallelAgent`/`LoopAgent` containers and the `RunIfAgent`/`RetryUntilKeyAgent` wrappers; `tests/test_public_api.py` guards against their return (no references in the agent packages, and importing every agent emits no legacy-container `DeprecationWarning`).

### Frontend — `frontend/`

Next.js 16 (App Router) + TypeScript + Tailwind CSS + shadcn/ui. Light "proof room" theme with Archivo (see the design-system note below). Consumes the backend REST endpoints at `localhost:8000` — ADK's canned session/artifact CRUD plus the async-job `/runs` kick-off/poll/resume endpoints (served together by `deployment/async_app.py`).

**Deployment:** the frontend now ships to Cloud Run as two services — `trend-trawler-web` (Next.js standalone) and `trend-trawler-api`. The backend runs the **custom launcher `deployment/async_app.py`** under uvicorn (entrypoint `deployment/backend_entrypoint.sh`): it mounts ADK's canned FastAPI app (session/artifact CRUD, `getSession`, `list-apps`) **plus** the async-job `/runs` router from the flat `runserver/` package — both sharing one `VertexAiSessionService`. It **must** be deployed with `--no-cpu-throttling --min-instances 1` so detached runs keep CPU and aren't killed by scale-to-zero (see the async-run runbook). The backend is private; the same-origin `/api/adk` proxy reaches it with a metadata-server ID token (`roles/run.invoker`). The frontend is **IAP-gated** (domain-restricted to `jordantotten.altostrat.com` via Cloud Run direct IAP), and the backend uses **persistent Agent Engine sessions** via `SESSION_SERVICE_URI` (a dedicated `trend-trawler-sessions` Reasoning Engine). Runbook: [deployment/README.md → Frontend + api_server on Cloud Run](deployment/README.md#frontend--api_server-on-cloud-run).

**Per-user authz (P3 trust model, `docs/plans/2026-09-29-p3-per-user-runs-authz.md`):** the proxy is authoritative; the backend only trusts it.
- **Proxy** (`frontend/src/lib/iap-identity.ts`, `user-scoping.ts`, `app/api/adk/[...path]/route.ts`): verifies the `x-goog-iap-jwt-assertion` (ES256, IAP issuer, audience = `/projects/N/locations/R/services/K_SERVICE` from the metadata server or `IAP_AUDIENCE`; requires `exp`/`iat`/`email`, `hd == IAP_ALLOWED_HD`; the spoofable `x-goog-authenticated-user-*` headers are never read) → normalized email (strip, lowercase, drop `accounts.google.com:`). Only allowlisted UI routes pass (else 404; segments with a decoded `/` or `\` or a non-identifier app name are refused); every path/body `userId` (clients send placeholder `me`) is rewritten to the caller, only `since`/`version` query params forwarded (plus a positive-integer `run` on `GET experiments/{u}/{id}/metrics|creatives`), and `X-TT-User` set. On Cloud Run a missing/invalid JWT or unset `IAP_ALLOWED_HD` → 401; locally (no JWT, no `K_SERVICE`) it passes through unscoped (fail-open by design).
- **Backend** (`runserver/authz.py`, installed in `deployment/async_app.py`): trusts `X-TT-User` only alongside a verified Google ID token for `TRUSTED_PROXY_SA` (`tt-web-sa`; minted with `format=full` so it carries `email`) with `aud ∈ TRUSTED_PROXY_AUDIENCES`. Path/body `userId` ≠ trusted user → 403; missing/untrusted `X-TT-User` on a user-scoped route → 401; blocked canned routes (`/run`, `/run_sse`, `/run_live`, memory, agent-identity) → 404; a foreign session (`VertexAiSessionService` ownership `ValueError`) → 404.
- **Modes:** `USER_AUTHZ_MODE=enforce` (default; refuses to boot without `TRUSTED_PROXY_SA` + `TRUSTED_PROXY_AUDIENCES`) | `observe` (logs `authz observe: would deny …`, blocked routes still 404); `TRUST_CLIENT_USER_ID=1` = trust the client `userId` (local dev only; refused when `K_SERVICE` is set).

**Design system ("proof room", `docs/plans/2026-10-02-frontend-proof-room.md`):** tokens in `src/app/globals.css` (`primary` #0077A8 is the only action colour; `mark-pass`/`mark-fail`/`mark-pending` status marks), Archivo, sentence-case `FieldLabel`; no uppercase eyebrows, glass cards or entrance animations; mono only for code-like data.

**Pages:**
- `/` — Campaign input form (brand, audience, product, selling points, agent tiles: `trend_scout`, `creative_agent`, `interactive_creative`) plus a recent-runs sidebar
- `/runs` — Run history (brand, trend, agent, status, updated) with **Duplicate brief** to prefill a new run from an old one
- `/run/[sessionId]` — Live run view: the page **polls** the async-job run (fire-and-forget kick-off + `GET /runs/.../{session}?since=N`) and shows a stage spine, a current-stage panel, outputs so far, and a collapsed technical log. Because progress is read from the persistent session log (not a browser-held SSE stream), a run **survives disconnect/reload/IAP re-auth** — reloading re-polls from `since=0` and replays; opening an existing run follows it without re-sending the kick-off message. Interactive-mode review checkpoints take over the main area.
- `/results/[sessionId]` — Contact sheet of creatives (image, headline, ad-copy/visual scores, sortable) with a proof-detail dialog per creative, plus artifacts, research PDF, evaluation report and session state

Both the run view and results view also surface the optional visual art-direction inputs (the PR #114 visual-intent keys) read-only in a "Visual Direction" section alongside the campaign metadata — driven by `buildDisplayFields` + `VISUAL_DIRECTION_FIELDS` in `frontend/src/lib/utils.ts`; unset keys collapse to `""` so non-creative/no-intent runs show nothing.

**Key files:**
- `frontend/src/app/layout.tsx` — Root layout, fonts (Archivo + JetBrains Mono), header with active-page nav (`components/main-nav.tsx`)
- `frontend/src/app/page.tsx` — Campaign input form
- `frontend/src/app/run/[sessionId]/page.tsx` — async-job polling (`pollRun`), pipeline widgets, status tracking, stall-timeout
- `frontend/src/app/results/[sessionId]/page.tsx` — Results viewer with artifact tabs
- `frontend/src/lib/api.ts` — API client (session CRUD, async-job `startRun`/`pollRun`/`resumeRun`, artifact fetching)
- `frontend/src/lib/run-history.ts` — session list → run-history rows (status, trend, agent) for `/runs` and the home sidebar
- `frontend/src/lib/run-stages.ts` — per-agent stage lists whose progress is derived from session state, for the run page's stage spine
- `frontend/src/lib/eval-matching.ts` — eval-report types + pairing of each visual concept with its ad copy and both eval verdicts (results contact sheet), plus `imageCheckFor` (the post-render image check from `state.generated_images`, shown as the proof detail's "Image check" row)
- `frontend/src/lib/agents.ts` — agent catalog (labels, descriptions, durations, review pauses)
- `frontend/src/app/api/gcs/route.ts` — Authenticated GCS proxy for serving artifacts

### Bandit experiments — `bandit/`, `bandit_serving/`, `bandit_traffic/`

Optional post-run step (guide: [docs/bandit/README.md](docs/bandit/README.md)): the results-page Deploy panel turns 2–4 creatives into the arms of a contextual bandit for a synthetic publisher page about the trend. `bandit/` is the JAX core (linear Thompson sampling, `ctx-v1` features d=19, synthetic scenarios `clear_winner`/`segment_winners`/`drift`, simulator + baselines, `python -m bandit.cli simulate`); `bandit_serving/predictor.py` is the CPR `BanditPredictor` (typed `decision`/`reward`/`reset`/`state` instances over `:predict`, posterior checkpointed to GCS); `bandit_traffic/` is the synthetic-traffic Cloud Run Job (CRN baseline replay → BigQuery `bandit_events`/`bandit_episode_metrics`); `runserver/experiments*.py` is the user-scoped `/experiments` API (status machine, Vertex deploy via `deployment/bandit/endpoint.py`, Cloud Run Job launch, TTL reaper every 5 min, metrics aggregation); the frontend adds `/experiments` + `/experiments/[experimentId]` with hand-drawn SVG charts.
- **Single worker, single replica:** the posterior is in-memory, so the CPR model is always deployed with `VERTEX_CPR_WEB_CONCURRENCY=1` on exactly one replica (not HA; pending decisions are lost on restart). One active experiment per user.
- **JAX is dev-group only:** never add `jax` to the root `[project] dependencies` or `requirements.txt` (the api image and Agent Engine bundles must stay JAX-free); it lives in the uv dev group plus `bandit_serving/requirements.txt` / `bandit_traffic/requirements.txt` (same pin). `runserver/` never imports `bandit/` (it duplicates the noise-var formula, parity-tested).
- **Contracts:** [docs/bandit/contracts.md](docs/bandit/contracts.md) is the source of truth for the core API, endpoint instances, BigQuery tables, context object and REST API; change it in the same PR as any interface change.
- **Scripted behaviour shifts (contracts §10):** a `POST …/traffic` carries ≤ 4 `shifts` (`promote` / `demote` / `mix` / `shock`; `creativeId: "leader"` only for demote/shock, resolved at the shift's round) + `forget`; shifts belong to the **traffic run**, not `experiment.json`, and change the ground truth the endpoint **and** every baseline see (job env `SHIFTS_JSON` / `TRAFFIC_RUN` / `FORGET`). Runs are numbered: rows carry `traffic_run` (NULL = run 1), `bandit_experiments.traffic_runs` is the JSON run list, `/metrics` + `/creatives` take `?run=N` (default latest; also the only extra query param the proxy forwards). With shifts the job also replays a **ghost** `linear_ts_unshifted` (local LinTS on the unshifted env, same episode key); it is identical to the endpoint before the first shift because the endpoint keys its LinTS with the simulator's policy stream (reset `policy_key`/`batch_size` + decision `batch`/`row`) — exact round-for-round parity, `tests/test_bandit_endpoint_parity.py`. **Forgetting:** `forget` (default on with shifts) sends a reset `discount` = `bandit.config.default_shift_discount` (horizon/8 memory), clamped to the floor 0.95 (`RESET_DISCOUNT_BOUNDS`). `/metrics` adds `shiftResponse`, `regimes` and `shiftCost` (**paired** per-episode ghost − endpoint clicks/reward, 95% t-interval).
- **Migration order (shifts):** add the `bandit_events`/`bandit_episode_metrics` columns (`traffic_run`, `shift_response`, `regimes`) **before** the traffic image that writes them; `bandit_experiments.traffic_runs` **after** the api (deployment/README.md).
- **Synthetic readers (simulated users):** defined by `bandit/features.py` `CONTEXT_SPEC` (10 `ctx-v1` attributes) + `bandit/config.py` `BASE_MARGINALS` + per-scenario `segments:` in `bandit/scenarios/*.yaml` (name, weight, marginals, `dwell_factor`, `winner_key`); generated per batch by `bandit/environment.sample_contexts` (segment first, then attributes; CRN-keyed draws). The UI controls only scenario, click rates, reward, audience mix (`scenario_overrides`) and mid-run shifts; segment definitions/attributes need YAML (+ the api's parity-tested `SCENARIO_SEGMENT_NAMES` and the regenerated `scenario-presets.generated.json`). Full write-up: docs/bandit/README.md → Synthetic readers.
- **Continuous learning (contracts §11):** a traffic run's `learning: "continuous"` (UI "Keep learning"; job env `LEARNING_MODE`) resets the endpoint **once** and keeps its posterior across the run: one episode of E × T rounds cut into E segments (rows per segment, global `round`, shifts/drift/forgetting over the whole run), capped at `episodes × horizon ≤ 2,000,000` with `horizon % batch_size == 0`. `/metrics` stitches the segments into one band-free timeline (global checkpoints, `lo = hi = mean`, `segmentStarts` ticks) and adds `continuousSummary` (endpoint − best baseline per-segment clicks, batch means over the post-warm-up half, `status` ok/too_few_segments/autocorrelated/still_trending); the page's headline reads only that, never the curves.
- **Local dev:** `BANDIT_DEPLOY_MODE=fake` (in-memory store, fake deployer + jobs; no metrics) on the api; `python -m bandit_traffic.main --in-process --dry-run` for the traffic loop without GCP.

### Event-Driven Orchestration — `cloud_functions/`

Fan-out pattern using two Cloud Run Function deployments from the same source (`cloud_functions/creative_fanout/`):
- **Orchestrator** (`crf_entrypoint`): Triggered by `CREATIVE_TOPIC_NAME` PubSub topic, queries BigQuery for unprocessed trends (oldest first, capped at `CRF_MAX_ROWS_PER_RUN`, default 3; a message's optional `max_rows` can only lower it), dispatches one message per trend to worker topic. Concurrency=100.
- **Worker** (`agent_worker_entrypoint`): Triggered by `CREATIVE_WORKER_TOPIC_NAME`, processes a single trend row by invoking Agent Engine. Concurrency=1 (prevents duplicate processing), max-instances=1 (serializes runs under the project-wide pro/image quotas). Timeout=1800s.

### Configuration

Shared building blocks live in **`agent_common/`** (a lightweight package bundled into every deployed engine; it depends on `google-adk` but is deliberately free of any per-agent business logic, and no cloud function imports it):
- `agent_common/config.py` — `BaseAgentConfiguration`, the single source of truth for the model names, rate-limit knobs, and GCP/BigQuery env vars. Each agent's `config.py` subclasses it (`ResearchConfiguration(BaseAgentConfiguration)`) and adds only its genuine differences (e.g. `trend_scout`'s `SetupConfiguration`), which is why the two agent configs no longer drift.
- `agent_common/retry.py` — `build_infra_retry(extra_exceptions=(), max_attempts=3)`, the one place the ADK `RetryConfig` transient-exception list is defined (`creative_agent` passes the genai `ServerError`).
- `agent_common/retry_node.py` — `RetryUntilKeyNode`, the retry-on-empty graph wrapper (a `BaseNode` that re-runs a flaky `google_search`+thinking producer child — usually a searcher → synthesizer `Workflow` — until its `output_key` is populated, bounded; on exhaustion leaves the key unset, records `<key>__retry_exhausted`, and still yields a truthy notice so a `NodeTool` caller never stalls; a later success clears a stale marker to `None`, which every reader treats as clean), plus `is_populated(value)`, the shared populated-check (also used by `creative_agent`'s truthy terminal nodes). Shared here so every agent wraps producers without cross-importing another agent's package.
- `agent_common/fail_soft_node.py` — `FailSoftNode`, the fail-soft wrapper for *optional* graph steps (RetryUntilKeyNode retries only empty output; an exception propagates and fails the Workflow): runs the child once and turns an exception (unwrapped from ADK's `DynamicNodeFailError`) into a logged error + `on_error(state_before, exc)` state delta, then yields a truthy notice so successors run. Used around `creative_agent`'s brief writer (→ `creative_brief__retry_exhausted`) and reviser (keeps the pre-revision brief, → `creative_brief__issues`).
- `agent_common/schemas.py` — `PipelineRequest`, the `input_schema` (`request: str`) that gives a graph `Workflow`/node exposed as a `NodeTool` the same model-facing declaration `AgentTool` had.
- `agent_common/genai_retry.py` — `build_genai_http_retry()`, the status-code-based genai HTTP retry (429/500/503/504 with backoff; permanent 4xx fail fast), wired into `build_gemini()` and the `creative_eval` judge client; ADK-free. Also the single source of truth for the **per-request model timeout** `MODEL_REQUEST_TIMEOUT_SECONDS` (env, default 240, clamped 30–900, `0` disables; read at import, so for Agent Engine the deployer's env is pickled in) — `model_request_timeout_ms()` gives genai's `HttpOptions.timeout` (MILLISECONDS), applied to every agent model (via `build_gemini`), the judge client and the image-gen client. Sync httpx clients (judge, image gen): `httpx` timeouts are retried by genai `retry_options` (judge) / `_generate_image_with_backoff` (image).
- `agent_common/rate_limit.py` — `build_rate_limit_callback(config)`, the shared `before_model_callback` enforcing each agent's `rpm_quota`.
- `agent_common/sanitize.py` — `scrub_lone_surrogates` / `scrub_surrogates_in_response` (`after_model_callback`), which strip lone Unicode surrogates from model JSON before `output_schema` validation.
- `agent_common/history.py` — `drop_other_agent_context`, the `before_model_callback` (ahead of the rate limiter) on the `creative_agent` + `interactive_creative` roots. ADK 2.10 replays every session event onto a root (branch `None` matches every branch), so each earlier NodeTool pipeline's sub-agent turns (user-role `OTHER_AGENT_CONTEXT_PREAMBLE` "For context: … [agent] said:" contents) and node inputs (user-authored events on the node's branch) piled up in the root's prompt (~33.5k tokens before the empty turns of 2026-10-07); only the latest call's interior is hidden by ADK, and NodeTool has no isolation option. It drops exactly those, keeping user messages, the root's turns and all function call/response pairs (incl. checkpoint responses). Matches ADK's private `_fencing` preamble constant; `tests/test_root_history.py` runs a real root through two pipelines, so an ADK change fails loudly.
- `agent_common/state.py` — the shared `memorize` ADK tool (re-exported from each agent's `tools.py`; the tool name must stay `memorize`) and `seed_initial_state(...)`, the one-time session-state seeding behind each agent's `callbacks._set_initial_states` (per-agent output dir / extra keys / `setdefault` defaults stay local).
- `agent_common/clients.py` — `get_gcs_client()` / `get_bigquery_client()`, the shared lazy client getters (SDK imports inside the functions). Agent modules bind them to their `_get_gcs_client` / `_get_bigquery_client` names (the test monkeypatch points); `creative_agent.gcs_tools` wraps its GCS getter in `functools.cache`.
- `agent_common/idempotency.py` — `stable_row_id(*parts, length=8)`, a deterministic (`json.dumps`-framed sha256) row key. The BigQuery write tools derive their row keys from `tool_context.session.id` with it (never uuid4) and write via `MERGE … WHEN NOT MATCHED THEN INSERT`, so at-least-once tool execution (resumed apps, CRF retries) leaves exactly one logical row (the `creative_evals` row also `WHEN MATCHED THEN UPDATE`s, so a re-run's row matches its re-written GCS report JSON).
- `agent_common/safety.py` — `build_safety_plugins(root_agent_names)`, the opt-in Model Armor plugin list every agent's `App(plugins=...)` is wired with (`[]` unless `MODEL_ARMOR_TEMPLATE` is set; `MODEL_ARMOR_RESPONSE_TEMPLATE` override; fail-closed unless `MODEL_ARMOR_FAIL_CLOSED=false`). `ScopedModelArmorPlugin` screens only the root orchestrator's turns — `AgentTool`/`NodeTool` propagate App plugins into every sub-agent run. Read at agent-module import, so for Agent Engine it's the *deployer's* env that is baked into the pickled App. `creative_agent/__init__.py` re-exports `app` so ADK's canned loader serves the App, not the bare `root_agent`.
- `agent_common/locations.py` + `agent_common/models.py` — `MODEL_LOCATION` (default `global`) and `build_gemini(name)`, which pin every gemini-3.x call's serving location in code (Agent Engine *reserves* `GOOGLE_CLOUD_LOCATION`, so it can't be forced via deploy env vars). `build_gemini` returns a `TimeoutRetryingGemini` (a `Gemini` subclass): it sets the timeout per request on `llm_request.config.http_options.timeout` (not via `client_kwargs`, which would clobber ADK's headers/`retry_options`) and retries a timed-out request up to `TIMEOUT_RETRY_ATTEMPTS` (3 total) before any output is yielded — needed because ADK's async genai client uses aiohttp, whose timeout surfaces as builtin `TimeoutError`, which genai's `HttpRetryOptions` does not retry. An exhausted `TimeoutError` carries no HTTP status, so `FallbackModel` does **not** fail over on it (429/5xx only); it propagates (node `RetryConfig` lists `TimeoutError` where set). The three root orchestrators also pass `empty_turn_retries=ROOT_EMPTY_TURN_RETRIES` (2): a clean empty turn (`STOP`, no text/function call — a Pro root sometimes answers a long NodeTool result that way, and ADK then ends the invocation) is re-asked inside the model call, so it is covered under `adk eval` and Agent Engine too, not only by the runserver auto-continue (which stays the backstop). Non-streaming only; sub-agents don't opt in.
- `agent_common/observability.py` — the shared debugging callbacks used by every agent: `log_run_start` (run→session correlation line), `log_empty_turn_finish_reason` (`after_model_callback` that warns only on empty/abnormal producer turns), `make_final_state_summary(label, keys)` (factory → `after_agent_callback` logging load-bearing state keys + `*__retry_exhausted` markers), and `collect_degradation_warnings(state)` — the single source of truth that turns the generic `<key>__retry_exhausted` and `<key>__issues` (list/str of residual quality issues → "<Label> has unresolved issues: n (e.g. …)", capped) markers into the notes surfaced on the eval report (`warnings`), the `creative_evals.research_gaps` BQ column, and the HTML gallery "Run notes" banner (HTML-escaped). Snapshots `state.to_dict()` before scanning (an ADK `State` isn't directly iterable).

The bucket name comes from `GOOGLE_CLOUD_STORAGE_BUCKET` (the var deploy actually ships) — not the local-only `GCS_BUCKET_NAME`; the `gs://` form (`GCS_BUCKET`) is derived from it (there is no `BUCKET` env var). Key settings:
- **Models**: `gemini-3.8-flash` (worker), `gemini-3.1-pro-preview` (critic + `creative_eval` judge — no GA Pro yet), `gemini-3.5-flash-lite` (lite planner), `gemini-3.5-flash` (creative_agent campaign research, see below; `trend_scout` `picker_model`), `gemini-3.1-flash-lite` (`trend_scout` `gather_model`), `gemini-nano-banana-2.1` (image gen; was `gemini-3.1-flash-image` until 2026-10). The 2026-09 refresh retired every gemini-2.5 agent model (Vertex shuts 2.5 down Oct 2026–Mar 2027); `trend_scout` still fans its 5 agents across 5 distinct base-model buckets, now all @ `global`.
- **Pro-producer failover**: the six `critic_model` producers (`creative_agent` root / `visual_generator` / `combined_report_composer` / `combined_web_evaluator`, `interactive_creative` root, `trend_scout` root) fail over to `worker_model` (`gemini-3.8-flash`, never `ALT_GLOBAL_MODEL`) on 429/5xx via ADK `FallbackModel` (`agent_common.models.build_gemini_with_fallback`; both delegates global-pinned `build_gemini` instances, primary HTTP retry attempts=2 so failover fires in ~10s). Target is `BaseAgentConfiguration.critic_fallback_model` (`CRITIC_FALLBACK_MODEL`); kill switch `CRITIC_FALLBACK_MODEL=""` returns the plain Pro model — on Cloud Run it is read at service start, but on Agent Engine the agent is pickled at deploy time, so the deployer's local env decides (redeploy with `CRITIC_FALLBACK_MODEL=""` to disable there). The `creative_eval` judge and image gen have no fallback (direct genai calls; a silent judge swap would skew pass rates) — the eval report records `judge_model` instead.
- **Model location**: gemini-3.x models are only served from the `global` Vertex location — set `GOOGLE_CLOUD_LOCATION=global`. Regional resources (BigQuery, GCS, PubSub, Agent Engine) stay in `us-central1`.
  - **Agent Engine region (`GCP_REGION`):** Agent Engine / Reasoning Engine is a *regional* resource, so its Vertex AI SDK clients read `GCP_REGION` (default `us-central1`), decoupled from `GOOGLE_CLOUD_LOCATION=global`. Wired through `deployment/deploy_agent.py`, `deployment/test_deployment.py`, `deployment/integration_test.py`, and the `cloud_functions/*/config.py` constants (`config.GCP_REGION`). The `global` model location is used only by the genai model clients (`creative_agent/image_tools.py`, `creative_eval/evaluate.py` — the eval judge defaults to `MODEL_LOCATION`, overridable via `EVAL_MODEL_LOCATION`) plus the ADK agents' `build_gemini()` models; BigQuery and GCS clients take no location.
- **Rate limiting**: `before_model_callback` enforces rpm_quota (1000) over 60s intervals
- **Cloud Trace (opt-in)**: api `ADK_OTEL_TO_CLOUD=true` → `get_fast_api_app(otel_to_cloud=True)` (`runserver/otel.py`); pair with `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false` (ADK defaults it on). Engines: `deploy_agent.py --enable_tracing`. IAM/runbook: deployment/README.md §9.
- **Campaign-research placement (`CAMPAIGN_RESEARCH_PLACEMENT`)**: selects which model bucket the `campaign_researcher` sub-agent runs on, via `ResearchConfiguration.campaign_models()` in `creative_agent/config.py`. Default `global_altbucket` runs campaign research on `ALT_GLOBAL_MODEL` = `gemini-3.5-flash` @ `global` — a different per-base-model quota bucket from the trend half's `gemini-3.8-flash`/`gemini-3.5-flash-lite` (the PR #101 spread that halves contention in the research fan-out). Alternate arm `global_3x` shares the trend buckets (comparison baseline); unknown values (incl. the retired `regional_25` gemini-2.5 arm) fall back to the default — see `experiments/quota_spread/` and [experiments/README.md](experiments/README.md). Leave unset for production behavior.
- **Post-render image QA (on by default)**: `image_tools.generate_image` inspects every render with one structured vision call (`creative_agent/image_qa.py` `inspect_image` → `ImageQAResult`, model `IMAGE_QA_MODEL`, default the worker model, temperature 0, global client with HTTP retry + the shared timeout). The pure rule `qa_failures` fails an image on: product or trend motif not visible, gibberish text, an unrequested third-party logo (only the campaign brand's logo may appear — the image model was seen adding a swoosh), severe artifacts, unsafe content, or requested in-image text (`expected_text` = `concept_guard.in_image_quotes`) not exact/legible; a missing `brand_cue` is advisory. A failing image is re-rendered at most `IMAGE_QA_MAX_RERENDERS` times (default 1, clamped 0–2) with "Correct these issues from the previous attempt: …" appended (same references / aspect ratio, through the quota-paced backoff); the attempt with fewer failed rules is kept (ties → latest) and only it is uploaded. `IMAGE_QA_ENABLED=false` is the kill switch (single render, no QA calls). All three knobs live on `BaseAgentConfiguration` and ship via `deploy_agent.py` `ENV_VAR_DICT` (read at import: Agent Engine bakes in the deployer's env). Fail-open: a QA error keeps the render (`qa: None`). Results: `state["generated_images"] = {concept_name: {gcs_uri, artifact_key, attempts, qa}}` (`qa` = verdict fields + `passed` + `failures`, None when disabled/unavailable; `_generated_artifact_keys` unchanged); still-failing images and "QA unavailable for <concept>" go to `image_qa__issues` (→ `collect_degradation_warnings`); the HTML gallery shows a per-image "Image check" line and the results proof detail an "Image check" row.
- **Session state keys**: `brand`, `target_product`, `target_audience`, `key_selling_points`, `target_search_trends` — seeded deterministically via `createSession` initialState (frontend, CRF worker, `test_deployment.py`) and only `setdefault`ed by `creative_agent/callbacks.py`; the root `memorize`s just the ones missing from state (the kickoff message is a readable echo)
- **Optional visual-intent keys** (`creative_agent`/`interactive_creative`, all default `""`, seeded via `createSession` initialState → `setdefault` in `creative_agent/callbacks.py`, never via the user message): `visual_intent` (free-text art direction → `{visual_intent?}` in art_director + drafter), `brand_colors` (→ `{brand_colors?}` in both), `visual_style_preference` (preferred STYLE_PALETTE family → `{visual_style_preference?}` in drafter, seed-with-diversity), `visual_avoid` (→ `{visual_avoid?}` in art_director, reframed positively), `visual_aspect_ratio` (deterministic per-render override read in `image_tools.generate_image`; empty = keep per-concept diversity; allowed set in `agent_common/config.py`), `reference_image_role` (`product`|`logo`|`style`; legacy single reference with `reference_image_uri`), `reference_images` (default `[]`: up to 3 `{uri, role}` references, gs:// or http(s); `creative_agent/references.py` `resolve_references` folds the legacy pair in first (empty role → product; list entries must name a valid role, else skipped with a warning), dedupes by uri, caps at `MAX_REFERENCE_IMAGES`; `image_tools.generate_image` fetches them concurrently (http(s) hardened: public hosts only, ≤3 re-validated redirects, `image/*`, 10 MB cap; gs:// also capped) and sends `[prompt + numbered "Reference image N (role)" block (+ "The <role> reference image is unavailable…" for a failed fetch) + ignore-reference-text line, part1, part2, …]` — only the tool numbers references, prompts refer to them by role; product = reproduce exactly, logo = small/legible/undistorted, style references guide palette/texture/lighting only, not content, and never override the 4-distinct-families rule), `reference_roles` (derived at state init from the resolved references, e.g. `product, style` → `{reference_roles?}` in the drafter, critic and finalizer). Interactive mode also writes `visual_revision_notes` on resume (from checkpoint-3 edits — see `runserver/async_runs.merge_visual_concept_edits`), consumed by the `visual_concept_reviser` before rendering.

### Agent Definition Pattern

Agents use `before_agent_callback` to initialize session state, `before_model_callback` for rate limiting, and `output_key` to store results in state for downstream agents. Instructions use context variables like `{brand}`, `{target_product}`, `{target_audience}`.

Agent `instruction=` strings live in the package's `prompts.py` (as `<AGENT_VAR>_INSTR` constants), not inline in `agent.py`.

Agent `output_schema=` Pydantic models live in the package's `schemas.py` (re-imported into `agent.py`, which keeps them importable from the agent module), not inline in `agent.py`.

Image-generation prompt guidance lives in `creative_agent/prompts.py` as `IMAGE_PROMPT_GUIDE` (a style-first prompting grammar for still ad images); visual agents must select a `visual_style` per concept rather than defaulting to photorealism. It is spliced into the drafter/critic instructions by string concatenation and must contain no `{...}` braces (ADK would treat them as state tokens — fill-in slots use `[square brackets]`). Image diversity (`docs/plans/2026-10-06-image-diversity.md`): each session gets a random stratified `style_shortlist` of 6 style families (`creative_agent/style_shortlist.py`, seeded once in `callbacks._set_initial_states`, read via `{style_shortlist?}` by the drafter/critic/finalizer, which pick 4 distinct families from it); the guide's palette entries are descriptors (when-to-use + cues), not fill-in templates to copy; in-image text is capped at 2 of 4 concepts (short headline/CTA; meme captions and comic speech bubbles are exempt); and an across-set composition rule allows at most one centred hero with varied camera distance. Each concept also carries a `trend_motif` and a `brand_cue` (a brand distinctive asset from the brief or `{brand_colors?}`; shared brace-free rules `VISUAL_CONCEPT_RULES` also require in-image text to be quoted exactly from the paired copy's headline/CTA and the brief's avoid list / fit_mode to be respected), and `callbacks.ensure_trend_and_product_callback` (`after_agent_callback` on `visual_concept_finalizer`, `visual_concept_fixer` and interactive's `visual_concept_reviser`, pure logic in `creative_agent/concept_guard.py`) appends the motif, `{target_product}` and/or the `brand_cue` to any final `image_generation_prompt` missing them, logging a warning. "Missing" (motif, product and brand cue alike) is token overlap, not a substring (`creative_agent/text_match.mentions`: the full phrase, or ≥60% of its content tokens, plural/accent-insensitive; size tokens and a size's container ("16oz can") ignored, a packaging head noun ("iPhone case") required; a single-token phrase needs its full core ("Pixel 9"); brand-anchored — a partial product/brand-cue match must include a brand token, unlike the copy gate where naming the brand counts); intangible products (subscription/app/service/insurance…) get a softer branded-screen/logo cue, skipped when the brand is mentioned. `concept_gate` then runs `concept_guard.concept_issues` (deliberately conservative string heuristics — only a double-quoted span with a text cue ≤6 words before it counts as in-image text, idioms like "sign of", "title of", "poster style", "vibe reads" excluded; only copy-matching text concepts count toward the cap (mismatches get their own issue); meme/comic concepts are exempt (style contains "meme" or a meme/comic family, or the prompt says meme caption/speech bubble|balloon/thought bubble/comic panel/top|bottom text/Impact font); a bare "centred" needs a subject/composition word, and the centred-hero check skips "off-centre", "centred between", "camera centred on", "… third" placements and sentences about text/logos) and routes flagged concepts to one bounded `visual_concept_fixer` round (`CONCEPT_REVISION_ROUNDS`, default 1, clamped 0–2).

### Data Flow

- **BigQuery**: Stores trend recommendations (`target_trends_crf`), creative results (`trend_creatives`), per-run evaluation summaries (`creative_evals` — one row per run, joins `trend_creatives` via `creative_uuid`, links to the full report JSON in GCS)
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
- `creative_agent/finalize.py` — `finalize_pipeline` nodes: `evaluate_creatives_node` (judge in a thread on a state snapshot), `persist_node` (eval JSON + gallery to GCS, both BigQuery rows; bounded transient retry, then fail-soft `<key>__issues`), `finalize_ready` (summary + `finalize_done` completion marker)
- `creative_eval/evaluate.py` — Core LLM-as-judge evaluation logic
- `creative_eval/schemas.py` — Pydantic models for evaluation reports
- `tests/eval/eval_config.json` — ADK eval criteria config (rubric-based scoring)
- `tests/eval/evalsets/` — ADK eval cases per agent
- `deployment/deploy_agent.py` — Agent Engine deploy/list/delete CLI; `AGENT_EXTRA_PACKAGES`/`AGENT_DEPLOY_SPECS` maps are the single source of truth for what each agent bundles
- `deployment/test_deployment.py` — Invoke deployed agents for testing
- `runserver/async_runs.py` — async-job run model: `/runs` FastAPI router + pure helpers. Kicks off a **detached `asyncio` task** driving `Runner.run_async` to completion decoupled from the HTTP request, appends a terminal `__run_status` marker event on done/error, and serves poll (`GET ?since=N`) + resume endpoints. Replaces browser-held SSE so runs survive client disconnect. **Auto-continue:** if a segment ends cleanly on an empty root turn (no text/function call) with the app's completion key unset and no unanswered long-running checkpoint call (`should_auto_continue`), it re-prompts the same session with `AUTO_CONTINUE_MESSAGE` before writing `done`. This happens inside the same task and `RUN_MAX_SECONDS` budget, records `__auto_continues` in state, and is capped per segment by `RUN_MAX_AUTO_CONTINUES` (default 2, clamped 0–3). Creative apps count as finished only at `finalize_done` (set by `finalize_pipeline`'s terminal node on every path, even with no eval report or a failed eval BQ write), not at `eval_report_gcs_uri` / `eval_bq_row_uuid`.
- `runserver/authz.py` — P3 per-user authz: `resolve_mode` (`TRUST_CLIENT_USER_ID` / `USER_AUTHZ_MODE`), `normalize_user_id`, `verify_proxy_caller` (proxy-SA ID-token check), `UserAuthzMiddleware` (401/403/404 per `decide`), `authorize_body_user` (kick-off body), and the ownership-`ValueError` → 404 handler
- `deployment/async_app.py` — launcher that mounts the `/runs` router on ADK's canned FastAPI app, sharing one `VertexAiSessionService`; run under uvicorn by `deployment/backend_entrypoint.sh`
- `cloud_functions/creative_fanout/main.py` — Orchestrator and worker entry points
- `cloud_functions/creative_fanout/session.py` — `agent_session` async context manager (create→query→delete under one `user_id`, delete-on-error)
- `bandit/linear_ts.py` / `bandit/features.py` / `bandit/config.py` — JAX LinTS core, `ctx-v1` context encoding (privacy key rejection), `experiment.json` config + calibrated noise variance
- `bandit/simulate.py` / `bandit/cli.py` — offline simulator + CLI (`experiments/bandit/notebook_parity.py` regenerates the parity figures)
- `bandit_serving/predictor.py` — CPR `BanditPredictor` (contracts §2/§7)
- `bandit_traffic/main.py` / `bandit_traffic/traffic.py` — synthetic-traffic Cloud Run Job entrypoint + episode loop
- `runserver/experiments.py` — `/experiments` REST API, status machine, TTL reaper, `BANDIT_DEPLOY_MODE`; helpers in `experiments_store.py` / `experiments_deploy.py` / `experiments_jobs.py` / `experiments_metrics.py`
- `deployment/bandit/build_image.py` / `deployment/bandit/endpoint.py` — CPR image build/local-test/push; model upload + endpoint lifecycle
- `docs/bandit/contracts.md` — bandit interface contracts (source of truth)
- `bandit/environment.py` — synthetic env; `build_true_model(..., shifts=)` / `resolved_shifts` (shift resolution, §10)
- `frontend/src/lib/shifts.ts` / `app/experiments/[experimentId]/shift-timeline.tsx` / `run-selector.tsx` / `shift-results.tsx` — shift editor model + readers, the editor, `?run=N` picker, per-shift result cards (`applyShifts` in `lib/scenario-preview.ts` ports the shift resolution for the preview)

## Requirements

- Python >=3.13
- `google-adk[eval,otel-gcp]>=2.10.0,<3.0.0` (`otel-gcp` adds the GenAI SDK instrumentor, so `generate_content` spans appear when tracing is on; content capture stays off by default)
- google-cloud-aiplatform 2.x (via `[tool.uv] override-dependencies` — google-adk[eval] caps <2)
- Node.js >=22.13 (for frontend)
- GCP project with BigQuery, Cloud Storage, PubSub, and Agent Engine enabled
- `.env` file populated from `.env.example`
