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
