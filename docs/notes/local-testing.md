# Local testing notes

Practical, non-obvious things about running/validating the agents locally.
Written 2026-07-12 on the `creative-eval` branch (ADK 2.1.0).

## The UI can't finish a full creative_agent run — HTTP request timeout

A full `creative_agent` run takes ~10–12 min. When driven through the browser
UI (`adk api_server` + Next.js frontend on Cloud Workstations), the run gets
**cancelled ~12 min in, during the eval phase**. Root cause is the Cloud
Workstations / proxy HTTP request timeout (~12 min) on the single long-lived
`POST /run_sse` request. The eval phase (`creative_eval_agent`, 12 creatives ×
~28s on `gemini-2.5-pro`) streams nothing for minutes, so it pushes the request
past the timeout. Result: eval report / gallery / BigQuery steps never run via
the UI, even though the agent logic is correct.

**Workaround: run headless.** `deployment/headless_run.py` drives the same
`root_agent` through a local ADK `Runner` with no HTTP layer, so it completes.

**Fix (2026-07-12): eval now runs concurrently.** The eval phase was the long
silent stretch that blew the timeout. `evaluate_all_creatives`
(`creative_eval/agent.py`) and `evaluate_creatives` (`creative_eval/evaluate.py`)
now score all creatives in parallel via `evaluate_all_concurrently()` (a
`ThreadPoolExecutor`, `EvalConfig.max_eval_workers=12`) instead of a sequential
`for` loop. Measured: eval dropped from ~5.5 min (12 creatives sequential) to
~26–30 s (bounded by the slowest single judge call, roughly constant regardless
of creative count). This keeps the whole final leg well under the ~12-min
timeout. Note the per-creative `evaluate_ad_copy`/`evaluate_visual_concept`
signatures now take a shared `client` as a 4th positional arg (the pool creates
one client and reuses it); test mocks must accept it.

## Headless runner details (`deployment/headless_run.py`)

- Uses `Runner(app_name="creative_agent", agent=root_agent,
  session_service=InMemorySessionService(),
  artifact_service=FileArtifactService(root_dir=".adk/artifacts"))`.
  `FileArtifactService` lives at
  `google.adk.artifacts.file_artifact_service` and takes a single `root_dir`.
  `.adk/artifacts` is the same dir `adk api_server` writes to
  (`.adk/artifacts/users/<uid>/sessions/<sid>/...`).
- **Do not pre-seed campaign state.** `load_session_state` only initializes
  `gcs_bucket`/`gcs_folder`/`agent_output_dir` when `config.state_init` is
  absent, and it overwrites `brand`/etc. with empty strings. The intended flow
  is: start with empty state, let the callback set the `gcs_*` keys, and pass
  campaign metadata in the **kickoff user message** — the root agent's
  instructions call the `memorize` tool to store them. Seeding state with
  `state_init` already set skips the `gcs_*` initialization and breaks the run.
- Sessions are **in-memory per process**, so a headless run will NOT appear in
  the running api_server's `/results/[sessionId]` UI (separate process, separate
  memory). Verify via script stdout + GCS + BigQuery instead.
- **Caveat: AgentTool sub-invocation events don't surface in the top-level
  `runner.run_async` stream.** Tools invoked *inside* an `AgentTool` wrapper
  (e.g. `generate_image` inside `visual_production_pipeline`) never appear as
  function-call events at the top level, so counting them from the event stream
  reads 0. To verify exactly-once image generation, count the tool's own
  `"Saved image artifact"` log lines instead (one `generate_image` call saves
  one line per concept — 6 concepts → 6 lines; the old duplicate bug gave 12).

Verified 2026-07-12 (run `a23fb408`, gcs_folder `2026_07_12_05_55_1a27`): full
end-to-end pass — 6 distinct PNGs, `creative_eval_report.json`,
`creative_portfolio_gallery.html`, research PDF all in GCS; BQ row written;
every top-level tool called exactly once.

## Where results actually land (state vs return value)

Not all tools persist their output to session state — some only return it in the
tool response. When validating, check the right place:

- `save_eval_report_to_gcs` → sets `state["eval_report_gcs_uri"]`.
- `generate_image` → sets `state["_images_generated"]` (bool guard) and
  `state["_generated_artifact_keys"]` (list).
- `save_creative_gallery_html` → **no state key**; returns `{status, gcs_uri}`.
- `write_trends_to_bq` → **no state key**; returns `{status, ...}`.

So the headless script captures function-response payloads for the terminal
tools rather than reading state for them.

## GCS / BigQuery layout for a run

- GCS output: `gs://{GCS_BUCKET_NAME}/{gcs_folder}/{agent_output_dir}/` where
  `agent_output_dir="creative_output"` and `gcs_folder` is
  `YYYY_MM_DD_HH_MM_<4hex>` (set in `_set_initial_states`).
- Bucket: `GCS_BUCKET_NAME=trend-trawler-deploy-ae`.

## Gotcha: pkill/pgrep self-match

Commands that contain the literal string `adk api_server` (or a script name)
will be matched by `pkill -f` / `pgrep -f` against the very shell running them,
killing your own command (seen as exit 143/144). Kill servers by explicit PID
instead of pattern.
