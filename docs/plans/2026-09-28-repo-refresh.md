# Trend Trawler Repo Refresh — Status Report & Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` (or `subagent-driven-development`) to implement this plan task-by-task.
> On approval, copy this file to `docs/plans/2026-09-28-repo-refresh.md` as the first commit.

**Goal:** Get the ADK → Agent Engine → Cloud Run Functions/PubSub example repo back to a secure, current, CI-guarded, portable state after ~10 weeks dormant, and scope the two big upstream migrations as follow-ups.

**Architecture:** Five phases, each its own branch/PR (squash-merge, matching repo convention): (0) urgent security and deadline fixes, (1) TDD correctness fixes, (2) dependency bumps within current major versions, (3) CI and cleanup, (4) written follow-up proposals. No AgentPlatform-SDK or ADK-Workflow rewrite happens in this plan (your call: fix and bump now, plan the migrations).

**Tech Stack:** Python 3.13 / uv / google-adk 2.x / google-cloud-aiplatform 1.x / google-genai 2.x / pytest; Next.js 16 / React 19 / Vitest; Cloud Run, Cloud Run Functions, Pub/Sub, BigQuery, GCS, Vertex AI Agent Engine (now branded "Agent Runtime").

**Conventions:** Commits must NOT include `Co-Authored-By` trailers, and PRs must not include the "Generated with Claude Code" line (CODE_STANDARDS + standing user rule). Branch off `main`. After any `trend-trawler-api` deploy, explicitly pin traffic to the new revision (a known gotcha).

---

## Part A — Status Report

### Snapshot
- `main` @ `956c559` (#120, 2026-07-20), clean. 0 open issues. 1 open PR (#32, an unsolicited "A2AS certificate" from 2026-02 that looks like spam). 27 stale remote branches, all squash-merged.
- 451 Python tests (35 files) and about 125 Vitest tests (20 files). pytest collects offline in 12s.
- Every Python lower bound equals its locked version (for example adk 2.4.0, genai 2.11.0, aiplatform 1.160.0). `uv.lock` resolves from public PyPI; there is no private mirror anymore.

### Strengths (keep)
- **One source of truth for deploy bundling:** `AGENT_EXTRA_PACKAGES`/`AGENT_DEPLOY_SPECS` plus `validate_extra_packages` (`deployment/deploy_agent.py:64-145`).
- **A shared `agent_common/` layer:** config, `build_infra_retry`, `RetryUntilKeyAgent`, `build_gemini` location pinning, and observability. `collect_degradation_warnings` makes silent degradation visible end to end (eval report, BigQuery, gallery).
- **Hardened fan-out:** BigQuery row lock, orphan recovery, a stale-PROCESSING reaper, and the `agent_session` create→query→delete-on-error context manager.
- **An async-job `/runs` model** that survives disconnects: strong task references, an overall timeout, and a retried terminal marker.
- **Parameterized BigQuery inserts** in the agents, no secrets committed, a curated `creative_agent` facade (`__all__`), strong test density, and good design history in `docs/plans` and `docs/experiments`.

### Weaknesses
- **No Python CI** (no pytest, ruff or ty job). Frontend CI runs tests only (no lint, typecheck or build). No Dependabot. `ruff`/`ty` aren't declared in the dev group, and there's no pytest config or coverage.
- **Deprecations are suppressed:** `SequentialAgent`/`ParallelAgent` are `@deprecated` in ADK 2.x. They're hidden by `ty deprecated="ignore"` and by 17 module-level `warnings.filterwarnings("ignore")` calls, which also swallow every other warning.
- **Dead code and weight:**
  - `cloud_functions/trawler_scheduler/` is empty apart from a TODO.
  - `VEO3_INSTR` and `video_gen_model`/`video_analysis_model`/`max_results_yt_trends` are unused.
  - `litellm` is a declared dependency but never imported.
  - `deploy-to-agent-engine.ipynb` is 1.1 MB and stale (AdkApp, gemini-2.0/2.5).
  - Commented-out code remains in `deploy_agent.py`.
- **Duplication:**
  - `memorize` in `trend_scout/tools.py:41` and `creative_agent/tools.py:50`.
  - `_set_initial_states` in both packages' `callbacks.py`.
  - `pretty_print_event` twice.
  - Three copies of the `_get_gcs_client`/`_get_bigquery_client` pattern.
  - Two bucket env vars (`BUCKET` and `GOOGLE_CLOUD_STORAGE_BUCKET`).
- **Not portable as an example:**
  - `hybrid-vertex`, project number `934903580331` and an engine ID are hardcoded (`cloud_functions/*/config.py:8,12`, `deployment/create_session_engine.py:25`, `main.py:30`).
  - The user ID `"Ima_CloudRun_jr"` is hardcoded.
  - Infrastructure (topics, BigQuery tables, IAM, IAP) is click-ops plus a runbook, with no IaC.
- **Docs drift:**
  - CLAUDE.md says Black (ruff is the standard), claims tests need GCP credentials (clients are now lazy), gives a 900s worker timeout (it's actually 1800s), points at `tools.py` for the genai client (actually `image_tools.py`), and has an outdated agent tree.
  - CODE_STANDARDS §4 references `src/`, §7 bans requirements.txt (which the repo uses), and §8 says ty isn't configured (it is).
  - 18 July plans in `docs/plans` have no status markers.
- **Inconsistent tooling:**
  - The Dockerfile pulls `uv:latest` despite a "pinned" comment.
  - Node is 20 in CI, 22 in Docker and `@types/node ^20`, with no `engines` field or `.nvmrc`.
  - `create_session_engine.py` still uses the legacy module-level `vertexai.agent_engines` API.
  - Cloud Function `requirements.txt` lists the obsolete PyPI `asyncio` package, an unneeded `google-adk`, and unpinned `numpy`/`pytz`/`python-dateutil`/`tzdata`.

### Needs immediate attention
| # | Issue | Why now | Task |
|---|---|---|---|
| 1 | **Next.js 16.2.10 / React 19.2.4 are vulnerable:** July middleware/proxy bypass, SSRF in rewrites and Server Actions, August unauthenticated RCE via AVIF image optimization on self-hosted Linux (fixed only on 16.3.x), a Sep 22 critical out-of-band fix (16.3.6), 16.3.7 due Sep 30; RSC DoS (React <19.2.6). | The web service is self-hosted on Cloud Run. IAP reduces exposure but doesn't fix it. | 0.1 |
| 2 | **gemini-2.5 retirement on Vertex:** projects with no calls since Jul 22 get blocked around **Oct 20, 2026**; shutdown is Jan–Mar 2027. `campaign_researcher` defaults to `gemini-2.5-flash`/`-lite` (`creative_agent/config.py:65-66`). | About 3 weeks left, and the backend has likely been idle. | 0.2 |
| 3 | **Agent Engine deploys drop resumability:** `deploy_agent.py:189-195` deploys bare `root_agent`, but `interactive_creative` (`agent.py:151`) and `trend_scout` (`agent.py:226`) need their `App(resumability_config=…)`. Checkpoints can't pause on Agent Engine; Cloud Run is fine. | Blocks the next batch redeploy (the deployed engine is already stale). | 1.1 |
| 4 | **`/runs` gaps:** no duplicate-run guard (a double POST starts two runs), and `userId` is trusted from the client (any IAP user can poll or resume any session). | Correctness plus multi-user safety. | 1.2, 4 (P3) |
| 5 | **Cloud Function correctness:** f-string SQL takes `dataset`/`table` from the Pub/Sub payload (`main.py:145-152,316`). The worker marks a row FAILED then re-raises (`:301-305`), so the retry does nothing: the redelivery can't get the lock and is acknowledged. | Low exposure (internal topic), but a bad pattern for an *example* repo. | 1.3 |

### Upstream changes since July (web research, 2026-09-28)
- **google-adk 2.10.0** (Sep 25):
  - 2.9 re-runs a *failed* node on resume, so resume side effects must be idempotent.
  - 2.9 confines GCS tools to `local_file_root`, makes `InMemorySessionService` raise `SessionNotFoundError`, and has `VertexAiSessionService` retry 429s.
  - 2.10 stops substituting `${var}` in instructions. I checked: the repo has no `${`, so this is safe.
  - 2.7 moved `pyarrow` to the `bigquery-analytics` extra.
  - New: `FallbackModel` (2.9), eval efficiency metrics and custom metrics (2.8), a Model Armor plugin.
  - Sequential/Parallel/Loop agents are deprecated in favor of graph Workflows, with no removal date or public 3.0 roadmap.
- **google-cloud-aiplatform 2.x** (2.2.0; 2.0.0 was yanked): Vertex AI is now "Gemini Enterprise Agent Platform" and Agent Engine is "Agent Runtime". `vertexai.Client` is deprecated in favor of `agentplatform.Client` (package `google-cloud-agentplatform`), `agent_engines` becomes `runtimes`, and errors are now `GoogleAPICallError` subclasses. The repo's `<2.0.0` pin shields it for now.
- **google-genai 2.25.0.** A future 3.0 drops automatic function calling from `generate_content`, so keep the `<3` pin.
- **Models:**
  - Gemini **3.8 Flash** (stable, Sep 2) and **3.5 Flash-Lite** are Google's recommended picks for new work.
  - 3.5 Flash is stable but now "previous generation".
  - 3.1-pro-preview is still Preview with no GA Pro.
  - 3.1-flash-lite: Gemini API shutdown May 7, 2027.
  - veo-3.1 retirement on Nov 17, 2026 is unverified; the repo doesn't use Veo anyway.
- **Cloud Run Functions:** python313 is supported until 2029. `gcloud functions deploy` still works, but Google now recommends the Cloud Run Admin API path (`gcloud run deploy` + Eventarc), which is what the repo already uses.
- **Tooling:** uv 0.12.19, ruff 0.16.9, ty 0.0.84 (still Beta, so pin it exactly), pytest 9.1.1, Vitest 5.0.2 (major), Tailwind 4.3.3.
- **Unverified:** the exact Vertex model IDs for 3.8 Flash and 3.5 Flash-Lite, and the conflicting gemini-2.5 block date (Oct 16 vs Oct 20). Task 0.2 probes the model IDs live.

---

## Part B — Implementation Plan

### Phase 0 — Urgent (this week)

#### Task 0.1: Frontend security bump
**Files:** `frontend/package.json`, `frontend/package-lock.json`
1. `git checkout -b fix/frontend-security-sept-2026`
2. `cd frontend && npm install next@16.3.6 eslint-config-next@16.3.6 react@19.3.0 react-dom@19.3.0` (keep React pinned exactly, as now).
3. Run `npm test && npx tsc --noEmit && npm run lint && npm run build`. Expected: all pass. If 16.3 changes break the build, fix them in this PR.
4. `npm audit --omit=dev`. Expected: no high or critical findings for `next`/`react`.
5. Commit: `fix(frontend): bump next 16.3.6 + react 19.3.0 for Jul–Sep 2026 security releases`.
6. Open the PR, merge, and redeploy `trend-trawler-web` using the safe-redeploy recipe (preserve env vars and IAP).
7. **On/after Sep 30:** repeat steps 2–6 with `next@16.3.7`.

#### Task 0.2: Model-lineup refresh (retire gemini-2.5)
**Files:** `agent_common/config.py:55-61`, `creative_agent/config.py:33,62-100`, `creative_agent/sub_agents/campaign_researcher/agent.py:25,45,104`, `tests/test_config.py`, CLAUDE.md config section, `experiments/README.md`

**Design:** move to the current generation and keep the #101 per-base-model quota spread without any gemini-2.5:
- trend half: `worker_model` = Gemini 3.8 Flash, `lite_planner_model` = Gemini 3.5 Flash-Lite
- campaign half (new default arm): Gemini 3.5 Flash for both planner and worker. That's a distinct global base-model bucket, the same shape as the existing `global_altbucket` arm.
- `critic_model` and the `creative_eval` judge stay on `gemini-3.1-pro-preview`, since there's no GA Pro. A prior A/B rejected 2.5-pro as a softer grader.
- The image model stays `gemini-3.1-flash-image`. Delete the unused video fields.

1. Branch `feat/model-lineup-2026-09`.
2. **Probe the IDs:** `uv run python -c "from google import genai; c=genai.Client(vertexai=True,location='global'); [print(m, c.models.generate_content(model=m, contents='ping').text[:20]) for m in ['gemini-3.8-flash','gemini-3.5-flash-lite','gemini-3.5-flash']]"`. Expected: three replies. If one returns 404, look up the real ID with `c.models.list()` and use that below.
3. **Write failing tests** in `tests/test_config.py`:
```python
from creative_agent.config import ResearchConfiguration

def test_default_campaign_arm_uses_no_retiring_models():
    lite, worker, loc = ResearchConfiguration().campaign_models()
    assert not lite.startswith("gemini-2.5") and not worker.startswith("gemini-2.5")
    assert loc == "global"

def test_campaign_default_bucket_differs_from_trend_bucket():
    cfg = ResearchConfiguration()
    _, worker, _ = cfg.campaign_models()
    assert worker not in {cfg.worker_model, cfg.lite_planner_model}

def test_unknown_arm_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CAMPAIGN_RESEARCH_PLACEMENT", "bogus")
    cfg = ResearchConfiguration(campaign_research_placement="bogus")
    assert cfg.campaign_models() == ResearchConfiguration().campaign_models()
```
4. Run `uv run pytest tests/test_config.py -v`. Expected: the first test FAILS (the default is `regional_25`).
5. **Implement:**
   - Set `ALT_GLOBAL_MODEL = "gemini-3.5-flash"`.
   - Set `worker_model="gemini-3.8-flash"` and `lite_planner_model="gemini-3.5-flash-lite"`.
   - Remove the `regional_25` arm and the `regional_*` fields, and change the fallback plus the `CAMPAIGN_RESEARCH_PLACEMENT` default to `global_altbucket`.
   - Delete `video_gen_model`, `video_analysis_model` and `max_results_yt_trends`.
   - Update the comments and docstrings, plus the stale `regional_25` references in `campaign_researcher/agent.py`.
6. `uv run pytest tests/ -q`. Expected: all pass. Fix any `test_pipeline_structure.py` assertions that hardcode model names.
7. **Validate live:** do one headless run per agent (`deployment/headless_run.py`), then run the creative eval from the CLAUDE.md command. Compare latency and pass rate against `docs/baselines/main.md`, and record the result in `docs/baselines/main.md`.
8. Commit: `feat(models): move to gemini-3.8-flash/3.5-flash-lite; retire gemini-2.5 campaign arm`.
9. Open the PR, merge, and redeploy `trend-trawler-api`, then **pin traffic** to the new revision. Remove any stale `CAMPAIGN_RESEARCH_PLACEMENT` env var from the service.

### Phase 1 — Correctness fixes (TDD)

#### Task 1.1: Deploy the resumable `App` to Agent Engine
**Files:** `deployment/deploy_agent.py:183-195`, `tests/test_deploy_utils.py`
1. Branch `fix/agent-engine-deploy-app`.
2. **Failing test** (a pure helper, so no `vertexai.Client` import is needed; follow the file's existing copy/import pattern):
```python
import types
from deployment.deploy_agent import resolve_deploy_target

def test_prefers_resumable_app_when_module_exports_one():
    mod = types.SimpleNamespace(root_agent="agent", app="app")
    assert resolve_deploy_target(mod) == ("app", "app")

def test_falls_back_to_root_agent():
    mod = types.SimpleNamespace(root_agent="agent")
    assert resolve_deploy_target(mod) == ("agent", "agent")
```
3. Run the tests. Expected: FAIL with ImportError.
4. **Implement:** `def resolve_deploy_target(module) -> tuple[str, object]` returns `("app", module.app)` when the module has `app`, else `("agent", module.root_agent)`. In `deploy_agent`, build `AdkApp(**{kind: target})` from `vertexai.agent_engines` (the installed 1.160 accepts `app=`, per `templates/adk.py:677`) and pass that to `agent_engines.create(agent=...)`. Delete the commented-out line.
5. Run the tests. Expected: PASS. Then run the whole suite.
6. Commit: `fix(deploy): deploy resumable App (not bare root_agent) to Agent Engine`.
7. **Also:** derive the `--agent` choices in `deployment/test_deployment.py:57` and `deployment/integration_test.py:556` from `AGENT_DEPLOY_SPECS`, which adds `interactive_creative`. Separate commit.
8. **Just-in-time follow-through:** the next batch fan-out needs an Agent Engine redeploy anyway (the engine is stale and dormant). Validate with `integration_test.py --check smoke --agent interactive_creative` then.

#### Task 1.2: Duplicate-run guard on `/runs`
**Files:** `runserver/async_runs.py:173,274-300`, `tests/test_async_runs.py`
1. **Failing test:** start a run on `(app, user, session)` with a runner stub that blocks on an `asyncio.Event`. A second `start_run` on the same key should raise the new `RunAlreadyActive` exception, and the router should map it to HTTP 409. After the event is set and the task finishes, a third start should succeed. Reuse the existing stubs and fixtures in `test_async_runs.py`.
2. Run the test and confirm it FAILS.
3. **Implement:** add an `_ACTIVE_RUNS: dict[tuple[str, str, str], asyncio.Task]` next to `_BACKGROUND_TASKS`. Check it in `start_run` and in the resume path (`:457`), register the task there, and remove the key in the task's done-callback. This is single-process only, which is fine with `--min-instances 1`; say so in the docstring.
4. Run the tests (expect PASS), then commit: `fix(runserver): reject concurrent runs on the same session (409)`.

#### Task 1.3: Cloud Function SQL + retry semantics
**Files:** `cloud_functions/creative_fanout/main.py:145-152,292-305,316`, `tests/test_crf_logic.py`, `tests/test_crf_worker_async.py`
1. **Failing tests:**
   - (a) `update_rows_status` / `_build_lock_sql` reject a `table` outside an allow-list built from config (`BQ_TABLE_TARGETS`) with `ValueError`.
   - (b) the generated SQL has no interpolated timestamps or IDs and uses `bigquery.ScalarQueryParameter`/`ArrayQueryParameter`. Assert on `job_config.query_parameters`.
   - (c) after the worker marks a row FAILED, it returns normally instead of re-raising, so the message is acknowledged. Only unexpected errors *before* the status write still re-raise.
2. Run them and confirm they FAIL.
3. **Implement:** validate identifiers against the allow-list and backtick-quote them. Move all values into query parameters. At `:301-305`, log and return after a successful FAILED write, and fix the comment. The reaper still handles PROCESSING rows that stay stuck.
4. Run `uv run pytest tests/test_crf_*.py -v`. Expected: PASS.
5. Commit: `fix(crf): parameterize BQ SQL, allow-list tables, stop no-op worker retries`.
6. Redeploy the worker and orchestrator (runbook in `deployment/README.md`) and confirm the triggers are intact.

### Phase 2 — Dependency updates (in-major, one PR per ecosystem)

#### Task 2.1: Python
**Files:** `pyproject.toml`, `uv.lock`, `requirements.txt`, `cloud_functions/creative_fanout/requirements.txt`, `Dockerfile`
1. **Pre-bump audit for ADK 2.9/2.10:**
   - Confirm interactive resume side effects are idempotent when a failed node re-runs. Check `runserver/async_runs.merge_visual_concept_edits` and the GCS/BigQuery export tools (for example, whether a re-run double-inserts into `trend_creatives`).
   - Grep for `InMemorySessionService` get-session-returns-None handling (`deployment/headless_run.py`, tests).
   - `${` in prompts: already checked, none.
2. Edit `pyproject.toml`:
   - `google-adk[eval]>=2.10.0,<3`, `google-genai>=2.25.0,<3`, `google-cloud-aiplatform[agent-engines]>=<latest 1.x>,<2`. Find the latest 1.x with `uv pip index versions` or `uv lock --upgrade-package`.
   - Refresh the other floors with `uv lock --upgrade`.
   - Remove `litellm` (unused).
   - Add upper bounds to `plotly`/`kaleido`.
   - Add `google-cloud-storage` explicitly (it's used directly).
   - Dev group: `pytest>=9.1.1`, `pytest-asyncio` if tests need it, `ruff==0.16.9`, `ty==0.0.84` (exact, since ty is Beta).
3. `uv lock --upgrade && uv sync`, then `uv run pytest tests/ -q`. Expected: all pass. Triage new deprecation warnings.
4. Regenerate the deploy file: `uv export --no-dev --no-hashes -o requirements.txt` (match the existing header flags).
5. **Cloud Function requirements:** match the lock pins, remove `asyncio` and `google-adk`, and pin `numpy`/`python-dateutil`/`pytz`/`tzdata`. Verify with `uv pip compile cloud_functions/creative_fanout/requirements.txt` in a scratch venv plus `uv run pytest tests/test_crf_*.py`.
6. `Dockerfile`: replace `ghcr.io/astral-sh/uv:latest` with `ghcr.io/astral-sh/uv:0.12.19`.
7. Commit: `chore(deps): adk 2.10, genai 2.25, aiplatform latest 1.x; drop litellm; pin tooling`.
8. Deploy api (pin traffic) and the CRF functions, and smoke-test one interactive run through all 3 checkpoints on Cloud Run.

#### Task 2.2: Frontend
**Files:** `frontend/package.json`, `frontend/package-lock.json`, `frontend/.nvmrc`, `.github/workflows/frontend-tests.yml`
1. Node 22 everywhere: add `"engines": {"node": ">=22"}` and a `.nvmrc` of `22`, change CI `node-version` to `22`, and set `@types/node` to `^22`.
2. Minor bumps: `npm update` (tailwind 4.3, typescript 5.9.x, testing-library). Run `npm test && npm run build`, then commit.
3. **Vitest 5 (major, separate commit):** `npm i -D vitest@5 @vitejs/plugin-react@latest`, read the migration notes, then `npm test`. Expected: about 125 tests pass. Commit.

### Phase 3 — CI & hygiene

#### Task 3.1: Python CI + stronger frontend CI
**Files:** create `.github/workflows/python-ci.yml`; modify `.github/workflows/frontend-tests.yml`, `pyproject.toml`
1. First confirm tests pass with no credentials: `env -u GOOGLE_APPLICATION_CREDENTIALS CLOUDSDK_CONFIG=/tmp/none uv run pytest tests/ -q`. Fix any test that calls `google.auth.default()` (`test_async_runs.py`) by monkeypatching it in a new `tests/conftest.py` autouse fixture.
2. **Workflow:** `astral-sh/setup-uv` pinned by version, then `uv sync --locked`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pytest tests/ -q`. Triggers: push/PR to `main` on `**.py`, `pyproject.toml`, `uv.lock`.
3. The first ruff/ty run will be noisy. Land `ruff format` as one mechanical commit, and add `[tool.ruff.lint] select = ["E","F","I","UP","B"]` with fixes (`ruff check --fix`), triaging the rest.
4. **Frontend workflow:** add `npm run lint`, `npx tsc --noEmit` and `npm run build` steps.
5. Commit: `ci: add Python lint/type/test workflow; frontend lint+typecheck+build`.

#### Task 3.2: Dependabot
Create `.github/dependabot.yml`: weekly updates for `uv` (root), `pip` (`/cloud_functions/creative_fanout`), `npm` (`/frontend`), `github-actions` and `docker`, grouping minor and patch updates per ecosystem. Commit: `ci: add dependabot`.

#### Task 3.3: Stop hiding warnings
Remove the 17 module-level `warnings.filterwarnings("ignore")` calls (`grep -rn 'filterwarnings("ignore")'`). Add a single targeted filter in `agent_common/__init__.py` for the known ADK Sequential/ParallelAgent deprecation (`category=DeprecationWarning, message=".*Workflow.*"`), with a comment linking to proposal P2. Run pytest and check that remaining warnings are real. Commit: `refactor: replace blanket warning suppression with one targeted ADK filter`.

#### Task 3.4: Dead code & dedup
1. Delete:
   - `cloud_functions/trawler_scheduler/`
   - `VEO3_INSTR`
   - the commented-out code in `deploy_agent.py` and `gcs_tools.py:51`
   - `deploy-to-agent-engine.ipynb`, which is superseded by `deploy_agent.py`. **Confirm with you before deleting**, since it may be referenced by a blog post.
2. Move `memorize` and the shared part of `_set_initial_states` into `agent_common/state.py`, and the lazy `_get_gcs_client`/`_get_bigquery_client` into `agent_common/clients.py`. Re-point the importers, keeping the `creative_agent` facade (`creative_agent/__init__.py`) intact, and add or move tests (`tests/test_tools.py`, `tests/test_callbacks.py`).
3. Collapse `BUCKET` into `GOOGLE_CLOUD_STORAGE_BUCKET` by deriving the `gs://` form. Update `.env.example`.
4. One commit per bullet. Run the full suite after each.

#### Task 3.5: Make it a portable example
Replace the hardcoded `hybrid-vertex`, project number, engine ID (`cloud_functions/*/config.py`, `deployment/create_session_engine.py`, the `main.py:30` docstring) and the worker user ID with env vars, and document them in `.env.example` and `deployment/README.md`. Port `create_session_engine.py` to `vertexai.Client().agent_engines` for consistency with the other scripts. Commit: `refactor: remove hardcoded project identifiers`.

#### Task 3.6: Docs refresh
- **CLAUDE.md and READMEs:**
  - ruff, not Black (also update `.vscode/settings.json`).
  - Fix the test-credential claim.
  - Worker timeout is 1800s.
  - The genai client lives in `image_tools.py`.
  - Refresh the agent tree: `RetryUntilKeyAgent`, `RunIfAgent`, `visual_concept_reviser`, trend_scout `review_trends` + `App`.
  - New model lineup.
  - Add an "Agent Engine = Agent Runtime" naming note.
- **CODE_STANDARDS:** fix §4 (`ty check`, no `src/`), §7 (document the exported deploy `requirements.txt` as an allowed exception) and §8.
- **`docs/plans`:** move the July plans to `docs/plans/archive/`, adding a `Status: shipped (#PR)` line to each.

#### Task 3.7: GitHub housekeeping (outward-facing; confirm before running)
- `gh pr close 32 --comment "Closing: unsolicited, out of scope."`
- Delete the 27 merged remote branches: list them with `git branch -r --merged` (squash merges won't show there), cross-check each against merged PRs with `gh pr list --state merged --json headRefName`, then run `git push origin --delete <b>` for each one confirmed.

### Phase 4 — Follow-up proposals (write-ups only; each becomes its own plan)
- **P1. AgentPlatform SDK migration (aiplatform 2.x):**
  - Move `vertexai.Client().agent_engines` to `agentplatform.Client().runtimes` in `deploy_agent.py`, `test_deployment.py`, `integration_test.py` and `cloud_functions/creative_fanout/main.py:83-210`.
  - Move sessions to `client.sessions`.
  - Update error handling to catch `GoogleAPICallError`.
  - Spike: deploy `trend_scout` to a scratch runtime, try `source_packages` deploy (drops the staging bucket), and measure the cold-start difference.
- **P2. ADK graph-Workflow migration:** start with the smallest case, trend_scout's single `SequentialAgent` (`trend_scout/agent.py:114`), and check parity against `tests/test_pipeline_structure.py` plus the trend_scout evalset. Then convert `creative_agent`'s Parallel research, and only then the interactive resume path, which needs the 2.9 idempotency rules. Wait until ADK publishes a removal timeline, or do it as the repo's "ADK 2 showcase" milestone.
- **P3. Per-user authorization for `/runs`:** have the Next.js `/api/adk` proxy forward the IAP-verified identity (`X-Goog-Authenticated-User-Email` / the IAP JWT) as `userId`, and make the backend ignore the client-supplied `userId`.
- **P4. Showcase enhancements, in priority order:**
  1. **Terraform** (or a `gcloud` bootstrap script) for topics, BigQuery tables, service accounts, IAM, the sessions engine and Cloud Run services. This is the biggest gap in an "end-to-end example".
  2. ADK **`FallbackModel`** on quota-bound producers. This directly addresses the documented 5-RPM Pro / 2-RPM image quota ceilings.
  3. A nightly or manual `adk eval` GitHub workflow using ADK 2.8 efficiency metrics (tokens, latency) as a regression gate.
  4. Cloud Trace/OpenTelemetry via `AdkApp(enable_tracing=True)` once Task 1.1 lands.
  5. Model Armor plugin as a safety demo.
  6. Regenerate the architecture diagrams after P2.

---

## Verification (end to end, after Phases 0–3)
1. `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest tests/ -q`: all green, with no credentials required.
2. `cd frontend && npm run lint && npx tsc --noEmit && npm test && npm run build`: green. `npm audit --omit=dev` shows no high or critical findings.
3. Both GitHub workflows are green on the PRs, and Dependabot opens its first grouped PRs.
4. **Cloud Run (live):** start `creative_agent` and `interactive_creative` runs from the web UI, pass all three checkpoints, reload mid-run (it should replay), and double-click Start (expect a 409). The results page shows the gallery, PDF and eval report.
5. `grep -rn "gemini-2.5" --include=*.py agent_common creative_agent trend_scout creative_eval` returns nothing.
6. **Batch path (just-in-time):** redeploy the Agent Engine agents with the `App` fix, then run `integration_test.py --check all` and publish one orchestrator message. Expect one `creative_evals` row with no PROCESSING rows left stuck.

**Housekeeping after approval (outside plan mode):** update stale memories. The dependency-mirror note is wrong (the lock now resolves from PyPI), and the work-status board needs this plan.
