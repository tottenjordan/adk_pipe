# Where to serve the agent: Cloud Run vs Agent Engine

**Status:** proposal · **Date:** 2026-07-14 · **Author:** (design note)

**Scope:** This note is *only* about where the **agent executes**. A Cloud Run tier
(Next.js web + `/api/adk` proxy) fronts the browser in every option and is taken as a
given — it is not part of the decision. The question is whether the agent runs **in-process
in the Cloud Run `adk api_server`** (today) or **remotely in Agent Engine**.

## TL;DR

- **Two ways to serve the agent:** **A —** in-process in the Cloud Run `api_server`
  (today); **B —** Agent Engine `async_stream_query`, reached through the existing proxy.
- **B's real payoff is operational:** one runtime for both the UI and the CRF batch,
  eliminating the current dual-deploy drift. It is **not** a new capability, and it
  *lowers* the long-run ceiling (15 min vs Cloud Run's 60).
- **The failures you actually hit** (runs "produce no results", stall silently, 429) are
  about the **synchronous-streaming interaction model** and the **shared 5/2 RPM quota** —
  **neither is fixed by changing where the agent runs.**
- **Recommendation:** (1) move long runs to an **async job model** first (runtime-agnostic,
  fixes the real failures); (2) treat **quota** as its own workstream; (3) migrate agent
  execution to **Agent Engine** only if dual-deploy drift keeps hurting — and only after
  async makes the 15-min cap irrelevant.

---

## 1. How the agent is served today

- **Execution:** the `trend-trawler-api` Cloud Run service runs `adk api_server agents`;
  the agent runs **in-process** inside that service. Tuned for long runs already:
  `timeoutSeconds=900`, concurrency 320, 4 vCPU / 8 GiB, `maxScale=100`.
- **Sessions:** already externalised to Agent Engine
  (`SESSION_SERVICE_URI = agentengine://…/reasoningEngines/70718938631110656`).
- **Models:** Vertex AI, gemini-3.x served from `global`.
- **Batch path (separate):** the CRF fan-out runs the **same agent code** deployed as
  Agent Engine engines (`creative_agent` v7, `trend_scout` v2) — **not** used by the UI.

**The friction that motivates this note:** the same agent code is served **two ways** —
in-process for the UI, and as Agent Engine engines for batch. Keeping them in sync is a
standing chore ("redeploy the image *and* the engines") and a real drift risk.

---

## 2. The two ways to serve the agent

| | **A — Cloud Run `api_server`** (today) | **B — Agent Engine** |
|---|---|---|
| Where the agent runs | in-process in `trend-trawler-api` | managed Agent Engine runtime (`async_stream_query`) |
| How the UI reaches it | direct ADK REST/SSE from the proxy | proxy translates to/from `async_stream_query` |
| Agent deploy targets | **two** (Cloud Run image **and** batch engines) | **one** (UI and batch share the engine) |
| Sessions | Agent Engine (unchanged) | Agent Engine (unchanged) |

---

## 3. Scorecard (A vs B)

Legend: ✅ clear win · ⚠️ works with caveats · ❌ notable drawback

| Dimension | A | B | Why it matters for serving this agent |
|---|:--:|:--:|---|
| **Deploy drift** — one source of truth for agent code | ❌ two targets kept in sync by hand | ✅ one runtime for UI **and** batch | B's headline benefit; today every change means redeploy image *and* engines |
| **Change cost to adopt** | ✅ already live | ❌ new proxy translation + api-client rework | A is done; B is a build |
| **Long-run ceiling** (creative ≈ 5–8 min) | ✅ up to 60 min (at 15 now) | ⚠️ streamQuery capped at **15 min** (bidi 10) | A has headroom; B is tight under load unless runs go async (§4) |
| **Ops / maintenance** | ⚠️ you own the container + runtime | ✅ managed runtime, native tracing, sandbox isolation | B removes agent-tier container upkeep |
| **Cold start / provisioning** | ⚠️ container cold start | ✅ sub-second cold start, provision < 1 min, `min_instances` ≤ 10 | marginal for an internal tool |
| **Cost at low / bursty traffic** | ✅ per-request, scales to zero | ⚠️ billed on runtime vCPU/mem-hours (**verify**) | A likely cheaper for this workload |
| **Auth** | ✅ ID-token proxy in place | ⚠️ proxy authenticates to Vertex as SA (`aiplatform.user`) | comparable effort |
| **Long-run robustness** | ❌ fragile live SSE for minutes | ❌ *also* fragile if kept synchronous | **neither fixes it** — see §4 |
| **Model quota (5/2 RPM)** | ❌ shared, unraisable | ❌ identical | independent of where the agent runs |

**Reading:** B wins **deploy drift** and **ops**; A wins **already-shipped**, **long-run
headroom**, and **cost**. The two rows that hurt most today — **long-run robustness** and
**quota** — are ❌ for *both*, i.e. they are not a hosting decision.

---

## 4. The higher-leverage change: serve long runs as async jobs (runtime-agnostic)

The recurring failures are structural to **synchronous streaming**: one HTTP connection
held open for the entire multi-minute run. Any hiccup mid-run (network blip, IAP re-auth,
tab sleep, proxy recycle, a 429) surfaces as "no results / no error."

The CRF fan-out already avoids this: **fire the run, let it finish server-side, persist the
results.** Serving the UI's agent the same way is the real fix — and it applies equally to
A and B.

### 4.1 Target flow

```
POST /runs            → create session, kick off the run, return {runId} immediately
   │                     (run executes to completion server-side, untied to this request)
   ▼
run executes ──► appends events to a durable store as it goes
   │             writes artifacts (PDF, gallery, eval JSON) to GCS (as today)
   ▼
GET /runs/{id}/events?since=N   → UI polls (or a short-lived stream FROM the store)
GET /runs/{id}                  → status: running | done | error(+message)
```

The live stream is **decoupled from the run's lifetime**: the browser can disconnect,
reload, or return later and replay events. A 429 becomes a recorded `error` event, not a
dropped connection.

### 4.2 Minimal changes

- **Serving side:** a kick-off endpoint that starts the run in the background (or hands it
  to a worker), plus an events/status read endpoint over a durable log. Reuse existing
  artifact-to-GCS writes and `collect_degradation_warnings` surfacing.
- **Frontend:** `run/[sessionId]` polls `…/events?since=N` and renders the same timeline it
  already builds from SSE, plus a status pill. `results/[sessionId]` is unchanged.
- **Event store** (pick one): per-run GCS append object (cheapest, fits current usage);
  Firestore (nicer incremental/real-time reads); or the ADK session event log.

### 4.3 Why serve runs this way first

- Fixes the **actual** recurring failure under *both* A and B.
- Makes Agent Engine's **15-min streamQuery cap a non-issue**, which makes a later move to B
  much simpler — the proxy just forwards kick-off + reads, no long-lived SSE to babysit.
- Turns 429/quota failures into first-class recorded errors instead of silent stalls.

---

## 5. Recommendation & sequencing

1. **Now — serve long runs as async jobs (§4).** Highest leverage, runtime-agnostic.
2. **Parallel — quota workstream.** The 5/2 RPM ceiling gates real usage (see
   `vertex-model-quotas`): serialise stages, pace/backoff, escalate a quota increase, or
   use a dedicated project. No hosting change helps.
3. **Then — migrate agent execution to Agent Engine (B) only if drift keeps hurting**
   (§6), after async has neutralised the 15-min cap. If drift is tolerable, **A is a fine
   long-term home** — already tuned, cheaper at this traffic.

**Do not** move to Agent Engine as step one: it's build cost for a mostly-operational
payoff, it *lowers* the long-run ceiling, and it leaves both ❌ rows unsolved.

---

## 6. Option B migration checklist (only if/when we consolidate)

- [ ] Publish agents via `AdkApp(agent=root_agent, enable_tracing=True)` +
      `agent_engines.create(...)` — scaffold already stubbed in `deployment/deploy_agent.py`.
- [ ] Point the **UI at the same engine IDs the CRF batch uses** (single source of truth);
      pass the engine ID via config, not baked into an image.
- [ ] Build the proxy translation: `async_stream_query` ⇄ the frontend's event shape;
      sessions via the existing `VertexAiSessionService`; artifact reads from GCS. (Trivial
      once §4 async is in place.)
- [ ] Rework `frontend/src/lib/api.ts` (session CRUD, run kick-off, event reads).
- [ ] Confirm auth: proxy SA has `aiplatform.user`.
- [ ] Validate runs stay < **15 min** under load (or confirm async makes the cap moot).
- [ ] Retire the `adk api_server` execution path from `trend-trawler-api`.
- [ ] Delete the redundant deploy step (no more "redeploy the image *and* the engines").

---

## 7. Open questions before committing to B

- **Pricing:** Agent Engine runtime vCPU/mem-hour billing vs the current Cloud Run bill at
  *real* traffic (needs a concrete estimate — not in docs).
- **Real run duration under load:** does any creative run approach the **15-min**
  streamQuery cap when the pipeline is contended? If yes, §4 async is a prerequisite.
- **Event store choice** for §4 (GCS append vs Firestore vs ADK session log).
- **A2A / Memory Bank futures:** heading toward agent-to-agent protocols or managed Memory
  Bank tilts toward B independently of the above.

## References

- Agent Engine runtime limits (verified via Google dev-knowledge): streamQuery timeout
  **15 min**, bidi **10 min** / 10 concurrent, long-running ops up to 7 days, query quota
  **90/min**, sessions 100 writes & 10 000 reads/min, default 4 vCPU/4 GiB (1–8 vCPU,
  1–32 GiB), `max_instances` 1–1000 (100 with VPC-SC/PSC-I), sub-second cold start.
- Cloud Run: request timeout configurable to **60 min** (`trend-trawler-api` at 900 s).
- Related: `docs/notes/frontend.md`, `docs/notes/ambient-agents-vs-cloud-functions.md`,
  `deployment/README.md`, memory `vertex-model-quotas`.
