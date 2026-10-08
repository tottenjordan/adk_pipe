# GEMINI.md — Trend Trawler (`adk-pipe`)

This file provides persistent, project-wide context and instructions to Gemini agents working in this repository.

## 1. Identity & Role

You are an expert AI software engineering partner working on **Trend Trawler** (`adk-pipe`), a multi-agent system that automates trend-to-creative ad generation and post-run contextual-bandit experimentation.
- **Core workflow:** Identifies culturally relevant Google Search trends (`trend_scout`), conducts parallel web research, synthesizes a strategic brief, drafts/critiques ad copy and visual concepts, renders ad images (`creative_agent` and human-in-the-loop `interactive_creative`), scores creatives with a 12-dimension LLM judge (`creative_eval`), and optionally deploys top creatives as arms of a live JAX contextual bandit (`bandit*`).
- **Platform stack:** Built with Google's Agent Development Kit (ADK 2.x), deployed to Vertex AI Agent Engine (*Agent Runtime* in the 2026 Gemini Enterprise Agent Platform rebrand, using `agentplatform.Client().runtimes`), orchestrated headless via Cloud Run Functions + Pub/Sub or interactively via a Next.js 16 web UI + FastAPI async-job backend on Cloud Run.

---

## 2. Tech Stack & Tooling Constraints

**Always follow [CODE_STANDARDS.md](./CODE_STANDARDS.md) when writing code or modifying the environment.**

@./CODE_STANDARDS.md

- **Python & Packaging:** Python `>=3.13,<4.0`. Manage all packages, virtual environments, and command execution via **`uv`** (`uv sync`, `uv add`, `uv remove`, `uv run`).
- **Linting & Formatting:** **`ruff`** (`uv run ruff format .`, `uv run ruff check .`). Rules are pinned in `pyproject.toml` to `["E", "F", "I", "UP", "B"]` with `E501` ignored (`ruff format` owns line length 88).
- **Type Checking:** **`ty`** (`uv run ty check`). Configured in `pyproject.toml` `[tool.ty]` (`tests/` is excluded). Code must pass `ty check` before a task is considered complete.
- **Testing:** **`pytest`** (`uv run pytest tests/ -q -n 4`) for Python unit/integration tests; **Vitest + React Testing Library** (`cd frontend && npm test`) for frontend tests; **`adk eval`** for LLM-as-judge agent evaluations.
- **Core SDKs:**
  - `google-adk[eval,otel-gcp]>=2.10.0,<3.0.0` (`otel-gcp` adds the GenAI SDK instrumentor for Cloud Trace spans; message content capture stays off by default).
  - `google-genai>=2.25.0,<3.0.0`
  - `google-cloud-aiplatform[agent-engines,evaluation]>=2.2.0,<3.0.0` (enforced via `[tool.uv] override-dependencies` in `pyproject.toml`).
- **Frontend:** Next.js 16 (App Router), TypeScript, Tailwind CSS, shadcn/ui (Node.js `>=22.13`).
- **Bandit Engine:** JAX (`jax[cpu]>=0.11.2`) lives **only** in the `dev` dependency group (`[dependency-groups]`) and in `bandit_serving/requirements.txt` / `bandit_traffic/requirements.txt`.

---

## 3. Commands

### Environment & Local Development

```bash
# Install dependencies
uv sync

# Local development (ADK web UI)
uv run adk web .
# Direct agent URL pattern in ADK web UI (skips dropdown):
# http://127.0.0.1:8000/dev-ui/?app=trend_scout (or creative_agent / interactive_creative)

# Validate an agent directly in the CLI
uv run adk run trend_scout

# Local development (custom Next.js frontend + async-job backend)
# IMPORTANT: Run `deployment.async_app:app`, NOT bare `adk api_server`.
# The frontend's run page polls `/runs` async-job endpoints, which only `async_app` mounts
# alongside the canned ADK CRUD/getSession/artifact endpoints.
# - TRUST_CLIENT_USER_ID=1 is required locally (default authz mode is enforce, which returns
#   401 without a proxy-asserted X-TT-User; refused on Cloud Run when K_SERVICE is set).
# - SESSION_SERVICE_URI=memory:// sidesteps ADK's per-agent local SQLite path check, which
#   rejects the `agents/` symlinks ("resolves outside base directory").
TRUST_CLIENT_USER_ID=1 SESSION_SERVICE_URI=memory:// ALLOW_ORIGINS=http://localhost:3000 \
  uv run uvicorn deployment.async_app:app --port 8000

# In a second terminal:
cd frontend && npm install && npm run dev   # http://localhost:3000
```

### Linting, Formatting & Type Checking

```bash
uv run ruff format .        # Format Python files
uv run ruff format --check  # Check formatting (CI check)
uv run ruff check .         # Lint Python files
uv run ruff check --fix .   # Lint + autofix
uv run ty check             # Type-check production Python sources
```

### Testing & Evaluation

```bash
# Python unit tests (pytest) — no GCP credentials needed, but GOOGLE_CLOUD_PROJECT must be
# set to any non-empty value (e.g., from .env or `GOOGLE_CLOUD_PROJECT=test-project`) because
# genai.Client(vertexai=True) resolves the project eagerly.
uv run pytest tests/ -v
uv run pytest tests/ -q -n 4                 # Parallel via pytest-xdist (~2x faster; avoid -n auto)
uv run pytest tests/ -q -n 6 -m "not slow"   # Fast local loop (skips JAX-compile/golden/image-staging)

# Frontend tests (Vitest + React Testing Library)
cd frontend && npm test            # Single run
cd frontend && npm run test:watch  # Watch mode
cd frontend && npm run build       # Production build + full TypeScript type check

# ADK Evals — end-to-end agent evaluation with LLM-as-judge (real API calls, ~5 min per case)
PYTHONPATH="$PWD" uv run adk eval trend_scout tests/eval/evalsets/trend_scout_evalset.json \
  --config_file_path=tests/eval/eval_config.json --print_detailed_results

# creative_agent eval requires PYTHONPATH="$PWD" (so sibling `creative_eval` imports resolve)
# and uses its own creative-specific rubric config:
PYTHONPATH="$PWD" uv run adk eval creative_agent tests/eval/evalsets/creative_agent_evalset.json \
  --config_file_path=tests/eval/creative_eval_config.json --print_detailed_results

# Live GCP integration checks (requires deployed agents + GCP credentials)
uv run python deployment/integration_test.py --check health                          # Verify agents reachable
uv run python deployment/integration_test.py --check session --agent trend_scout     # Session lifecycle
uv run python deployment/integration_test.py --check smoke --agent creative_agent    # Full end-to-end
uv run python deployment/integration_test.py --check all                             # All checks
```

- **Test suites overview:**
  - **Frontend (`frontend/src/__tests__/`):** Async-job poll client (`poll-run.test.ts`), P3 proxy identity (`iap-identity.test.ts`, `user-scoping.test.ts`), form validation, GCS URI building, bandit experiments & shifts (`experiments.test.ts`, `chart.test.ts`, `deploy-selection.test.ts`, `shifts.test.ts`, `scenario-shifts.test.ts`, `shift-insights.test.ts`), widget layouts, trend markdown parsing, `extractItems`, interactive pause/resume, human ratings + judge agreement (`ratings.test.ts`, `rating-control.test.tsx`, `judge-agreement.test.tsx`).
  - **Python (`tests/`):** Pydantic schema validation, agent graph/pipeline structure, tools, callbacks (citation regex, state init, rate limiting), async-job run helpers (`test_async_runs.py`), P3 authz (`test_authz.py`), deployment utilities, Cloud Function logic (`test_crf_*.py`), bandit core/predictor/traffic/shifts/parity/goldens (`test_bandit_*.py`, `test_experiment*_*.py`), human ratings + judge calibration (`test_ratings_api.py`, `test_ratings_store.py`, `test_calibration.py`). See [tests/README.md](./tests/README.md) for the per-file breakdown.
  - **CI (`.github/workflows/`):**
    - `python-ci.yml`: PRs touching Python/deps/`agents/**` — runs `uv sync --locked`, `requirements.txt` drift check against `uv export`, `ruff check`, `ruff format --check`, `ty check`, and `pytest tests/ -n 4`.
    - `crf-deps.yml`: PRs touching `cloud_functions/**` or `tests/test_crf_*.py` — installs `cloud_functions/creative_fanout/requirements.txt` in a clean venv and runs `tests/test_crf_*.py`.
    - `frontend-tests.yml`: PRs touching `frontend/**` — runs `npm run lint`, `npm test`, `npm run build`.
    - `adk-eval.yml`: Nightly schedule + manual `workflow_dispatch` — runs `adk eval` per agent against isolated `trend_trawler_eval` dataset/bucket, gated by `tests/eval/efficiency_gate.py` against `docs/baselines/eval_efficiency.json`.

### Agent Engine Deployment

```bash
# Deploy agent to Agent Engine (bundled extra_packages come from AGENT_EXTRA_PACKAGES in deploy_agent.py)
uv run python deployment/deploy_agent.py --version=v1 --agent=trend_scout --create
uv run python deployment/deploy_agent.py --version=v1 --agent=creative_agent --create
uv run python deployment/deploy_agent.py --version=v1 --agent=interactive_creative --create
# Add --enable_tracing to opt a new engine into Cloud Trace
# (GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true + ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false)

# List or delete Agent Engine instances
uv run python deployment/deploy_agent.py --list
uv run python deployment/deploy_agent.py --resource_id=<ID> --delete

# Smoke-test deployed agents
export USER_ID='test_user'
uv run python deployment/test_deployment.py --agent=trend_scout --user_id=$USER_ID
uv run python deployment/test_deployment.py --agent=creative_agent --user_id=$USER_ID
```

---

## 4. Architecture & System Design

### Flat Package Layout (Deliberate)

First-party Python packages sit **flat at the repository root** (`trend_scout/`, `creative_agent/`, `interactive_creative/`, `creative_eval/`, `agent_common/`, `runserver/`, `bandit/`, `bandit_serving/`, `bandit_traffic/`), **not** under `src/` or `agents/`.
- **Why:** Agent Engine's `extra_packages` staging preserves each package's relative path as its archive path (`tarfile.add(path)` → arcname). Nesting packages would break every bare `from creative_agent ...` or `from agent_common ...` import inside deployed engines.
- `agents/` at the repo root contains only symlinks back to `trend_scout`, `creative_agent`, and `interactive_creative` for ADK dev-server discovery.

### Two-Phase Agent Pipeline

1. **Phase 1 — `trend_scout/`**: Gathers top 25 Google Search trends from BigQuery (`bigquery-public-data.google_trends`), researches cultural context via web search, filters to the 3 most campaign-relevant trends (or pauses for human selection when `interactive_trend_pick` is set), and saves to BigQuery (`target_trends_crf`).
2. **Phase 2 — `creative_agent/`**: Takes a single `<trend, campaign>` pair, runs parallel web research (campaign researcher + trend researcher), synthesizes a cited research report and structured `CreativeBrief`, drafts/critiques/gates ad copy and visual concepts, renders images via `gemini-nano-banana-2.1`, evaluates all creatives via `creative_eval`, and exports a research PDF, HTML gallery, and evaluation JSON report to GCS + summary rows to BigQuery (`trend_creatives`, `creative_evals`).
3. **Phase 2 (Interactive) — `interactive_creative/`**: Thin resumable wrapper (`ResumabilityConfig(is_resumable=True)`) around `creative_agent`'s pipelines that pauses at 3 human-review checkpoints via `LongRunningFunctionTool`: (1) after research report, (2) after ad copies, (3) after visual concepts (with a `visual_concept_reviser` applying human edits before image rendering). Checkpoint 1 reviews the structured creative brief as an editable form (validated server-side by `runserver.async_runs.merge_brief_edit`; invalid → 400); a checkpoint-2 revision request with feedback revises the copies once (`prepare_copy_revision` → `ad_copy_user_reviser`) and re-presents them once. Imports reusable pipelines and schemas exclusively from `creative_agent`'s **public facade** (`creative_agent/__init__.py` `__all__`), plus `creative_agent.config`.
4. **Evaluation — `creative_eval/`**: LLM-as-judge module scoring each ad copy (6 dimensions) and visual concept (6 dimensions) concurrently using `gemini-3.1-pro-preview` at `global` with structured output. Scores are normalized `0.0–1.0` (`0.7` passing threshold) and saved as `CreativeEvaluationReport` to GCS and summarized in BigQuery `creative_evals`. A creative passes only when its score ≥ 0.7 **and** every non-advisory binary gate passed (historical score-only pass rates are not directly comparable). The judge gets the structured `creative_brief` (proposition, reasons to believe, mandatories, avoid + brand don'ts, trend bridge, brand cues; `creative_eval/brief.py`) and returns binary **gates** alongside the 6 advisory quality scores — ad copy: `delivers_proposition`, `product_named`, `uses_reason_to_believe`, `mandatories_met`, `avoid_respected`; visual: `product_visible`, `trend_motif_visible`, `text_correct`, `brand_cue_present` (**advisory**, never blocks — mirrors image QA), `avoid_respected` (names/labels in `creative_eval/dimensions.py`, mirrored in `frontend/src/lib/eval-dimensions.ts`, drift-tested). The visual judge sees the **rendered image** (`generated_images[concept].gcs_uri` as a `gs://` `Part`; falls back to the prompt with `image_judged=False` when the image can't be read). Without a brief the brief-dependent gates pass ("no brief", `brief_used=False`). The eval never triggers regeneration — the pre-eval gates do. Gate normalisation and prompt rules: `docs/notes/creative-quality-gates.md`. Report also records `passing_threshold`; summary `gates_pass_rate` (over **judged** creatives only — failed judge calls excluded; None when none judged) → BigQuery `creative_evals.gates_pass_rate`. Human ratings from the results page (`/ratings` API → BigQuery `creative_ratings`) calibrate it: `runserver/calibration.py` reports agreement, Cohen's kappa and Spearman's rho (`GET /ratings/{user}/calibration`, `scripts/eval_calibration.py`; protocol in `docs/notes/judge-calibration.md`).

### Agent Composition

```text
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
│   │   searcher+synthesizer Workflow; load_brand_history: no-LLM BigQuery read → brand_history) → research_join (JoinNode) → research_barrier (no output)
│   │   → merge_planners → refinement_gate ("refine" only when base research is degraded:
│   │   evaluator → RetryUntilKeyNode-wrapped refined search; else "skip")
│   │   → combined_report_composer → brief_writer_failsoft (FailSoftNode → RetryUntilKeyNode →
│   │   brief_writer, CreativeBrief → creative_brief) → brief_gate (deterministic brief_check.py;
│   │   "revise" → brief_reviser_failsoft → back to brief_gate, at most BRIEF_REVISION_ROUNDS
│   │   passes; residuals → creative_brief__issues; every exit writes creative_brief_md, the
│   │   compact Markdown the creative prompts read; "ok") → save_research_pdf_node (research PDF → research_report_gcs_uri;
│   │   fail-soft) → research_report_ready (truthy terminal)
│   ├── ad_creative_pipeline (Workflow: drafter (10 copies spread across the brief's angles, self-rated
│   │   typicality) → critic (final 4 cover ≥3 angles; per-copy brief_checks checklist) → copy_gate
│   │   (deterministic copy_gate.py: product named, CTA ≤8 words, headline/caption length, brief avoid
│   │   terms, plus the critic's failed proposition/mandatories checks (other self-reports advisory);
│   │   "revise" → ad_copy_reviser_failsoft (rewrites ONLY flagged copies;
│   │   unflagged edits reverted by restore_unflagged) → back to copy_gate, at most COPY_REVISION_ROUNDS
│   │   passes; deterministic residuals only → ad_copy_critique__issues; "ok") → ad_copies_ready)
│   ├── visual_production_pipeline (Workflow)
│   │   visual_generation_pipeline (Workflow: art_director → concept drafter/critic/finalizer
│   │   (brand_cue, copy-quoted in-image text, brief avoid/fit_mode) → concept_gate (deterministic
│   │   concept_guard.concept_issues; "revise" → visual_concept_fixer_failsoft (flagged concepts only)
│   │   → back to concept_gate, at most CONCEPT_REVISION_ROUNDS passes; residuals →
│   │   final_visual_concepts__issues) → visual_concepts_ready) → render_barrier → visual_generator_resilient
│   │   (RetryUntilKeyNode → visual_generator, generate_image: render → image QA vision check →
│   │   ≤ IMAGE_QA_MAX_RERENDERS targeted re-renders, keep the better attempt → generated_images /
│   │   image_qa__issues) → images_ready (truthy terminal)
│   └── finalize_pipeline (Workflow, creative_agent/finalize.py: evaluate_creatives_node (creative_eval
│       judge → creative_evaluation_report) → persist_node (eval report JSON → HTML gallery →
│       trend_creatives row → creative_evals row; transient errors retried, then fail-soft →
│       <key>__issues) → finalize_ready (sets finalize_done; truthy summary))
└── memorize

interactive_creative (root Agent `root_agent`; App + ResumabilityConfig(is_resumable=True); reviser via AgentTool)
├── combined_research_pipeline / ad_creative_pipeline / visual_generation_pipeline / finalize_pipeline (reused from creative_agent, incl. their gates; bare nodes → NodeTool)
├── review_research / review_ad_copies / review_visual_concepts (LongRunningFunctionTool checkpoints 1–3)
├── checkpoint 1 reviews the structured creative brief (editable; resume edit `creative_brief` validated by runserver merge_brief_edit → creative_brief + creative_brief_md); checkpoint 2 revision (feedback) → prepare_copy_revision → ad_copy_user_reviser (bare Workflow → NodeTool: copy_revision_guard (skips unless prepared) → FailSoftNode → shared single-turn ad_copy_reviser, once → ad_copies_revised (re-records ad_copy_critique__issues warning-only, sets ad_copy_user_revised)) → review_ad_copies once more
├── visual_concept_reviser (applies checkpoint-3 revision notes → final_visual_concepts; concept guard + issue re-check, no fix loop)
├── visual_generator_resilient (reused; render after the reviser) → finalize_pipeline
└── save_draft_report_artifact (only to re-save the PDF after a checkpoint-1 brief/report edit) + memorize
```

### Shared Building Blocks (`agent_common/`)

`agent_common/` is bundled into every deployed Agent Engine instance (depends on `google-adk`, free of per-agent business logic, never imported by Cloud Functions):
- `agent_common/config.py` — `BaseAgentConfiguration`: single source of truth for model names, rate-limit knobs, and GCP/BigQuery env vars. Per-agent `config.py` modules subclass it.
- `agent_common/locations.py` & `agent_common/models.py` — `MODEL_LOCATION` (default `"global"`), `build_gemini(name)` (returns `TimeoutRetryingGemini`, setting per-request `http_options.timeout`, retrying `TimeoutError` up to `TIMEOUT_RETRY_ATTEMPTS=3`, and supporting `empty_turn_retries=ROOT_EMPTY_TURN_RETRIES` on root orchestrators), and `build_gemini_with_fallback()` (wraps `critic_model` producers in ADK `FallbackModel` failing over to `worker_model` on HTTP 429/5xx; kill switch `CRITIC_FALLBACK_MODEL=""`).
- `agent_common/genai_retry.py` — `build_genai_http_retry()` (status-code retry on 429/500/503/504) and `MODEL_REQUEST_TIMEOUT_SECONDS` (default 240s, clamped 30–900, `0` disables).
- `agent_common/retry.py` — `build_infra_retry(extra_exceptions=(), max_attempts=3)` for ADK node `RetryConfig`.
- `agent_common/retry_node.py` — `RetryUntilKeyNode` (re-runs flaky search/thinking producer sub-workflows until `output_key` is populated; records `<key>__retry_exhausted` and yields a truthy notice on exhaustion so `NodeTool` callers never stall) + `is_populated(value)`.
- `agent_common/fail_soft_node.py` — `FailSoftNode` (catches exceptions on optional graph steps like brief writing/revision, applies `on_error` state delta, and yields a truthy notice so downstream nodes continue).
- `agent_common/schemas.py` — `PipelineRequest` (`request: str`), the `input_schema` for `Workflow` nodes exposed as `NodeTool`.
- `agent_common/rate_limit.py` — `build_rate_limit_callback(config)` enforcing `rpm_quota` (1000 RPM).
- `agent_common/sanitize.py` — `scrub_lone_surrogates` / `scrub_surrogates_in_response` (`after_model_callback` stripping lone Unicode surrogates before Pydantic validation).
- `agent_common/history.py` — `drop_other_agent_context` (`before_model_callback` on the `creative_agent` + `interactive_creative` roots): drops the NodeTool pipelines' replayed sub-agent turns and node inputs, which ADK 2.10 otherwise feeds into a root's prompt (branch `None` matches every branch), keeping user messages, the root's own turns and all function call/response pairs.
- `agent_common/state.py` — shared `memorize` tool (name must remain `memorize`) and `seed_initial_state(...)`.
- `agent_common/clients.py` — lazy `get_gcs_client()` and `get_bigquery_client()` getters (bound as `_get_gcs_client` / `_get_bigquery_client` in agent modules for test monkeypatching).
- `agent_common/idempotency.py` — `stable_row_id(*parts, length=8)` deterministic SHA-256 key derived from `tool_context.session.id` for idempotent BigQuery `MERGE ... WHEN NOT MATCHED THEN INSERT` writes.
- `agent_common/safety.py` — `build_safety_plugins(root_agent_names)` wiring opt-in `ScopedModelArmorPlugin` (`MODEL_ARMOR_TEMPLATE`, `MODEL_ARMOR_RESPONSE_TEMPLATE`, `MODEL_ARMOR_FAIL_CLOSED`).
- `agent_common/observability.py` — `log_run_start`, `log_empty_turn_finish_reason`, `make_final_state_summary`, and `collect_degradation_warnings(state)` (scans `state.to_dict()` for `<key>__retry_exhausted` and `<key>__issues` to populate eval report warnings, BigQuery `research_gaps`, and the HTML gallery banner).

### Models, Locations & Environment Variables

- **Models (all Gemini 3.x @ `global`):**
  - Worker: `gemini-3.8-flash`
  - Critic & `creative_eval` judge: `gemini-3.1-pro-preview`
  - Lite planner: `gemini-3.5-flash-lite`
  - Campaign research (`ALT_GLOBAL_MODEL`) & `trend_scout` picker: `gemini-3.5-flash` (controlled via `CAMPAIGN_RESEARCH_PLACEMENT`, default `global_altbucket` to spread quota away from the trend researcher's `gemini-3.8-flash` bucket)
  - `trend_scout` gatherer: `gemini-3.1-flash-lite`
  - Image generation: `gemini-nano-banana-2.1`
- **Locations (Keep Separate!):**
  - `GOOGLE_CLOUD_LOCATION=global`: Required for all Gemini 3.x model calls (requesting them in `us-central1` fails with `404 NOT_FOUND`). Because Agent Engine reserves `GOOGLE_CLOUD_LOCATION` at runtime, `agent_common/locations.py` (`MODEL_LOCATION = "global"`) pins model calls in code.
  - `GCP_REGION=us-central1`: Used for all regional GCP resources (Agent Engine / Reasoning Engine, BigQuery, Cloud Storage, Pub/Sub, Cloud Run, Cloud Run Functions).
- **Storage Env Var:** Use `GOOGLE_CLOUD_STORAGE_BUCKET` (bare bucket name without `gs://`) — never `GCS_BUCKET_NAME` or `BUCKET`.
- **Session State Keys:**
  - Core brief keys: `brand`, `target_product`, `target_audience`, `key_selling_points`, `target_search_trends` (seeded via `createSession` `initialState`, `setdefault`ed in `callbacks.py`).
  - Optional visual-intent keys (`creative_agent` / `interactive_creative`, default `""`, seeded via `initialState` only): `visual_intent`, `brand_colors`, `visual_style_preference`, `visual_avoid`, `visual_aspect_ratio`, `reference_image_role` (`product`|`logo`|`style`), `reference_images` (default `[]`; up to 3 `{uri, role}` references, legacy `reference_image_uri` folded in first; style references guide palette/texture only), the derived `reference_roles`, plus `visual_revision_notes` on checkpoint-3 resume.
  - Creative pipeline keys: `creative_brief` + `creative_brief_md` (compact Markdown read by the creative prompts), `creative_brief_edited`, `brand_history`, `style_shortlist`, `ad_copy_critique`, `ad_copy_user_revised`, `final_visual_concepts`, `generated_images` (`{concept: {gcs_uri, artifact_key, attempts, qa}}`), `research_report_gcs_uri`, `creative_evaluation_report`, `eval_report_gcs_uri`, `creative_gallery_gcs_uri`, `creative_row_uuid`, `eval_bq_row_uuid`, `finalize_done` (completion key). Residual `<key>__issues` / `<key>__retry_exhausted` markers surface as run warnings.
- **Creative-quality knobs** (shipped via `deploy_agent.py` `ENV_VAR_DICT`; Agent Engine bakes in the deployer's env): `BRIEF_REVISION_ROUNDS` / `COPY_REVISION_ROUNDS` / `CONCEPT_REVISION_ROUNDS` (default 1, 0–2), `IMAGE_QA_ENABLED` (true), `IMAGE_QA_MAX_RERENDERS` (1, 0–2), `IMAGE_QA_MAX_RERENDERS_PER_RUN` (2, 0–8), `IMAGE_QA_MODEL` (worker model), `BRAND_HISTORY_ENABLED` (true), `BRAND_HISTORY_RUNS` (5, 0–20). api-only: `RATINGS_STORE` (`bigquery`|`memory`), `BQ_TABLE_RATINGS` (`creative_ratings`, rows snapshot the judge verdict + `judge_source`), `RUN_MAX_AUTO_CONTINUES` (2). Migrations: `deployment/README.md` → Creative quality: migrations + knobs. Plan: `docs/plans/2026-10-07-creative-quality.md`.

### Frontend (`frontend/`) & Async-Job Backend (`runserver/`, `deployment/async_app.py`)

- **Cloud Run Architecture:** Deployed as two services: `trend-trawler-web` (Next.js standalone, IAP-gated to `jordantotten.altostrat.com`) and private `trend-trawler-api` (runs `deployment/async_app.py` via `deployment/backend_entrypoint.sh` with `--no-cpu-throttling --min-instances 1` and persistent `VertexAiSessionService` via `SESSION_SERVICE_URI`).
- **Async-Job Run Model (`runserver/async_runs.py`):** Kicks off a detached `asyncio` task driving `Runner.run_async` decoupled from the HTTP request, appends a terminal `__run_status` marker event, and serves `GET /runs/.../{session}?since=N` polling + resume endpoints so runs survive disconnects/reloads. Includes bounded `should_auto_continue` recovery (capped by `RUN_MAX_AUTO_CONTINUES`, default 2, clamped 0–3) if a root turn finishes empty before setting the app's completion key (`finalize_done` for creative apps, set by `finalize_pipeline`'s terminal node on every path).
- **P3 Per-User Authz (`frontend/src/lib/iap-identity.ts`, `user-scoping.ts`, `runserver/authz.py`):**
  - Proxy verifies `x-goog-iap-jwt-assertion` (ES256, audience, `hd == IAP_ALLOWED_HD`; never reads spoofable `x-goog-authenticated-user-*` headers), allowlists UI routes, rewrites path/body `userId` (`me` → normalized caller email), and sets `X-TT-User`.
  - Backend `UserAuthzMiddleware` trusts `X-TT-User` only when accompanied by a verified Google ID token for `TRUSTED_PROXY_SA` (`tt-web-sa`) with `aud ∈ TRUSTED_PROXY_AUDIENCES`. Blocks canned `/run`, `/run_sse`, `/run_live`, memory, and agent-identity routes with 404.
- **"Proof Room" Design System:** Defined in `frontend/src/app/globals.css`. Light theme, `Archivo` + `JetBrains Mono` fonts, `#0077A8` primary action color, `mark-pass`/`mark-fail`/`mark-pending` status marks, sentence-case `FieldLabel`s. No uppercase eyebrows, glass cards, or entrance animations.

### Brand History (`creative_agent/brand_history.py`)

- **What:** `load_brand_history` (function node, no LLM) runs in `combined_research_pipeline`'s START fan-out beside the two planners (feeds `research_join`). It reads the brand's latest `BRAND_HISTORY_RUNS` (default 5, 0–20) BigQuery `creative_evals` rows (parameterised SELECT) plus their eval-report JSON (configured bucket only, 5 MB cap) and writes `brand_history`, a brace-free ≤120-word note read via `{brand_history?}` by the brief writer and art director (build on what worked, fix recurring weaknesses, avoid recently used styles). Unless `visual_style_preference` is set it re-draws `style_shortlist` without the recent styles.
- **Safety:** fail-soft (10 s timeout in a worker thread; any error → `""`); `BRAND_HISTORY_ENABLED=false` skips the query. Eval report entries carry code-set `visual_style` / `angle_id` (`""` in old reports).

### Contextual Bandit Experiments (`bandit/`, `bandit_serving/`, `bandit_traffic/`, `runserver/experiments*.py`)

- **Overview:** Post-run Deploy panel turns 2–4 creatives into arms of a JAX linear Thompson sampling (`LinTS`) bandit (`ctx-v1` features `d=19`, synthetic scenarios `clear_winner`/`segment_winners`/`drift`).
- **Components:** `bandit/` (JAX core, synthetic env, simulator CLI `python -m bandit.cli simulate`), `bandit_serving/predictor.py` (Vertex AI Custom Prediction Routine `BanditPredictor` deployed with `VERTEX_CPR_WEB_CONCURRENCY=1` on a single replica, checkpointed to GCS), `bandit_traffic/` (Cloud Run Job driving CRN-keyed synthetic readers and replaying 5 baseline policies → BigQuery `bandit_events` & `bandit_episode_metrics`), `runserver/experiments*.py` (user-scoped `/experiments` REST API, TTL reaper, metrics/series aggregation).
- **Contracts & Parity:** [docs/bandit/contracts.md](./docs/bandit/contracts.md) is the authoritative contract for the core API, endpoint instances (`decision`/`reward`/`reset`/`state`), BigQuery tables, context encoding, scripted behaviour shifts (§10), and continuous learning (§11). Update `docs/bandit/contracts.md` in the same PR as any interface change.
- **Local Dev:** Set `BANDIT_DEPLOY_MODE=fake` on the API server; run `uv run python -m bandit_traffic.main --in-process --dry-run` for local traffic simulation without GCP.

### Event-Driven Orchestration (`cloud_functions/creative_fanout/`)

- **Orchestrator (`crf_entrypoint`):** Triggered by `CREATIVE_TOPIC_NAME` Pub/Sub topic, queries BigQuery `target_trends_crf` for unprocessed trends (oldest first, capped at `CRF_MAX_ROWS_PER_RUN`, default 3), and fans out one Pub/Sub message per trend to `CREATIVE_WORKER_TOPIC_NAME` (concurrency=100).
- **Worker (`agent_worker_entrypoint`):** Triggered by `CREATIVE_WORKER_TOPIC_NAME`, invokes the deployed `creative_agent` on Agent Engine using `agent_session` (`cloud_functions/creative_fanout/session.py`), and marks the trend processed (concurrency=1, max-instances=1, timeout=1800s).

---

## 5. Style Guide & Code Conventions

- **Agent Instructions (`prompts.py`):** All agent `instruction=` strings must live in the package's `prompts.py` as `<AGENT_VAR>_INSTR` constants, never inline in `agent.py`.
- **Agent Schemas (`schemas.py`):** All `output_schema=` Pydantic models must live in the package's `schemas.py` (and be re-imported into `agent.py` where needed), never inline in `agent.py`.
- **Image Prompt Grammar (`IMAGE_PROMPT_GUIDE`):** Defined in `creative_agent/prompts.py` and spliced into visual drafter/critic instructions via string concatenation. It **must not contain `{...}` curly braces** (ADK interprets `{token}` as session state interpolation; use `[square brackets]` for fill-in slots). Each session seeds a stratified 6-family `style_shortlist` (`creative_agent/style_shortlist.py`), and `creative_agent/concept_guard.py` ensures every final `image_generation_prompt` includes the concept's `trend_motif` and `{target_product}`.
- **Cross-Package Imports (`creative_agent` → `interactive_creative`):** `interactive_creative` must only import from `creative_agent`'s public facade (`creative_agent/__init__.py` `__all__`) and `creative_agent.config`, never directly from internal submodules like `creative_agent.agent` or `creative_agent.schemas`.
- **BigQuery Idempotency:** Derive BigQuery row keys deterministically from `tool_context.session.id` using `agent_common.idempotency.stable_row_id` (never `uuid.uuid4()`) and write via `MERGE ... WHEN NOT MATCHED THEN INSERT`.

---

## 6. Key File Locations

| Area | Path | Purpose |
|---|---|---|
| **Phase 1 Agent** | `trend_scout/{agent,prompts,schemas,tools,callbacks,config,review_tools}.py` | Google Trends discovery, cultural context research, BigQuery persistence |
| **Phase 2 Agent** | `creative_agent/{agent,prompts,schemas,tools,bq_tools,gcs_tools,image_tools,callbacks,config}.py` | Parallel web research, brief gate (`brief_check.py`, `brief_render.py`), copy gate (`copy_gate.py`), visual generation (`style_shortlist.py`, `concept_guard.py`), HTML gallery (`gallery_template.py`) |
| **Interactive Agent** | `interactive_creative/{agent,prompts,callbacks,review_tools}.py` | Resumable 3-checkpoint wrapper over `creative_agent` |
| **LLM Judge** | `creative_eval/{agent,evaluate,prompts,schemas,dimensions,config}.py` | 12-dimension concurrent Gemini Pro creative evaluation |
| **Shared Agent Lib** | `agent_common/*.py` | Shared config, `build_gemini`, `RetryUntilKeyNode`, `FailSoftNode`, rate limiting, Model Armor safety, observability, idempotency |
| **Backend Server** | `deployment/async_app.py`, `runserver/{async_runs,authz,otel,experiments*,ratings*,calibration}.py` | FastAPI launcher, detached `/runs` polling/resume router, P3 per-user authz, `/experiments` API, `/ratings` human-rating + judge-calibration API |
| **Bandit Engine** | `bandit/*.py`, `bandit_serving/predictor.py`, `bandit_traffic/*.py`, `docs/bandit/contracts.md` | JAX LinTS core, Vertex CPR predictor, Cloud Run traffic job, interface contracts |
| **Cloud Functions** | `cloud_functions/creative_fanout/{main,session,config}.py` | Pub/Sub orchestrator and worker entrypoints |
| **Deployment** | `deployment/{deploy_agent,test_deployment,integration_test,create_session_engine,create_bq_tables}.*` | Agent Engine CLI (`AGENT_EXTRA_PACKAGES`), smoke/integration tests, BQ schema setup |
| **Frontend UI** | `frontend/src/app/{page,runs,run,results,experiments}/`, `frontend/src/lib/*.ts` | Next.js 16 "proof room" UI, IAP proxy (`app/api/adk/[...path]/route.ts`), GCS proxy (`app/api/gcs/route.ts`) |
| **Tests & Evals** | `tests/test_*.py`, `tests/eval/{eval_config,creative_eval_config,efficiency_gate}.*` | Pytest unit/parity/golden suite and ADK rubric evaluation configs |

---

## 7. Negative Constraints (DO NOT DO)

- **DO NOT** run bare `pip`, `pip install`, `uv pip install`, or bare `python`/`pytest` commands. Always use `uv add`, `uv remove`, `uv sync`, and `uv run <cmd>`.
- **DO NOT** use `black`, `flake8`, `isort`, `pyupgrade`, `mypy`, or `pyright`. Always use `uv run ruff format .`, `uv run ruff check .`, and `uv run ty check`.
- **DO NOT** nest the root first-party Python packages (`trend_scout/`, `creative_agent/`, `agent_common/`, etc.) under a `src/` or `agents/` directory; doing so breaks Agent Engine `extra_packages` import paths.
- **DO NOT** use deprecated ADK container agents (`SequentialAgent`, `ParallelAgent`, `LoopAgent`) or legacy `RunIfAgent`/`RetryUntilKeyAgent` wrappers, or legacy `vertexai.preview.reasoning_engines` / `agent_engines` APIs. Use `google.adk.workflow` (`Workflow`, `JoinNode`), `RetryUntilKeyNode`, `FailSoftNode`, and `agentplatform.Client().runtimes`. (`tests/test_public_api.py` and `tests/test_no_legacy_agent_engines_api.py` enforce this.)
- **DO NOT** add `jax` to the root `[project] dependencies` in `pyproject.toml` or root `requirements.txt`, and **DO NOT** import `bandit/` inside `runserver/` or agent packages. The API server image and Agent Engine bundles must remain JAX-free.
- **DO NOT** hand-edit dependency lists in `pyproject.toml` or hand-edit the root `requirements.txt`. After changing runtime dependencies with `uv add`/`uv remove`, regenerate the root `requirements.txt` via:
  ```bash
  uv export --format requirements-txt --no-hashes --no-dev -o requirements.txt
  ```
- **DO NOT** put `{...}` curly braces in `IMAGE_PROMPT_GUIDE` or any non-state-token prompt template spliced into ADK agent instructions (use `[square brackets]` instead).
- **DO NOT** request Gemini 3.x models from a regional endpoint like `us-central1` (always use `global`), and **DO NOT** pass `GOOGLE_CLOUD_LOCATION=global` to regional GCP clients like Agent Engine (`GCP_REGION=us-central1`).
- **DO NOT** add `Co-Authored-By` trailers to git commit messages or pull requests, and **DO NOT** commit or push unless explicitly asked by the user.
