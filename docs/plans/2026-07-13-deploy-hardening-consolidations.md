# CRF/Agent Fixes → p95 → Deploy-Hardening Consolidations

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task.
> On execution, copy this plan to `docs/plans/2026-07-13-deploy-hardening-consolidations.md` first.

**Goal:** (Part A) fully resolve the two bugs blocking p95 data collection, redeploy, and capture
the p95 number; then land the three structural consolidations that kill the *classes* of bug we hit:
(B) centralized deploy package-bundling, (C) a single config source-of-truth, (D) a session-lifecycle
helper.

**Architecture:** Keep the flat top-level package layout (so `adk web .`, `adk eval <agent>`,
`adk deploy cloud_run <agent>/` and all docs keep working — no `src/` move, no `uv package` flip,
no `uv.lock` churn). Deploy correctness comes from *deriving* the per-agent `extra_packages` list and
the deploy `env_vars` from single sources of truth, and from encapsulating the Agent Engine
create→query→delete session triad so `user_id` cannot drift.

**Tech Stack:** Python 3.13, `uv`, `uvx ruff`, pytest (no pytest-asyncio → drive coroutines with
`asyncio.run`), `functions-framework` / `gcloud run deploy`, Vertex AI Agent Engine, BigQuery/PubSub.

**Standing constraints:** `export PATH="$HOME/.local/bin:$PATH"` before any `uv`/`uvx`; never add
`Co-Authored-By` trailers; `uvx ruff` for lint+format (ruff not in venv); pytest + `ty`; commit
per-task; **only push/PR when the user asks**; do NOT commit the PyPI-re-resolved `uv.lock` or
`.python-version`; models use `GOOGLE_CLOUD_LOCATION=global`, regional resources use `GCP_REGION=us-central1`.

---

## Context

While collecting p95 pipeline-duration data (the gate for the ambient-agent experiment), a 20-row
run produced **zero valid data**: every run failed in ~8–13 s. Root causes:

1. **`my_delete_task` user_id mismatch** — deleted the session with the bare `_USER_ID`
   (`Ima_CloudRun_jr`) though it was created per-row as `Ima_CloudRun_jr_<index>` →
   `FAILED_PRECONDITION: Session <id> does not belong to user …`. **Already fixed + tested** on
   branch `fix/crf-worker-session-user-id` (commit `21ac3fe`, not pushed).
2. **Deployed agent model 404** — the agent called `gemini-3.1-pro-preview` in `us-central1`
   (404; gemini-3.x is served only from `global`). Mechanism: Agent Engine auto-injects
   `GOOGLE_CLOUD_LOCATION=<engine region>=us-central1`, and `deployment/deploy_agent.py`'s
   `ENV_VAR_DICT` has `GOOGLE_CLOUD_LOCATION` **commented out** (lines 34-35), so nothing overrides
   the injected regional value. This is the real p95 blocker.

Exploration also surfaced the structural roots the user asked to consolidate, plus two extra latent
config bugs (`creative_agent` reads env `GCS_BUCKET_NAME`, absent from `.env.example`/deploy → always
`None`; `LOCATION` has no default → `None` in a deployed agent). Decisions taken: packaging =
**centralized mapping only**; config = **merge the two near-identical agent configs into a shared base**.

---

# PART A — Resolve #1 & #2, redeploy, capture p95

Goal: minimal, fast fixes so the pipeline runs end-to-end; get the p95 number. (Parts B/C generalize
these same fixes afterward — this is the quick unblock.)

### Task A1: minimal deploy fixes (#2) — env var + extra_packages
**Files:** `deployment/deploy_agent.py`.
- In `ENV_VAR_DICT` (lines 32-44): uncomment/add `"GOOGLE_CLOUD_LOCATION": os.getenv("GOOGLE_CLOUD_LOCATION")`
  (resolves to `global` from `.env`) and `"GOOGLE_CLOUD_PROJECT": os.getenv("GOOGLE_CLOUD_PROJECT")`.
  This overrides Agent Engine's auto-injected `us-central1` so gemini-3.x resolves to `global`.
- In `deploy_creative_agent` `extra_packages` (line ~152): `["./creative_agent", "./creative_eval"]`
  (this is the fix already committed on branch `fix/creative-agent-deploy-extra-packages`; fold it in
  here rather than juggling that branch).
- Commit: `fix(deployment): pass GOOGLE_CLOUD_LOCATION=global + bundle creative_eval (#2)`.

### Task A2: redeploy worker + agent
- **Worker** (carries the #1 fix — already committed on `fix/crf-worker-session-user-id`; merge/rebase
  that into the working branch first so the deployed source has it):
  ```bash
  cd cloud_funktions/creative_crf
  gcloud run deploy $CREATIVE_WORKER_CRF_NAME --source . --function $CREATIVE_WORKER_ENTRYPOINT \
    --base-image $BASE_IMAGE --region us-central1 --max-instances 100 --timeout 900s \
    --concurrency=1 --memory 8Gi --cpu 4 --no-allow-unauthenticated \
    --labels agent-workflow=trend-trawler,function=creative-worker
  ```
- **Fresh creative_agent** (carries A1): `uv run python deployment/deploy_agent.py --version=v3 --agent=creative_agent --create`
  (writes `CREATIVE_AGENT_ENGINE_ID` to `.env`; note the new reasoning-engine id).
- Verify the new engine reaches `Ready` (not the v1/v2 404 failure).

### Task A3: re-run the 20-row batch + capture p95
- Reset the throwaway table (or make a fresh one): set `processed_status = NULL` on all rows of
  `hybrid-vertex.trend_trawler.target_trends_crf_p95`.
- Publish the orchestrator trigger to `creative-eventarc-topic` with the **new** `agent_resource_id`:
  `{"bq_dataset":"trend_trawler","bq_table":"target_trends_crf_p95","agent_resource_id":"<new id>"}`.
- Wait ~15 min; pull `AGENT_RUN_DURATION_SECS` markers (filter:
  `resource.type="cloud_run_revision" AND resource.labels.service_name="creative-worker-crf" AND textPayload:"AGENT_RUN_DURATION_SECS"`),
  parse `status=`/`secs=`, compute **p50/p95 over PROCESSED rows only**.
- Record measured p50/p95 + the go/no-go verdict (p95 <~8 min → ambient experiment worth running;
  ≥~10 min → keep CRF) in `docs/notes/ambient-agents-vs-cloud-functions.md`. Commit the doc.
- **Cleanup:** drop the `target_trends_crf_p95` test table; delete the broken v2 engine
  `3311903295454314496`.

---

# PART B — Consolidation 1: centralized deploy package-bundling

**Kills:** the "forgot to bundle a sibling package" class (the `No module named 'creative_eval'`
deploy failure). Supersedes Task A1's ad-hoc `extra_packages` edit.

**Files:** `deployment/deploy_agent.py`; test `tests/test_deploy_utils.py` (existing).

**Design:** one source-of-truth mapping derived from the real import graph
(`creative_agent`→`creative_eval`; `interactive_creative`→`creative_agent`+`creative_eval`;
`trend_trawler` standalone; every agent also gets `./agent_common` once Part C lands):
```python
# agent name -> local package dirs to bundle (root pkg first, then its cross-package deps)
AGENT_EXTRA_PACKAGES = {
    "trend_trawler": ["./trend_trawler"],
    "creative_agent": ["./creative_agent", "./creative_eval"],
    "interactive_creative": ["./interactive_creative", "./creative_agent", "./creative_eval"],
}
```
- Both `deploy_trawler`/`deploy_creative_agent` read from the mapping (collapse the near-duplicate
  deploy functions into one `deploy_agent(name, version)` parameterized by the mapping + display/gcs
  names). Add `interactive_creative` to the `--agent` enum (line 53) + a `deploy` branch.
- Add a light validation: assert every listed dir exists before calling `agent_engines.create`.

**Steps (TDD):**
1. Add/extend `tests/test_deploy_utils.py`: assert `AGENT_EXTRA_PACKAGES["creative_agent"]` contains
   both `./creative_agent` and `./creative_eval`; assert the mapping covers every value in the
   `--agent` enum; assert listed dirs exist on disk.
2. Run → fails (mapping doesn't exist yet).
3. Implement the mapping + unified `deploy_agent()` + enum/branch for `interactive_creative`.
4. Run → passes. `uvx ruff check/format`. Import-smoke `deployment/deploy_agent.py`.
5. Commit: `refactor(deployment): derive extra_packages from a single agent→packages map`.

---

# PART C — Consolidation 2: single config source of truth (+ merge the two agent configs)

**Kills:** the deploy env-var drift class (dropped `GOOGLE_CLOUD_LOCATION`) and the ~95% config
copy-paste. Supersedes Task A1's `ENV_VAR_DICT` edit.

**Files:** new `agent_common/__init__.py`, `agent_common/config.py`; modify `trend_trawler/config.py`,
`creative_agent/config.py`, `creative_eval/config.py` (project/location only), `deployment/deploy_agent.py`,
`.env.example`; add `./agent_common` to every entry in `AGENT_EXTRA_PACKAGES` (Part B); tests under `tests/`.

**Design:**
- `agent_common/config.py` defines:
  - A base dataclass `BaseAgentConfiguration` holding the shared fields (`GCS_BUCKET`, `GCS_BUCKET_NAME`,
    `PROJECT_ID`, `PROJECT_NUMBER`, `LOCATION` **with default `"global"`**, the five `BQ_*`, and the
    shared model-name constants). `LOCATION` default fixes the deployed-`None` bug.
  - `REQUIRED_AGENT_ENV_VARS`: the canonical list of env vars a deployed agent needs — including
    `GOOGLE_CLOUD_LOCATION` and `GOOGLE_CLOUD_PROJECT`. **This is the contract `deploy_agent.py`
    builds `ENV_VAR_DICT` from**, so a needed var can't be silently dropped again.
  - A `build_infra_retry(extra_exceptions=())` factory for the shared `INFRA_RETRY` (creative_agent
    passes genai `ServerError`).
- `trend_trawler/config.py` / `creative_agent/config.py`: subclass/compose `BaseAgentConfiguration`;
  keep only their genuine differences (trawler's `SetupConfiguration`; creative_agent's retry extra).
  **Fix the bucket-var bug:** both read `GOOGLE_CLOUD_STORAGE_BUCKET` (was `GCS_BUCKET_NAME` in
  creative_agent → always None).
- `creative_eval/config.py`: source `project_id`/`location` from `agent_common` (keep its own
  eval-specific fields).
- `deployment/deploy_agent.py`: build `ENV_VAR_DICT` from `REQUIRED_AGENT_ENV_VARS`, forcing
  `GOOGLE_CLOUD_LOCATION="global"`.
- `.env.example`: drop the phantom `GCS_BUCKET_NAME` reference confusion (document one bucket var).

**Steps (TDD):**
1. Test `tests/test_config.py` (new/extend): `creative_agent.config.config.LOCATION == "global"` when
   `GOOGLE_CLOUD_LOCATION` unset; both agents resolve the same bucket env var; `REQUIRED_AGENT_ENV_VARS`
   contains `GOOGLE_CLOUD_LOCATION`; `deploy_agent.ENV_VAR_DICT` (or its builder) includes every name
   in `REQUIRED_AGENT_ENV_VARS`.
2. Run → fails.
3. Implement `agent_common`, refactor the three configs + deploy builder; add `./agent_common` to the
   mapping.
4. Run full suite; import-smoke each agent (`uv run python -c "import creative_agent.agent"` etc.);
   `uvx ruff check/format`.
5. Commit: `refactor(config): shared agent_common base + env-var contract for deploy`.

---

# PART D — Consolidation 3: Agent Engine session-lifecycle helper

**Kills:** the `user_id` drift class (today's #1 bug, and the same latent shape in `test_deployment.py`).

**Files:** new `cloud_funktions/creative_crf/session.py` (bundled by `gcloud run deploy --source .`);
modify `cloud_funktions/creative_crf/main.py`, `deployment/test_deployment.py`; extend
`tests/test_crf_worker_async.py`.

**Design:**
```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def agent_session(remote_agent, user_id):
    """Create → yield → delete an Agent Engine session with ONE user_id (no drift)."""
    session = await remote_agent.async_create_session(user_id=user_id)
    try:
        yield session
    finally:
        await remote_agent.async_delete_session(user_id=user_id, session_id=session["id"])
```
- `main.py::create_agent_run`: replace the manual create + `my_delete_task` with
  `async with agent_session(remote_agent, user_id) as session: await async_send_message(...)`.
  Remove `my_delete_task` (or keep as thin wrapper). `user_id` now flows through exactly one place.
- `deployment/test_deployment.py`: apply the same helper *shape* (fix its `my_delete_task` reaching the
  global `args.user_id` — pass `user_id` explicitly; also make its stream error re-raise to match main).
  `deployment/` isn't bundled, so it can import the helper from the repo or use a local equivalent.

**Steps (TDD):**
1. Extend `tests/test_crf_worker_async.py` (reuse the existing fake-remote_agent / `asyncio.run`
   idiom and the `test_session_created_and_deleted_with_same_user_id` precedent): assert the session
   is deleted with the creating `user_id` **even when the stream raises** (delete-on-error via
   `finally`), and that the delete uses the same `session_id`.
2. Run → fails (no helper yet).
3. Implement `session.py`; refactor `create_agent_run`; fix `test_deployment.py`.
4. Run suite; import-smoke `cloud_funktions.creative_crf.main`; `uvx ruff check/format`.
5. Commit: `refactor(cloud_funktions): agent_session context manager (create→query→delete, one user_id)`.

### Task D2: re-validate deploy end-to-end
Redeploy worker + a fresh creative_agent from the fully-consolidated branch and re-run a short
batch (≥5 rows) to confirm the refactors deploy and run clean (bonus: reconfirms p95). Clean up the
test table + any throwaway engine after.

---

## Verification

**No creds (after each Part):**
```bash
export PATH="$HOME/.local/bin:$PATH"
uv run pytest tests/ -q
uvx ruff check . && uvx ruff format --check .
uv run python -c "import cloud_funktions.creative_crf.main; import creative_agent.agent; import trend_trawler.agent; import interactive_creative.agent; print('OK')"
```
**With creds:** worker + fresh agent redeploy cleanly; the deployed agent no longer 404s on
gemini-3.1-pro-preview (Part A); p95 markers show real ~minutes durations with `status=PROCESSED`;
p50/p95 + verdict recorded in the design note.

## Sequencing & PRs
- Part A first (unblocks p95 — the pending deliverable). Then B → C → D (C depends on B's mapping to
  bundle `agent_common`; D is independent).
- One commit per task; **open PRs only when the user asks.** Likely PRs: (1) A1+A2/A3 fixes + p95 doc,
  (2) Part B, (3) Part C, (4) Part D — or a single "deploy hardening" PR if the user prefers.
- The `fix/crf-worker-session-user-id` (#1) and `fix/creative-agent-deploy-extra-packages` branches
  get folded into this work (A1 re-adds the extra_packages fix; the session fix is Part D's basis).
