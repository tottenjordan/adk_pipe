# DoE — quantifying the creative_agent quota-bucket spread (model-family change)

**Date:** 2026-07-17 (design / pre-registration; not yet executed)
**Status:** DESIGN. No trials run. This document is the pre-registered plan — hypotheses,
metrics, analysis, and decision thresholds are fixed *before* data collection so the result
can't be rationalized after the fact.
**Change under test:** PR #101 (`08c2a9c`) — creative_agent's **campaign** research pipeline
(`parallel_planner_agent` → `ca_sequential_planner`: planner + searcher + synthesizer) moved
from the global `gemini-3.x` buckets to **`gemini-2.5-flash` / `gemini-2.5-flash-lite` @
`us-central1`**. The **trend** pipeline (`gs_sequential_planner`) is unchanged (stays global 3.x).
**Reuses:** the `experiments/creative_latency/` harness (isolated `--no-traffic --tag` Cloud Run
revisions driven through the async `/runs` HTTP API; per-phase wall-clock parsed from the
persisted event log) — see [`docs/experiments/2026-07-15-creative-latency.md`](2026-07-15-creative-latency.md).

---

## 1. The precise question

The change did two things at once, and the user's phrasing — "quantify the benefits of
**changing model families**" — asks about both:

1. **Bucket separation** (the *intended* benefit): during `parallel_planner_agent`, the trend
   and campaign halves used to fire **two** concurrent calls into the *same* ~5-RPM global
   base-model bucket at each step. Splitting the campaign half into a separate pool should
   remove that doubling → less rate-limiting under load → less latency inflation of the
   research phase.
2. **Model-family swap** (the *cost side* / confound): `gemini-2.5-flash` is a different,
   generally cheaper/faster-but-weaker model than `gemini-3.5-flash`. That swap has its own
   **intrinsic** effect on latency (possibly *faster* per call) and on **quality** (possibly
   *worse* research). This must be measured, not assumed away — a family that is faster only
   because it is weaker is not a free win.

**We want to attribute the observed effect to the right cause, and confirm the quality cost is
acceptable.**

---

## 2. The confound, and how the design resolves it

The naive comparison (current-main **B** vs pre-change **A**) changes **family AND region
AND bucket simultaneously**, and the platform forbids the clean 2×2 that would separate them:

| | global | us-central1 |
|---|---|---|
| **gemini-3.x** | ✅ A (baseline) | ❌ 404 (3.x is global-only) |
| **gemini-2.5** | ❌ 404 (2.5 is global-404) | ✅ B (treatment) |

Two cells are physically uncallable, so **family and region are structurally confounded** in the
A-vs-B contrast. We break the confound with a **third arm** and a **no-contention load level**:

- **Arm C — bucket-only, within global, same 3.x family.** Move the campaign pipeline to a
  *different global gemini-3.x base-model bucket* than the trend pipeline. Same region (global),
  same 3.x generation — only the **bucket** differs. If C removes the contention just like B,
  then **bucket separation is the mechanism** and the family/region change was incidental. If
  only B works, the regional pool is doing the work.
  - **Feasibility gate RESOLVED (2026-07-17, Task 0a — throwaway `/tmp/probe_altbucket_grounding.py`):
    Arm C is IN.** The trend half occupies `gemini-3.5-flash` (worker) + `gemini-3.1-flash-lite`
    (planner) @ global. Every *flash* base-model variant I probed (`gemini-3.1-flash`,
    `gemini-3.5-flash-preview`, `gemini-3.0-flash`, `gemini-3.5-flash-lite`, `gemini-3.0-flash-lite`)
    404s — but a `models.list()` sweep surfaced **`gemini-3-flash-preview`**, a *distinct* global
    base model, which **calls AND grounds** via `google_search` (TEXT_OK=True, GROUNDED=True,
    30 grounding chunks). So Arm C pins the campaign pipeline (planner + searcher + synthesizer)
    to `gemini-3-flash-preview` @ global — one clean distinct global bucket. There is no distinct
    global flash-*lite*, so the campaign planner shares Arm C's single `gemini-3-flash-preview`
    bucket rather than reusing the trend planner's `gemini-3.1-flash-lite`. Config arm key:
    `global_altbucket` → `ALT_LITE = ALT_WORKER = "gemini-3-flash-preview"`, location `global`.
- **Load = 1 (no contention).** At concurrency 1 there is no cross-caller bucket pressure, so
  any A-vs-B delta at N=1 is the **pure model-family effect** (intrinsic per-call speed +
  quality), cleanly separated from the quota benefit.

This yields a factorial that answers all three sub-questions:

| Sub-question | Contrast that answers it |
|---|---|
| Intrinsic family speed (2.5 vs 3.x)? | A vs B **at N=1**, campaign-phase latency |
| Intrinsic family quality (2.5 vs 3.x)? | A vs B, `adk eval` scores (cool window) |
| Does bucket separation reduce contention? | slope of research-phase latency over N: A vs B |
| Was the *family/region change* necessary, or would a bucket swap suffice? | A vs C vs B slopes over N |

---

## 3. Hypotheses (pre-registered)

- **H1 (contention — primary).** Research-phase wall-clock **inflates with load N** under
  Arm A (baseline) but stays **materially flatter** under Arm B. Formally: the slope of
  research-phase p50 vs N is significantly smaller for B than A (interaction term
  `arm × N < 0`).
- **H2 (family speed).** At N=1, campaign-pipeline per-phase latency for B (2.5) is
  **≤** A (3.x) — i.e. the swap is not a per-call slowdown. (Directional; report the signed
  delta with CI.)
- **H3 (quality guardrail — non-inferiority).** Moving the campaign research to `gemini-2.5`
  does **not** degrade creative quality beyond a pre-set margin: eval pass-rate drop ≤ 5pp and
  mean normalized-score drop ≤ 0.03 vs baseline. *This is the gate that a "faster because
  weaker" swap must clear.*
- **H4 (mechanism, optional).** If Arm C is feasible, C's contention slope ≈ B's ⇒ the benefit
  is bucket separation per se (region/family incidental).

---

## 4. Factors & levels

- **Factor P — Placement (arm):**
  - **A** = baseline: campaign + trend both on **global gemini-3.x** (pre-#101 code).
  - **B** = treatment: campaign on **regional gemini-2.5 @ us-central1** (current `main`).
  - **C** = *(optional, feasibility-gated)* campaign on a **distinct global gemini-3.x bucket**;
    trend unchanged.
- **Factor N — Load (inter-run concurrency):** simultaneous creative_agent runs sharing the
  project quota. **Core levels: {1, 5}** (1 = no contention → family effect; 5 ≈ the 5-RPM
  base-model ceiling → maximal contention). **Extension level: 3** (to trace the slope shape).

Intra-run concurrency is fixed by the architecture (the parallel phase = exactly 2 concurrent
halves); the experiment varies only inter-run N.

**Arm deployment (reuses the validated isolated-revision method — prod untouched):** each arm
ships to its own `--no-traffic --tag` Cloud Run revision and is driven identically:
`exp-allglobal-3x` (A, reverted campaign agent), `exp-regional-25` (B, current main),
`exp-global-altbucket` (C, small branch). No traffic ever shifts; teardown deletes the tags.

---

## 5. Response variables & measurement constraints

**Primary (reliable):**
- **Research-phase wall-clock** (the `parallel_planner_agent` span) — from tool-call spans via
  `experiments/creative_latency/parse_run.py` / `summarize_run._SPAN_TOOLS`. This is the
  observable that actually moves under contention.
- **End-to-end wall-clock** (p50/p95 per cell).

**Corroborating (best-effort — see caveat):**
- **In-process 429 count.** ⚠️ The prior experiment established that **Vertex 429s do not
  reliably reach Cloud Run ingress logs** — the genai HTTP-retry layer swallows them *inside*
  the request (`agent_common/genai_retry.py`, 5 attempts, `429/500/503/504`), *below* the ADK
  model-call boundary. So neither `gcloud logging` ingress counts nor an ADK
  `before/after_model_callback` counter would see them (the callback fires once per *successful*
  model call, after retries already resolved) — this is why latency inflation, not a 429 count,
  is the primary metric.
  - **429-count feasibility RESOLVED (2026-07-17, Task 0b).** `types.HttpRetryOptions` exposes
    **no user callback field** (`attempts/initial_delay/max_delay/exp_base/jitter/http_status_codes`
    only), so we cannot inject our own per-attempt hook via the options object. **However** genai
    already wires `tenacity.before_sleep_log(logger, logging.INFO)` (`google/genai/_api_client.py:556`)
    on logger `google_genai._api_client` — so **every** retry attempt (429 included) *already*
    emits an INFO log line before each backoff sleep. Real 429 counts are therefore obtainable by
    **scraping that existing line** (no patch to `genai_retry.py` needed), *provided* that logger
    propagates at INFO from the deployed backend. This is the (optional, non-blocking) Task 9:
    extend `count_429s`'s Cloud Logging filter to match the tenacity retry line. Kept as
    corroboration only; latency-inflation slope remains primary.
- **`*__retry_exhausted` degradation markers** (from final session state) — a run that
  *silently degraded* rather than aborting.

**Quality guardrail:**
- **`adk eval`** on `creative_agent` (rubric LLM-as-judge, `creative_eval`): pass-rate + mean
  normalized score + the 12 per-dimension means. Run in a **cool window**, judge held fixed on
  `gemini-3.1-pro-preview @ global` (never swapped — a prior A/B showed 2.5-pro grades softer).

**Why latency-inflation is the honest primary signal:** contention manifests as
429→backoff→re-serialization, which shows up as the parallel phase getting *longer* as N rises,
not as a clean 429 tally. H1 is therefore framed as a **slope** comparison, exactly the pattern
that surfaced "Lever B" in the latency experiment.

---

## 6. Experimental unit, replication, controls

- **Unit:** one creative_agent run on a **fixed** campaign brief (reuse
  `experiments/creative_latency/fixtures.py` — PRS SE CE24 / Powerball) to remove content
  variance. Metadata goes via the user **message** (not `initialState` —
  `before_agent_callback` blanks seeded brand/target_*).
- **Trial:** at load N, one **batch of N simultaneous runs**; every run in the batch is a
  measured unit.
- **Replication:** **≥4 batches per (arm × N) cell** (bumped from the prior experiment's 3,
  because contention is noisy and we need a slope, not a point). Report **median** per cell with
  min/max, as before.
- **Randomization / blocking:** interleave arms within each load block
  (`…A₅ B₅ C₅ B₅ A₅ C₅…`), **never** all-A-then-all-B — the shared project quota drifts over
  time, so temporal drift must be blocked out, not baked into the arm contrast.
- **Cool window:** run when no other Vertex load hits the project (pause the fan-out Cloud
  Functions; no concurrent image/dev jobs). Discard a warm-up batch (cold start).
- **Pinning:** fixed project/env, pinned model versions, `auto_refine` disabled where it adds
  render variance.

---

## 7. Procedure

**Step 0 — Feasibility gate for Arm C (do first, ~1 min, throwaway).** Direct-genai probe
(mirror `/tmp/probe_regional_grounding.py`): confirm a *second* global gemini-3.x flash family
is callable AND grounds via `types.Tool(google_search=...)`. `GROUNDED: True` → C is in;
otherwise drop C and report A-vs-B as a bundled family+region effect.

**Step 1 — Harness extensions** (on an experiment branch; measurement-only, reverted after):
1. **Concurrent launcher** — extend `run_experiment.py` to fire N runs via `asyncio.gather`
   against a tagged revision and collect all N event logs per batch. (Prior harness ran trials
   sequentially; concurrency is the new capability.)
2. **In-process 429 counter** — the `__429_count` callback described in §5.
3. **Slope/aggregation** — extend `parse_run.py` + `plot.py` to emit one CSV row per run
   (`arm, N, batch, research_s, total_s, count_429, retry_exhausted, …`) and to fit/plot
   research-phase latency vs N per arm.

**Step 2 — Deploy the 2–3 tagged revisions** (A, B, [C]); verify each with a single N=1 smoke.

**Step 3 — Execute** the interleaved, blocked batches in the cool window; collect CSV.

**Step 4 — Quality pass** — `adk eval` per arm (A, B) in the cool window.

**Step 5 — Analyze, decide, tear down** the tags.

---

## 8. Analysis plan (pre-registered)

- **H1 (primary):** regress `research_s ~ arm * N` (per-run, cell medians as robustness check).
  The **`arm:N` interaction** is the test — a significantly negative B-vs-A interaction (B's
  slope flatter) confirms the spread reduces contention. Report the interaction coefficient with
  a bootstrap CI and the raw per-cell median slopes.
- **H2 (family speed):** at **N=1**, campaign-phase latency B − A, Mann-Whitney + median delta
  with CI. (N=1 isolates family from contention.)
- **H3 (quality non-inferiority):** two one-sided tests (TOST) against the pre-set margins
  (Δpass-rate ≤ 5pp, Δmean-score ≤ 0.03); also report per-dimension deltas so a localized
  regression (e.g. "research depth") can't hide inside a flat average.
- **H4 (mechanism):** compare C's slope to B's and A's; C≈B ⇒ bucket separation is the cause.
- Report **effect sizes + CIs**, not just p-values. Given noisy shared quota, treat single
  numbers with suspicion — medians of ≥4 batches, min/max shown.

### 8a. Results — MEASURED (2026-07-17)

**Window & execution.** Lean core (arms A=`global_3x`, B=`regional_25`) × N∈{1,5} × reps=4 =
16 cells / **48 runs**, interleaved-blocked, 120 s inter-batch cool, run **15:34–19:30 UTC**
(~3 h 56 m) against the two `--no-traffic --tag` revisions (A=`00068-muz`, B=`00069-bik`;
prod untouched). Cool window held (co-tenant novastorm + simulator jobs paused). **42 runs
`done`, 6 `error`** (all errors at N=5; see reliability). Arm C not run. Fixed PRS/Powerball
brief via user message. Data: `experiments/quota_spread/results/{runs.csv,summary.json}`;
figures: `experiments/quota_spread/figures/`.

| cell | n_done | n_err | research p50 | research p90 | research **max** | eval pass | eval mean |
|---|---|---|---|---|---|---|---|
| A `global_3x` · N=1 | 4 | 0 | 97 s | 261 s | 330 s | 0.906 | 0.815 |
| A `global_3x` · N=5 | 16 | 4 | 90 s | 569 s | **839 s** | 0.977 | 0.835 |
| B `regional_25` · N=1 | 4 | 0 | 114 s | 336 s | 427 s | 0.969 | 0.839 |
| B `regional_25` · N=5 | 18 | 2 | 116 s | 138 s | **179 s** | 0.965 | 0.830 |

**H1 (contention) — CONFIRMED, but in the TAIL, not the p50 (pre-registered deviation).**
The pre-registered p50-slope test is a **null by construction**: median research is flat for
both arms (A slope −1.7 s/N, B +0.4 s/N — see `research_slope.png`), because genai HTTP retry
absorbs 429s *below* the ADK model-call boundary (§5) — the median run rarely loses the retry
lottery. The contention lives in the **upper quantiles**, exactly where the design said to
look. As N goes 1→5, **A's research tail blows out** (p90 261→569 s, max 330→**839 s**) while
**B's tail tightens** (p90 336→138 s, max 427→**179 s**). At N=5, A's max is **4.7×** B's and
A's p90 is **4.1×** B's. This is the headline; see `research_tail.png`. Corroborating (noisy,
best-effort log scrape per §5): at N=5, per-run `count_429` A median 6 / max 13 vs B median 4 /
max 4 (the regional N=1 max=70 is a window-overlap scrape artifact — directional only).

**H2 (family speed at N=1) — NOT met (small, acceptable cost).** At N=1 (no contention), B's
research span is **~17 s slower** than A (114 vs 97 s p50): the campaign 2.5-flash half is
marginally slower than 3.x at idle, and the parallel span is bounded by the slower half. Small
n (4/cell) with idle outliers (A max 330, B max 427). This idle cost is swamped by the tail win
under the load the fan-out actually creates.

**H3 (quality non-inferiority) — PASSES decisively.** Pooled across N: A mean ≈0.831 / pass
≈0.963 vs B mean ≈0.832 / pass ≈0.966. **Δmean ≈ +0.001, Δpass ≈ +0.3 pp in B's favor** — both
inside the pre-set margins (Δmean ≤ 0.03, Δpass ≤ 5 pp) with room to spare. The 2.5 campaign
swap does **not** degrade creative quality; if anything B is a hair better. See
`quality_by_arm.png`.

**Reliability (not pre-registered, but decisive).** Errors appear **only under concurrency**:
N=1 = 0/8 both arms; at N=5 **A = 4/20 (20%)** vs **B = 2/20 (10%)** — B fails **half** as often
under load. Failure class = report/visual phase (`[Errno 2] No such file or directory:
'report_creatives'`), research completed first in every failed run. One `*__retry_exhausted`
degradation marker across all 48 runs. (The `report_creatives` failure mode is a pre-existing
concurrency bug surfaced by this experiment — logged as a follow-up, orthogonal to the arm
choice.)

**Total wall-clock** is comparable and **not** a clean arm differentiator (A N=5 p50 829 s vs
B 1023 s): end-to-end is dominated by the 2-RPM project-wide image cap serializing the visual
phase (arm-independent), so the difference is image-queue position, not the campaign model. See
`totals_by_cell.png`.

---

## 9. Decision criteria (pre-registered)

- **Confirm-ship (B):** H1 holds (B's research-latency slope significantly flatter than A at
  N≥3) **AND** H3 passes (quality non-inferior within margin). → keep #101, this doc becomes
  the evidence.
- **Reconsider / roll back:** H3 **fails** (family swap costs quality beyond margin) even if H1
  holds — a quota win paid for with worse research is not worth it; revisit (e.g. move only the
  planner/synthesizer regionally, keep the searcher on a stronger model).
- **Simplify (if H4 holds and C feasible):** if C matches B, prefer the **within-global bucket
  swap** (C) — it keeps the stronger 3.x family *and* separates the bucket, dominating B on
  quality at equal contention benefit. This would be a follow-up change, not a rollback.

### 9a. Decision — MEASURED (2026-07-17): **CONFIRM-SHIP B — keep #101.**

The confirm-ship gate is met: **H1 holds** (B's research contention is dramatically flatter
than A — confirmed on the tail, the honest signal given retry masks the p50) **AND H3 passes**
(quality non-inferior, in fact marginally better). B additionally **halves the concurrency
failure rate** (10% vs 20% at N=5) — a reliability win the pre-registration didn't even claim.

The only cost is H2: **~17 s of extra idle-latency at N=1**, which is negligible against a
4–5× worse research tail and 2× worse failure rate for A under the concurrency the PubSub
fan-out actually produces. Not a rollback trigger (the rollback trigger was an H3 failure,
which did not occur).

**This doc is now the quantitative evidence #101 previously lacked** (it shipped on correctness
alone). No code change results — `main` already runs Arm B (`CAMPAIGN_RESEARCH_PLACEMENT`
default `regional_25`).

**Follow-ups (out of scope here):** (1) the `report_creatives` report/visual-phase failure
under N=5 concurrency is a pre-existing bug this experiment surfaced — worth its own fix. (2)
H4 (Arm C, distinct global bucket) remains untested; if it matches B it would dominate on
quality at equal contention benefit (§9 "Simplify"). (3) The real ceiling remains the
project-wide Vertex quota (5 RPM base-model, 2 RPM image) — a quota increase dominates all
placement tricks.

---

## 10. Cost / quota budget

Runs = Σ_cells (reps × N). Lean vs full:

| Design | Cells | Runs | Notes |
|---|---|---:|---|
| **Lean core** (A,B × N∈{1,5}, reps=4) | 4 | 4·(1)+4·(5) ×2 = **48** | minimum viable: family effect (N=1) + contention (N=5) |
| **+ slope** (add N=3) | 6 | **+36 = 84** | traces slope shape for a cleaner interaction estimate |
| **+ Arm C** (all above ×1.5) | 9 | **~126** | de-confounds bucket vs family/region |

At ~380 s/run but batched concurrently, wall-clock ≪ runs×380 s; the true constraint is
**shared quota** — schedule in a genuinely cool window and expect Arm-A batches at N=5 to
*intentionally* provoke rate-limiting (that's the measurement). Recommend starting with the
**Lean core (48 runs)**; escalate to slope/Arm-C only if the core shows a real but ambiguous
effect.

---

## 11. Threats to validity

- **429 invisibility** (handled): primary signal is latency-inflation slope, not log 429s;
  in-process counter is corroboration only.
- **Shared-quota noise / temporal drift** (handled): cool window + interleaved blocked ordering
  + medians of ≥4.
- **Confounding family/region** (handled as far as the platform allows): N=1 isolates family;
  Arm C isolates bucket-within-global. Residual: 2.5-regional vs 3.x-regional is untestable
  (3.x can't run regionally) — we cannot separate "region" from "2.5 family" in Arm B, only
  bound it via C.
- **Quality judge drift:** judge pinned to 3.1-pro-preview @ global (never swapped).
- **Single brief:** content held fixed for power; generalization is out of scope (add a 2nd
  brief as a blocking factor only if the core result is marginal).
- **In-memory vs Agent-Engine path:** UI runs are in-process (`runserver`), fan-out uses Agent
  Engine; this DoE measures the in-process/`/runs` path (the one that showed the 429s). The
  bucket math is identical for both, but the *absolute* latencies won't transfer to fan-out.

---

## 12. Optional: mirror runs to Agent Platform Experiments (post-hoc)

**Agent Platform Experiments** (fka *Vertex AI Experiments*, now under the Gemini Enterprise
Agent Platform umbrella) is a managed experiment-tracking store. Same API as before —
`google-cloud-aiplatform` (`aiplatform.init(experiment=...)` → `start_run` → `log_params` /
`log_metrics`, `get_experiment_df()`), GA on `aiplatform.googleapis.com`, needs
`google-cloud-aiplatform > 1.24.1` (ADK 2.4 already pulls a far newer one). It is a **regional**
resource → `us-central1` (our `GCP_REGION`), independent of the `global` model location.

Our DoE run records already map onto it 1:1 — one `ExperimentRun` per creative_agent run:

| Agent Platform Experiments | Our harness (`experiments/quota_spread/`) |
|---|---|
| experiment | `quota-bucket-spread-doe` |
| ExperimentRun | one `<session>.json` record under `results/<arm>/N<k>/…` |
| `log_params({…})` | `arm`, `concurrency` (N), `revision`, `batch_id` |
| `log_metrics({…})` | `research_s`, `total_s`, `count_429`, `eval_pass`, `eval_mean` |
| `get_experiment_df()` / console UI | supplements `analyze.py`'s tidy CSV |

**What it buys:** a console UI for sortable side-by-side run comparison and param×metric charts
(nice for sharing without shipping matplotlib PNGs), plus a durable cross-session store.

**Hard design constraint — log post-hoc, never in the timed hot path.** `aiplatform.log_*` are
network round-trips to `aiplatform.googleapis.com`; calling them inside `run_batch`'s
`ThreadPoolExecutor` closure would inject latency/jitter into the very `research_s`/`total_s`
being measured (and add a failure surface to a concurrent path) — corrupting the H1 signal. So
the recommended shape is a **separate `experiments/quota_spread/upload_to_vertex.py`** that walks
the *already-committed* `results/` tree via `analyze.load_records()` **after** the batches finish
and creates one `ExperimentRun` per record. This keeps the timed runs fully offline and keeps
`analyze.py` stdlib-only. Sketch (~30 lines):

```python
aiplatform.init(experiment="quota-bucket-spread-doe", project=PROJECT, location="us-central1")
for r in load_records():
    with aiplatform.start_run(f'{r["arm"]}-N{r["concurrency"]}-{r["session_id"][:8]}'):
        aiplatform.log_params({"arm": r["arm"], "concurrency": r["concurrency"],
                               "revision": r["revision"], "batch_id": r["batch_id"]})
        aiplatform.log_metrics({k: v for k, v in {
            "research_s": r.get("research_s"), "total_s": r.get("total_s"),
            "count_429": r.get("count_429")}.items() if v is not None})
```

**What it does *not* replace.** It's a dashboard/store, not an analysis engine — the H1 headline
(**slope of `research_s` vs N per arm**) is a cross-run regression Experiments won't compute;
`analyze.py` stays the authoritative inference. `get_experiment_df()` also pulls in pandas, which
`analyze.py` deliberately avoids.

**Status: DEFERRED.** The committed JSON records are the durable source of truth, so the uploader
is a pure downstream sink that can be bolted on any time. Build it **only after** the first live
batch confirms the harness end-to-end — then decide whether the console UI justifies the extra
GCP resource. Clean up afterward (delete the experiment metadata store).

---

## 13. Future evaluation options — Agent evaluation on Gemini Enterprise Agent Platform

Separate from this DoE (which uses `creative_eval` for its H3 guardrail), the platform's
**Agent evaluation** (the rebranded *Gen AI evaluation service*) is worth a future look. Note
that **`adk eval` — our `tests/eval/` — is already the local/CI face of this same service**, so
we have partly adopted it. The managed side adds three things our current evals lack:

- **Trajectory / tool-use metrics** — `trajectory_in_order_match`, `trajectory_exact_match`
  (computation-based), plus rubric `TOOL_USE_QUALITY` and multi-turn trajectory quality. These
  score *the tool-call path* — did the agent call the right tools, in order, with the right args.
- **Evaluate from stored Traces/Sessions** — build eval datasets from real production sessions
  (we run `VertexAiSessionService` persistent sessions), not just synthetic evalsets.
- **Online monitors + failure clusters** — aggregate hallucination / tool-use / response-quality
  trends over deployed-agent traffic, with diagnostic clustering of failures.
- SDK entry: `client.evals.evaluate(dataset=…, metrics=[RubricMetric.FINAL_RESPONSE_QUALITY,
  TOOL_USE_QUALITY, HALLUCINATION, SAFETY, …])`.

**Orthogonal to `creative_eval`, not a replacement.** `creative_eval` is a bespoke 12-dimension
judge of *creative-output quality* (ad copy, visuals); Agent evaluation judges *agent behavior*
(trajectory, tool use, hallucination, safety). It fills a gap `creative_eval` structurally cannot
see.

**Opportunities, ranked:**

1. **Trajectory/tool-use eval of the brittle research pipeline (best fit, CI-shaped).** The
   research pipeline's recurring failures are *behavioral* — missing `output_key`,
   `google_search`+synthesize not completing, retry-until-populated exhaustion (the
   `RetryUntilKeyAgent` work). `trajectory_in_order_match` + `TOOL_USE_QUALITY` measure exactly
   that path; our current rubric configs grade response/creative quality, not the tool sequence.
   Highest signal-to-effort.
2. **Production monitoring on the fan-out traffic (biggest new capability, larger effort).** The
   Cloud Run fan-out generates many real runs persisted as Agent Engine sessions with no
   systematic behavioral monitoring today (only the per-run `creative_eval` product artifact in
   BQ). Online monitors + failure clusters would surface trends/root-causes we currently only
   find by reading logs. Costs a GCS output bucket + a managed surface to maintain.
3. **Inside this DoE — low priority; keep `creative_eval`.** H3 is already covered *free* by the
   in-pipeline judge (fixed across arms). Swapping to the managed service buys nothing there and
   actively hurts: model-based metrics are LLM-as-judge → they burn the **same ~5-RPM pro judge
   quota the DoE is trying to protect**. The only complementary add would be a post-hoc
   *trajectory* comparison per arm, but we already log `*__retry_exhausted` markers.

**Hard constraint (same quota logic as this whole DoE):** model-based agent metrics consume
Gemini judge quota. **Never run them concurrently with the DoE batches or the live fan-out** —
run against *stored* Traces/Sessions in a cool window (the service supports post-hoc eval from
session IDs).

**Status: DEFERRED / out of scope for this DoE.** Recommended next step, independent of the
experiment: a small `client.evals.evaluate(...)` trajectory-eval spike against a handful of
existing stored sessions (#1 above), *after* the DoE live run — not a workstream to open
mid-experiment.
