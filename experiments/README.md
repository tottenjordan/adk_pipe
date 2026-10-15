# `experiments/`

External, **offline-first** measurement harnesses and spikes. Six subdirectories:
two *quantify* `creative_agent` behavior (run latency, per-phase breakdowns,
quota-contention effects) by driving live runs against **isolated Cloud Run
revisions** and reducing the persisted event logs into tidy data + figures; one
A/B-tests the Gemini 3 sampling temperatures the same way (both creative agents'
and trend_scout's runs, against a pre-registered decision rule); one re-grades
stored, human-rated creatives offline at two judge and two image-QA temperatures;
one calibrates the person-reference feature directly on the image model; one renders
the bandit simulator's notebook-parity figures.

These are research tooling, not product code. Each experiment has a matching
pre-registered / written-up design under [`docs/experiments/`](../docs/experiments).

> **Not in the agent import graph — on purpose.** No package here is imported by
> any agent, so none is ever bundled into an Agent Engine deployment
> (`deployment/deploy_agent.py`'s hand-kept `AGENT_EXTRA_PACKAGES` is checked against
> each agent's real import graph by `tests/test_deploy_import_graph.py`). Keep it that way — importing an agent *from* here is fine;
> importing one of these *into* an agent is not.

## Subdirectories

| Directory | Purpose | Design / write-up doc |
|---|---|---|
| [`creative_latency/`](#creative_latency) | Measure end-to-end + per-phase `creative_agent` run latency; compare code variants (baseline vs. levers). **Strictly serial** — self-contention would corrupt the signal. | [`docs/experiments/2026-07-15-creative-latency.md`](../docs/experiments/2026-07-15-creative-latency.md) |
| [`quota_spread/`](#quota_spread) | The quota-bucket-spread **DoE**: fire N *concurrent* runs per cell to measure how research-phase latency inflates with load, per model-placement arm (PR #101). **Concurrency is the signal.** | [`docs/experiments/2026-07-17-quota-bucket-spread-doe.md`](../docs/experiments/2026-07-17-quota-bucket-spread-doe.md) |
| [`grader_temperature/`](#grader_temperature) | Offline grader temperature check: re-judge (0.3 vs 1.0) and re-inspect (0.0 vs 1.0) stored, human-rated `creative_agent` creatives 3x each; flip rates + agreement with human ratings, a pre-registered ADOPT/KEEP rule. | grader-temperature Phase 2 plan (PR 2) |
| [`person_reference/`](person_reference/README.md) | Two person-reference spikes on the image model, no Cloud Run involved. `calibrate.py` (gate for PR 0 of the person-reference plan): likeness + safety-block rates of consented test photos, with and without `person_generation=ALLOW_ADULT`, per style family. `swap_compare.py` (PR 0 of the faithful-person-swaps plan): edit the original render vs re-generate it for personalised variants, scored on fidelity and likeness. | [`docs/plans/2026-10-09-person-reference.md`](../docs/plans/2026-10-09-person-reference.md), [`docs/plans/2026-10-11-faithful-person-swaps.md`](../docs/plans/2026-10-11-faithful-person-swaps.md) (results: [`docs/notes/person-reference-calibration.md`](../docs/notes/person-reference-calibration.md), [`docs/notes/person-swap-edit-mode.md`](../docs/notes/person-swap-edit-mode.md)) |
| [`temperature_ab/`](#temperature_ab) | Gemini 3 temperature A/B: two tagged no-traffic api revisions (`GEMINI_TEMPERATURE_PROFILE=tuned\|default`), serial counterbalanced (ABBA) `creative_agent` + `trend_scout` runs, a pre-registered PASS/FAIL rule. | [`docs/plans/2026-10-15-gemini3-temperature-ab.md`](../docs/plans/2026-10-15-gemini3-temperature-ab.md) |
| `bandit/` | Offline notebook-parity figures for the JAX contextual bandit (`bandit/` package): `notebook_parity.py` runs `bandit.cli` simulations (no GCP) and writes `figures/*.png`. | [`docs/experiments/bandit-simulation.md`](../docs/experiments/bandit-simulation.md) |

## Shared conventions

`creative_latency/` and `quota_spread/` follow the same posture, so tooling and
habits transfer (`person_reference/` shares the pure-core + thin-network split and
the committed-summary habit, but calls the image model directly):

- **Offline-first, pure-core + thin-network split.** Parsing, aggregation,
  record-shaping, and cell-ordering are **pure and unit-tested** (see
  `tests/test_experiment_*.py`, `tests/test_quota_spread_*.py`,
  `tests/test_creative_latency_poll.py`, and for `person_reference/`
  `tests/test_person_calibration.py` + `tests/test_swap_compare.py`). The live network
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
  same ~2 RPM image-model quota (measured on `gemini-3.1-flash-image`; the image model is now `gemini-nano-banana-2.1`) the runs themselves need; these
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
the research fan-out, then a `ParallelAgent`; now a graph `Workflow`). One codebase deploys as multiple **arms** via the
`CAMPAIGN_RESEARCH_PLACEMENT` env seam; this package fires N *concurrent* runs per
`(arm, load)` cell — the one capability `creative_latency` deliberately lacks —
because here contention is the signal, not noise.

> **2026-09-28:** the `regional_25` arm (gemini-2.5 @ us-central1) is retired —
> gemini-2.5 is being shut down on Vertex. Live arms are now `global_altbucket`
> (the production default, campaign on `gemini-3.5-flash` @ global) and `global_3x`;
> `regional_25` survives only as a label in the committed historical `results/`.

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

## `temperature_ab/`

Does pinning the Gemini 3 agents' sampling temperatures (`tuned`) beat the model
defaults (`default`)? Both arms are the **same image** deployed as two tagged,
no-traffic revisions of `trend-trawler-api` that differ only by
`GEMINI_TEMPERATURE_PROFILE`; the api runs the agents in-process, so `/runs`
exercises the profile directly. Design, deploy commands and teardown:
[`docs/plans/2026-10-15-gemini3-temperature-ab.md`](../docs/plans/2026-10-15-gemini3-temperature-ab.md).

**Auth (P3).** The api is in `USER_AUTHZ_MODE=enforce`, so the driver acts as
the web proxy: it impersonates `tt-web-sa` (`$EXP_INVOKER_SA`, required for live
runs) to mint an ID token **with an `email` claim**
(`run_trial.mint_proxy_id_token`, google-auth `IDTokenCredentials(...,
include_email=True)`; audience = the base service URL) and sends
`X-TT-User: temperature-ab@jordantotten.altostrat.com`, the user id in every
path/body. Your ADC needs `roles/iam.serviceAccountTokenCreator` on `tt-web-sa`.

| File | Role |
|---|---|
| `fixtures.py` | The two evalset campaigns (PRS, Estee Lauder ANR) with evergreen trends, as frontend-shaped initial state + kickoff message; the experiment user. |
| `metrics.py` | **Pure.** Per-run state metrics (creative: `finalized`, `gates_pass_rate`, `mean_score`, `pass_rate`, `n_issues`, `n_retry_exhausted`, `image_qa_attempts`, `headline_jaccard`; trend_scout: `completed`, `trend_flags`, `info_gtrends_exhausted`) + the log scrape (empty/abnormal turns, `ValidationError`, 429 in the run's window on its revision; injectable reader). |
| `run_ab.py` | **Live.** Serial driver: counterbalanced ABBA pairs (pair k runs tuned→default when k is even, default→tuned when odd; block order rotates per rep), apps × campaigns balanced across arms, deterministic cell ids, resumable (existing records are skipped), one record per run at `results/<arm>/<app>/<cell_id>.json` (a failed run is a record). `--dry-run` prints the order. |
| `analyze.py` | **Pure/offline.** Records → per-app metric table (n, mean, median per arm, `default − tuned` with a seeded 10k bootstrap 95% CI), classifier stability, the pre-registered rule (PASS/FAIL per check + overall verdict) → `summary.md` + `records.csv`. |
| `arms.example.json` | Template for the gitignored `arms.json` (`{arm: {base_url, audience, revision, tag}}`, arms named `tuned` and `default`). |

Unlike the older harnesses, `arms.json` and `results/` are **gitignored** (records
hold full session state); commit only the write-up.

```bash
# 1. Deploy the two tagged revisions (plan doc), then fill in arms.json.
cp experiments/temperature_ab/arms.example.json experiments/temperature_ab/arms.json
# 2. Check the order, then pilot one rep (both campaigns, both apps, both arms).
PYTHONPATH="$PWD" uv run python -m experiments.temperature_ab.run_ab \
  --arms experiments/temperature_ab/arms.json --reps 1 --dry-run
PYTHONPATH="$PWD" EXP_INVOKER_SA=tt-web-sa@<proj>.iam.gserviceaccount.com \
  uv run python -m experiments.temperature_ab.run_ab \
  --arms experiments/temperature_ab/arms.json --reps 1 --gap-secs 60
# 3. Full run (~10 runs per arm per app). It resumes: the pilot's records count as rep 0,
#    so move results/ aside first if the pilot shouldn't be in the analysis.
PYTHONPATH="$PWD" EXP_INVOKER_SA=tt-web-sa@<proj>.iam.gserviceaccount.com \
  uv run python -m experiments.temperature_ab.run_ab \
  --arms experiments/temperature_ab/arms.json --apps creative_agent,trend_scout \
  --reps 10 --gap-secs 60
# 4. Analyse against the pre-registered rule.
PYTHONPATH="$PWD" uv run python -m experiments.temperature_ab.analyze
```

## `grader_temperature/`

Offline check of the two graders' sampling temperatures: does raising the
`creative_eval` judge from 0.3 to 1.0, or the post-render image QA from 0.0 to
1.0, keep verdicts as stable and as close to human ratings? No live runs, no
Cloud Run: stored, human-rated `creative_agent` sessions are re-graded directly.
The arms are set through `EvalConfig(judge_temperature=…)` and
`inspect_image(..., temperature=…)` (production: 0.3 and 0.0).

| File | Role |
|---|---|
| `select.py` | Parameterised query on `creative_ratings` (`app_name='creative_agent'`, never `note`); picks `--n` sessions with both kinds rated, most recent first (ties by `--seed`), stratified to at least 12 human fails (kappa needs both classes); `--exclude` drops sessions. Writes `data/selection.json`. |
| `fetch.py` | Read-only `VertexAiSessionService.get_session` per session from the sessions engine in `SESSION_SERVICE_URI` (`agentengine://<id>` or the full resource name); caches `data/state/<sid>.json`; missing sessions → `data/missing.json` (re-run `select --exclude …`). |
| `reinspect.py` | Downloads each rendered image once to `data/img/` (≤ 20 MB) and calls `inspect_image` with the production inputs (brand, product, rating strictness, safer angle, logo reference, the person photo for cast concepts only) `--repeats` times per temperature → `results/qa/<sid>/<concept>/<t>-r<k>.json` (+ `qa_failed_rules`). Resumable. |
| `rejudge.py` | Builds the judge inputs exactly like `creative_eval/agent.py`'s full evaluation and calls `evaluate_all_concurrently` (2 workers, one client per batch) per (session, temperature, repeat), arms interleaved per session → `results/judge/<sid>/<t>-r<k>.json`; failed judge calls listed and later excluded. Resumable. |
| `analyze.py` | **Pure/offline.** Pass / gate / rule flip rates, score SD, majority verdicts vs human ratings (kappa, Spearman), fail-reason ↔ QA-rule overlap, bootstrap CIs, and the pre-registered rule (PASS / FAIL / N/A per check; ADOPT / KEEP / INCONCLUSIVE per grader, INCONCLUSIVE keeps the current temperature) → `results/summary.md` + `records.csv`. |

Order: select → fetch → reinspect → rejudge → analyze.

```bash
PYTHONPATH="$PWD" uv run python -m experiments.grader_temperature.select --n 10 --seed 20261015
PYTHONPATH="$PWD" SESSION_SERVICE_URI=agentengine://<sessions engine> \
  uv run python -m experiments.grader_temperature.fetch
PYTHONPATH="$PWD" uv run python -m experiments.grader_temperature.reinspect --temps 0.0,1.0 --repeats 3
PYTHONPATH="$PWD" uv run python -m experiments.grader_temperature.rejudge --temps 0.3,1.0 --repeats 3
PYTHONPATH="$PWD" uv run python -m experiments.grader_temperature.analyze
```

**Quota:** the judge is the Pro model (5 RPM, project-wide, shared with live
runs): 10 sessions × 2 temperatures × 3 repeats ≈ 480 judge calls ≈ 2 h; run it
in a quiet window. Image QA uses the flash worker model (cheap).
**Clean up:** `data/` and `results/` are gitignored; delete `data/` when done
(cached session state and possibly consented-person images). Commit only the
write-up.

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
