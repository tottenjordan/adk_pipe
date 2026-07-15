# Async-Job Run Model Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the browser-held synchronous SSE run (which silently drops multi-minute runs on any network blip, IAP re-auth, tab sleep, proxy recycle, or 429) with a fire-and-forget async-job model: kick the run off server-side, let it run to completion untied to the HTTP request, and have the UI poll the already-persisted session event log.

**Architecture:** Add async-job endpoints (`POST /runs/...` kick-off, `GET /runs/...` status+events, `POST /runs/.../resume`) to the ADK backend by mounting a custom FastAPI router onto `get_fast_api_app()`. The kick-off spawns a **detached `asyncio` task** that drives `Runner.run_async(...)` to completion, catching exceptions and writing a **terminal status marker** into the session. Because sessions live in `VertexAiSessionService` (Agent Engine) and the Runner appends every final event to them during the run, **the durable event store already exists** — the UI just polls `session_service.get_session().events` (which the resume fallback already does today). No new event store, no PubSub, no schema.

**Tech Stack:** google-adk 2.x (`get_fast_api_app`, `Runner`, `VertexAiSessionService`, `Event`, `EventActions`), FastAPI `APIRouter` + uvicorn, pydantic v2, pytest (offline with `InMemorySessionService` + a fake Runner double), Next.js 16 / TypeScript / Vitest, `uv` / `uvx ruff`, Cloud Run (`--no-cpu-throttling`), the isolated tagged-revision smoke harness + SA-impersonation auth recipe.

---

## Context (why this change)

The recurring "run produced no results / stalled silently" failures are structural to **synchronous streaming**, not to where the agent runs. Both ADK run endpoints are strictly request-bound and **cancel the run on client disconnect**:

- `POST /run` (`google/adk/cli/api_server.py:1533-1597`): a `monitor()` task awaits `request.receive()` and calls `worker_task.cancel()` on `http.disconnect` → returns 499.
- `POST /run_sse` (`api_server.py:1599-1693`): runs inside a `StreamingResponse` generator wrapped in `Aclosing(...)`; a disconnected SSE client closes the generator → the run is cancelled.

`deployment/headless_run.py` exists solely to *"bypass the api_server SSE/HTTP request timeout that kills UI runs during the ~5-min eval phase"* — proof the problem is real and the fix (drive `Runner.run_async` detached from any request) is known.

The design note `docs/notes/agent-engine-vs-cloud-run.md §4` recommends this as the **highest-leverage, runtime-agnostic** change: it fixes the actual failure under both Cloud Run and Agent Engine, turns 429s into recorded `error` events instead of dropped connections, and (later) neutralizes Agent Engine's 15-min streamQuery cap.

**The enabling fact (from exploration):** `SESSION_SERVICE_URI=agentengine://…` resolves to `VertexAiSessionService`; the `Session` model carries `events: list[Event]` (the full log); `GET /apps/{app}/users/{user}/sessions/{sid}` returns it; and the Runner persists every **final** (non-`partial`) event to the session service as the run proceeds. The frontend's `handleResume` zero-events fallback (`frontend/src/app/run/[sessionId]/page.tsx:753-794`) already reconstructs pause state by fetching `getSession().events` and scanning them — i.e. poll-from-session is a proven pattern in this codebase, we're just generalizing it to the whole run.

**Design decision (recommended; call out on review):** **in-process detached `asyncio` task** on the existing `trend-trawler-api` Cloud Run service, not a new PubSub worker. Rationale: the event store already exists (Vertex sessions), it adds zero new infra/drift, and it keeps all agent execution in one place. The two real risks — Cloud Run CPU throttling and instance recycling — have concrete mitigations (Task 6 `--no-cpu-throttling`; frontend stall-timeout) and a documented escalation path (Variant 2 below) if in-process proves fragile.

**Variant 2 (escalation, NOT built here):** hand UI runs to the existing CRF-style worker via PubSub (`cloud_functions/creative_fanout` pattern) so a crashed web instance can't orphan a run (PubSub redelivery + BQ status lock). More robust, more infra. Only pursue if Task 10's live smoke shows in-process runs getting orphaned by instance recycling under real load.

---

## Key facts from exploration (do not re-discover)

**Backend**
- `deployment/backend_entrypoint.sh` runs **canned** `adk api_server agents --host 0.0.0.0 --port $PORT [--session_service_uri $SESSION_SERVICE_URI]`. No custom app today. `agents/` is a dir of symlinks (`agents/creative_agent -> ../creative_agent`, etc.).
- `get_fast_api_app(...)` (`google/adk/cli/fast_api.py:403`) returns a plain mountable `fastapi.FastAPI`; it does **not** accept an existing `app=`. Class docstring (`api_server.py:655-657`) explicitly supports adding endpoints to the returned app. To mount our router we replace the CLI entrypoint with a tiny launcher module that calls `get_fast_api_app()`, `app.include_router(...)`, and runs uvicorn.
- Sessions: `SESSION_SERVICE_URI` → `VertexAiSessionService` (scheme `agentengine`, `cli/service_registry.py:245-283`). `Session.events` persisted; `get_session` returns them. Runner appends each **final** event during the run (`partial` streaming chunks are NOT persisted — acceptable, the timeline renders final events and pause-detection already skips `partial`).
- Artifacts: entrypoint passes no `--artifact_service_uri` → default **ephemeral local `FileArtifactService`**. Not a blocker: agents write final outputs (PDF/HTML/eval JSON) to **GCS** via their own tools, and the results page reads GCS, not artifacts. (Optional hardening: wire a `gs://` artifact URI later — out of scope.)
- Root agents: `creative_agent/agent.py:879`, `trend_scout/agent.py:325` (`root_agent = trend_scout`), `interactive_creative/agent.py:23` (resumable, 3 `LongRunningFunctionTool` checkpoints). In-process Runner example: `deployment/headless_run.py:85-108` (`Runner(app_name, agent=root_agent, session_service, artifact_service)` + `runner.run_async(user_id, session_id, new_message)`).

**Frontend (the contract to preserve) — `frontend/src/lib/`**
- `api.ts`: `API_BASE = "/api/adk"`. `createSession` (`POST /apps/{app}/users/{user}/sessions`, body `{state}`), `getSession` (`GET …/sessions/{sid}` → `{state, events[]}`), `streamRun` (`POST /run_sse`, body `{appName,userId,sessionId,newMessage:{role:"user",parts:[{text}]},streaming:true}`), `resumeRun` (`POST /run_sse`, `newMessage.parts:[{functionResponse:{id,name,response}}]` + `functionCallEventId`), `getEventError` (429/RESOURCE_EXHAUSTED friendly). Campaign metadata is passed **as message text**, stashed in `sessionStorage["run:"+sessionId]`.
- `types.ts:24` `AgentEvent`: load-bearing fields the timeline/logic read = `id` (dedup + React key), `author`, `timestamp` (epoch **seconds**, may be absent), `content.parts[]` (`text` / `functionCall{id,name,args}` / `functionResponse{name,response.status}`), `partial`, `longRunningToolIds`, `actions.stateDelta` (→ merged `sessionState` → all sidebar/widgets/results), `errorCode`/`errorMessage`/`error`.
- Run page `run/[sessionId]/page.tsx`: kickoff guarded by `startedRef`; per-event loop (`:616-674`) = dedup-by-`id` (`seenEventIds`), `getEventError`→error, append, merge `stateDelta`, **pause detect** only when `!partial && longRunningToolIds.length>0` (match `functionCall.id` ∈ `longRunningToolIds`). Stream-end → `completed`. `handleResume` (`:688-801`) mirrors it and has the getSession fallback (`:753-794`).
- Proxy `api/adk/[...path]/route.ts`: same-origin catch-all → private backend with metadata ID token; strips inbound IAP creds; **streams SSE through** with a zero-timeout undici `Agent` (`PROXY_STREAM_TIMEOUTS`). Under poll, run requests become short GETs → the streaming/zero-timeout machinery is no longer needed for runs (keep the proxy + auth; the timeout override becomes dead code for run traffic).
- Results page `results/[sessionId]/page.tsx`: pure session-state + GCS reads, **consumes no events** → unaffected by this change.
- Tests `frontend/src/__tests__/`: `api-client.test.ts` (SSE parser — to be replaced by poll tests), `interactive-mode.test.ts` (`detectPause` + resume body — **must keep passing**), `event-error.test.ts` + `event-log-time.test.ts` (import real fns — keep), `adk-proxy-stream.test.ts` (zero-timeout — trim/retire), `adk-proxy-auth.test.ts` (keep).

**Reference async pattern — `cloud_functions/creative_fanout/`**
- `session.py:agent_session` create→yield→delete CM, single `user_id`, delete-in-`finally`. **We do NOT delete the UI session** (the frontend must poll it) — mirror the single-user_id discipline but skip teardown.
- `main.py` state-machine + `agent_common/observability.py:collect_degradation_warnings` (surfaces `*__retry_exhausted` markers) — already fire on the agents' own state; unaffected.

---

## Tasks

### Task 1: Backend async-run module skeleton + pure helpers *(TDD, offline)*

**Files:**
- Create: `runserver/__init__.py`, `runserver/async_runs.py`
- Test: `tests/test_async_runs.py`

Put the async-job code in a new top-level package `runserver/` (flat, like the agent packages, so it bundles/imports cleanly and is importable in tests without ADC). It holds the FastAPI router + helpers; it imports the three root agents.

**Step 1 — failing tests** (`tests/test_async_runs.py`, offline, no creds — do NOT import any agent package at module top; import `runserver.async_runs` helpers only, and keep agent-touching tests behind the creds gate like `test_pipeline_structure.py`):
- `test_app_name_to_agent_maps_three_agents`: a pure `ROOT_AGENTS` mapping / `get_root_agent(app_name)` returns the right root for `"creative_agent"`, `"trend_scout"`, `"interactive_creative"`; raises `KeyError`/HTTP 404 shape for unknown. (Gate behind creds — importing roots builds module-level genai clients; follow the `test_pipeline_structure.py` pattern.)
- `test_build_user_message_text`: `build_user_message(text)` → `types.Content(role="user", parts=[Part(text=...)])`.
- `test_build_resume_message`: `build_resume_message(function_call_id, name, response)` → `Content(role="user", parts=[Part(function_response=FunctionResponse(id=…,name=…,response=…))])`.
- `test_terminal_marker_event_done` / `_error`: `build_terminal_event(status, error=None)` returns an ADK `Event` whose `actions.state_delta` carries `{"__run_status": "done"}` (or `{"__run_status":"error","__run_error": <msg>}`), authored by a stable non-agent author (e.g. `"__runserver__"`), no `content` parts that would render in the timeline.
- `test_events_since_slices_by_index`: `events_since(events, n)` returns `events[n:]` (int cursor); `n<=0` → all; `n>len` → `[]`.

**Step 2 — run → FAIL:** `export PATH="$HOME/.local/bin:$PATH"; cd /home/user/adk_pipe; PYTHONPATH="$PWD" uv run pytest tests/test_async_runs.py -q`.

**Step 3 — implement** the pure helpers in `runserver/async_runs.py`:
```python
from google.genai import types
from google.adk.events import Event, EventActions

RUNSERVER_AUTHOR = "__runserver__"
RUN_STATUS_KEY = "__run_status"
RUN_ERROR_KEY = "__run_error"

ROOT_AGENTS = {}  # populated lazily to keep import creds-light where possible

def get_root_agent(app_name: str):
    from creative_agent.agent import root_agent as creative
    from trend_scout.agent import root_agent as scout
    from interactive_creative.agent import root_agent as interactive
    agents = {
        "creative_agent": creative,
        "trend_scout": scout,
        "interactive_creative": interactive,
    }
    if app_name not in agents:
        raise KeyError(app_name)
    return agents[app_name]

def build_user_message(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])

def build_resume_message(function_call_id, name, response) -> types.Content:
    return types.Content(role="user", parts=[types.Part(
        function_response=types.FunctionResponse(id=function_call_id, name=name, response=response)
    )])

def build_terminal_event(status: str, error: str | None = None) -> Event:
    delta = {RUN_STATUS_KEY: status}
    if error is not None:
        delta[RUN_ERROR_KEY] = error
    return Event(author=RUNSERVER_AUTHOR, actions=EventActions(state_delta=delta))

def events_since(events, n: int):
    return events[max(n, 0):] if n and n > 0 else list(events)
```
Confirm the exact `Event`/`EventActions` import paths and the `state_delta` field name against the installed adk (`uv run python -c "from google.adk.events import Event, EventActions; import inspect; print(inspect.signature(EventActions))"`) before finalizing.

**Step 4 — run → PASS.** `uvx ruff check runserver/ tests/test_async_runs.py`.

**Step 5 — commit:** `feat(runserver): async-run helpers (message/terminal-marker/cursor)`.

---

### Task 2: Kick-off endpoint + detached run wrapper *(TDD, offline with fakes)*

**Files:**
- Modify: `runserver/async_runs.py`
- Test: `tests/test_async_runs.py`

The kick-off must (a) ensure the session exists, (b) spawn a detached `asyncio.create_task` that drives `Runner.run_async(...)` to completion, (c) on completion append `build_terminal_event("done")`, on exception append `build_terminal_event("error", str(exc))` — both via `session_service.append_event(session, event)` so any polling instance sees terminal status, (d) return `{runId, status:"running"}` immediately without awaiting the task.

**Step 1 — failing tests** (offline; use `InMemorySessionService` + a **fake runner** double whose `run_async` is an async generator yielding a couple of `Event`s then, in an error variant, raising):
- `test_kickoff_starts_detached_task_and_returns_runid`: call the kick-off coroutine with an injected fake runner + `InMemorySessionService`; assert it returns `{runId: session_id, status:"running"}` promptly, then `await` a drain hook (expose the created task, e.g. return it or store in a registry) and assert the fake's events were appended to the session and a terminal `__run_status=="done"` marker was appended last.
- `test_kickoff_records_error_marker_on_exception`: fake runner raises mid-stream → after drain, session has `__run_status=="error"` and `__run_error` contains the message; **no exception escapes** the kick-off.
- `test_kickoff_creates_session_if_missing` / `test_kickoff_uses_existing_session`.

Design the runner wrapper to accept an injected `runner_factory(app_name) -> Runner` and `session_service` so tests pass fakes (no agents, no creds). Keep a module-level `_RUNNER_CACHE` for prod but allow override.

**Step 2 — run → FAIL.**

**Step 3 — implement:**
```python
async def _drive_run(runner, session_service, app_name, user_id, session_id, new_message):
    try:
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=new_message):
            pass  # Runner already persists final events to session_service
        session = await session_service.get_session(app_name=app_name, user_id=user_id, session_id=session_id)
        await session_service.append_event(session, build_terminal_event("done"))
    except Exception as exc:  # noqa: BLE001 — terminal marker is the contract; log+persist, never raise
        logging.exception("detached run failed app=%s session=%s", app_name, session_id)
        session = await session_service.get_session(app_name=app_name, user_id=user_id, session_id=session_id)
        await session_service.append_event(session, build_terminal_event("error", str(exc)))

async def start_run(*, app_name, user_id, session_id, message, session_service, runner_factory):
    # ensure session exists
    existing = await session_service.get_session(app_name=app_name, user_id=user_id, session_id=session_id)
    if existing is None:
        await session_service.create_session(app_name=app_name, user_id=user_id, session_id=session_id, state={})
    runner = runner_factory(app_name)
    task = asyncio.create_task(_drive_run(runner, session_service, app_name, user_id, session_id, build_user_message(message)))
    _BACKGROUND_TASKS.add(task); task.add_done_callback(_BACKGROUND_TASKS.discard)  # keep a ref so GC can't cancel it
    return {"runId": session_id, "status": "running"}, task
```
Keep a module-level `_BACKGROUND_TASKS: set` (prevents the task being GC'd — a real asyncio footgun). Verify `Runner.run_async` persists final events to `VertexAiSessionService` without an explicit append (it does for the canned server; confirm in Task 10 live smoke). The `append_event` signature must be confirmed against installed adk.

**Step 4 — run → PASS.** ruff.

**Step 5 — commit:** `feat(runserver): detached kick-off with terminal status marker`.

---

### Task 3: Poll endpoint (status + events + state) *(TDD, offline)*

**Files:**
- Modify: `runserver/async_runs.py`
- Test: `tests/test_async_runs.py`

**Step 1 — failing tests** (seed an `InMemorySessionService` session with a few events incl. `stateDelta`s and optionally a terminal marker):
- `test_poll_returns_events_since_cursor`: `get_run_status(..., since=N)` returns `{status, events: events[N:], nextCursor: len(events), state}`.
- `test_poll_status_running_when_no_marker`: no terminal marker and no error event → `status=="running"`.
- `test_poll_status_done_on_marker` / `test_poll_status_error_on_marker`: `__run_status` marker → `"done"`/`"error"` (+ `error` message from `__run_error`).
- `test_poll_status_error_on_error_event`: an event carrying `errorCode`/`errorMessage` (mirror `getEventError`) → `status=="error"` even without a marker (in-pipeline model 429).
- `test_poll_state_is_session_state`: returns the session's merged `state` dict (so the frontend can seed sidebar/widgets without replaying every delta).

**Step 2 — FAIL.**

**Step 3 — implement** `get_run_status(*, app_name, user_id, session_id, since, session_service)`: `get_session`, derive status (marker wins; else scan for error event; else `"running"`), slice events via `events_since`, return the dict. Serialize events with the same field names the frontend expects (`id, author, timestamp, content, actions, longRunningToolIds, partial, errorCode, errorMessage, error`) — reuse ADK's event `.model_dump()`/`to_dict` (confirm which the canned `get_session` uses so the JSON shape matches `AgentEvent` exactly).

**Step 4 — PASS.** ruff.

**Step 5 — commit:** `feat(runserver): poll endpoint (status + events-since + state)`.

---

### Task 4: Resume endpoint (detached) *(TDD, offline)*

**Files:**
- Modify: `runserver/async_runs.py`
- Test: `tests/test_async_runs.py`

Interactive checkpoints must resume detached too (the resume segment also runs minutes). Resume is a second kick-off whose `new_message` is a `functionResponse` and which passes the paused `functionCallEventId` through to the Runner.

**Step 1 — failing tests:**
- `test_resume_builds_function_response_message_and_drives_run`: `start_resume(..., function_call_id, function_name, response, function_call_event_id)` builds the resume Content, drives the run detached, and writes the terminal marker (reuse `_drive_run` with the resume message; thread `function_call_event_id` if the Runner API needs it — confirm how the canned `/run_sse` resume passes it, `api_server.py` resume path).
- `test_resume_records_error_marker_on_exception`.

**Step 2 — FAIL. Step 3 — implement** (share `_drive_run`; add a `run_config`/kwarg for the paused event id if required by `runner.run_async`). **Step 4 — PASS.** ruff.

**Step 5 — commit:** `feat(runserver): detached resume for interactive checkpoints`.

---

### Task 5: Wire the FastAPI router + custom launcher *(TDD-light: import smoke + route table)*

**Files:**
- Modify: `runserver/async_runs.py` (add `router = APIRouter()` + route handlers delegating to the tested functions)
- Create: `deployment/async_app.py` (launcher)
- Modify: `deployment/backend_entrypoint.sh`
- Test: `tests/test_async_runs.py` (route registration), manual import smoke

**Routes** (mounted under the same origin the frontend proxies to):
- `POST /runs/{app_name}` — body `{userId, sessionId, message}` → `start_run(...)` → `{runId,status}`. (Session may be pre-created by the frontend's existing `createSession`; `start_run` is idempotent about it.)
- `GET /runs/{app_name}/{user_id}/{session_id}?since=N` → `get_run_status(...)`.
- `POST /runs/{app_name}/{user_id}/{session_id}/resume` — body `{functionCallId, functionName, response, functionCallEventId}` → `start_resume(...)`.

Build the prod `runner_factory` + shared `VertexAiSessionService` from `SESSION_SERVICE_URI` here (mirror how the canned server resolves it via `cli/service_registry.py`; simplest: import and construct `VertexAiSessionService` from the parsed URI, or reuse ADK's factory). Use the **same** session backend as the canned server so `createSession`/`getSession` and our poll see one store.

**`deployment/async_app.py`:**
```python
import os
from google.adk.cli.fast_api import get_fast_api_app
from runserver.async_runs import router

app = get_fast_api_app(
    agents_dir="agents",
    session_service_uri=os.getenv("SESSION_SERVICE_URI") or None,
    allow_origins=(os.getenv("ALLOW_ORIGINS") or "").split(",") if os.getenv("ALLOW_ORIGINS") else None,
    web=False,
)
app.include_router(router)
```

**`backend_entrypoint.sh`** — replace the canned `adk api_server` invocation with uvicorn on the mounted app, preserving the `SESSION_SERVICE_URI` behavior (now read inside `async_app.py`):
```sh
exec uv run uvicorn deployment.async_app:app --host 0.0.0.0 --port "${PORT:-8080}"
```
Keep the canned CRUD/`list-apps`/`getSession`/artifacts endpoints intact (they come free from `get_fast_api_app`; the frontend still uses `createSession`/`getSession`/`listArtifacts`/`getArtifact`).

**Step 1 — failing test:** `test_router_registers_expected_paths`: import `runserver.async_runs.router`, assert the three paths + methods are present (`{r.path, m for r in router.routes for m in r.methods}`). This is creds-light (router construction shouldn't import agents — defer `get_root_agent` to request time).

**Step 2 — FAIL. Step 3 — implement** the router + launcher + entrypoint. **Step 4 — PASS** + import smoke: `PYTHONPATH="$PWD" uv run python -c "import deployment.async_app"` (needs ADC for agent imports — run on the deploy host/this session).

**Step 5 — commit:** `feat(runserver): mount async-run router on ADK app + uvicorn launcher`.

---

### Task 6: Deploy config — CPU always allocated + bundle `runserver/` *(config + docs)*

**Files:**
- Modify: `deployment/README.md` (Frontend + api_server on Cloud Run runbook)
- Modify: deploy invocation / any `deployment/deploy_*` doc for `trend-trawler-api`
- Verify: `runserver/` is included in the container build context (it's at repo root → the `--source .` build picks it up; confirm no `.gcloudignore`/`.dockerignore` excludes it)

**Why:** a detached `asyncio` task that outlives the kick-off HTTP response only keeps running if the Cloud Run instance has **CPU allocated outside requests**. Default is CPU-throttled-between-requests → the background run would stall the instant `POST /runs` returns. Fix: deploy `trend-trawler-api` with **`--no-cpu-throttling`** (CPU always allocated / instance-based billing). Note the cost implication (billed for allocated CPU on idle instances) in the runbook — acceptable for an internal tool at `min-instances` low.

- Document the new deploy flag and the async-run env (`SESSION_SERVICE_URI` unchanged; add `ALLOW_ORIGINS` if used).
- Add a `deployment/README.md` subsection "Async-job run model" describing kick-off/poll/resume, the terminal marker, and the `--no-cpu-throttling` requirement + the instance-recycling caveat (a run orphaned by instance shutdown is surfaced to the user via the frontend stall-timeout, Task 8; Variant-2 worker is the escalation).

**No code test here.** **Commit:** `docs(deployment): async-run deploy config (--no-cpu-throttling) + runbook`.

---

### Task 7: Frontend API client — `pollRun` + async resume *(TDD, Vitest)*

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Test: `frontend/src/__tests__/poll-run.test.ts` (new), update `api-client.test.ts`

Replace the SSE generator with a poll loop that preserves the exact `AgentEvent` consumption contract (so `run/[sessionId]/page.tsx` changes minimally).

**Step 1 — failing tests** (`poll-run.test.ts`, mock `fetch`):
- `test kickoff posts correct body`: `startRun(appName,userId,sessionId,message)` → `POST /api/adk/runs/{appName}` body `{userId,sessionId,message}`.
- `pollRun yields only new events by cursor`: mock sequential `GET /api/adk/runs/{app}/{user}/{sid}?since=N` returning `{status,events,nextCursor,state}`; assert the async iterator yields each event once, advances `since` to `nextCursor`, and stops when `status!=="running"`.
- `pollRun surfaces error status`: `{status:"error", error}` → iterator throws/emits an error event that `getEventError` catches (keep the shape the run page expects).
- `resumeRun posts function response`: `POST /api/adk/runs/{app}/{user}/{sid}/resume` body `{functionCallId,functionName,response,functionCallEventId}`.
- Keep `interactive-mode.test.ts`'s `detectPause` importing the unchanged pure helper.

**Step 2 — FAIL.**

**Step 3 — implement:**
```ts
export async function startRun(appName, userId, sessionId, message) {
  const res = await fetch(`${API_BASE}/runs/${appName}`, { method:"POST",
    headers:{ "Content-Type":"application/json" }, body: JSON.stringify({ userId, sessionId, message }) });
  if (!res.ok) throw new Error(`Failed to start run (${res.status}): ${await res.text()}`);
  return res.json();  // {runId,status}
}

export async function* pollRun(appName, userId, sessionId, { intervalMs = 1500, signal } = {}) {
  let since = 0;
  for (;;) {
    const res = await fetch(`${API_BASE}/runs/${appName}/${userId}/${sessionId}?since=${since}`, { signal });
    if (!res.ok) throw new Error(`Failed to poll run (${res.status})`);
    const { status, events, nextCursor, error } = await res.json();
    for (const ev of events) yield ev as AgentEvent;
    since = nextCursor ?? since + events.length;
    if (status === "error") throw new Error(error || "run failed");
    if (status !== "running") return;              // "done"
    await new Promise(r => setTimeout(r, intervalMs));
  }
}
```
`resumeRun` → POST the resume body, then the caller re-enters `pollRun`. Keep `getEventError`, `createSession`, `getSession` unchanged.

**Step 4 — run → PASS.** `cd frontend && npm test`. Remove the now-dead SSE-parser assertions from `api-client.test.ts` (keep session/create URL + body assertions).

**Step 5 — commit:** `feat(frontend): pollRun/startRun async-job client (replaces SSE streamRun)`.

---

### Task 8: Frontend run page — swap stream for poll + stall-timeout *(TDD where testable)*

**Files:**
- Modify: `frontend/src/app/run/[sessionId]/page.tsx`
- Modify/trim: `frontend/src/__tests__/adk-proxy-stream.test.ts` (SSE timeout override no longer used by runs)
- Test: existing `interactive-mode.test.ts` must stay green

**Changes (preserve the per-event loop contract exactly):**
- Kick-off: after `createSession` (unchanged), call `startRun(...)` then iterate `for await (const event of pollRun(appName,userId,sessionId,{signal}))`. Keep `startedRef`, `seenEventIds` dedup, `getEventError`→error, append, `stateDelta` merge, and the `!partial && longRunningToolIds` pause detection **verbatim** — poll yields the same `AgentEvent`s. On `pollRun` normal return → `completed`; on throw → `error`.
- Seed initial state from the poll payload's `state` (so sidebar/widgets populate immediately on reconnect/reload) before/at first event batch.
- **Reconnect/reload survives:** because events live in the session, a page reload re-enters `pollRun` from `since=0` and replays the whole run (dedup handles re-render). This is the core win — remove any assumption that the run dies with the tab.
- **Stall-timeout:** if `status==="running"` but no new events for e.g. 3 min AND no pause, show a "run may have stalled" state (covers the instance-recycling orphan case). Make the threshold a const.
- `handleResume`: call the new async `resumeRun(...)` then re-enter `pollRun`; keep the pause re-detection + the getSession fallback (now largely redundant but harmless).
- Interactive: unchanged pause/resume UX; the review panels still read `sessionState`.

**Proxy:** no code change required (the catch-all proxy already forwards `/runs/*`); the `PROXY_STREAM_TIMEOUTS` zero-timeout override is now dead for run traffic — either leave it (harmless) or trim it and update `adk-proxy-stream.test.ts`. Prefer trimming to avoid confusion; keep `adk-proxy-auth.test.ts`.

**Step — tests:** keep `interactive-mode.test.ts` green (pause detection unchanged). Add a small unit test for the stall-timeout helper if extracted as a pure fn. `cd frontend && npm test`.

**Commit:** `feat(frontend): run page polls async job (survives disconnect/reload) + stall-timeout`.

---

### Task 9: Full offline gate *(gate)*

- `PYTHONPATH="$PWD" uv run pytest tests/ -q` → green (note `test_async_runs.py` agent-touching cases + `test_pipeline_structure.py` are creds-gated — run with ADC on the deploy host / this session).
- `uvx ruff check . && uvx ruff format --check .` → clean.
- `cd frontend && npm test` → green (incl. `interactive-mode`, `event-error`, `event-log-time`, `adk-proxy-auth`).
- Import smoke: `PYTHONPATH="$PWD" uv run python -c "import deployment.async_app; import runserver.async_runs"`.

---

### Task 10: Live smoke on an isolated tagged revision *(gate before PR ready)*

Deploy the branch as a **no-traffic tagged revision with CPU always allocated** (auto-mode-allowed — private service, no auth change):
```
gcloud run deploy trend-trawler-api --source . --region us-central1 --project hybrid-vertex \
  --no-cpu-throttling --no-traffic --tag async-job
```
Verify clean boot: `/list-apps` (SA-impersonation token, audience = BASE service URL) returns all 3 agents. Then, hitting the **tag URL**:

- **creative_agent async run:** `POST /apps/creative_agent/users/{uid}/sessions/{sid}` (lowercase/digits/hyphens id), then `POST /runs/creative_agent` `{userId,sessionId,message:"<campaign metadata as text>"}` (build with `json.dumps`). Immediately poll `GET /runs/creative_agent/{uid}/{sid}?since=0`. **Verify:** kick-off returns `{status:"running"}` fast; polling shows events accumulating; **stop polling / "disconnect" for 60s, then resume polling** and confirm the run **kept going server-side** (events continued) — the core disconnect-survival proof; run reaches a `__run_status:"done"` terminal marker; `combined_final_cited_report` present; citations rendered; eval→GCS→BQ succeed.
- **trend_scout async run** (`/runs/trend_scout`): 3 trends, `write_trends_to_bq` success, terminal `done`.
- **interactive_creative:** kick off, poll to the first checkpoint (pause = unresolved `longRunningToolIds`), `POST /runs/interactive_creative/.../resume` with the functionResponse, poll through all 3 checkpoints to `done`. Confirm resume also runs detached (disconnect between resume and completion, then re-poll).
- **429/error path:** if a real 429 occurs, confirm it lands as a recorded `error` event (poll `status:"error"`) — not a silent stall. (Optionally force by bursting.)
- **Reload survival:** re-issue `GET /runs/...?since=0` from scratch mid-run → full replay.

Delete the tagged revision when done (once a newer revision exists).

---

### Task 11: Finalize *(only when the user asks to commit/deploy)*

- Ensure this plan is at `docs/plans/2026-07-15-async-job-run-model.md` (it is).
- Per-task conventional commits already made (`feat(runserver|frontend|deployment):` / `test:` / `docs:`); **no `Co-Authored-By`**. Branch off `main`; open a PR (body ends with the Claude Code trailer).
- **Prod deploy (when approved):** `gcloud run deploy trend-trawler-api --source . --no-cpu-throttling --no-traffic --tag main-async` → verify boot → `update-traffic --to-revisions <rev>=100 --update-tags main-current=<rev>` (traffic is pinned; new revisions land at 0%). Prune the old revision after.
- Update memory: `adk-pipe-work-status` (async-job model shipped), and add a note that the run path is now poll-based (supersedes the SSE description in `frontend.md`/CLAUDE.md — update those docs too).

---

## Verification (end-to-end)

- **Offline (CI-safe):** `test_async_runs.py` proves the pure helpers, the detached kick-off writes a `done`/`error` terminal marker (fake runner, `InMemorySessionService`, no creds), the poll endpoint derives status + slices events by cursor + returns state, and resume drives a detached functionResponse run. Frontend Vitest proves `startRun`/`pollRun` request shapes, cursor-dedup, error surfacing, and that `detectPause`/resume-body are unchanged.
- **Structure (creds-gated):** router registers the three `/runs` paths; `deployment.async_app` imports and mounts the router on the ADK app; `get_root_agent` maps all three agents.
- **Live (isolated tag, prod untouched):** a creative_agent run **survives a 60s polling gap** and completes server-side to a `done` marker with real artifacts (PDF/HTML/eval) in GCS + BQ eval row; trend_scout writes 3 trends; interactive_creative pauses/resumes across all 3 checkpoints detached; a 429 surfaces as a recorded `error`; a from-scratch re-poll replays the full run.
- **Contract preserved:** the timeline, pipeline widgets, sidebar, results page, and interactive review panels render identically because the poll payload carries the same `AgentEvent` fields and merged `state`.

## Risks / call-outs

- **Cloud Run CPU throttling (blocking).** A detached task only runs post-response with `--no-cpu-throttling`. Without it the run stalls immediately after kick-off. Task 6 makes this a hard deploy requirement; Task 10 validates it live.
- **Instance recycling can orphan an in-process run.** If the instance running the task is scaled down/redeployed/OOM-killed mid-run, no terminal marker is written → the frontend would poll `running` forever. Mitigations: the **stall-timeout** (Task 8) surfaces it to the user; keep `min-instances`≥1 and avoid mid-run redeploys; **Variant 2 (PubSub worker)** is the durable escalation if this bites under real load.
- **`partial` streaming chunks are not persisted to the session.** Poll renders **final** events only → text appears per-final-event rather than token-streamed. Acceptable (the note accepts coarser streaming); pause detection already ignores `partial`, and the timeline renders final text fine.
- **Background task GC.** `asyncio.create_task` results must be held in a module-level set (`_BACKGROUND_TASKS`) or Python may cancel the task when the local ref drops — a classic footgun. Covered in Task 2.
- **Terminal-marker vs error-event double-signal.** Poll status derivation must prefer an explicit `__run_status` marker but still treat an in-pipeline error event as `error` (Task 3) so model 429s aren't masked by a late `done` marker (they won't co-occur, but derive defensively).
- **Session/state consistency across the two servers.** The canned CRUD endpoints and our router must share one `VertexAiSessionService` backend (same `SESSION_SERVICE_URI`) so `createSession`/`getSession` and our poll agree. Task 5 wires both from the same env var.
- **ADK internal API drift.** `Event`/`EventActions`/`append_event`/`run_async` resume-event-id and the event JSON shape from `get_session` must be confirmed against the installed adk (Tasks 1–3 each say "confirm signature"). The frontend `AgentEvent` shape is the fixed contract; the serializer must match it.
- **Auto-mode gates.** The tagged-revision deploy (private service, `--no-cpu-throttling --no-traffic`) and traffic migration are allowed; do NOT attempt IAM/auth changes. Interactive login (if ADC lapses) → ask the user to run via `!`.

## Out of scope (later)
- **Variant 2:** hand UI runs to a PubSub worker (CRF pattern) for crash-survival — only if instance-recycling orphaning proves real.
- **GCS artifact service** (`--artifact_service_uri gs://…`) so ADK artifacts (not just agent-written GCS outputs) survive across instances — agents already write final outputs to GCS, so not needed for this plan.
- **Server-Sent-Events-over-store** (a short-lived stream that reads FROM the session instead of the live run) — a polish over polling; polling is simpler and sufficient first.
- **Run cancellation** endpoint (`DELETE /runs/{id}` → cancel the task) — nice-to-have, not required for the failure-fix.
- **Firestore/GCS-append event store** — unnecessary; the Vertex session log already is the durable store.
