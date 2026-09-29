# P4b Execution Plan: FallbackModel, Cloud Trace, Model Armor, Eval CI

> **For Claude:** REQUIRED SUB-SKILL: Use `subagent-driven-development` to implement this plan task-by-task.
> On approval, the first commit copies this file to `docs/plans/2026-09-29-p4b-execution.md` and sets the P4b doc's `**Status:**` to `executing 2026-09-29 via 2026-09-29-p4b-execution.md`.

**Goal:** Carry out `docs/plans/2026-09-29-p4b-showcase-enhancements.md` (the "P4b doc") Sections 1–4, including the gated live rollout. When done:
- the Pro producers fail over to flash when a quota error hits;
- the Agent Engine engines and the Cloud Run api can export traces to Cloud Trace (opt-in);
- the root orchestrators can be screened by Model Armor (opt-in);
- a manual and nightly `adk eval` workflow gates on token and call-count regressions, writing only to an isolated eval dataset.

**Architecture:** Unchanged from the P4b doc. Every feature is either default-safe (FallbackModel, which has a kill switch) or opt-in behind an env flag (tracing, Model Armor). Shared helpers go in `agent_common/`, host wiring in `deployment/` and `runserver/`. There is **one PR per section**, merged in this order: S1 → S3 → S4 → S2. Eval CI comes last so its baseline reflects the final agents. The api is deployed **once**, after S1, S3 and S4 have merged, to limit how many times in-flight runs get killed.

**Tech Stack:** Python 3.13, uv, google-adk 2.10 (`FallbackModel`, `ModelArmorPlugin`, `get_fast_api_app(otel_to_cloud=)`, efficiency eval metrics), google-cloud-aiplatform 2.2 / `agentplatform.frameworks.AdkApp`, absl flags, GitHub Actions + Workload Identity Federation, Cloud Trace via telemetry.googleapis.com (OTLP), Model Armor, BigQuery.

---

## Context

P4b was proposed on 2026-09-29, before P2 (graph Workflows), P1b (agentplatform SDK), PR #187 (diagram refresh) and PR #191 (the `GOOGLE_GENAI_USE_ENTERPRISE` rename) landed. Three read-only audits re-checked it against the current code. Its designs still hold, but many facts and line numbers have moved, and **Section 5 is already done** (#187/#188/#192 regenerated every diagram post-P2). The user chose Sections 1–4, a **separate eval dataset** for CI, and **code plus a gated rollout**.

The P4b doc is the source of truth for the per-task code and tests. **This plan adds the execution order, the rollout mechanics, and the corrections below.** Read the P4b task text before each task, then apply the matching corrections.

## Corrections to the P4b doc (verified 2026-09-29)

**Section 1 (FallbackModel)**
1. **Producer sites and line numbers.** All six sites are **serial**; none sits inside the `research_join` fan-out (`creative_agent/agent.py:206`), so the bucket-spread argument holds.
   - `creative_agent/agent.py`: `combined_web_evaluator` at `:71`, `combined_report_composer` at `:151`, `visual_generator` at `:491`, `root_agent` at `:650`.
   - `interactive_creative/agent.py:62` (`root_agent`).
   - `trend_scout/agent.py:183` (root `trend_scout`, which has `retry_config=INFRA_RETRY`).
2. **`build_genai_http_retry(attempts=5, initial_delay=10.0, max_delay=60.0)`** (`agent_common/genai_retry.py`) already takes `attempts`. Only `build_gemini` (`agent_common/models.py:20`) needs the new `retry_attempts` parameter.
   - Keep the existing comment explaining why `retry_options` is passed separately from `client_kwargs`.
3. **`BaseAgentConfiguration`** (`agent_common/config.py:29`) is a plain `@dataclass`, and its model fields don't read env today.
   - Add `critic_fallback_model: str = field(default_factory=lambda: os.getenv("CRITIC_FALLBACK_MODEL", "gemini-3.8-flash"))`. This is the `EvalConfig` pattern: env is read at instantiation, so monkeypatch tests need no reload.
   - Importantly, `CRITIC_FALLBACK_MODEL=""` must give `""` (the kill switch), so use `os.getenv` and not `or`.
4. **Test gotcha.** A `FallbackModel` has `.model` (the primary's name) but no `client_kwargs`. Any location assertion on these producers must look at `.models[i].client_kwargs`.
   - No existing test asserts on the Pro producers' `.model`. The graph tests replace `.model` with fakes, so they are unaffected.
   - The `[EXPERIMENTAL]` UserWarning is already filtered (`pyproject.toml:69`).
5. **`creative_eval`.** `CreativeEvaluationReport` (`schemas.py:79`) has no `judge_model` field yet.
   - `evaluate_creatives(campaign_context, ad_copies, visual_concepts, config=None)` is at `evaluate.py:307`.
   - It uses `_get_client(config)` (`:36`) and `evaluate_all_concurrently` (`:227`). The P4b test's monkeypatch targets are correct.
   - When `config is None`, default to `EvalConfig()` before reading `eval_model`.

**Section 3 (Cloud Trace)**
6. **The deploy path is `agentplatform.frameworks.adk.AdkApp`** (site-packages `agentplatform/frameworks/adk.py:731`), not `vertexai/…/templates/adk.py`. The truth table still matches the P4b decision.
   - `_tracing_enabled()` (`:2093`) is on when `enable_tracing=None` and `GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true`.
   - Content capture stays off unless `enable_tracing=True` (`:1013-1016`).
7. **`deployment/deploy_agent.py` uses absl** (`flags`, `main(argv)` at `:313`), not argparse. There is no `build_env_vars`.
   - `deploy_agent(name, version)` is at `:209`.
   - `runtimes.create(agent=adk_app, config={..., "env_vars": ENV_VAR_DICT})` is at `:236-250`.
   - Add `flags.DEFINE_bool("enable_tracing", False, ...)` and `build_env_vars(enable_tracing=False)`, and thread the value through `deploy_agent(name, version, enable_tracing=False)`.
   - `ENV_VAR_DICT` now carries `GOOGLE_GENAI_USE_ENTERPRISE` (#191).
8. **Cloud Run api.** `deployment/async_app.py:44-50` calls `get_fast_api_app(agents_dir=, session_service_uri=, artifact_service_uri=, allow_origins=, web=False)`. Add `otel_to_cloud=otel_to_cloud_enabled()`.
   - `trace_to_cloud` must **not** be used: `opentelemetry.exporter.cloud_trace` isn't installed.
   - `opentelemetry-exporter-otlp-proto-http` 1.42.1 is installed.
   - Put the `ADK_OTEL_TO_CLOUD` flag test in a new `tests/test_otel_flag.py`. `test_async_runs.py` is already 1349 lines.

**Section 4 (Model Armor)**
9. **Dependency.** `google-cloud-modelarmor` isn't installed, and the plugin import fails without it. Run `uv add "google-cloud-modelarmor>=0.7,<1"`, then the requirements export.
   - Also add it to the CRF? **No.** CRF doesn't import `agent_common`.
10. **Plugin and wiring.**
    - Plugin API (verified): `ModelArmorPlugin(*, config, name=..., client=None, credentials=None)`. It overrides `before_model_callback` (`_plugin.py:93`) and `after_model_callback` (`:118`).
    - `ModelArmorConfig` uses `extra='forbid'`. Leaving `response_template_name` unset skips output screening.
    - The Apps today: `trend_scout/agent.py:239` and `interactive_creative/agent.py:100` are resumable Apps. `creative_agent` has **no** App.
    - Add `app = App(name="creative_agent", root_agent=root_agent, plugins=build_safety_plugins(...))` **without** a resumability config (non-resumable, which preserves today's semantics).
    - Before relying on it, check the `ScopedModelArmorPlugin` override signature against the installed `BasePlugin`.
11. **Test changes (deliberate).**
    - `tests/test_deploy_utils.py:228`: creative_agent's case becomes `"app"`.
    - `:236-238` asserts every App `is_resumable`. Make that expectation per agent (creative is False).
    - `tests/test_async_runs.py:114`: creative becomes an App.
    - `runserver/async_runs.get_root_agent` (`:61-82`) returns the creative `app`. `async_app.py:89-96` already branches on `isinstance(obj, App)`.
    - Deploy impact: the next creative engine deploy becomes `AdkApp(app=...)`. It is covered by the integration smoke in Task 8.

**Section 2 (Eval CI)**
12. **`adk eval` exits 0 even when cases fail** (`cli_tools_click.py:1496-1512`). The gate's `final_eval_status` check is therefore **the** failure signal, not a backup.
    - Metrics, statuses and paths are all verified: the 4 efficiency metrics are always on and INFORMATIONAL, and `_reject_threshold` raises if they appear in `criteria`.
    - `EvalStatus` values: PASSED=1, FAILED=2, NOT_EVALUATED=3, INFORMATIONAL=4.
    - The results path is `<app>/.adk/eval_history/*.evalset_result.json`.
    - Each evalset has 2 cases.
13. **pytest won't collect `tests/eval/efficiency_gate.py`**: the default is `test_*.py` and `python_files` isn't overridden. Keep the empty `tests/eval/__init__.py` so `from tests.eval.efficiency_gate import …` resolves under `pythonpath=["."]`.
14. **Workflow pins.** Match the repo: `actions/checkout@v7`, `astral-sh/setup-uv@v10.2.0` (uv `0.12.19`, py `3.13`).
    - Resolve the latest `google-github-actions/auth` major at implementation time with `gh api repos/google-github-actions/auth/releases/latest`.
    - Env uses `GOOGLE_GENAI_USE_ENTERPRISE=1` (not `…_VERTEXAI`).
15. **Isolation.** Eval runs call `write_trends_to_bq`, `write_eval_report_to_bq` and the GCS saves unconditionally, and the rubrics require those calls. trend_scout also writes `BQ_TABLE_TARGETS` (`target_trends_crf`), which the **CRF orchestrator claims**. So an eval run against prod would queue fake trends for the batch.
    - CI therefore points `BQ_DATASET_ID` at a new **`trend_trawler_eval`** dataset.
    - It also points `GOOGLE_CLOUD_STORAGE_BUCKET` at a new **`$PROJECT-trend-trawler-eval`** bucket with a 30-day delete lifecycle.
16. **Eval judge.** The configs judge with `gemini-3.8-flash`, and the agents use Pro. The nightly cron `17 9 * * *` stays off-peak, but skip or cancel it if a CRF batch is running (P4b open question 2).

## Conventions (restate in every subagent dispatch)

- One branch and one PR per section: `feat/p4b-fallback-model`, `feat/p4b-cloud-trace`, `feat/p4b-model-armor`, `ci/p4b-eval-gate`. Squash-merge after CI is green.
- **Never add `Co-Authored-By` trailers or any AI attribution to commits, and never put "Generated with Claude Code" in PR bodies.**
- Loop: `uv run ruff format --check . && uv run ruff check . && uv run ty check && uv run pytest tests/ -q -n 4`, with `GOOGLE_CLOUD_PROJECT=test-project` if unset.
- After a dependency change, run `uv export --format requirements-txt --no-hashes --no-dev -o requirements.txt` (CI drift check).
- TDD per the P4b doc: failing test first, then one commit per task using the P4b commit message.
- Use `uv add` only. Don't hand-edit `.env`. No BigQuery writes to prod tables.
- Don't touch `campaign_models()` or `ALT_GLOBAL_MODEL`. The fallback target is always `worker_model`.

---

## Task 0: Commit the plan
Copy this file to `docs/plans/2026-09-29-p4b-execution.md`. Set the P4b doc's Status line, and mark Section 5 "done via #187/#188/#192". Commit `docs(p4b): execution plan` on `feat/p4b-fallback-model`.

## Tasks 1–3: Section 1, FallbackModel (P4b Tasks 1.1–1.3)
- **Task 1:** `build_gemini_with_fallback` plus `build_gemini(retry_attempts=)`, in `agent_common/models.py` and `tests/test_agent_common_models.py`. Take the code as in P4b, with Correction 2. The existing `_fresh_locations`-style reload helper in that test file can be reused.
- **Task 2:** the `critic_fallback_model` config field (Correction 3) plus wiring the 6 sites (Correction 1).
  - Tests: `tests/test_config.py` (default, empty-string kill switch, never `ALT_GLOBAL_MODEL`) and the parametrized `test_pro_producers_fall_back_to_worker` in `tests/test_pipeline_structure.py`, also asserting `.models[i].client_kwargs == {"location": "global"}`.
  - Add a CLAUDE.md Models bullet.
- **Task 3:** `judge_model` on `CreativeEvaluationReport` (Correction 5). Add it to the eval JSON; no BigQuery column change.
- Open the PR, run a final whole-diff review, and merge.

## Tasks 4–5: Section 3, Cloud Trace (P4b Tasks 3.1–3.2)
- **Task 4:** `build_env_vars` and `--enable_tracing` (Correction 7). Tests go in `tests/test_deploy_utils.py`, loading `deploy_agent` via its existing importer helper.
- **Task 5:** `runserver/otel.py` `otel_to_cloud_enabled()` plus the `async_app.py` wiring (Correction 8), with tests in `tests/test_otel_flag.py`.
  - Document `ADK_OTEL_TO_CLOUD` and `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS` in `deployment/README.md` (env var table) and `.env.example` (commented, optional).
  - Boot check: `TRUST_CLIENT_USER_ID=1 SESSION_SERVICE_URI=memory:// uv run uvicorn deployment.async_app:app --port 8000`, then `curl localhost:8000/list-apps` returns 3 apps (flag unset).
- PR, review, merge.

## Task 6: Section 4, Model Armor (P4b Task 4.1)
- Follow the P4b Task 4.1 steps with Corrections 9–11.
- `tests/test_safety_plugins.py` as in P4b, plus a case with `MODEL_ARMOR_RESPONSE_TEMPLATE` and a `MODEL_ARMOR_FAIL_CLOSED=false` case.
- Docs: `.env.example`, a CLAUDE.md Configuration bullet, and `deployment/README.md` (template setup).
- PR, review, merge.

## Task 7: Section 2, Eval CI (P4b Tasks 2.1–2.2)
- **Task 7a:** `tests/eval/efficiency_gate.py`, `tests/eval/__init__.py`, `tests/test_eval_efficiency_gate.py`, and `docs/baselines/eval_efficiency.json` (`{}`), as in P4b.
  - Add a test that a case with `final_eval_status: 2` is a failure even when there is no baseline (Correction 12).
  - Add a test that `main()` exits 1 on failures, using a tmp `eval_history` directory.
- **Task 7b:** `.github/workflows/adk-eval.yml`, per P4b step 2 with Corrections 14–16.
  - Validate with `uv run --with check-jsonschema check-jsonschema --builtin-schema vendor.github-workflows .github/workflows/adk-eval.yml`.
  - Add a `deployment/README.md` "Eval CI (WIF)" subsection documenting the setup in Task 9. Update `tests/README.md`.
- PR, review, merge. The workflow is inert until the repo variables exist.

## Task 8: Gated rollout of S1+S3+S4 (from a clean `main`)

**Gate before every live change:**
1. Check for in-flight runs: recent api logs, looking for a `running` `__run_status`.
2. Show the user the exact commands, and **wait for confirmation**.

**8a. IAM and APIs** (one confirmation):
- `gcloud services enable telemetry.googleapis.com cloudtrace.googleapis.com modelarmor.googleapis.com`.
- Grant `tt-api-sa` `roles/cloudtrace.agent`, `roles/telemetry.tracesWriter`, `roles/monitoring.metricWriter`, `roles/logging.logWriter` and `roles/modelarmor.user`.
- Check the Agent Engine service agent `service-$PROJECT_NUMBER@gcp-sa-aiplatform-re.iam.gserviceaccount.com` with `gcloud projects get-iam-policy` before granting it `roles/cloudtrace.agent` and `roles/modelarmor.user`.
- Create the Model Armor template `tt-demo` in `us-central1`, using the endpoint override per P4b.

**8b. One api deploy.**
- Use the safe-redeploy recipe in `deployment/README.md` (env and IAP preserved) as `--no-traffic --tag verify`.
- `--update-env-vars "ADK_OTEL_TO_CLOUD=true,ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false,GOOGLE_GENAI_USE_ENTERPRISE=1"`. The last one is the #191 follow-up. Leave `GOOGLE_GENAI_USE_VERTEXAI` alone; ENTERPRISE takes precedence.
- Find the new revision by newest timestamp, verify authed `/list-apps` on the tag URL, then **pin**: `--to-revisions <new>=100 --update-tags prev=<old> --remove-tags verify`.
- Ask the user to run one trend_scout campaign from the web UI. Then confirm in Trace Explorer that the spans cover the detached `/runs` task.
  - If they don't, note it and file it as an open item. Don't block.

**8c. Model Armor demo on a tag only.**
- Deploy a `--no-traffic --tag armor` revision with `MODEL_ARMOR_TEMPLATE=projects/$PROJECT/locations/us-central1/templates/tt-demo`.
- The user submits a prompt-injection brand string via the tag URL. Confirm the blocked message and `model_armor_blocked` in the session event's `custom_metadata`, and measure the added root-turn latency.
- **Prod traffic stays without Model Armor** unless the user asks. Remove the `armor` tag afterwards.

**8d. Agent Engine** (confirm separately; follow the batch-sync runbook memory):
- Redeploy the creative engine with `env -u SCOUT_AGENT_ENGINE_ID -u CREATIVE_AGENT_ENGINE_ID -u INTERACTIVE_AGENT_ENGINE_ID -u VIRTUAL_ENV python deployment/deploy_agent.py --version=v7 --agent=creative_agent --create --enable_tracing`.
- Then run `python deployment/integration_test.py --check smoke --agent creative_agent` and look at the Trace Explorer `invoke_agent`/`call_llm` spans.
- Update the CRF trigger message and the drift check to the new ID per the runbook, then delete the old engine only after the smoke passes.
- trend_scout and interactive stay dormant and are redeployed just-in-time.

## Task 9: Eval CI setup + baseline (after Task 7 merges)

1. **Confirm with the user, then create:**
   - The dataset: `bq mk --dataset $PROJECT:trend_trawler_eval`.
   - The tables, cloned empty from prod schemas, e.g. `bq show --schema --format=prettyjson $PROJECT:trend_trawler.target_trends_crf > /tmp/s.json && bq mk --table $PROJECT:trend_trawler_eval.target_trends_crf /tmp/s.json`. Do the same for `trend_creatives` and `creative_evals`.
   - The bucket: `gcloud storage buckets create gs://$PROJECT-trend-trawler-eval --location=us-central1`, with a 30-day delete lifecycle.
   - WIF: the pool, provider and `tt-eval-ci-sa` per P4b step 1. The attribute condition uses repo `tottenjordan/adk_pipe` and `refs/heads/main`.
   - Role bindings for `tt-eval-ci-sa`: project-level `aiplatform.user` and `bigquery.jobUser`; `bigquery.dataEditor` on `trend_trawler_eval` only; `storage.objectAdmin` on the eval bucket only.
2. Set the repo variables with `gh variable set`: WIF provider, SA, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_STORAGE_BUCKET` (eval bucket), `BQ_PROJECT_ID`, `BQ_DATASET_ID=trend_trawler_eval`, the `BQ_TABLE_*` names, and `GCP_REGION`.
3. Run `gh workflow run adk-eval.yml -f agent=trend_scout`. When it passes, do the same with `-f agent=creative_agent`.
4. Download the artifacts and run the gate locally with `--update-baseline`. Commit `docs/baselines/eval_efficiency.json` plus a `docs/baselines/main.md` section (date, SHA, per-case numbers) in a small PR: `docs(baselines): seed adk eval efficiency baseline`.
5. Verify that prod `trend_trawler.target_trends_crf` gained **no** rows from the eval runs.

## Task 10: Close out
- Mark the P4b doc Status complete (docs PR).
- Update memory: work-status, the `trend-trawler-api-traffic-pin` revisions, the batch-sync runbook (new creative engine ID), and a new P4b note (flags, eval dataset/bucket, and open items such as trace coverage of `/runs` and the Model Armor latency).

## Stop conditions (report to the user; don't improvise)
- FallbackModel breaks `output_schema` agents or the graph tests in ways that aren't simple assertion updates.
- The Agent Engine creative smoke fails after the App switch. Keep the old engine and the CRF message on the old ID.
- The api `verify` tag fails `/list-apps` or boot with `ADK_OTEL_TO_CLOUD=true`. Don't pin; drop the flag.
- The eval workflow can't authenticate through WIF after one fix attempt.
- Any sign that eval runs wrote to the prod dataset.

## Verification (end to end)
- Each PR's CI is green, and the local loop passes (`pytest -n 4`).
- Unit tests prove:
  - the 6 producers are `FallbackModel`s, each delegate pinned to `global`, and the kill switch works;
  - `judge_model` is recorded;
  - the tracing env flag is default-off;
  - Model Armor is default-off and root-scoped;
  - the gate fails on a token regression or a FAILED case, and only warns on duration.
- In production:
  - the api is pinned to the new revision (`prev` tag on the old one);
  - traces appear for one web-UI run;
  - the Model Armor block shows on the `armor` tag only;
  - the creative engine passes the smoke with tracing;
  - the CRF points at the new engine ID;
  - the eval workflow passes both agents against `trend_trawler_eval` and the baseline is committed;
  - prod tables are untouched by CI.
