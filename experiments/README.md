# `experiments/`

External, **offline-first** measurement harnesses that *quantify* `creative_agent`
behavior — run latency, per-phase breakdowns, and quota-contention effects — by
driving live runs against **isolated Cloud Run revisions** and reducing the
persisted event logs into tidy data + figures.

These are research tooling, not product code. Each experiment has a matching
pre-registered / written-up design under [`docs/experiments/`](../docs/experiments).

> **Not in the agent import graph — on purpose.** No package here is imported by
> any agent, so none is ever bundled into an Agent Engine deployment
> (`deployment/deploy_agent.py`'s `AGENT_EXTRA_PACKAGES` is derived from the agent
> import graph). Keep it that way — importing an agent *from* here is fine;
> importing one of these *into* an agent is not.

## Subdirectories

| Directory | Purpose | Design / write-up doc |
|---|---|---|
| [`creative_latency/`](#creative_latency) | Measure end-to-end + per-phase `creative_agent` run latency; compare code variants (baseline vs. levers). **Strictly serial** — self-contention would corrupt the signal. | [`docs/experiments/2026-07-15-creative-latency.md`](../docs/experiments/2026-07-15-creative-latency.md) |
| [`quota_spread/`](#quota_spread) | The quota-bucket-spread **DoE**: fire N *concurrent* runs per cell to measure how research-phase latency inflates with load, per model-placement arm (PR #101). **Concurrency is the signal.** | [`docs/experiments/2026-07-17-quota-bucket-spread-doe.md`](../docs/experiments/2026-07-17-quota-bucket-spread-doe.md) |

## Shared conventions

Both packages follow the same posture, so tooling and habits transfer:

- **Offline-first, pure-core + thin-network split.** Parsing, aggregation,
  record-shaping, and cell-ordering are **pure and unit-tested** (see
  `tests/test_experiment_*.py`, `tests/test_quota_spread_*.py`). The live network
  drivers (`run_trial`, `run_batch`, `run_doe`, `recover_trials`) are integration
  paths with no unit tests, matching the repo convention for live scripts.
- **Async `/runs` API, not SSE.** Runs are kicked off + polled through the
  detached-job endpoints in `runserver/async_runs.py` (mounted by
  `deployment/async_app.py`). Because progress is read from the persisted session
  log, a run survives client disconnect — and a *completed* run can be re-harvested
  later with zero extra quota (`creative_latency/recover_trials.py`).
- **Isolated tagged revisions; prod untouched.** Arms deploy as
  `gcloud run deploy trend-trawler-api --source . --no-traffic --tag <tag> …`
  revisions and are addressed at `https://<tag>---<service>-<hash>-<region>.run.app`.
  Auth mints an impersonated ID token whose **audience is the BASE service URL**
  (not the tag URL); set `$EXP_INVOKER_SA` to an SA with `roles/run.invoker`, or
  leave it empty to use your own ADC. Tear tags down and restore `--to-latest`
  routing when done.
- **Latency is the honest primary signal.** 429s are absorbed by genai's HTTP
  retry *below* the ADK model-call boundary, so a callback-based counter would
  undercount. We measure the **latency inflation** the contention causes; the
  best-effort `count_429s` log scrape is corroboration only.
- **Fixed inputs.** A single constant campaign message
  (`creative_latency/fixtures.py`, reused by `quota_spread`) is passed to every
  run so the only thing varying is the code/placement under test.
- **Rendering is matplotlib/Plotly, never PaperBanana** — its image path burns the
  same 2 RPM `gemini-3.1-flash-image` quota the runs themselves need; these
  figures render purely from committed data.
- **Results are committed JSON** under each package's `results/` tree and are the
  source of truth; figures and CSVs regenerate from them offline.

---

## `creative_latency/`

Measures how long a `creative_agent` run takes and where the time goes, and
compares code variants (e.g. latency levers) against a baseline. Serial by design:
trials run one at a time with an inter-trial gap so shared Vertex quota recovers
and trials don't self-contend.

| File | Role |
|---|---|
| `run_trial.py` | **Live.** Drive ONE run through the `/runs` API (mint token → create session → kick off → poll to terminal → summarize → write `results/<config>/<session>.json`). |
| `run_experiment.py` | **Live.** Run N serial, spaced trials for one config and aggregate (`aggregate_records`, pure + tested). |
| `parse_run.py` | **Pure.** Event-log → per-phase wall-clock / model-call breakdown (`summarize_run`). The `research` span is the `combined_research_pipeline` tool-span. |
| `fixtures.py` | The fixed campaign message + `APP_NAME`, shared across all trials (and by `quota_spread`). |
| `recover_trials.py` | **Live (no new compute).** Re-poll + re-summarize an already-completed `user_id:session_id` when a live trial finished server-side but failed to write its JSON. |
| `plot.py` | Interactive `report.html` + PNGs via Plotly (PNG export via kaleido/headless Chrome, best-effort). |
| `render_static.py` | Static PNGs via matplotlib (Agg, no browser) — regenerates the same comparisons anywhere. |
| `results/` | Committed per-trial JSON records + `_summary.json` per config. |
| `figures/` | Rendered PNGs for the write-up. |

Run one config end-to-end:
```bash
PYTHONPATH="$PWD" uv run python -m experiments.creative_latency.run_experiment \
  --base-url https://<tag>---<service>-<hash>-<region>.run.app \
  --audience https://<service>-<hash>-<region>.run.app \
  --config-name baseline --revision <revision> --tag <tag> --n 3
```

## `quota_spread/`

The **design-of-experiments** harness for PR #101's quota-bucket spread (campaign
research pinned to `gemini-2.5 @ us-central1` to halve per-base-model contention in
the one `ParallelAgent`). One codebase deploys as multiple **arms** via the
`CAMPAIGN_RESEARCH_PLACEMENT` env seam; this package fires N *concurrent* runs per
`(arm, load)` cell — the one capability `creative_latency` deliberately lacks —
because here contention is the signal, not noise.

Primary metric: the **slope of median research-phase wall-clock vs concurrency N**
per arm (flatter ⇒ the spread absorbed the contention, H1). Quality (H3
non-inferiority) is harvested **free** from each run's in-pipeline `creative_eval`
report.

| File | Role |
|---|---|
| `run_batch.py` | **Live.** Fire N concurrent runs at one `(arm, load)` cell (ThreadPoolExecutor over `creative_latency.run_trial` helpers). Pure core `assemble_batch_records` is tested. |
| `run_doe.py` | **Live.** Drive the full DoE: blocked, interleaved `(arm, load, rep)` cells with inter-batch cool-down; writes records + `manifest.json`. Pure `plan_cell_order` is tested. |
| `arms.json` | The `{arm: {base_url, audience, revision, tag}}` map for the current live run (one tagged revision per arm). |
| `quality.py` | **Pure.** Read `state["creative_evaluation_report"]` → pass-rate + mean score + dims (the free H3 guardrail). |
| `analyze.py` | **Pure/offline.** Records → tidy CSV (one row per run) + per-arm contention slope. |
| `doe_plot.py` | Matplotlib figures: `research_slope.png` (primary), `totals_by_cell.png`, `quality_by_arm.png`. |
| `upload_to_vertex.py` | **Post-hoc, off the hot path.** Mirror committed records into Agent Platform Experiments (one `ExperimentRun` per record) for sortable console comparison. Pure `record_to_run` is tested; `--dry-run` shapes runs with no GCP calls. |
| `results/` | Committed run records at `results/<arm>/N<k>/<batch_id>/<session>.json` + `manifest.json`. |

Run the lean-core DoE (arms × loads {1,5} × reps):
```bash
PYTHONPATH="$PWD" EXP_INVOKER_SA=tt-web-sa@<proj>.iam.gserviceaccount.com \
  uv run python -m experiments.quota_spread.run_doe \
  --arm-map experiments/quota_spread/arms.json --loads 1 5 --reps 4 --cool-secs 120
```
Then reduce + render + (optionally) mirror to Experiments:
```bash
PYTHONPATH="$PWD" uv run python -m experiments.quota_spread.analyze
PYTHONPATH="$PWD" uv run python -m experiments.quota_spread.doe_plot
PYTHONPATH="$PWD" GOOGLE_CLOUD_PROJECT=<proj> \
  uv run python -m experiments.quota_spread.upload_to_vertex --dry-run
```

---

## Running an experiment live (checklist)

Live runs spend **shared, project-wide** Vertex quota and require a cool window
(pause the fan-out / co-tenant jobs). The safe protocol:

1. Deploy one `--no-traffic --tag` revision per arm (`--no-cpu-throttling
   --min-instances 1`); smoke each with an N=1 run.
2. Verify the window is quiet (no other model traffic) before firing the batch.
3. Run the driver in the background — a full DoE is multi-hour.
4. `analyze` → CSV + slopes; `doe_plot` → figures; write results into the design
   doc's results section.
5. **Tear down:** remove the tags, prune the 0%-traffic exp revisions, then restore
   `update-traffic --to-latest` on the service so prod follows `latestRevision`
   again (a `--no-traffic` deploy pins traffic to the previously-serving revision).
