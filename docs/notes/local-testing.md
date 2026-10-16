# Local testing notes

Practical, non-obvious things about running/validating the agents locally.
First written 2026-07-12 on the `creative-eval` branch (ADK 2.1.0); refreshed
2026-10-12 for the async-job `/runs` model, initialState seeding and the
current eval concurrency (ADK 2.10).

## Local backend: the `async_app` launcher, not bare `adk api_server`

The frontend's run page kicks off a run with `POST /runs/{app_name}` and then
**polls** `GET /runs/{app_name}/{user_id}/{session_id}?since=N` (resume:
`POST …/resume`). Only the custom launcher `deployment/async_app.py` mounts that
router (on top of ADK's canned session/artifact CRUD), so run it under uvicorn:

```bash
TRUST_CLIENT_USER_ID=1 ALLOW_ORIGINS=http://localhost:3000 \
  uv run uvicorn deployment.async_app:app --port 8000
cd frontend && npm install && npm run dev   # http://localhost:3000
```

- `TRUST_CLIENT_USER_ID=1` is required locally (the default authz mode is
  `enforce`, which returns 401 without a proxy-asserted `X-TT-User`); the app
  refuses the flag on Cloud Run (`K_SERVICE` set).
- Sessions default to ADK's per-agent local SQLite store
  (`agents/<app>/.adk/session.db`). The agents are real directories under
  `agents/`, so ADK's path check passes; the old `SESSION_SERVICE_URI=memory://`
  workaround (needed while `agents/` held symlinks) is gone
  (`tests/test_agents_dir.py` creates a session this way).
- The canned `/run`, `/run_sse` and `/run_live` routes are blocked (404) by
  `runserver/authz.py`; there is no browser-held SSE stream any more.

**The old ~12-min UI timeout is gone.** Before the async-job model (2026-07-15,
`docs/plans/archive/2026-07-15-async-job-run-model.md`) a full run was a single
long-lived `POST /run_sse`, and the Cloud Workstations / proxy HTTP timeout
(~12 min) cancelled it during the silent eval phase. Now `/runs` starts a
**detached `asyncio` task** decoupled from the HTTP request, bounded only by
`RUN_MAX_SECONDS` (default 1800). A run survives disconnect, reload and IAP
re-auth; the poll re-reads the persistent session log. Status comes only from
the terminal `__run_status` marker event (done/error); contained step errors
show up as non-fatal `warnings` in the poll payload (#322).

## Eval concurrency

`creative_eval` scores each creative with an independent judge call on a
`ThreadPoolExecutor` (`evaluate_all_concurrently`, `creative_eval/evaluate.py`).
The pool size comes from `EvalConfig.workers_for` (`creative_eval/config.py`):

- mixed ad-copy + visual batches use `EVAL_MAX_WORKERS` (default **2**, the
  sustained-rate cap under the 5-RPM Pro quota);
- the visuals-only batch left after early ad-copy judging (in `creative_agent`,
  ad copies are judged next to `visual_production_pipeline`) runs as one wave of
  up to `EVAL_MAX_VISUAL_WAVE_WORKERS` (default **4**).

(The 2026-07 note's `max_eval_workers=12` is long gone: it tripped the Pro
quota.) The per-creative `evaluate_ad_copy` / `evaluate_visual_concept` take a
shared `client` (one per pool); test mocks must accept it.

## Headless runner (`deployment/headless_run.py`)

Still useful to drive `creative_agent` through a local ADK `Runner` with no HTTP
layer (`uv run python deployment/headless_run.py`):

- `Runner(app=creative_agent.app, app_name="creative_agent",
  session_service=InMemorySessionService(),
  artifact_service=FileArtifactService(root_dir=".adk/artifacts"))`.
  `FileArtifactService` lives at `google.adk.artifacts.file_artifact_service`
  and takes a single `root_dir`.
- **Seed campaign state up front (since #263, 2026-10-07).** Callers (frontend
  `createSession` initialState, the CRF worker, `test_deployment.py`) seed
  `brand`, `target_product`, `target_audience`, `key_selling_points`,
  `target_search_trends` (plus optional visual-intent keys, `reference_images`,
  `person_reference`, `learn_from_ratings`, `trend_detected_on`,
  `trend_scout_context`). `creative_agent/callbacks._set_initial_states` only
  `setdefault`s them, so a seeded value wins; the root `memorize`s just the
  missing ones and the kickoff message is a readable echo. **Don't** seed the
  `config.state_init` marker key itself: its presence skips the one-time seeding
  (`gcs_bucket`, `gcs_folder`, `agent_output_dir`, date keys, style shortlist).
  `headless_run.py` itself still starts from `state={}` and passes the campaign
  in the message, which also works.
- Sessions are **in-memory per process**, so a headless run will NOT appear in a
  separately running backend's `/results/[sessionId]` UI. Verify via stdout +
  GCS + BigQuery instead.
- Sub-agent / node tool calls (e.g. `generate_image` inside
  `visual_production_pipeline`, which the root reaches through the single
  `creative_pipeline` NodeTool call) don't appear as function-call events at the
  top level of `runner.run_async`. To verify exactly-once rendering, read
  `generated_images` in state or count the `generate_image` log lines.

## Where results actually land (state vs return value)

The root calls one `creative_pipeline` workflow, ending in `finalize_pipeline`
(`creative_agent/finalize.py`), whose nodes record every outcome in session
state (a failed step leaves `<key>__issues` instead), so validate by reading
state:

- `research_report_gcs_uri` — research PDF (saved inside `combined_research_pipeline`).
- `creative_evaluation_report` — the judge's report
  (`creative_evaluation_report__retry_exhausted` when there is none).
- `eval_report_gcs_uri` / `creative_gallery_gcs_uri` — the eval JSON and HTML gallery in GCS.
- `creative_row_uuid` / `eval_bq_row_uuid` — the `trend_creatives` / `creative_evals` rows.
- `generated_images` — `{concept: {gcs_uri, artifact_key, attempts, qa, …}}`
  (`image_qa__issues` for images still failing the post-render check).
- `finalize_done` — set by the terminal node on every path (runserver's
  auto-continue completion key).

`deployment/headless_run.py` prints these keys and exits non-zero unless the
smoke test's output check (`integration_test.check_agent_outputs`) passes.

## GCS / BigQuery layout for a run

- GCS output: `gs://{GOOGLE_CLOUD_STORAGE_BUCKET}/{gcs_folder}/{agent_output_dir}/`
  where `agent_output_dir="creative_output"` and `gcs_folder` is
  `YYYY_MM_DD_HH_MM_<4hex>` (set at state init).
- The bucket name comes from `GOOGLE_CLOUD_STORAGE_BUCKET` (what deploy ships;
  `agent_common/config.py` derives `GCS_BUCKET_NAME` / `GCS_BUCKET` from it).
  The old local-only `GCS_BUCKET_NAME` env var is no longer read.

## Gotcha: pkill/pgrep self-match

Commands that contain the literal string `uvicorn deployment.async_app` (or a
script name) will be matched by `pkill -f` / `pgrep -f` against the very shell
running them, killing your own command (seen as exit 143/144). Kill servers by
explicit PID instead of pattern.
