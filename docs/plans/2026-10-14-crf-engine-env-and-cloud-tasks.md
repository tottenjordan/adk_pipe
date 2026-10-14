# Batch Worker: Engine ID from Env + Cloud Tasks Delivery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: use `executing-plans` / `subagent-driven-development` to carry this out PR by PR. PR 2 is based on PR 1 because both change the worker message shape, so they run **in sequence**. Each PR: TDD, mutation checks, CI green (`lint-type-test` + `cloud-function-deps`), merge, then deploy. Copy this plan into `docs/plans/2026-10-14-crf-engine-env-and-cloud-tasks.md` in PR 1.

**Goal:**
1. The batch worker takes the creative engine from a `CREATIVE_AGENT_ENGINE_ID` env var, not from the trigger message (recommendation #1).
2. The worker is driven by a Cloud Tasks queue with one task in flight and a 30-minute dispatch deadline, instead of a Pub/Sub topic plus an Eventarc push trigger (recommendation #3).

**Architecture:**
- **PR 1:**
  - **Config:** a new `CrfConfig.CREATIVE_AGENT_ENGINE_ID`, accepting the full resource name or a bare id.
  - **Messages:** `agent_resource_id` becomes optional and ignored (a warning when it is present and differs).
  - **Handoff:** `engine_handoff.py apply --update-worker` pushes the env var to `creative-worker-crf`, and refuses while a row is PROCESSING.
- **PR 2:**
  - **Orchestrator:** creates Cloud Tasks HTTP tasks (OIDC to the worker URL, deterministic task names for de-duplication).
  - **Worker:** a new `@functions_framework.http` entry point `agent_worker_task` returns 200 to finish a task or 503 to have Cloud Tasks retry it.
  - **Rollback:** a transport switch `CRF_WORKER_TRANSPORT=pubsub|tasks` keeps the old path for one release.

**Tech stack:** functions-framework 3.10.2, google-cloud-tasks (new pin), google-cloud-bigquery / pubsub, agentplatform 2.4.0, pytest; `gcloud tasks queues`, `gcloud run`.

---

## Context

**Today:**
- **The engine ID travels in the message.** The orchestrator trigger carries `agent_resource_id` (`messages.py:25` `_TRIGGER_FIELDS`). The orchestrator forwards it in every worker message (`orchestrator.py:106-111`), and the worker builds the engine path from it (`worker.py:159-164`). Every engine redeploy changes that ID, so whoever triggers a batch must remember the new one. That's the recurring "CRF trigger must carry the v47 ID".
- **Runs are serialised by bouncing deliveries.** The worker is reached through Pub/Sub plus an Eventarc push subscription. A 600 s ack deadline against runs of up to 1800 s means long runs are redelivered mid-run. `max-instances=1` serialises runs by answering extra deliveries with 429, which uses up delivery attempts (dead-letter cap 20). Review item C5 and `deployment/README.md` §3 "Known limitation".

**After:**
- The engine ID lives in one place, an env var on the worker, updated by the engine handoff.
- A Cloud Tasks queue (`max_concurrent_dispatches=1`, `dispatch_deadline=1800s`) delivers one row at a time, with no 429 bounces and no redelivery during a run. Its own retry policy replaces the Eventarc/dead-letter tuning.

**Facts from exploration:**
- **ID format:** `.env` stores `CREATIVE_AGENT_ENGINE_ID` as a full resource name, but the CRF validates a bare id (`messages._AGENT_ID_RE`). Normalise it in config.
- **Config cache:** new env vars must also go into `config._ENV_NAMES`, the `lru_cache` key.
- **Image:** the Dockerfile CMD hard-codes `--signature-type=cloudevent` for both services. `Dockerfile.dockerignore` is an exact allow-list. `tests/test_creative_fanout_image.py` pins both, plus the root `cloudbuild.yaml` CRF deploy step.
- **Dependencies:** CRF deps live in `cloud_functions/creative_fanout/requirements.txt` **and** the pyproject `dev` group (with `uv.lock`). The root `requirements.txt` drift check excludes dev, and `crf-deps.yml` runs `tests/test_crf_*.py` with only the CRF requirements.
- **Accounts and engine IDs:**
  - The worker currently runs as the default compute SA, which is a project Owner, and Eventarc invokes it with the same SA.
  - The live creative engine is v47, `projects/934903580331/locations/us-central1/reasoningEngines/6183205068745998336`.

## PR 1: `feat/crf-engine-from-env` (about 45 min)

### Task 1.1: config field with normalisation
**Files:** `cloud_functions/creative_fanout/config.py`, `tests/test_crf_config.py`.

1. **Failing tests:**
   - `CREATIVE_AGENT_ENGINE_ID` accepts `6183205068745998336`, or `projects/<PROJECT_NUMBER>/locations/<GCP_REGION>/reasoningEngines/6183205068745998336`, and normalises both to the bare id.
   - Unset or blank gives `""`.
   - A full name with another project number or region, or a bad id, gives `""` and logs one ERROR. Never raise at load.
   - The name is in `_ENV_NAMES`, so it busts the cache.
2. **Run** `uv run pytest tests/test_crf_config.py -q`. Expect a failure.
3. **Implement:** `_engine_id_env()` in `config.py`, a `CrfConfig.CREATIVE_AGENT_ENGINE_ID: str` field, and the `_ENV_NAMES` entry. Reuse the `[A-Za-z0-9_-]{1,128}` rule; move `_AGENT_ID_RE` from `messages.py` into a shared spot in `config.py` and import it back.
4. **Run** the tests. Expect them to pass.
5. **Commit:** `feat(crf): CREATIVE_AGENT_ENGINE_ID config (full name or bare id)`.

### Task 1.2: messages and orchestrator stop requiring or forwarding the id
**Files:** `messages.py`, `orchestrator.py`, tests in `test_crf_hardening.py`, `test_crf_entrypoint.py`, `test_crf_row_state.py`, `test_crf_sql_params.py`.

1. **Failing tests:**
   - The trigger is valid without `agent_resource_id`.
   - A trigger with a malformed one is still accepted, but the value is ignored with a WARNING.
   - The worker payload built by `dispatch` has **no** `agent_resource_id`.
   - `parse_worker_message` returns `(dataset, table, row_data)` and ignores a legacy `agent_resource_id` (in-flight messages during rollout).
2. **Implement:**
   - `_TRIGGER_FIELDS = ("bq_dataset", "bq_table")` and `_WORKER_FIELDS = (*_TRIGGER_FIELDS, "row_data")`.
   - A `_note_legacy_engine_id(payload)` helper that warns when the field is present and doesn't equal `config.CREATIVE_AGENT_ENGINE_ID`.
3. **Update the tests' `_event` / `_worker_payload` helpers.** Run the tests and commit: `refactor(crf): engine id is no longer part of trigger or worker messages`.

### Task 1.3: the worker reads the engine from config
**Files:** `main.py`, `worker.py`, `test_crf_worker_async.py`, `test_crf_time_budget.py`.

1. **Failing tests:**
   - `agent_worker_entrypoint` calls `process_row` with `agent_id=config.CREATIVE_AGENT_ENGINE_ID`.
   - With the env var unset it logs an ERROR and ACKs **before** taking the lock, so the row stays QUEUED for the next trigger. Assert that no BigQuery lock query runs.
2. **Implement:** in `main.agent_worker_entrypoint`, after parsing: `agent_id = config.CREATIVE_AGENT_ENGINE_ID`, and if it's empty, `logger.error(...)` and return. `worker.py` is unchanged; it still takes `agent_id`.
3. **Run** the tests. **Mutation check:** dropping the unset guard must fail a test. Commit.

### Task 1.4: `engine_handoff.py apply --update-worker`
**Files:** `deployment/engine_handoff.py`, `tests/test_deploy_utils.py` (or a new `tests/test_engine_handoff_worker.py`).

1. **Failing tests (subprocess and BigQuery mocked):**
   - `apply --ids f --update-worker` first runs a BigQuery check: `SELECT COUNT(*) … WHERE processed_status='PROCESSING'` on `BQ_DATASET_ID.BQ_TABLE_TARGETS`. If it finds rows it exits non-zero with "worker busy", unless `--force`.
   - Otherwise it runs exactly `gcloud run services update creative-worker-crf --region <GCP_REGION> --update-env-vars CREATIVE_AGENT_ENGINE_ID=<creative resource_name> --quiet` (the service name can be overridden with `--worker-service`).
   - It is a no-op with a message when the ids file has no `creative_agent` record.
2. **Implement it** with `subprocess.run([...], check=True)`, without a shell.
3. **Run** the tests. Commit: `feat(deploy): engine_handoff apply --update-worker`.

### Task 1.5: docs and the plan copy
- **`deployment/README.md`:**
  - the required-env table and note (:38-54): the worker env var;
  - handoff step 2 (:303-305): run `--update-worker` instead of editing `message.json`;
  - rollback (:313);
  - §3.3 worker env, adding `CREATIVE_AGENT_ENGINE_ID`;
  - §5.2: drop `agent_resource_id` from the `message.json` example.
- **Other docs:** the `.env.example` comment, CLAUDE.md (the Worker bullet plus the engine-handoff line), and the `main.py` docstring example.
- **Commit:** `docs(crf): engine id from the worker env`, plus the plan copy.

**Gates:** ruff format/check, ty, `GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 8`, and the clean-venv CRF tests.

### Deploy PR 1
1. Build the CRF image with the root `cloudbuild.yaml`.
2. Deploy the worker only when idle (no PROCESSING rows):
   `gcloud run deploy creative-worker-crf --image <CRF_IMAGE> --update-env-vars CREATIVE_AGENT_ENGINE_ID=projects/934903580331/locations/us-central1/reasoningEngines/6183205068745998336`
3. Deploy the orchestrator with the same image.
4. **Verify:** a trigger **without** `agent_resource_id` and `max_rows=1` reaches PROCESSED. Afterwards the runbook memory no longer needs "CRF trigger must carry the vN ID".

## PR 2: `feat/crf-cloud-tasks` (about 90 min, based on PR 1)

### Task 2.1: dependency
**Files:** CRF `requirements.txt`, `pyproject.toml` (dev group), `uv.lock`.

- Pin `google-cloud-tasks==<latest>`, check it resolves with the existing pins (`uv lock`), and confirm `crf-deps.yml`'s clean-venv install works locally.
- **Commit:** `build(crf): add google-cloud-tasks`.

### Task 2.2: config for the transport
**Files:** `config.py`, `test_crf_config.py`.

**New fields, all in `_ENV_NAMES`:**

| Env var | Default / rule |
|---|---|
| `CRF_WORKER_TRANSPORT` | `pubsub` or `tasks`, default `pubsub` (flip to `tasks` at deploy) |
| `CRF_TASKS_QUEUE` | `creative-worker-queue` |
| `CRF_TASKS_LOCATION` | `GCP_REGION` |
| `CRF_WORKER_URL` | required when the transport is `tasks` |
| `CRF_TASKS_INVOKER_SA` | default: the compute SA built from the project number |
| `CRF_TASK_DISPATCH_DEADLINE_SECS` | default = `WORKER_TIMEOUT_SECS`, clamped 15–1800 (the Cloud Tasks HTTP maximum) |

**Cross-check:** `tasks` without `CRF_WORKER_URL` logs an ERROR and falls back to `pubsub`.

TDD as in Task 1.1.

### Task 2.3: the orchestrator enqueues tasks
**Files:** `orchestrator.py`, `clients.py` (lazy, lock-guarded `get_tasks_client()` like `get_pubsub_client`), `tests/test_crf_tasks_dispatch.py` (new, with a fake client in `tests/crf_fakes.py`).

1. **Failing tests (transport `tasks`):**
   - **One task per selected row,** each `create_task` called with:
     - `parent = projects/<project>/locations/<loc>/queues/<queue>`;
     - `http_request = {http_method: POST, url: CRF_WORKER_URL, headers: {"Content-Type": "application/json"}, body: json({bq_dataset, bq_table, row_data}), oidc_token: {service_account_email: CRF_TASKS_INVOKER_SA, audience: CRF_WORKER_URL}}`;
     - `dispatch_deadline = CRF_TASK_DISPATCH_DEADLINE_SECS`;
     - a deterministic task name `…/tasks/crf-<sha256(uuid, target_trend, processing_attempts or 0)[:32]>`, using `entry_timestamp` for legacy rows without a key.
   - **Rows are marked QUEUED before the first `create_task`** (call order, like M24).
   - **`AlreadyExists` counts as dispatched,** not a failure. An orphan re-query must not double-enqueue the same attempt.
   - **Any other `create_task` error** is counted and logged, leaving the row QUEUED (same as a failed publish today).
   - **A timeout per `create_task`** (`timeout=30`).
   - **Transport `pubsub`** still publishes exactly as before (the existing tests stay green).
2. **Implement:**
   - `dispatch` picks the sender by `config.WORKER_TRANSPORT`, via a small `_send_pubsub(rows)` / `_send_tasks(rows)` split. The `_worker_payload(row_dict)` builder is shared, so the bodies are identical on both transports.
   - The `select_unprocessed` row must include `processing_attempts` for the task name. Add it to the SELECT if it's missing (`status_store.py`).
3. **Mutation checks:** drop the attempt from the name hash; treat `AlreadyExists` as a failure; enqueue before marking QUEUED. Each must fail a test. Commit.

### Task 2.4: the worker's HTTP entry point
**Files:** `main.py`, `messages.py` (`parse_task_request(body: bytes)`, sharing validation with `parse_worker_message`), `tests/test_crf_worker_http.py` (new).

1. **Failing tests**, using functions-framework's test client or a Flask request built directly:

   | Request or outcome | Response |
   |---|---|
   | Malformed JSON, missing fields, unset engine env | **200**, logged (Cloud Tasks must not retry a permanent error) |
   | Lost lock, PROCESSED, FAILED, requeued | **200** |
   | An exception that today leads to a NACK (`raise`): a BigQuery error before the lock, etc. | **503**, so Cloud Tasks retries with its policy |

   - The deadline is still taken at request start (reuse `worker.run_deadline`).
   - Log `X-CloudTasks-TaskName` and `X-CloudTasks-TaskRetryCount` as JSON fields.
   - The existing `agent_worker_entrypoint` (CloudEvent) keeps working unchanged for rollback.
2. **Implement:**
   - `@functions_framework.http def agent_worker_task(request)`, which shares a private `_handle_worker_message(dataset, table, row_data, deadline) -> Outcome` with the CloudEvent entry point, so ACK/NACK becomes 200/503 in one place.
   - Keep `main.py` thin; the shared helper can live in `worker.py`.
3. **Mutation check:** return 200 on the retry path. Commit.

### Task 2.5: the image runs either signature
**Files:** `Dockerfile` CMD, root `cloudbuild.yaml` CRF deploy step, `tests/test_creative_fanout_image.py`, `Dockerfile.dockerignore` if a file is added.

- **CMD:** `--signature-type="${FUNCTION_SIGNATURE_TYPE:-cloudevent}"`.
- **The `cloudbuild.yaml` worker deploy step** sets `--update-env-vars FUNCTION_TARGET=agent_worker_task,FUNCTION_SIGNATURE_TYPE=http`. Only once transport is `tasks` in production; until the cutover, keep the current values and document the switch.
- **Simpler alternative:** the deploy step leaves `FUNCTION_TARGET` / `FUNCTION_SIGNATURE_TYPE` untouched when they are already set on the service. Pick whichever matches how the step is written today.
- **Tests:** update the image tests (the signature env default, the allow-list).
- **Commit.**

### Task 2.6: docs
- **`deployment/README.md` §3,** rewritten around the queue:
  - **queue creation:**
    `gcloud tasks queues create creative-worker-queue --location=us-central1 --max-concurrent-dispatches=1 --max-dispatches-per-second=1 --max-attempts=5 --min-backoff=60s --max-backoff=600s --max-doublings=3`;
  - **IAM:** the orchestrator SA needs `roles/cloudtasks.enqueuer` plus `roles/iam.serviceAccountUser` on the invoker SA; the invoker SA needs `roles/run.invoker` on the worker;
  - **worker deploy:** `FUNCTION_TARGET=agent_worker_task`, `FUNCTION_SIGNATURE_TYPE=http`, `--timeout=1800s`, `--max-instances=1`, `--concurrency=1`, no Eventarc trigger;
  - **orchestrator env:** `CRF_WORKER_TRANSPORT=tasks`, `CRF_WORKER_URL`, `CRF_TASKS_QUEUE`;
  - **rollback:** set the transport back to `pubsub`, restore the worker target/signature, recreate the worker Eventarc trigger;
  - **remove the "Known limitation" section;**
  - **state that the worker topic and its dead-letter topic stay for one release, then get deleted.**
- **Also update:** CLAUDE.md "Event-Driven Orchestration" (the worker is now triggered by Cloud Tasks), `.env.example`, and the ambient note.
- **Commit.**

**Gates:** as in PR 1, plus a clean-venv CRF run with the new requirement.

### Deploy PR 2 (cutover, about 20 min, worker idle)
1. **Infra:**
   - **Queue:** create it with the command above.
   - **Permissions:** grant the roles above (the compute SA already holds them as Owner, but grant them explicitly so a later dedicated SA works).
   - **Worker URL:** read it with `gcloud run services describe creative-worker-crf --format='value(status.url)'`.
2. **Image:** build it with the root `cloudbuild.yaml`.
3. **Worker:** `gcloud run deploy creative-worker-crf --image <CRF_IMAGE> --update-env-vars FUNCTION_TARGET=agent_worker_task,FUNCTION_SIGNATURE_TYPE=http` (keeps the 1800 s timeout and max-instances=1).
4. **Orchestrator:** `gcloud run deploy creative-trawler-crf --image <CRF_IMAGE> --update-env-vars CRF_WORKER_TRANSPORT=tasks,CRF_WORKER_URL=<url>,CRF_TASKS_QUEUE=creative-worker-queue`.
5. **Delete the worker Eventarc trigger** `creative-worker-starter-trigger`, so the worker topic has no subscriber invoking the worker. Keep the topic and the dead-letter topic for rollback.
6. **Verify live:**
   - Publish an orchestrator trigger with `max_rows=2` (no engine id). Both rows reach PROCESSED.
   - `gcloud tasks list --queue=creative-worker-queue` shows one task dispatched at a time.
   - The worker logs show **no** "no available instance" 429s and no redelivery during a run.
   - JSON logs carry the task name and a retry count of 0.
7. **Update memory:** the runbook (queue, transport) and drop the "CRF trigger must carry the vN ID" note.

## Verification (summary)
- **Per PR:** all gates green and the listed mutations killed. The existing CRF suites stay green on the `pubsub` transport.
- **PR 1 live:** a trigger without the engine id is processed, using the engine from the env var.
- **PR 2 live:** a 2-row batch through Cloud Tasks finishes with no 429 bounces and no mid-run redelivery. Rollback is documented and possible with env vars only, plus recreating the trigger.
- **Follow-ups (not in this plan):**
  - delete the worker topic and its dead-letter topic after one quiet release;
  - a dedicated minimal service account for the orchestrator and the task invoker (review item C2);
  - Cloud Scheduler for the orchestrator trigger (recommendation #2).

**Estimated wall clock:** PR 1 ~45 min + deploy ~10 min, then PR 2 ~90 min + cutover ~20 min, so about 2.75 h (sequential by design).
