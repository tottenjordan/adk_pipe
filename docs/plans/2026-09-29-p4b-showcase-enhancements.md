# P4b: Showcase Enhancements (FallbackModel, Eval CI, Tracing, Model Armor, Diagrams) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` (or `subagent-driven-development`) to implement this plan task-by-task.

**Status:** proposal (not started)
**Goal:** Carry out Phase 4 items P4.2–P4.6 of `docs/plans/2026-09-28-repo-refresh.md`. That means graceful degradation when the Pro quota is exhausted, a scheduled `adk eval` regression gate with efficiency metrics, Cloud Trace for Agent Engine and the Cloud Run api, an opt-in Model Armor safety demo, and refreshed diagrams after the P2 migration.
**Architecture:** Each section below is independent and ships as its own PR, in priority order. Every enhancement is either default-safe or opt-in through an env flag, so nothing in prod changes until a flag is flipped or a deploy is made. Shared helpers go in `agent_common/`, which is bundled into every engine. Host wiring goes in `deployment/` and `runserver/`.
**Tech Stack:** Python 3.13, uv, google-adk 2.10 (`FallbackModel`, `ModelArmorPlugin`, `get_fast_api_app(otel_to_cloud=)`, efficiency eval metrics), google-cloud-aiplatform 1.165.1 (`AdkApp`), GitHub Actions with Workload Identity Federation, Cloud Trace via the Telemetry (OTLP) API, Model Armor, and the PaperBanana MCP.

---

## Conventions (apply to every task)

- Follow `CODE_STANDARDS.md`: `uv add` (never edit deps by hand), `ruff`, `ty`, `pytest`. Tests use `asyncio.run` because the repo has no pytest-asyncio.
- Verification loop for every task: `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest tests/ -q`. These need `GOOGLE_CLOUD_PROJECT` set; any dummy value works.
- After any dependency change, regenerate the Agent Engine requirements file with `uv export --format requirements-txt --no-hashes --no-dev -o requirements.txt`. CI fails on drift.
- **Commit messages must NEVER include `Co-Authored-By` or any AI/tool attribution trailer, and PR bodies must not either.** Branch off `main` with one branch per section.
- **After any `trend-trawler-api` deploy, always pin traffic.** The deploy success line prints the OLD revision, so find the new one by timestamp:
  ```bash
  NEW=$(gcloud run revisions list --service=trend-trawler-api --region=us-central1 \
        --sort-by=~metadata.creationTimestamp --limit=1 --format='value(metadata.name)')
  gcloud run services update-traffic trend-trawler-api --region=us-central1 --to-revisions=${NEW}=100
  ```
- Do not change the per-base-model quota-bucket spread (CLAUDE.md, "Campaign-research placement"). `campaign_models()` and the `ALT_GLOBAL_MODEL` bucket stay untouched.

---

## Section 1 — P4.2: ADK `FallbackModel` on quota-bound producers

### Verified API facts (from `.venv/.../google/adk/models/_fallback_model.py`)
- `from google.adk.models import FallbackModel` is exported lazily from `models/__init__.py`. It is `@experimental(FeatureName.FALLBACK_MODEL)`, a registry feature that is `EXPERIMENTAL, default_on=True`, so it works without opting in but emits a `UserWarning`.
- `class FallbackModel(BaseLlm)` has these fields: `models: list[str | BaseLlm] = Field(min_length=1)` and `retriable_status_codes: frozenset[int] = DEFAULT_STATUS_CODES`, where `DEFAULT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})`. 408 is deliberately excluded. `model` is derived from `models[0]`, and passing a different `model=` raises `ValueError`.
- A fallback is triggered only when the error carries a status in `retriable_status_codes`. For genai this is `APIError.code`. Errors with no status, such as connection errors, propagate without a fallback. After the first response has been yielded, a failure propagates. When the last model fails, its error is raised, so `on_model_error_callback` and ADK node retry still apply.
- "Each model is tried exactly once. Retrying a single model is … handled by that model's own retry configuration (`Gemini(model=..., retry_options=...)`)." String entries are resolved via `LLMRegistry.new_llm(entry)`, which does **not** get our `client_kwargs={"location": "global"}` pin. **Always pass `build_gemini(...)` instances, never bare strings.**
- **How it composes with our retry layers:**
  - `build_gemini` sets `retry_options=build_genai_http_retry()`, which is 5 attempts with 10→60 s backoff on 429/500/503/504. That HTTP retry runs *inside* each delegate. With the defaults, the Pro primary would burn about 130 s of backoff before the fallback fires. The primary therefore gets `attempts=2`, so it fails over after about 10 s. The backup keeps the full 5-attempt retry.
  - `build_infra_retry` (the node-level `RetryConfig`, matched by exact class name) is unchanged. It only sees an exception once *every* model has failed. genai `ClientError` (429) is not in its list, and `creative_agent`'s genai `ServerError` extra retries the whole node only on a terminal 5xx.

### Decisions
| Producer (all `config.critic_model` = `gemini-3.1-pro-preview`) | Fallback | Rationale |
|---|---|---|
| `creative_agent` `root_agent`, `interactive_creative` root (`agent.py:58`), `trend_scout` root (`agent.py:167`) | `worker_model` `gemini-3.8-flash` | Orchestrators that route and call tools. Low quality risk, and aborting a run on a 429 is worse. |
| `creative_agent` `visual_generator` (`agent.py:388`) | `gemini-3.8-flash` | It only drives `generate_image`. |
| `combined_report_composer` (`:140`), `combined_web_evaluator` (`:63`) | `gemini-3.8-flash` | These are quality-sensitive, but a flash-written report is better than a `KeyError` abort. The serving model is logged by ADK's warning (`"Model %s failed with status %s; falling back…"`). |
| `creative_eval` judge | **none** | It calls genai directly (`creative_eval/evaluate.py:118`), not through an ADK model, so `FallbackModel` cannot apply. A prior A/B rejected 2.5-pro because it grades softer, so a silent judge swap would skew pass rates. Instead the report records `judge_model`. |
| Image gen (`creative_agent/image_tools.py:120,272`) | **none** | This path is genai `generate_content` directly, not an ADK `BaseLlm`. There is also no second image model in the lineup, and the 2 RPM ceiling is project-wide. Keep `_generate_image_with_backoff`. |

**Bucket spread:** the fallback target is `worker_model`, which is the trend half's bucket and **never** `ALT_GLOBAL_MODEL` (`gemini-3.5-flash`, the campaign bucket). All Pro producers run serially outside `parallel_planner_agent`, so failover never contends with the one `ParallelAgent` that PR #101 spread out. For `trend_scout`, root failover lands on the `understand` agent's bucket. This is acceptable because the AgentTool sub-agents run serially.

The kill switch is `CRITIC_FALLBACK_MODEL=""`, which returns the plain `build_gemini` model.

### Task 1.1: `build_gemini_with_fallback` factory
**Files:** modify `agent_common/models.py`. Test in `tests/test_agent_common_models.py`.

1. Write failing tests by appending to `tests/test_agent_common_models.py`:
```python
def _fresh_locations(monkeypatch):
    monkeypatch.delenv("MODEL_LOCATION", raising=False)
    import agent_common.locations as locations

    importlib.reload(locations)


def test_build_gemini_with_fallback_wraps_pinned_models(monkeypatch):
    _fresh_locations(monkeypatch)
    from google.adk.models import FallbackModel, Gemini

    from agent_common.models import build_gemini_with_fallback

    m = build_gemini_with_fallback("gemini-3.1-pro-preview", "gemini-3.8-flash")
    assert isinstance(m, FallbackModel)
    assert m.model == "gemini-3.1-pro-preview"  # primary name drives spans/requests
    primary, backup = m.models
    assert isinstance(primary, Gemini) and isinstance(backup, Gemini)
    # bare strings would bypass the global pin (LLMRegistry.new_llm)
    assert primary.client_kwargs == {"location": "global"}
    assert backup.client_kwargs == {"location": "global"}
    # primary fails over fast; backup keeps the full quota-paced retry
    assert primary.retry_options.attempts == 2
    assert backup.retry_options.attempts == 5
    assert {429, 503} <= m.retriable_status_codes


def test_build_gemini_with_fallback_disabled_returns_plain(monkeypatch):
    _fresh_locations(monkeypatch)
    from google.adk.models import FallbackModel

    from agent_common.models import build_gemini_with_fallback

    m = build_gemini_with_fallback("gemini-3.1-pro-preview", "")
    assert not isinstance(m, FallbackModel)
    assert m.model == "gemini-3.1-pro-preview"


def test_fallback_moves_on_after_429():
    """Behavioural: a 429 from the primary is served by the backup."""
    import asyncio
    from typing import AsyncGenerator

    from google.adk.models import BaseLlm, FallbackModel
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.genai import errors as genai_errors
    from google.genai import types

    class _Quota(BaseLlm):
        async def generate_content_async(
            self, llm_request, stream=False
        ) -> AsyncGenerator[LlmResponse, None]:
            raise genai_errors.ClientError(429, {"error": {"message": "quota"}})
            yield  # pragma: no cover

    class _Ok(BaseLlm):
        async def generate_content_async(
            self, llm_request, stream=False
        ) -> AsyncGenerator[LlmResponse, None]:
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text=self.model)])
            )

    fm = FallbackModel(models=[_Quota(model="pro"), _Ok(model="flash")])

    async def _go():
        return [r async for r in fm.generate_content_async(LlmRequest(model="pro"))]

    (resp,) = asyncio.run(_go())
    assert resp.content.parts[0].text == "flash"
```
2. Run `uv run pytest tests/test_agent_common_models.py -q`. Expect the first two tests to FAIL with ImportError. The third should already pass because it pins the ADK contract.
3. Implement in `agent_common/models.py`:
```python
from google.adk.models import FallbackModel, Gemini

def build_gemini(model_name: str, location: str | None = None,
                 retry_attempts: int | None = None) -> Gemini:
    retry = (genai_retry.build_genai_http_retry() if retry_attempts is None
             else genai_retry.build_genai_http_retry(attempts=retry_attempts))
    return Gemini(model=model_name, retry_options=retry,
                  client_kwargs={"location": location or locations.MODEL_LOCATION})

PRIMARY_FAILOVER_ATTEMPTS = 2  # ~10s of backoff before failing over, not ~130s

def build_gemini_with_fallback(primary: str, fallback: str | None) -> Gemini | FallbackModel:
    """Pro-quota-bound producer model: primary, then ``fallback`` on 429/5xx.
    Empty ``fallback`` disables (kill switch) and returns the plain pinned model."""
    if not fallback:
        return build_gemini(primary)
    return FallbackModel(models=[
        build_gemini(primary, retry_attempts=PRIMARY_FAILOVER_ATTEMPTS),
        build_gemini(fallback),
    ])
```
4. Re-run the tests and expect PASS. Run the full verification loop.
5. Commit: `feat(agent_common): add build_gemini_with_fallback (ADK FallbackModel, global-pinned)`

### Task 1.2: config knob + wire the Pro producers
**Files:** modify `agent_common/config.py`, `creative_agent/agent.py` (lines 63, 140, 388, 488), `interactive_creative/agent.py:58`, and `trend_scout/agent.py:167`. Tests go in `tests/test_config.py` and `tests/test_pipeline_structure.py`.

1. Write failing tests. In `tests/test_config.py`:
```python
def test_critic_fallback_defaults_to_worker_bucket(monkeypatch):
    monkeypatch.delenv("CRITIC_FALLBACK_MODEL", raising=False)
    from agent_common.config import BaseAgentConfiguration

    cfg = BaseAgentConfiguration()
    assert cfg.critic_fallback_model == cfg.worker_model == "gemini-3.8-flash"


def test_critic_fallback_never_uses_campaign_bucket():
    """Keep the PR #101 spread: failover must not land on ALT_GLOBAL_MODEL."""
    from creative_agent.config import ALT_GLOBAL_MODEL, config

    assert config.critic_fallback_model != ALT_GLOBAL_MODEL
```
   In `tests/test_pipeline_structure.py`:
```python
import pytest


@pytest.mark.parametrize("path", [
    "creative_agent.agent:root_agent",
    "creative_agent.agent:visual_generator",
    "creative_agent.agent:combined_report_composer",
    "creative_agent.agent:combined_web_evaluator",
    "interactive_creative.agent:root_agent",
    "trend_scout.agent:root_agent",
])
def test_pro_producers_fall_back_to_worker(path):
    import importlib

    from google.adk.models import FallbackModel

    mod, attr = path.split(":")
    agent = getattr(importlib.import_module(mod), attr)
    assert isinstance(agent.model, FallbackModel)
    assert [m.model for m in agent.model.models] == [
        "gemini-3.1-pro-preview", "gemini-3.8-flash"]
```
2. Run `uv run pytest tests/test_config.py tests/test_pipeline_structure.py -q` and expect FAIL.
3. Implement. Add this to `BaseAgentConfiguration`:
   `critic_fallback_model: str = field(default_factory=lambda: os.getenv("CRITIC_FALLBACK_MODEL", "gemini-3.8-flash"))`. Import `field` alongside `dataclass`.
   Then replace `build_gemini(config.critic_model)` with `build_gemini_with_fallback(config.critic_model, config.critic_fallback_model)` at the six sites. If `ty` flags `Agent(model=...)` typing, note that `Agent.model` accepts `str | BaseLlm`, and `FallbackModel` is a `BaseLlm`.
4. Re-run and expect PASS. Also run `uv run pytest tests/ -q`, because `test_public_api.py` and `test_trend_scout_concurrency.py` touch these agents.
5. Update CLAUDE.md (Configuration → Models), adding one bullet on the `CRITIC_FALLBACK_MODEL` kill switch.
6. Commit: `feat(agents): fall back Pro producers to gemini-3.8-flash on quota 429/5xx`

### Task 1.3: record the judge model on the eval report (no judge fallback)
**Files:** modify `creative_eval/schemas.py` (`CreativeEvaluationReport`) and `creative_eval/evaluate.py` (`evaluate_creatives`). Test in `tests/test_creative_eval.py`.

1. Write failing tests:
```python
def test_report_records_judge_model(monkeypatch):
    from unittest.mock import MagicMock

    import creative_eval.evaluate as ev
    from creative_eval.config import EvalConfig

    monkeypatch.setattr(ev, "_get_client", lambda cfg: MagicMock())
    monkeypatch.setattr(ev, "evaluate_all_concurrently", lambda *a, **k: ([], []))
    ctx = {"brand": "B", "target_product": "P", "target_search_trend": "t"}
    report = ev.evaluate_creatives(ctx, [], [], EvalConfig(eval_model="gemini-x"))
    assert report.judge_model == "gemini-x"


def test_judge_model_defaults_empty_for_old_reports():
    from creative_eval.schemas import CreativeEvaluationReport

    payload = {"brand": "B", "target_product": "P", "target_search_trend": "t",
               "ad_copy_evaluations": [], "visual_concept_evaluations": [],
               "summary": {"total_ad_copies": 0, "ad_copies_passed": 0,
                           "avg_ad_copy_score": 0.0, "total_visual_concepts": 0,
                           "visual_concepts_passed": 0, "avg_visual_score": 0.0,
                           "overall_pass_rate": 0.0, "weakest_dimensions": []}}
    assert CreativeEvaluationReport.model_validate(payload).judge_model == ""
```
2. Run `uv run pytest tests/test_creative_eval.py -q` and expect FAIL.
3. Implement `judge_model: str = Field(default="", description="Model that judged this report.")` and pass `judge_model=config.eval_model` in `evaluate_creatives`.
4. Re-run and expect PASS. Commit: `feat(creative_eval): record judge_model on CreativeEvaluationReport`
5. Deploy follows the normal api deploy runbook and then the traffic pin (Conventions). Agent Engine stays dormant (see memory: redeploy just-in-time).

---

## Section 2 — P4.3: nightly / manual `adk eval` workflow with an efficiency gate

### Verified API facts (`google/adk/evaluation/eval_metrics.py`, `_efficiency_evaluators.py`)
- `PrebuiltMetrics` defines `TOOL_CALL_COUNT_V1="tool_call_count_v1"`, `INFERENCE_CALL_COUNT_V1="inference_call_count_v1"`, `TOKEN_USAGE_V1="token_usage_v1"`, and `INVOCATION_DURATION_V1="invocation_duration_v1"` (seconds).
- They are **informational only**: status is always `EvalStatus.INFORMATIONAL`, and they are "reported automatically on every eval". Listing them in `criteria` with a threshold **raises `ValueError`** (`_reject_threshold`: "Remove it from `criteria` entirely"). So `tests/eval/*.json` stays unchanged, and the gate must be our own script comparing against a stored baseline.
- Values are per-invocation averages ("tokens per turn"). `token_usage_v1` also carries a `details.token_usage_details` breakdown (`input_tokens`, `output_tokens`, `reasoning_tokens`, …). The ADK docs warn that duration is noisy and that the counts should be used to judge regressions.
- Results are written by `LocalEvalSetResultsManager` to `<agents_dir>/<app>/.adk/eval_history/*.evalset_result.json` as an `EvalSetResult`. Parse it with `EvalSetResult.model_validate_json`, which accepts both camel and snake keys.
- *Unverified:* whether `adk eval` exits non-zero on a rubric FAIL. The gate therefore also checks `final_eval_status` itself.

### Gate policy
The baseline lives in `docs/baselines/eval_efficiency.json`, which is machine-readable. `docs/baselines/main.md` gains a section that links to it and records the capture date and commit. Per agent and eval case the gate works as follows:
- **Fail** if `token_usage_v1` is more than 25 % over baseline, if `inference_call_count_v1` or `tool_call_count_v1` is more than 30 % over baseline, or if any case has `final_eval_status != PASSED`.
- **Warn only** when `invocation_duration_v1` is more than 50 % over baseline.

Baselines are refreshed deliberately with `--update-baseline` in a reviewed PR, never automatically.

### Task 2.1: efficiency gate script
**Files:** create `tests/eval/__init__.py` (empty, so `tests.eval` is importable), `tests/eval/efficiency_gate.py` (a CLI with pure functions), `tests/test_eval_efficiency_gate.py`, and `docs/baselines/eval_efficiency.json` (seeded as `{}`).

1. Write failing tests in `tests/test_eval_efficiency_gate.py`:
```python
"""Tests for the adk-eval efficiency regression gate (pure logic, no API calls)."""

from tests.eval.efficiency_gate import compare, extract_metrics

_RESULT = {
    "eval_set_result_id": "r1", "eval_set_id": "trend_scout_evalset",
    "eval_case_results": [{
        "eval_set_id": "trend_scout_evalset", "eval_id": "case_a",
        "final_eval_status": 1, "session_id": "s",
        "overall_eval_metric_results": [
            {"metric_name": "token_usage_v1", "score": 1000.0, "eval_status": 4},
            {"metric_name": "inference_call_count_v1", "score": 10.0, "eval_status": 4},
            {"metric_name": "invocation_duration_v1", "score": 60.0, "eval_status": 4},
        ],
        "eval_metric_result_per_invocation": [],
    }],
}


def test_extract_metrics_reads_informational_scores():
    got = extract_metrics(_RESULT)
    assert got["case_a"]["token_usage_v1"] == 1000.0
    assert got["case_a"]["_passed"] is True


def test_compare_fails_on_token_regression():
    base = {"case_a": {"token_usage_v1": 700.0}}
    failures, warnings = compare(extract_metrics(_RESULT), base)
    assert any("token_usage_v1" in f for f in failures)


def test_duration_regression_only_warns():
    base = {"case_a": {"token_usage_v1": 1000.0, "invocation_duration_v1": 30.0}}
    failures, warnings = compare(extract_metrics(_RESULT), base)
    assert failures == [] and any("invocation_duration_v1" in w for w in warnings)


def test_missing_baseline_is_not_a_failure():
    failures, _ = compare(extract_metrics(_RESULT), {})
    assert failures == []
```
   The numbers `1`/`4` are `EvalStatus.PASSED`/`EvalStatus.INFORMATIONAL`. This is verified in `google/adk/evaluation/eval_metrics.py:47` (`PASSED = 1, FAILED = 2, NOT_EVALUATED = 3, INFORMATIONAL = 4`).
2. Run `uv run pytest tests/test_eval_efficiency_gate.py -q` and expect FAIL (module missing).
3. Implement `efficiency_gate.py` with the following pieces:
   - `TOLERANCES = {"token_usage_v1": 0.25, "inference_call_count_v1": 0.30, "tool_call_count_v1": 0.30}` and `WARN_ONLY = {"invocation_duration_v1": 0.50}`.
   - `extract_metrics(raw: dict)`, which validates through `EvalSetResult.model_validate` and returns `{eval_id: {metric: score, "_passed": bool}}`.
   - `compare(current, baseline) -> (failures, warnings)`.
   - `main()` with argparse for `--agent`, `--baseline`, and `--update-baseline`. It picks the newest file in `<agent>/.adk/eval_history/`, prints a markdown table to `$GITHUB_STEP_SUMMARY` if set, and exits 1 on failures.
4. Re-run and expect PASS. Commit: `feat(eval): add efficiency regression gate over adk eval informational metrics`

### Task 2.2: WIF + eval workflow
**Files:** create `.github/workflows/adk-eval.yml`. Modify `docs/baselines/main.md` and `tests/README.md`.

1. One-time GCP setup, recorded in `deployment/README.md` under a new "Eval CI (WIF)" subsection. Confirm with the user before running it, because it creates IAM resources:
```bash
gcloud iam workload-identity-pools create github --location=global
gcloud iam workload-identity-pools providers create-oidc adk-pipe --location=global \
  --workload-identity-pool=github --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping=google.subject=assertion.sub,attribute.repository=assertion.repository \
  --attribute-condition="assertion.repository=='<OWNER>/adk_pipe' && assertion.ref=='refs/heads/main'"
gcloud iam service-accounts create tt-eval-ci-sa --display-name="adk eval CI"
gcloud iam service-accounts add-iam-policy-binding tt-eval-ci-sa@$PROJECT.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/<OWNER>/adk_pipe"
for r in roles/aiplatform.user roles/bigquery.jobUser; do
  gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:tt-eval-ci-sa@$PROJECT.iam.gserviceaccount.com --role=$r; done
# dataset-scoped bigquery.dataEditor on the EVAL dataset + objectAdmin on the bucket (see open questions)
```
2. Write the workflow. It needs `on: workflow_dispatch` (with an `agent` input: `all|trend_scout|creative_agent`) and `schedule: - cron: "17 9 * * *"`, which is 02:17 PT and off-peak for the shared 5-RPM Pro quota. Set `permissions: {contents: read, id-token: write}` and `concurrency: {group: adk-eval, cancel-in-progress: false}`. Use one job with a matrix over `trend_scout`/`creative_agent` and `max-parallel: 1` (quota), with `timeout-minutes: 45`. Steps:
   - `actions/checkout@v7`
   - `astral-sh/setup-uv@v10.2.0` (uv `0.12.19`, py `3.13`)
   - `google-github-actions/auth@v2` with `workload_identity_provider` and `service_account` taken from repo **variables**. Check the latest major at implementation time.
   - `uv sync --locked`
   - `PYTHONPATH="$PWD" uv run adk eval <agent> tests/eval/evalsets/<agent>_evalset.json --config_file_path=<cfg> --print_detailed_results`, where `<cfg>` is `tests/eval/eval_config.json` for `trend_scout` and `tests/eval/creative_eval_config.json` for `creative_agent`
   - `uv run python tests/eval/efficiency_gate.py --agent <agent> --baseline docs/baselines/eval_efficiency.json`
   - `actions/upload-artifact` of `<agent>/.adk/eval_history/`

   Env comes from repo variables: `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION=global`, `GOOGLE_GENAI_USE_VERTEXAI=true`, `GCP_REGION=us-central1`, `GOOGLE_CLOUD_STORAGE_BUCKET`, and `BQ_*` pointing at an **eval** dataset.
3. Validate with `uv run --with check-jsonschema check-jsonschema --builtin-schema vendor.github-workflows .github/workflows/adk-eval.yml`.
4. After merge, run `gh workflow run adk-eval.yml -f agent=trend_scout`, download the artifact, run the gate locally with `--update-baseline`, and commit the baseline JSON plus a `main.md` section giving the date, commit SHA, and per-case numbers.
5. Commit (workflow): `ci: add nightly/manual adk eval workflow with efficiency gate (WIF auth)`. Commit (baseline): `docs(baselines): seed adk eval efficiency baseline`

---

## Section 3 — P4.4: Cloud Trace / OpenTelemetry

### Verified API facts
- **Agent Engine** (`vertexai/agent_engines/templates/adk.py`, aiplatform 1.165.1): the constructor is `AdkApp.__init__(*, app=None, agent=None, app_name=None, plugins=None, enable_tracing: Optional[bool] = None, …, instrumentor_builder=None)`. `_tracing_enabled()` follows this truth table: `enable_tracing=True` with env unset gives ON, and `enable_tracing=None` with `GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true` gives ON (ADK ≥ 1.17). `enable_tracing=True` **also forces `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=true`**, which puts prompt and response content in spans, and passing `False` triggers a deprecation warning that points users to the env var. Spans are exported over OTLP to `https://telemetry.googleapis.com/v1/traces`, and the needed exporter (`opentelemetry-exporter-otlp-proto-http`) is already installed.
  - **Decision:** add a `--enable_tracing` deploy flag that sets `GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY="true"` in `env_vars` and leaves `AdkApp(enable_tracing=None)`. This is the non-deprecated path, it does not force content capture, and the Console toggle keeps working. `AdkApp(enable_tracing=True)` remains the documented alternative if the env route misbehaves.
- **Cloud Run api** (`google/adk/cli/fast_api.py`): `get_fast_api_app(..., trace_to_cloud: bool = False, otel_to_cloud: bool = False, ...)`.
  - `trace_to_cloud` imports `opentelemetry.exporter.cloud_trace.CloudTraceSpanExporter`, which is **not installed** (only `cloud_logging` and `otlp` exporters are present).
  - `otel_to_cloud` (EXPERIMENTAL) calls `get_gcp_exporters(enable_cloud_tracing=True, enable_cloud_metrics=True, enable_cloud_logging=True)`, which sends OTLP to `telemetry.googleapis.com` and sets the *global* tracer provider. The detached `/runs` `Runner` built in `_runner_factory` should therefore emit spans too. Verify this in step 5 below.
  - ADK defaults `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS` to `true`, so we set it to `false` on the api.
- **IAM/APIs:**
  - Enable `telemetry.googleapis.com` and `cloudtrace.googleapis.com`.
  - Grant `tt-api-sa` the roles `roles/cloudtrace.agent` (a community source says it includes `telemetry.traces.write`; Google's OTLP samples list the narrower `roles/telemetry.tracesWriter`, so grant that too if spans 403), `roles/monitoring.metricWriter`, and `roles/logging.logWriter` (because `otel_to_cloud` also exports metrics and logs).
  - For Agent Engine, the Reasoning Engine service agent `service-$PROJECT_NUMBER@gcp-sa-aiplatform-re.iam.gserviceaccount.com` needs `roles/cloudtrace.agent`. *Unverified* whether it has that by default.

### Task 3.1: Agent Engine telemetry flag
**Files:** modify `deployment/deploy_agent.py` (`ENV_VAR_DICT`, `deploy_agent`, `main` flags). Test in `tests/test_deploy_utils.py`.

1. Write failing tests:
```python
class TestTelemetryEnv:
    def test_tracing_off_by_default(self):
        from deployment.deploy_agent import build_env_vars

        assert "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY" not in build_env_vars()

    def test_tracing_flag_sets_engine_telemetry_env(self):
        from deployment.deploy_agent import build_env_vars

        env = build_env_vars(enable_tracing=True)
        assert env["GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"] == "true"
        # never the reserved vars
        assert "GOOGLE_CLOUD_LOCATION" not in env and "GOOGLE_CLOUD_PROJECT" not in env
```
2. Run `uv run pytest tests/test_deploy_utils.py -q` and expect FAIL.
3. Implement `build_env_vars(enable_tracing: bool = False) -> dict`, which returns `{**ENV_VAR_DICT}` plus the flag. Add an absl flag `--enable_tracing` (default False), thread it into `deploy_agent(name, version, enable_tracing=...)`, and use `"env_vars": build_env_vars(enable_tracing)`.
4. Re-run and expect PASS. Commit: `feat(deploy): opt-in Agent Engine Cloud Trace via GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY`
5. Verify just-in-time with the next batch redeploy. Do not deploy just for this, because the engine is dormant. Run `python deployment/deploy_agent.py --version=v2 --agent=trend_scout --create --enable_tracing`, then `python deployment/test_deployment.py --agent=trend_scout --user_id=test_user`, then Console → Trace Explorer, filtering on `service.name`/`gen_ai.agent.name` to see `invoke_agent` / `call_llm` spans.

### Task 3.2: Cloud Run api `otel_to_cloud` behind an env flag
**Files:** create `runserver/otel.py`. Modify `deployment/async_app.py`. Test in `tests/test_async_runs.py`.

1. Write a failing test:
```python
import pytest


@pytest.mark.parametrize("val,expected", [(None, False), ("", False), ("false", False),
                                          ("0", False), ("true", True), ("1", True)])
def test_otel_to_cloud_flag(monkeypatch, val, expected):
    from runserver.otel import otel_to_cloud_enabled

    if val is None:
        monkeypatch.delenv("ADK_OTEL_TO_CLOUD", raising=False)
    else:
        monkeypatch.setenv("ADK_OTEL_TO_CLOUD", val)
    assert otel_to_cloud_enabled() is expected
```
2. Run `uv run pytest tests/test_async_runs.py -q -k otel` and expect FAIL.
3. Implement `otel_to_cloud_enabled()`, which reads `ADK_OTEL_TO_CLOUD` in `{"1","true","yes"}`. In `async_app.py`, pass `otel_to_cloud=otel_to_cloud_enabled()` to `get_fast_api_app`.
4. Re-run and expect PASS. Commit: `feat(api): opt-in Cloud Trace export for the Cloud Run backend (ADK otel_to_cloud)`
5. Deploy and verify:
```bash
gcloud services enable telemetry.googleapis.com cloudtrace.googleapis.com
for r in roles/cloudtrace.agent roles/monitoring.metricWriter roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:tt-api-sa@$PROJECT.iam.gserviceaccount.com --role=$r; done
# source redeploy per deployment/README.md safe-redeploy recipe, then:
gcloud run services update trend-trawler-api --region=us-central1 \
  --update-env-vars=ADK_OTEL_TO_CLOUD=true,ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false
# PIN TRAFFIC (see Conventions) — then start one trend_scout run from the web UI and
# confirm a trace with the run's invocation spans (including the detached /runs task).
```

---

## Section 4 — P4.5: Model Armor plugin (opt-in safety demo)

### Verified API facts (`google/adk/integrations/model_armor/`)
- The imports are `from google.adk.integrations.model_armor import ModelArmorConfig, ModelArmorPlugin`.
- `ModelArmorConfig(prompt_template_name=None, response_template_name=None, input_blocked_message=…, output_blocked_message=…, block_on_screening_failure=True)` uses `extra='forbid'`, and at least one template is required. Templates are full names of the form `projects/{p}/locations/{loc}/templates/{t}`. Both templates must share one location, and the client uses `modelarmor.{loc}.rep.googleapis.com`.
- The constructor is `ModelArmorPlugin(*, config, name='model_armor_plugin', client=None, credentials=None)`, and it is a `BasePlugin`. `before_model_callback` screens the **latest user-role text** in the request, and `after_model_callback` screens response text. On a block it returns a replacement `LlmResponse` with `custom_metadata={'model_armor_blocked': True}`.
- It requires `google-cloud-modelarmor>=0.7,<1`, which is **not installed**. It is pulled in by the `google-adk[gcp]` extra, but that extra is heavy (spanner, bigtable, …), so we `uv add` the single package instead.
- For wiring, `App(plugins=[...])` is the supported path (`Runner(plugins=)` is deprecated). `AgentTool(include_plugins=True)` is the default, so plugins run on **every** model call in every sub-agent. That would mean dozens of Model Armor calls per creative run, plus blocked replacement text breaking `output_schema` agents. **Decision:** subclass the plugin to screen only the root orchestrator, which is where user input enters and where the final answer leaves.
- Setup needs the `modelarmor.googleapis.com` API enabled, a template created in `us-central1` (`gcloud model-armor templates create …` with the endpoint override `gcloud config set api_endpoint_overrides/modelarmor https://modelarmor.us-central1.rep.googleapis.com/`), and `roles/modelarmor.user` granted to `tt-api-sa` and, for engines, to the Agent Engine service agent.

### Task 4.1: `agent_common/safety.py` + App wiring
**Files:** create `agent_common/safety.py`. Modify `trend_scout/agent.py`, `interactive_creative/agent.py`, `creative_agent/agent.py` (add `app = App(name="creative_agent", root_agent=root_agent, plugins=...)`, non-resumable), and `runserver/async_runs.get_root_agent` (return the creative `app`). Tests go in `tests/test_safety_plugins.py`. The `test_async_runs.py` expectations for creative_agent become App-typed.

1. Run `uv add "google-cloud-modelarmor>=0.7,<1"`, then regenerate `requirements.txt`.
2. Write failing tests in `tests/test_safety_plugins.py`:
```python
"""Model Armor is opt-in: no env -> no plugins; env set -> root-scoped plugin."""

import asyncio
from unittest.mock import MagicMock

T = "projects/p/locations/us-central1/templates/tt"


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MODEL_ARMOR_TEMPLATE", raising=False)
    from agent_common.safety import build_safety_plugins

    assert build_safety_plugins(root_agent_names={"root_agent"}) == []


def test_enabled_builds_scoped_plugin(monkeypatch):
    monkeypatch.setenv("MODEL_ARMOR_TEMPLATE", T)
    from google.adk.integrations.model_armor import ModelArmorPlugin

    from agent_common.safety import build_safety_plugins

    (plugin,) = build_safety_plugins(root_agent_names={"root_agent"})
    assert isinstance(plugin, ModelArmorPlugin)


def test_sub_agents_are_not_screened(monkeypatch):
    monkeypatch.setenv("MODEL_ARMOR_TEMPLATE", T)
    from agent_common.safety import build_safety_plugins

    (plugin,) = build_safety_plugins(root_agent_names={"root_agent"})
    ctx = MagicMock(agent_name="combined_report_composer")
    out = asyncio.run(plugin.before_model_callback(callback_context=ctx, llm_request=MagicMock()))
    assert out is None  # skipped without calling Model Armor


def test_apps_carry_plugin_list(monkeypatch):
    from creative_agent.agent import app as creative_app
    from interactive_creative.agent import app as ic_app
    from trend_scout.agent import app as ts_app

    for a in (creative_app, ic_app, ts_app):
        assert isinstance(a.plugins, list)
```
3. Run `uv run pytest tests/test_safety_plugins.py -q` and expect FAIL.
4. Implement `ScopedModelArmorPlugin(ModelArmorPlugin)`, which takes `root_agent_names: frozenset[str]` and overrides `before_model_callback`/`after_model_callback` to `return None` unless `callback_context.agent_name in root_agent_names`, else delegating to `super()`. Also implement `build_safety_plugins(root_agent_names)`, which reads `MODEL_ARMOR_TEMPLATE` and returns `[]` when unset. Otherwise it uses one template for both prompt and response (optional `MODEL_ARMOR_RESPONSE_TEMPLATE` override) with `block_on_screening_failure` driven by `MODEL_ARMOR_FAIL_CLOSED` (default `true`). Pass `plugins=build_safety_plugins(root_agent_names={root_agent.name})` into each `App`, and add the creative `App`. Update `get_root_agent` and the `_runner_factory` comment in `deployment/async_app.py`. Also update the `deploy_agent.resolve_deploy_target` docstring, because creative_agent now deploys as an App.
5. Re-run and expect PASS. Run the full suite, because `test_deploy_utils.TestResolveDeployTarget`, `test_async_runs`, and `test_public_api` may assert on creative_agent's type. Update those assertions deliberately.
6. Docs: add `MODEL_ARMOR_TEMPLATE` / `MODEL_ARMOR_FAIL_CLOSED` to `.env.example`, and add a CLAUDE.md Configuration bullet.
7. Commit: `feat(safety): opt-in Model Armor plugin scoped to root orchestrators (MODEL_ARMOR_TEMPLATE)`
8. Demo (manual): enable the API, create the template, and grant `roles/modelarmor.user` to `tt-api-sa`. Then deploy the api with `--update-env-vars=MODEL_ARMOR_TEMPLATE=projects/$PROJECT/locations/us-central1/templates/tt-demo` and **pin traffic**. Submit a prompt-injection brand string from the UI, and confirm the blocked message plus `model_armor_blocked` in the session event's `custom_metadata`.

---

## Section 5 — P4.6: Regenerate architecture diagrams after P2

**Dependency:** this is **blocked on P2**, the ADK graph-Workflow migration, which has not been started (`docs/plans/2026-09-28-repo-refresh.md`, Phase 4). Diagrams that show `SequentialAgent`/`ParallelAgent` would be wrong after P2 and are premature before it. Sections 1 and 4 also change the picture: FallbackModel badges on the Pro producers and a Model Armor gate at the root. So regenerate once, after P2 plus whichever of 1/3/4 have merged.

### Task 5.1: regenerate the agent diagrams
**Files:** replace `docs/diagrams/trend_scout_architecture.png` and `docs/diagrams/creative_agent_architecture.png`, and optionally a new `docs/diagrams/observability_safety.png` covering Trace + Model Armor + Fallback. Modify `docs/diagrams/README.md` (table rows) and the CLAUDE.md "Agent Composition" tree.

1. Build a text spec from source, not memory. Dump the post-P2 graph with `uv run python -c "from creative_agent.agent import root_agent; print(root_agent)"`, or from the Workflow graph API P2 introduces, and cross-check against `tests/test_pipeline_structure.py`.
2. Generate **one at a time** with the `paperbanana-figures` skill (PaperBanana MCP `generate_diagram`, which uses `gemini-3.1-flash-image` under the shared 2 RPM cap and a separate Developer-API quota), in the "official Google Cloud documentation" style the README specifies. Refine labels with `continue_diagram(run_id=..., feedback=...)` rather than full regenerations. Check each result with `evaluate_diagram`.
3. Review each PNG visually against the spec. Node names must match real agent names.
4. Update the README table "Highlights" text and the CLAUDE.md tree to the Workflow node names.
5. Commit: `docs(diagrams): regenerate agent architecture diagrams for ADK Workflow (post-P2)`

---

## Open questions / blockers
1. **Eval CI side effects:** `creative_agent` evals write to BQ (`trend_creatives`, `creative_evals`) and GCS. Pointing them at a separate eval dataset needs those tables created there, which overlaps with P4.1 Terraform. Otherwise CI writes into prod tables.
2. **Eval CI quota:** 4 cases at about 5 min each on the shared 5-RPM Pro quota. The nightly run can collide with a batch fan-out. Consider skipping the schedule while the CRF orchestrator is active.
3. **Fallback quality:** should `combined_report_composer` fall back at all, or degrade via its existing `?` optional key? Measure one forced-429 run, e.g. `CRITIC_FALLBACK_MODEL` plus a tiny Pro quota in a scratch project, before deciding.
4. **Unverified items:**
   - `adk eval` exit code on rubric FAIL.
   - Whether `roles/cloudtrace.agent` includes `telemetry.traces.write`; check with `gcloud iam roles describe roles/cloudtrace.agent`.
   - Whether the Agent Engine service agent already has trace write access.
   - Whether `otel_to_cloud` spans cover the detached `/runs` runner.
   - The latest `google-github-actions/auth` major.
   - The Google Developer Knowledge MCP was unavailable during research because of an auth error (`does not support dynamic client registration`). IAM and gcloud facts come from docs.cloud.google.com search results.
5. Model Armor adds two synchronous API calls per root turn. Measure the latency on one run before leaving it enabled in any shared environment.
