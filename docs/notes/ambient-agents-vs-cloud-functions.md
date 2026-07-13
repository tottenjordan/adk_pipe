# Event-driven execution: ADK Ambient Agents vs. our Cloud Run Functions fan-out

**Date:** 2026-07-12 · **Branch:** `docs/readme-updates` (research note; no code change beyond
the duration instrumentation described below) · **Status:** decision recorded — keep the current
architecture as the primary executor; run a parallel Ambient-Agent experiment before committing.

This note captures a comparison between our current event-driven orchestration
(`cloud_funktions/creative_crf/`) and ADK's newer **Ambient Agent** concept
(<https://adk.dev/runtime/ambient-agents/>), plus the target event-native
architecture we want to move toward and the experiment that will decide it.

---

## TL;DR

- **Conceptually**, our creative fan-out is a textbook *ambient* workload: event-triggered,
  no human, background execution, results routed to external sinks (GCS/BigQuery).
- **Mechanically**, ADK's preferred ambient path — **trigger endpoints**
  (`/apps/{app}/trigger/pubsub` and `/trigger/eventarc`) — is **not a drop-in replacement**
  today, for two reasons:
  1. **10-minute synchronous ceiling.** Trigger endpoints hold the HTTP request open until
     the agent finishes and are capped by the Pub/Sub push ack deadline (~10 min). Our full
     creative pipeline runs *right around* 10 minutes and is growing. The ADK docs explicitly
     redirect >10-min workloads to "Pub/Sub pull subscriptions, Cloud Run Jobs, or a worker
     pool architecture" — which is essentially what we already built.
  2. **No built-in idempotency.** Ambient trigger sessions are ephemeral and per-delivery
     ("each redelivery creates a new session… stateless by design"). Pub/Sub is at-least-once,
     so we'd reprocess trends (each an expensive image-gen run) without the atomic BigQuery lock
     we rely on today.
- **Recommendation:** keep the current CRF + Agent Engine + BigQuery-lock fan-out as the primary
  executor. Adopt ambient concepts *selectively* (Eventarc triggering, retry defaults) and, per
  the owner's request, **stand up a parallel Ambient-Agent Cloud Run deployment as an experiment**
  to measure the differences before deciding.

---

## What we built today

A two-stage Pub/Sub fan-out where **the agent runs on Vertex AI Agent Engine** and the Cloud Run
Functions are thin orchestration glue (`cloud_funktions/creative_crf/main.py`):

- **Orchestrator** (`crf_entrypoint`, concurrency=100): Eventarc→Pub/Sub-triggered
  (`CREATIVE_TOPIC_NAME`/`CREATIVE_TRIGGER_NAME`) → queries BigQuery for `processed_status IS NULL`
  rows → marks them `QUEUED` → publishes one worker message per row (fire-and-forget).
- **Worker** (`agent_worker_entrypoint`, concurrency=1, timeout=900s): Pub/Sub-triggered
  (`CREATIVE_WORKER_TOPIC_NAME`) → **atomic BigQuery lock** (`acquire_processing_lock`:
  conditional `UPDATE … WHERE processed_status='QUEUED'`, verifying `num_dml_affected_rows == 1`)
  → invokes Agent Engine via `async_stream_query` → marks `PROCESSED` / `FAILED`.

The BigQuery status column (`NULL → QUEUED → PROCESSING → PROCESSED/FAILED`) does triple duty as
**work queue + idempotency lock + observability surface**.

> Note: `cloud_funktions/trawler_crf/main.py` is currently a `# TODO` stub — the trend_trawler
> half of the pipeline is not yet wired for scheduled/event-driven execution. The target
> architecture below is greenfield for that side.

---

## What an Ambient Agent is

An ADK agent configured to activate in response to external events/data rather than human input —
"run as background processes to process data, monitor events, and respond asynchronously." Two
implementation approaches:

- **Approach 1 — `/run` endpoint** (`adk api_server --auto_create_session`): full manual control;
  you parse the payload, create the session, handle concurrency/retries. A Cloud Run function
  acts as a thin webhook forwarder that POSTs to `/apps/{app}/run`. Best for non-GCP sources.
- **Approach 2 — Trigger endpoints** (preferred for GCP): ADK owns the plumbing. Sources are
  **Pub/Sub** (`/trigger/pubsub`) and **Eventarc** (`/trigger/eventarc`). On each event ADK
  parses the CloudEvent/Base64 payload, creates a UUID session, runs the agent with the normalized
  `{data, attributes}` as a user message, and returns `200` (ack) or `500` (retry).

Enabled at deploy time:

```
adk deploy cloud_run \
  --project=$GOOGLE_CLOUD_PROJECT --region=$GOOGLE_CLOUD_LOCATION \
  --trigger_sources="pubsub,eventarc" \
  path/to/your/agent
```

Config knobs (per-process): concurrency semaphore `ADK_TRIGGER_MAX_CONCURRENT` (default 10);
retry `ADK_TRIGGER_MAX_RETRIES` (3) with exponential backoff + jitter
(`ADK_TRIGGER_RETRY_BASE_DELAY`=1.0s, `ADK_TRIGGER_RETRY_MAX_DELAY`=30s).

---

## Compare & contrast

| Dimension | Current: CRF + Agent Engine | Ambient: trigger endpoints |
|---|---|---|
| Event parsing | Hand-rolled Base64/CloudEvent decode (×2 entrypoints) | Automatic (Base64, CloudEvents; normalized `{data, attributes}`) |
| Session lifecycle | Manual create/delete per run | Automatic per-event UUID session |
| Concurrency | Pub/Sub + worker concurrency=1 + BigQuery lock | Built-in per-process semaphore + Cloud Run autoscaling |
| Retry | Re-raise → Pub/Sub NACK/redeliver (coarse) | Built-in exp. backoff + jitter, then 500 → Pub/Sub |
| Idempotency / dedup | **Strong** — atomic BQ lock, exactly-once-ish | **None built-in** — per-delivery sessions, stateless |
| Max runtime | Worker timeout **900s**; CF Pub/Sub trigger extends ack deadline to the function timeout | **Synchronous, ~10-min hard ceiling** (Pub/Sub push ack) |
| Deployment | Two CF deployments from one source ("deploy twice") | One `adk deploy cloud_run --trigger_sources=…` |
| Agent hosting | Managed Agent Engine (scaling, sessions, tracing) | Self-hosted on Cloud Run |
| Glue to maintain | High (orchestrator + worker + lock + parse + stream loop) | Low (ADK owns the plumbing) |
| Eventarc / GCS/BQ-native triggers | Custom wiring | First-class |

### The two decisive factors

1. **10-minute ceiling.** Our pipeline (research → PDF → ad copy → visuals → eval judge) is
   ~10 min and growing. Trigger endpoints would time out; the docs point long workloads at the
   worker-pool/pull pattern we already have. **We must measure p95 before trusting any conclusion
   here** (see instrumentation below).
2. **Idempotency.** At-least-once delivery + expensive per-trend work means we can't drop the BQ
   lock. Ambient gives us nothing here, so a naive migration would force us to re-add exactly the
   lock we already wrote — erasing much of the "less glue" benefit.

---

## Recommendation

**Keep the current architecture as the primary batch executor.** It is the ADK-recommended shape
for >10-min agent work, it provides idempotency ambient lacks, and it keeps the managed Agent
Engine runtime. Migrating the *executor* to trigger endpoints would trade a working long-run design
for one with a timeout risk plus a re-implemented lock.

**Adopt ambient concepts selectively:**
- **Eventarc** to move toward event-native triggering (see target architecture).
- Borrow the ambient **retry defaults** (backoff + jitter) — today we just re-raise to Pub/Sub.
- Revisit trigger endpoints as the *executor* **only if** we decompose the pipeline into shorter
  (<10-min) stages, each its own event-triggered short agent. That is the architecture ambient is
  built for and would delete most of our glue. It's a larger refactor, tracked as a roadmap item.

---

## Q1 — Measuring pipeline duration (p95)

We had no wall-clock data, which is the single biggest input to the runtime-ceiling question. Added
lightweight, zero-schema-risk instrumentation to the worker: it times the end-to-end Agent Engine
run and emits a structured log marker on both the success and failure paths
(`cloud_funktions/creative_crf/main.py`, `_execute_agent_and_update_status`):

```
AGENT_RUN_DURATION_SECS row=<ts> index=<i> status=<PROCESSED|FAILED> secs=<float>
```

**Log-based p95 query** (Cloud Logging → Log Analytics / BigQuery-linked sink), once a handful of
runs have accumulated:

```sql
-- Extract secs=… from the structured marker and compute percentiles
SELECT
  APPROX_QUANTILES(
    CAST(REGEXP_EXTRACT(text_payload, r'secs=([0-9.]+)') AS FLOAT64), 100
  )[OFFSET(50)]  AS p50_secs,
  APPROX_QUANTILES(
    CAST(REGEXP_EXTRACT(text_payload, r'secs=([0-9.]+)') AS FLOAT64), 100
  )[OFFSET(95)]  AS p95_secs,
  COUNT(*)       AS runs
FROM `<log_sink_dataset>._AllLogs`
WHERE text_payload LIKE 'AGENT_RUN_DURATION_SECS%';
```

**Decision rule:** if p95 is reliably **< ~8 min**, trigger endpoints become viable and worth the
experiment below; if p95 flirts with **10+ min**, they're out for the executor and we stay put.
**Optional upgrade:** add a `duration_secs FLOAT64` column to the trends table and write it in
`update_rows_status` for first-class querying (deferred — it's a shared-schema change).

### Measured p95 — 20-row concurrent batch (2026-07-13)

First real run against a fresh, model-location-fixed `creative_agent` engine
(`1272898564162322432`, gemini-3.x pinned to `global`; the earlier runs 404'd before completing).
20 trends published to the orchestrator at 02:13:40Z, fanned out to the worker pool
(`creative-worker-crf`, concurrency=1, autoscaled to ~20 instances).

**Outcome: 11 PROCESSED, 9 FAILED (45% failure rate).**

| Metric | PROCESSED only (n=11) | All 20 (to terminal state) |
|---|---|---|
| p50 | **351 s ≈ 5.9 min** | 469 s ≈ 7.8 min |
| p90 | 464 s ≈ 7.7 min | — |
| p95 | **475 s ≈ 7.9 min** (469 s interp) | 662 s ≈ 11.0 min |
| min / max | 50 s / 475 s | 50 s / 679 s |

**Every one of the 9 failures was the same error:**
`google.genai.errors.ServerError: 503 UNAVAILABLE` ("The service is currently unavailable"),
raised from the model service ~605–679 s (≈10–11 min) into the run — i.e. after the runs had done
most of the work, then hit a sustained unavailability window. Failures are **not** a runtime-ceiling
timeout (worker timeout is 900 s and none reached it); they are the **model backend shedding load**
when ~20 pipelines hammer gemini-3.x `global` at once. The successful runs' durations climb
monotonically with completion order (50 s → 475 s), the classic signature of contention, not of a
per-run cost that grew.

**Verdict on the runtime-ceiling question:** the *successful-run* p95 (~7.9 min) lands just under the
~8-min bar, so the 10-min trigger-endpoint ceiling is **not** the immediate blocker for a single run.
**But this does not clear ambient for promotion**, for two reasons:

1. **The 45% 503 rate is the real blocker, and it is architecture-independent.** Both the current CRF
   worker pool and an ambient trigger deployment call the same gemini-3.x `global` backend; ambient
   would see the identical 503s under the same concurrency. Worse, ambient's *synchronous* trigger
   model turns a mid-run 503 into a failed HTTP request near the 10-min ack ceiling, whereas our
   worker catches it, marks the row `FAILED`, and the BQ lock lets a re-publish retry cleanly. The
   idempotency lock argument from the compare table is *reinforced*, not weakened.
2. **We must fix the 503s before trusting any p95.** Actions, in order: (a) **throttle fan-out
   concurrency** — the orchestrator currently dispatches all `QUEUED` rows at once; cap the worker
   pool `max-instances` (or batch the dispatch) so we don't self-inflict backend overload;
   (b) **honor the retry config end-to-end** — `INFRA_RETRY` lists `genai ServerError`, but these
   503s still surfaced as fatal, so the retry either exhausted its 3 attempts inside a long
   unavailability window or the failing call path (image/video gen via the direct genai client in
   `creative_agent/tools.py`) isn't covered by the ADK workflow `RetryConfig`; confirm and widen.
   Re-measure p95 *after* the failure rate is near zero — a p95 computed over an 11/20 survivor set
   is optimistic (it excludes exactly the slow, contended runs).

### Root cause: fan-out concurrency vs. actual project quotas (2026-07-13)

Pulled the live Vertex AI quotas (Service Usage API, project `hybrid-vertex` / `934903580331`) for
the `global` endpoint our gemini-3.x models use. They are **far** smaller than the code assumes:

| Model (role) | Metric | **Effective limit (project-wide, global)** |
|---|---|---|
| `gemini-3.1-pro-preview` (critic + eval judge) | `global_generate_content_requests_per_minute_per_project_per_base_model` | **5 RPM** |
| flash / flash-lite (workers/planners) | same metric, default bucket | **5 RPM** |
| `gemini-3.1-flash-image` (visual gen) | `generate_content_image_gen_per_project_per_base_model_global` | **2 RPM** |
| input / output tokens | `global_generate_content_{input,output}_tokens_…` | unlimited (`-1`) |

These are **per-project, shared across every instance** — not per-instance. Two hard mismatches:

1. **Our in-code rate limiter is per-instance and set 200× too high.** `rate_limit_callback`
   (`creative_agent/callbacks.py:99`) throttles at `rpm_quota = 1000` using a counter local to each
   agent instance. The real shared ceiling is **5 RPM** for the pro model. Even one instance is
   allowed 1000; twenty instances collectively assume 20,000 — against 5.
2. **A single pipeline already exceeds the pro quota by itself.** The eval judge fires up to
   `max_eval_workers = 12` **concurrent** `gemini-3.1-pro-preview` calls
   (`creative_eval/config.py`), plus the critic model is invoked several more times — all against a
   **5 RPM** pool. Visual generation needs ~6 image calls against a **2 RPM** pool. So even a
   *serialized, single* run is quota-bound; 20 concurrent runs are fundamentally impossible under
   these limits, which is why the losers of the quota race 503'd ~10 min in.

**Back-of-envelope:** 20 runs × ~18 pro-calls each ÷ 5 RPM ≈ **72 min** of pro-quota time; 20 × ~6
image-calls ÷ 2 RPM ≈ **60 min** of image-quota time. The batch is **quota-bound, not
compute-bound** — the ~8-min survivor p95 only reflects the handful that won the quota race early.

**Corrected fix (in priority order):**

1. **Request a Vertex quota increase** for the `global` endpoint on the models we actually use
   (pro RPM 5→e.g. 60+, image-gen RPM 2→e.g. 20+). These read like default sandbox limits;
   throttling *cannot* buy throughput the quota forbids. This is the real unblock for any batch.
2. **Make the fan-out quota-aware** until/unless quota rises: cap the worker pool so
   `concurrent_runs × per-run-peak-RPM ≤ project RPM`. With today's numbers that means
   effectively **max-instances = 1** (serialize trends), *and* cut in-run parallelism —
   drop `max_eval_workers` to ~2–4 and add real backoff — so even the single run stays under 5/2 RPM.
3. **Fix the rate limiter's mental model.** A per-instance counter can't protect a shared quota;
   `rpm_quota` should reflect the *project* ceiling divided by expected concurrent instances, or be
   replaced by reliance on server-side 429/backoff. Widen `INFRA_RETRY` to cover the direct genai
   image/video path in `creative_agent/tools.py` and confirm backoff actually engages on 503.

**Bottom line:** keep CRF as the executor (decision unchanged and strengthened). The ambient
experiment — and any real fan-out throughput — is gated on a **quota increase** first; concurrency
throttling is only the stopgap that keeps a single run alive under the current sandbox limits.

### Post-hardening re-measure — 10-row serialized batch (2026-07-13)

Re-ran the batch after landing the throttle-hardening (commit `a38dee0`): worker
`max-instances = 1` (serialize trends), `timeout = 1800s`, in-run parallelism cut
(`max_eval_workers 12→3`, creatives `6→4`), and image-gen backoff added — so a single run stays
under the 5 RPM (pro) / 2 RPM (image) `global` ceilings. Deployed against `creative_agent` v5
(reasoning-engine `1670341231277768704`). 10 trends published to the orchestrator ~03:24Z, fanned
out to the serialized worker.

**Outcome: 10 PROCESSED, 0 FAILED (0% failure rate).**

| Metric | PROCESSED (n=10) |
|---|---|
| p50 | **338.9 s ≈ 5.6 min** |
| p95 | **363.2 s ≈ 6.1 min** |
| mean | 290.9 s ≈ 4.8 min |
| min / max | 33.4 s / 366.2 s |

Durations are tightly clustered ~308–366 s (two fast outliers at 33 s and 131 s — light-work rows).
This is the first p95 computed over a **zero-failure** set, so it's not survivor-biased like the
prior 11/20 run. The **45% → 0% failure collapse** confirms the root cause was the shared-quota
contention, not a per-run compute or runtime-ceiling problem: serialize the fan-out under the 5/2
RPM ceiling and every run completes cleanly.

**Verdict — the runtime-ceiling question is settled:** true, unbiased **p95 ≈ 6.1 min**, comfortably
under the ~8-min bar and the 10-min trigger-endpoint ack ceiling. A single creative run fits inside
ambient's synchronous window with margin. **This clears the *duration* gate for the ambient
experiment (GO).** The two standing blockers are unchanged and independent of this result:
(1) real fan-out *throughput* is still gated on a **Vertex quota increase** — serialization makes a
single run reliable but caps the batch at one-trend-at-a-time; (2) ambient still lacks the
idempotency lock, so any experiment must reuse the BQ status lock. Net: **keep CRF as the executor;
the ambient experiment is duration-cleared and quota/idempotency-gated.**

---

## Q3 — Target event-native architecture

Two workflows, two trigger styles:

1. **trend_trawler → scheduled.** Cloud Scheduler (cron) → Pub/Sub topic → trigger. This is the
   natural fit and low-risk (`trawler_crf/main.py` is a stub today, so it's greenfield).
2. **creative_agent → on new BigQuery rows.** This is the nuanced one.

> **Caveat — BigQuery has no native per-row "row inserted" event.** Eventarc can trigger on BQ
> **audit-log job-completion** events (`google.cloud.bigquery.v2.JobService.InsertJob` /
> `jobCompleted`) — which fire when a **load or query job** writes — but **not on streaming
> inserts**. So "run when rows are added" needs a deliberate choice:
>
> - **(a) Eventarc on BQ audit logs** — works only if trend rows land via a load/query job, not
>   `insertAll` streaming; coarse (fires per job, not per row).
> - **(b) App-level event (recommended)** — have the trawler's persistence step *also* publish a
>   Pub/Sub message when it writes target trends to BQ. Most reliable, truly event-native, decouples
>   from BQ's audit-log semantics, and lets the message carry the exact rows/agent_resource_id.
> - **(c) Scheduled poll** — the current orchestrator pattern (Scheduler → orchestrator → BQ query).
>   Simplest; not strictly event-native.

**Recommendation for Q3:** trend_trawler on **(1)** Cloud Scheduler; creative_agent on **(b)**
app-level Pub/Sub emitted at BQ-write time. Keep the BigQuery status lock regardless of trigger
style — it's the idempotency guarantee, independent of what fires the run.

---

## Q2 — Parallel Ambient-Agent experiment (proposed, not yet built)

Per the owner: stand up a **parallel** Ambient-Agent Cloud Run deployment alongside the existing
CRF path so we can observe the differences on real traffic before committing. Sketch:

1. Deploy `creative_agent` (or a short sub-stage of it) to Cloud Run via
   `adk deploy cloud_run --trigger_sources="pubsub,eventarc"` — **without** decommissioning the
   CRF path.
2. Point a **separate** Pub/Sub push subscription (new topic, e.g. `creative-ambient-topic`) at
   `/apps/creative_agent/trigger/pubsub`; leave the existing worker topic untouched.
3. Mirror a *fraction* of trends to the ambient topic (shadow traffic), or drive it with a
   dedicated test trend, so both paths process comparable work.
4. Compare: end-to-end latency vs. the 10-min ceiling, timeout/retry behavior, cost, operational
   surface (one deployment vs. two), idempotency gaps (does at-least-once cause dupes without our
   lock?), and tracing/observability.
5. Decide: promote ambient (if p95 fits and dedup is solvable cheaply), keep CRF, or adopt the
   hybrid (Eventarc triggering into the existing worker-pool executor).

**Open prerequisites to resolve before building the experiment:** (i) p95 duration data from Q1;
(ii) confirm willingness to self-host the agent on Cloud Run for the experiment (Agent Engine is
the current managed runtime); (iii) a dedup strategy for the ambient path (reuse the BQ lock).

---

## Related

- `cloud_funktions/creative_crf/main.py` — the current fan-out (orchestrator + worker).
- `docs/notes/local-testing.md` — the ~12-min UI request timeout, headless Runner, where results land.
- GitHub issues for the two incidental bugs found while reviewing the worker (swallowed streaming
  exception → false `PROCESSED`; unguarded `message_payload`/`df` in `crf_entrypoint`).
