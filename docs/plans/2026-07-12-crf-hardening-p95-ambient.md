# CRF Hardening → p95 Data → Ambient Experiment Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task.
> On execution, copy this plan to `docs/plans/2026-07-12-crf-hardening-p95-ambient.md` first.

**Goal:** Land the three open threads from the ambient-agents research in dependency order:
(1) fix Cloud Run Function bugs #45 & #46 (TDD), (2) collect real p95 pipeline-duration data,
(3) run the parallel Ambient-Agent experiment — gated on the p95 verdict.

**Architecture:** Bug fixes require making `cloud_funktions/creative_crf/main.py` importable so the
real functions can be unit-tested (today they can't be imported, so the entrypoints are untested).
p95 data comes from the `AGENT_RUN_DURATION_SECS` log marker (already added, PR #47) once the #45
fix is deployed — #45 must land first or the marker records `PROCESSED` for failed runs and poisons
the dataset. The ambient experiment is a parallel, non-destructive Cloud Run deployment run only if
p95 < ~8 min.

**Tech Stack:** Python 3.13, `uv`, `uvx ruff`, pytest (no pytest-asyncio → drive coroutines with
`asyncio.run`), `functions-framework`, `gcloud run deploy`/Eventarc/Pub/Sub, `adk deploy cloud_run`.

**Standing constraints:** `export PATH="$HOME/.local/bin:$PATH"` before any `uv`/`uvx`; never add
`Co-Authored-By` trailers; commit per-task; only push/PR when asked; do NOT commit the local
PyPI-resolved `uv.lock`.

---

## Context

The ambient-agents research (PR #47, `docs/notes/ambient-agents-vs-cloud-functions.md`) decided to
keep the current CRF + Agent Engine executor and surfaced three follow-ups. While reviewing the
worker we found two real bugs (filed as #45/#46). The p95 duration question is the hard gate for
whether the ambient trigger-endpoint model is even viable (its ~10-min synchronous ceiling vs our
~10-min pipeline). This plan executes all three in the order the dependencies require.

---

# PART 1 — Fix issues #45 & #46 (TDD)

Branch: `fix/crf-worker-hardening` off `main`. PR closes #45 and #46.

### Task 1: Make `cloud_funktions.creative_crf.main` importable

**Why:** `tests/test_crf_logic.py` can't import `main.py` (import-time `vertexai.Client()`, relative
`from .config import config`, no `__init__.py`), so it replicates logic instead of testing it. Real
tests for #45/#46 need the real module. This also removes an import-time GCP/network dependency.

**Files:**
- Create: `cloud_funktions/__init__.py` (empty), `cloud_funktions/creative_crf/__init__.py` (empty)
- Modify: `cloud_funktions/creative_crf/main.py` (module-level client ~lines 78-82; usage in
  `create_agent_run` ~line 199)

**Steps:**
1. Add the two empty `__init__.py` files so `cloud_funktions.creative_crf.main` is an importable
   package (makes the existing relative `from .config import config` resolve under import).
2. Replace the module-level `client = vertexai.Client(...)` with a lazy factory mirroring the
   existing `_get_bigquery_client()`/`_get_pubsub_client()` pattern:
   ```python
   _vertex_client = None
   def _get_vertex_client():
       global _vertex_client
       if _vertex_client is None:
           _vertex_client = vertexai.Client(
               project=config.GOOGLE_CLOUD_PROJECT, location=config.GCP_REGION
           )
       return _vertex_client
   ```
   Update `create_agent_run` to call `remote_agent = _get_vertex_client().agent_engines.get(...)`.
3. Verify import works with no creds:
   `uv run python -c "import cloud_funktions.creative_crf.main; print('OK')"`
4. Run existing suite to confirm nothing broke: `uv run pytest tests/test_crf_logic.py -q`
5. Commit: `chore(cloud_funktions): make creative_crf.main importable (lazy vertex client + package)`

**Risk / fallback:** adding `__init__.py` + keeping the relative import changes package structure;
the deploy loader (`functions-framework` via `gcloud run deploy --source . --function`) must still
resolve it. This is validated for real by the worker redeploy in Part 2. If deploy can't load the
relative import, fallback is absolute `from config import config` + a `sys.path` shim in the test.

### Task 2: Fix #46 — `crf_entrypoint` unguarded `message_payload` / `df`

**Bug:** `message_payload` (assigned only inside the `if "message"…`/`try`, ~line 381) and `df`
(assigned only inside `if message_payload and "bq_dataset"…`, ~line 403) are referenced
unconditionally at ~lines 388 and 408 → `NameError`/`UnboundLocalError` on a malformed/empty
message, which NACKs and causes a redelivery loop.

**Files:** Test: `tests/test_crf_entrypoint.py` (new). Modify: `main.py::crf_entrypoint`.

**Steps (TDD):**
1. Write failing tests importing the real `crf_entrypoint`, passing a fake event
   (`types.SimpleNamespace(data=...)` or `MagicMock(data=...)`), monkeypatching
   `_get_bigquery_client`/`_get_pubsub_client` (MagicMock, per `test_crf_logic.py`'s BQ-mock style):
   - (a) `data={}` (no `message`) → returns cleanly, no exception, no dispatch
   - (b) `message.data` = base64 of non-JSON → returns cleanly
   - (c) valid JSON without `bq_dataset` → returns cleanly
   - (d) valid payload + mocked BQ returning an empty dataframe → no worker publishes
   - (e) valid payload + mocked BQ returning rows → publishes N worker messages (assert
     `pubsub_publisher.publish` call count)
2. Run → assert (a)-(c) currently raise `NameError`/`UnboundLocalError`:
   `uv run pytest tests/test_crf_entrypoint.py -q`
3. Implement: initialize `message_payload = None` and `df = None` at the top of the function;
   restructure so a missing/invalid/non-`bq_dataset` payload logs and `return`s early; guard
   `if df is None or df.empty:`.
4. Run → all pass.
5. Commit: `fix(cloud_funktions): guard crf_entrypoint against malformed trigger messages (#46)`

### Task 3: Fix #45 — `async_send_message` swallows streaming errors → false `PROCESSED`

**Bug:** `except Exception:` at ~line 176 only logs, so `create_agent_run` returns normally and
`_execute_agent_and_update_status` marks the row `PROCESSED` even when the agent run failed (and the
`AGENT_RUN_DURATION_SECS` marker records `status=PROCESSED` for a failed run).

**Files:** Test: `tests/test_crf_worker_async.py` (new). Modify: `main.py::async_send_message`.

**Steps (TDD):**
1. Write failing tests (drive coroutines with `asyncio.run`, per `test_tools_retry.py`):
   - A fake `remote_agent` whose `async_stream_query` raises → assert `async_send_message`
     re-raises (`pytest.raises`).
   - `_execute_agent_and_update_status` with the lock acquired (mock `acquire_processing_lock`→True)
     and the agent run raising → assert the row is updated to `FAILED` (assert on the mocked
     `update_rows_status`/BQ call args) and the exception propagates (NACK).
2. Run → fails (today the error is swallowed; row would be `PROCESSED`).
3. Implement: add `raise` after the `logging.error(...)` in `async_send_message`.
4. Run → passes.
5. Commit: `fix(cloud_funktions): propagate streaming errors so failed runs are marked FAILED (#45)`

### Task 4: Validate + open PR

1. `export PATH="$HOME/.local/bin:$PATH"; uv run pytest tests/ -q` (all pass)
2. `uvx ruff check cloud_funktions/ tests/ && uvx ruff format --check cloud_funktions/ tests/`
3. `uv run python -c "import cloud_funktions.creative_crf.main; print('OK')"`
4. Push branch; open PR with body "Closes #45, Closes #46" summarizing the importability refactor +
   both fixes. **(Only when the user asks to push/PR.)**

---

# PART 2 — Collect p95 pipeline-duration data

**Depends on:** Part 1 merged AND PR #47 (the `AGENT_RUN_DURATION_SECS` marker) merged — both must be
in the deployed worker so the dataset is trustworthy. Requires GCP creds; deploy commands are run by
the user (or via `! <cmd>` in-session). This part is a runbook; the deliverable is measured numbers +
a go/no-go recorded in the design note.

### Task 5: Redeploy the worker with instrumentation + #45 fix
```bash
cd cloud_funktions/creative_crf
gcloud run deploy $CREATIVE_WORKER_CRF_NAME \
  --source . --function $CREATIVE_WORKER_ENTRYPOINT --base-image $BASE_IMAGE \
  --region $GOOGLE_CLOUD_LOCATION --max-instances 100 --timeout 900s --concurrency=1 \
  --memory 8Gi --cpu 4 --no-allow-unauthenticated \
  --labels agent-workflow=trend-trawler,function=creative-worker
```
Confirms Task 1's import change loads under `functions-framework` (the Task 1 fallback trigger).

### Task 6: Generate a sample of runs (≥ ~15-20 for a meaningful p95)
Publish trends to the orchestrator topic (batch of real/test trend rows):
```bash
gcloud pubsub topics publish $CREATIVE_PUB_TOPIC --message "$(cat message.json | jq -c)"
```
(Or accumulate over normal operation.) Each worker run emits one
`AGENT_RUN_DURATION_SECS … status=… secs=…` line.

### Task 7: Compute p50/p95 and record the verdict
Route worker logs to a BigQuery log sink (or use Log Analytics), then run the query from the design
note (`REGEXP_EXTRACT(text_payload, r'secs=([0-9.]+)')` → `APPROX_QUANTILES(…,100)[OFFSET(95)]`).
Update `docs/notes/ambient-agents-vs-cloud-functions.md` with the measured p50/p95 and the
**go/no-go**: p95 < ~8 min → Part 3 is worth running; p95 ≥ ~10 min → stop, keep CRF, close out.
Commit the doc update. **(Decision gate for Part 3.)**

---

# PART 3 — Parallel Ambient-Agent experiment (conditional)

**Gated on:** Task 7 verdict (p95 < ~8 min) AND user confirmation to self-host `creative_agent` on
Cloud Run for the experiment. Non-destructive: runs alongside the CRF path, never touches the
existing topics/functions. Use a **test BigQuery table** so at-least-once dupes (no BQ lock on the
ambient path) don't pollute prod.

### Task 8: Prep `creative_agent` for `adk deploy cloud_run`
- Supply deps: `cp requirements.txt creative_agent/requirements.txt` (ADK auto-gen may miss
  `litellm`, `pillow`, `markdown-pdf`, `google-cloud-bigquery`, `google-cloud-pubsub`) — verify.
- `cp .env creative_agent/.env` (README pattern); confirm `root_agent` discovery
  (`creative_agent.agent.root_agent`) and that `sub_agents/` are packaged.

### Task 9: Deploy the ambient service (new service, trigger endpoints)
```bash
adk deploy cloud_run --project=$GOOGLE_CLOUD_PROJECT --region=$GOOGLE_CLOUD_LOCATION \
  --service_name=creative-ambient-cr --trigger_sources="pubsub,eventarc" \
  --trace_to_cloud creative_agent/
gcloud run services update creative-ambient-cr --region=$GOOGLE_CLOUD_LOCATION --timeout=600
```
Set `ADK_TRIGGER_MAX_CONCURRENT` low (e.g. 2) to bound cost during the experiment.

### Task 10: Wire a separate trigger + shadow traffic
- `gcloud pubsub topics create creative-ambient-topic`
- Create a **push** subscription to `https://<service-url>/apps/creative_agent/trigger/pubsub` with
  the service SA and ack deadline = 600s (max). Note the 10-min push ceiling explicitly.
- Mirror a small fraction of trends (or a dedicated test trend) to `creative-ambient-topic`; leave
  the CRF worker topic untouched.

### Task 11: Observe, compare, decide
Compare over the sample: end-to-end latency vs the 10-min ceiling; timeout/retry behavior (500s →
Pub/Sub redelivery); cost; **duplicate processing** (measure how often at-least-once causes re-runs
without the BQ lock); tracing/observability; operational surface (one service vs two functions).
Append findings + a recommendation (promote ambient / keep CRF / hybrid: Eventarc into the existing
worker-pool) to the design note. **Tear down** the ambient service, topic, and subscription after.

---

## Verification (end-to-end)

**Part 1 (no creds):**
```bash
export PATH="$HOME/.local/bin:$PATH"
uv run pytest tests/ -q
uvx ruff check cloud_funktions/ tests/ && uvx ruff format --check cloud_funktions/ tests/
uv run python -c "import cloud_funktions.creative_crf.main; print('OK')"
```
**Part 2 (creds):** worker redeploys cleanly; `AGENT_RUN_DURATION_SECS` lines appear in logs;
p50/p95 query returns numbers; design note updated with the verdict.
**Part 3 (creds, conditional):** ambient service healthy; shadow events processed; comparison +
recommendation appended to the design note; experiment resources torn down.

## Notes / decisions folded in
- Order is dependency-driven: #45 before p95 (else the duration dataset is poisoned by false
  `PROCESSED`); p95 before the experiment (it's the viability gate).
- `trawler_crf/main.py` is a `# TODO` stub — the scheduled-trawler half of the Q3 target
  architecture is out of scope here (separate future work).
- Idempotency: the ambient path has no built-in dedup; the experiment uses a test table and measures
  the gap rather than re-implementing the BQ lock.
