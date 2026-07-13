# Frontend IAP + Persistent Agent-Engine Sessions Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the two remaining Cloud Run follow-ups: (1) put **Identity-Aware Proxy**
in front of the public `trend-trawler-web` frontend so only org users can reach it, and
(2) give the `trend-trawler-api` backend a **persistent, multi-instance session store**
(Vertex AI Agent Engine sessions) so runs survive instance restarts / scale-out.

**Architecture:** Two independent, sequenced workstreams against the existing two-service
Cloud Run deployment (project `hybrid-vertex`, `us-central1`). **Sessions first** (mostly
code/config, locally verifiable, low blast radius), **IAP second** (infra/IAM, changes who
can reach the app). Sessions use ADK's `--session_service_uri=agentengine://…` with a
**fully-qualified** resource name so the store is pinned to `us-central1` while models stay
pinned to `global` (`GOOGLE_CLOUD_LOCATION` remains unset). IAP is enabled **directly on the
Cloud Run service** via `gcloud run … --iap` (GA) — no manual load balancer / serverless NEG.

**Tech Stack:** ADK `api_server` (`VertexAiSessionService`, already installed — **no new
Python deps**), Vertex AI Agent Engine (Reasoning Engine) sessions API, Cloud Run direct
IAP, gcloud, pytest, a small POSIX entrypoint script.

---

## Locked decisions (confirmed with the user)

1. **Session backend = Agent Engine sessions** (`agentengine://`), not Cloud SQL. Uses
   `VertexAiSessionService` (already importable — verified), so **zero new deps / no Cloud
   SQL / Secret Manager / VPC**. A **dedicated** Reasoning Engine (`trend-trawler-sessions`)
   is created to be the session container so the store's lifetime is decoupled from any
   served-agent deploy.
2. **IAP access = whole domain** — grant `roles/iap.httpsResourceAccessor` ("IAP-secured Web
   App User") to `domain:altostrat.com`.
3. **IAP method = direct on Cloud Run** (`gcloud run services update trend-trawler-web
   --iap --no-allow-unauthenticated`). The project is under org `595744329948` and an OAuth
   brand already exists (`projects/934903580331/brands/934903580331`), so no console-only
   first-run setup is required.

## Environment facts (already gathered — don't re-derive)

- Project: `hybrid-vertex`, number **`934903580331`**, org `595744329948`.
- Region (regional resources): `us-central1`. Models pinned to `global` in code
  (`agent_common`); `GOOGLE_CLOUD_LOCATION` intentionally **unset** — keep it that way.
- Frontend: `trend-trawler-web` (SA `tt-web-sa@hybrid-vertex.iam.gserviceaccount.com`),
  currently `--allow-unauthenticated`, ingress `all`, URL
  `https://trend-trawler-web-qqzji3hyoa-uc.a.run.app`.
- Backend: `trend-trawler-api` (SA `tt-api-sa@hybrid-vertex.iam.gserviceaccount.com`),
  private (`--no-allow-unauthenticated`), current revision `trend-trawler-api-00002-qxl`,
  runs `adk api_server agents` (see `agents/README.md`), URL
  `https://trend-trawler-api-qqzji3hyoa-uc.a.run.app`.
- IAP service agent (created on first IAP API use):
  `service-934903580331@gcp-sa-iap.iam.gserviceaccount.com`.
- Backend build/deploy: `gcloud run deploy trend-trawler-api --source . --region us-central1`
  from the repo root (the root `Dockerfile` bakes the CMD).

## Auto-mode guardrails (who runs what)

The Claude Code auto-mode classifier **blocks** and the **user must run via the `!` prefix**:
- **Project- or service-level IAM grants** (`add-iam-policy-binding` for the IAP SA, the
  domain grant, `roles/aiplatform.user` on `tt-api-sa`).
- **Auth-surface changes** (`--iap`, `--no-allow-unauthenticated`, removing `allUsers`).

Claude **may** run: all read-only `gcloud … describe/list`, `gcloud services enable`,
creating the session Reasoning Engine (an SDK create, not an IAM/auth change), a plain
`gcloud run deploy --source` **redeploy of the already-private backend** (no auth flag
change), pytest/ruff/local smokes, git.

For each user-run step, Claude prepares the exact command block, the user pastes it behind
`!`, and Claude verifies the result with a read-only check.

---

# Workstream A — Persistent Agent-Engine sessions (do first)

**Why first:** it's mostly local code + a private-backend redeploy (no auth change), fully
verifiable before touching who-can-reach-what.

**Design note (the one real risk):** ADK parses `agentengine://<x>` and builds
`VertexAiSessionService`. Passing the **fully-qualified** resource name
`agentengine://projects/934903580331/locations/us-central1/reasoningEngines/<ID>` encodes
project+location, so it does **not** depend on `GOOGLE_CLOUD_LOCATION` (which stays unset for
`global` models). Task A4 verifies this locally before any deploy; if the client still
demands `GOOGLE_CLOUD_LOCATION`, the fallback is documented there.

---

### Task A1: Backend entrypoint that conditionally adds `--session_service_uri`

The Dockerfile CMD currently hardcodes `adk api_server agents …`. We want the session URI to
be **opt-in via an env var** (so local `adk web`/tests are unaffected, and the URI is a plain
config value with no secret). A tiny POSIX script appends `--session_service_uri` only when
`SESSION_SERVICE_URI` is set and non-empty. TDD it by running the script in a dry-run mode
that prints the command instead of exec'ing.

**Files:**
- Create: `deployment/backend_entrypoint.sh`
- Test: `tests/test_backend_entrypoint.py`

**Step 1: Write the failing test**

```python
# tests/test_backend_entrypoint.py
"""The backend container entrypoint appends --session_service_uri only when set.

Keeps the session store opt-in (local `adk web` / CI unaffected) and out of the
Dockerfile CMD literal. ADK_DRYRUN=1 makes the script print the argv it would exec.
"""
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deployment" / "backend_entrypoint.sh"


def _run(env_extra):
    env = {"PATH": "/usr/bin:/bin", "PORT": "8080", "ADK_DRYRUN": "1", **env_extra}
    out = subprocess.run(
        ["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=True
    )
    return out.stdout.strip()

def test_no_session_uri_omits_flag():
    cmd = _run({})
    assert "adk api_server agents" in cmd
    assert "--host 0.0.0.0" in cmd
    assert "--port 8080" in cmd
    assert "--session_service_uri" not in cmd

def test_empty_session_uri_omits_flag():
    assert "--session_service_uri" not in _run({"SESSION_SERVICE_URI": ""})

def test_session_uri_present_appends_flag():
    uri = "agentengine://projects/934903580331/locations/us-central1/reasoningEngines/123"
    cmd = _run({"SESSION_SERVICE_URI": uri})
    assert f"--session_service_uri {uri}" in cmd
```

**Step 2: Run it — expect FAIL**

Run: `export PATH="$HOME/.local/bin:$PATH" && uv run pytest tests/test_backend_entrypoint.py -q`
Expected: FAIL (`backend_entrypoint.sh` does not exist).

**Step 3: Write the script**

```sh
#!/bin/sh
# Container entrypoint for the trend-trawler-api Cloud Run backend.
# Runs `adk api_server agents` and appends --session_service_uri ONLY when
# SESSION_SERVICE_URI is set and non-empty (opt-in persistent sessions).
# Set ADK_DRYRUN=1 to print the argv instead of exec'ing (used by tests).
set -eu

set -- adk api_server agents --host 0.0.0.0 --port "${PORT:-8080}"

if [ -n "${SESSION_SERVICE_URI:-}" ]; then
  set -- "$@" --session_service_uri "${SESSION_SERVICE_URI}"
fi

if [ -n "${ADK_DRYRUN:-}" ]; then
  echo "$@"
  exit 0
fi

exec uv run "$@"
```

**Step 4: Run the test — expect PASS**

Run: `export PATH="$HOME/.local/bin:$PATH" && uv run pytest tests/test_backend_entrypoint.py -q`
Expected: PASS (3 passed).

**Step 5: Lint + commit**

```bash
export PATH="$HOME/.local/bin:$PATH"
chmod +x deployment/backend_entrypoint.sh
uvx ruff check tests/test_backend_entrypoint.py && uvx ruff format tests/test_backend_entrypoint.py
git add deployment/backend_entrypoint.sh tests/test_backend_entrypoint.py
git commit -m "feat(deploy): opt-in backend entrypoint for --session_service_uri"
```

---

### Task A2: Wire the entrypoint into the Dockerfile

**Files:**
- Modify: `Dockerfile` (replace the CMD line)

**Step 1:** Replace the final CMD block. Current:

```dockerfile
# Cloud Run sets $PORT; bind all interfaces. Same-origin frontend proxy means no CORS needed.
CMD ["sh", "-c", "uv run adk api_server agents --host 0.0.0.0 --port ${PORT}"]
```

New:

```dockerfile
# Cloud Run sets $PORT; bind all interfaces. Same-origin frontend proxy means no CORS
# needed. The entrypoint appends --session_service_uri when SESSION_SERVICE_URI is set
# (persistent Agent Engine sessions); otherwise it runs the default in-memory store.
CMD ["sh", "deployment/backend_entrypoint.sh"]
```

> `WORKDIR /app` + `COPY . .` means `deployment/backend_entrypoint.sh` is present at
> `/app/deployment/backend_entrypoint.sh`; `PYTHONPATH=/app` is already set above.

**Step 2: Local smoke (no network needed)**

Run:
```bash
export PATH="$HOME/.local/bin:$PATH"
ADK_DRYRUN=1 PORT=8080 sh deployment/backend_entrypoint.sh
ADK_DRYRUN=1 PORT=8080 SESSION_SERVICE_URI="agentengine://proj/x" sh deployment/backend_entrypoint.sh
```
Expected: first prints without `--session_service_uri`; second ends with
`--session_service_uri agentengine://proj/x`.

**Step 3: Commit**

```bash
git add Dockerfile
git commit -m "feat(deploy): backend runs via entrypoint (session-uri aware)"
```

---

### Task A3: Create the dedicated session Reasoning Engine + grant the backend SA

The `agentengine://` store needs a Reasoning Engine resource to namespace sessions. Create a
dedicated, minimal one so its lifetime is independent of served-agent deploys.

**Files:**
- Create: `deployment/create_session_engine.py`

**Step 1: Write the creator script** (uses the already-installed `vertexai` SDK)

```python
# deployment/create_session_engine.py
"""Create (once) a dedicated Vertex AI Agent Engine to act as the ADK session store.

Prints the fully-qualified resource name to use as:
  SESSION_SERVICE_URI=agentengine://<resource_name>
Region is us-central1 (regional resource); models stay pinned to `global` elsewhere.
Idempotency: re-running creates another engine — list first and reuse if one named
`trend-trawler-sessions` already exists.
"""
import vertexai
from vertexai import agent_engines

PROJECT = "hybrid-vertex"
REGION = "us-central1"
NAME = "trend-trawler-sessions"

def main() -> None:
    vertexai.init(project=PROJECT, location=REGION)
    for e in agent_engines.list():
        if getattr(e, "display_name", None) == NAME:
            print(f"REUSING {e.resource_name}")
            return
    created = agent_engines.create(display_name=NAME)
    print(f"CREATED {created.resource_name}")

if __name__ == "__main__":
    main()
```

> `agent_engines.create(display_name=...)` provisions an empty Agent Engine — enough to host
> the Sessions API. If the installed SDK requires a positional agent arg, pass a trivial
> object per the version's signature; verify with `uv run python -c "from vertexai import
> agent_engines; help(agent_engines.create)"` first.

**Step 2: Create it** (Claude may run — SDK create, not IAM/auth)

Run: `export PATH="$HOME/.local/bin:$PATH" && uv run python deployment/create_session_engine.py`
Expected: prints `CREATED projects/934903580331/locations/us-central1/reasoningEngines/<ID>`
(or `REUSING …`). **Record the resource name** — call it `<SESSION_ENGINE>`.

**Step 3 (USER-run — service-level IAM grant):** the backend SA must call the Vertex AI
sessions API. Prepare for the user to paste behind `!`:

```bash
gcloud projects add-iam-policy-binding hybrid-vertex \
  --member="serviceAccount:tt-api-sa@hybrid-vertex.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

**Step 4: Verify the grant** (Claude, read-only)

Run:
```bash
gcloud projects get-iam-policy hybrid-vertex \
  --flatten="bindings[].members" \
  --filter="bindings.members:tt-api-sa@hybrid-vertex.iam.gserviceaccount.com" \
  --format="table(bindings.role)"
```
Expected: includes `roles/aiplatform.user`.

**Step 5: Commit the script**

```bash
git add deployment/create_session_engine.py
git commit -m "feat(deploy): script to create dedicated Agent Engine session store"
```

---

### Task A4: Local integration verify — sessions persist across a restart

Prove the URI works and survives a process restart **before** deploying.

**Step 1: Start api_server locally with the persistent store**

```bash
export PATH="$HOME/.local/bin:$PATH"
export SESSION_SERVICE_URI="agentengine://<SESSION_ENGINE>"   # fully-qualified name
PYTHONPATH="$PWD" uv run adk api_server agents \
  --session_service_uri "$SESSION_SERVICE_URI" --host 127.0.0.1 --port 8811
```
Expected: `Application startup complete` with no session-service import/location error.
(If it errors demanding `GOOGLE_CLOUD_LOCATION`: the fully-qualified name *should* prevent
this; if not, that is the finding to resolve here — do NOT globally set
`GOOGLE_CLOUD_LOCATION` as it breaks `global` models. Prefer passing project/location through
the resource name; capture the exact error for a targeted fix.)

**Step 2: Create a session, then restart the server, then GET it back**

```bash
# create
curl -s -X POST http://127.0.0.1:8811/apps/trend_scout/users/plan_test/sessions \
  -H 'Content-Type: application/json' -d '{"state":{"brand":"ACME"}}' | tee /tmp/sess.json
SID=$(python -c "import json;print(json.load(open('/tmp/sess.json'))['id'])")
# kill the server (Ctrl-C / kill), start it again with the same SESSION_SERVICE_URI, then:
curl -s http://127.0.0.1:8811/apps/trend_scout/users/plan_test/sessions/$SID
```
Expected: after restart the GET returns the same session id + `state.brand == "ACME"`
(proves persistence — the default in-memory store would 404 here).

**Step 3:** No code change if it passes — this is a verification gate. Note the result in the
PR description. (No commit.)

---

### Task A5: Deploy backend with the session store + verify live

**Step 1 (Claude may run — private backend redeploy, no auth change):**

```bash
export PATH="$HOME/.local/bin:$PATH"
gcloud run services update trend-trawler-api --region us-central1 \
  --update-env-vars "SESSION_SERVICE_URI=agentengine://<SESSION_ENGINE>"
```
(Or fold `--update-env-vars` into a `gcloud run deploy --source .` from the branch. Using
`services update` avoids a rebuild when only the env var changes — but the CMD change from
Task A2 needs a **build**, so if A2 isn't live yet, use `gcloud run deploy --source .
--update-env-vars SESSION_SERVICE_URI=…`.)

**Step 2: Verify live persistence across instances** (Claude, authed)

```bash
URL="https://trend-trawler-api-qqzji3hyoa-uc.a.run.app"
TOK=$(gcloud auth print-identity-token)
# create
curl -s -X POST "$URL/apps/trend_scout/users/live_test/sessions" \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"state":{"brand":"ACME"}}' | tee /tmp/live.json
SID=$(python -c "import json;print(json.load(open('/tmp/live.json'))['id'])")
# read back
curl -s -H "Authorization: Bearer $TOK" \
  "$URL/apps/trend_scout/users/live_test/sessions/$SID"
```
Expected: create returns a session; read-back returns the same id + state. Optionally repeat
the GET a few times to hit a cold/second instance.

**Step 3:** No commit (deploy/verify only). Record revision + result for the PR.

---

# Workstream B — IAP on the frontend (do second)

**Preconditions:** Workstream A verified; you accept that enabling IAP **removes public
access** — only `domain:altostrat.com` users will reach the app afterward.

**No frontend code change is expected.** IAP authenticates the user at the edge *before*
requests reach the container; the same-origin `/api/adk` and `/api/gcs` handlers run inside
the (now IAP-protected) origin and keep using the container SA to reach the private backend
and GCS — unaffected. Task B4 explicitly verifies SSE still streams through IAP.

---

### Task B1: Pre-flight (Claude — read-only + enable API)

**Step 1:** Enable the IAP API (safe, not an auth/IAM change):

```bash
gcloud services enable iap.googleapis.com --project hybrid-vertex
```

**Step 2:** Confirm the current public state so the change is reversible knowledge:

```bash
gcloud run services describe trend-trawler-web --region us-central1 \
  --format="value(metadata.annotations['run.googleapis.com/ingress'])"
gcloud run services get-iam-policy trend-trawler-web --region us-central1 \
  --format="table(bindings.role, bindings.members)"
```
Expected: ingress `all`; policy includes `roles/run.invoker → allUsers` (today's public MVP).
Record these — B3 removes the `allUsers` binding.

---

### Task B2: Enable IAP on the web service (USER-run — auth-surface change)

Prepare for the user to paste behind `!`:

```bash
gcloud run services update trend-trawler-web --region us-central1 \
  --iap --no-allow-unauthenticated
```

**Verify (Claude, read-only):**
```bash
gcloud run services describe trend-trawler-web --region us-central1 \
  --format="yaml(metadata.annotations, spec.template.metadata.annotations)" | grep -i iap
```
Expected: an IAP-enabled annotation is present. Enabling IAP also creates the IAP service
agent `service-934903580331@gcp-sa-iap.iam.gserviceaccount.com`.

---

### Task B3: IAM — let IAP invoke the service + grant the domain (USER-run)

Two service-level bindings the user pastes behind `!`:

```bash
# 1) IAP service agent must be able to invoke the Cloud Run service (IAP fronts it)
gcloud run services add-iam-policy-binding trend-trawler-web --region us-central1 \
  --member="serviceAccount:service-934903580331@gcp-sa-iap.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

# 2) Whole domain may pass IAP ("IAP-secured Web App User")
gcloud run services add-iam-policy-binding trend-trawler-web --region us-central1 \
  --member="domain:altostrat.com" \
  --role="roles/iap.httpsResourceAccessor"
```

**Then remove the old public binding** (USER-run — this is the actual "make it private" step):

```bash
gcloud run services remove-iam-policy-binding trend-trawler-web --region us-central1 \
  --member="allUsers" --role="roles/run.invoker"
```

**Verify (Claude, read-only):**
```bash
gcloud run services get-iam-policy trend-trawler-web --region us-central1 \
  --format="table(bindings.role, bindings.members)"
```
Expected: `roles/run.invoker → serviceAccount:service-934903580331@gcp-sa-iap…`,
`roles/iap.httpsResourceAccessor → domain:altostrat.com`, and **no** `allUsers`.

---

### Task B4: Verify IAP end-to-end

**Step 1: Unauthenticated request is intercepted by IAP** (Claude)

```bash
WEB="https://trend-trawler-web-qqzji3hyoa-uc.a.run.app"
curl -s -o /dev/null -w "%{http_code} -> %{redirect_url}\n" "$WEB/"
```
Expected: **302** redirecting to `https://accounts.google.com/…` (IAP sign-in) — **not** a
200 (public) and **not** a bare 403. A `302` to Google sign-in is the success signal.

**Step 2: Authenticated browser check** (user, manual): open `$WEB/` in a browser signed in
as an `@altostrat.com` user → Google consent (first time) → the app loads (200). Submit a
campaign and confirm the **SSE run stream** still updates live (proves IAP passes SSE
through) and an artifact loads via `/api/gcs`.

**Step 3:** No code change. Record results for the PR.

---

### Task B5: Docs + memory

**Files:**
- Modify: `deployment/README.md` — new subsection "Persistent sessions (Agent Engine)" (the
  `SESSION_SERVICE_URI` env var, the dedicated engine, `roles/aiplatform.user` on
  `tt-api-sa`) and "IAP on the frontend" (the `--iap` enablement, the two IAM grants, the
  `allUsers` removal, `roles/iap.httpsResourceAccessor` to `domain:altostrat.com`, the 302
  sign-in verification).
- Modify: `CLAUDE.md` — one line under the frontend/deploy notes: frontend is now IAP-gated
  (domain-restricted), backend uses persistent Agent Engine sessions via `SESSION_SERVICE_URI`.
- Modify: memory `frontend-cloudrun-deployment-live.md` — mark IAP + persistent-sessions
  follow-ups RESOLVED with the engine resource name and revisions.

**Commit:**
```bash
git add deployment/README.md CLAUDE.md
git commit -m "docs: IAP frontend + persistent Agent Engine sessions runbook"
```

---

## Verification (whole plan)

- **No-creds gate (CI-safe):** `uv run pytest tests/test_backend_entrypoint.py -q` +
  full `uv run pytest tests/ -q` green; `uvx ruff check` clean; entrypoint dry-run smokes.
- **Sessions (live):** create→restart→GET returns the same session with state (local A4 and
  live A5); backend stays private (`403` unauth).
- **IAP (live):** unauth `/` → `302` to Google sign-in; an `@altostrat.com` user loads the
  app, a full campaign run streams via SSE, an artifact loads via `/api/gcs`; policy shows no
  `allUsers`.

## Risks / call-outs

- **`GOOGLE_CLOUD_LOCATION` conflict (sessions):** models need `global`, the session engine
  needs `us-central1`. Mitigated by the **fully-qualified** `agentengine://projects/…/
  locations/us-central1/reasoningEngines/<ID>` URI; A4 is the gate that proves it before deploy.
- **IAP removes public access immediately** after B2+B3. If a non-domain demo viewer needs
  access, add them individually with `roles/iap.httpsResourceAccessor` — do not re-add
  `allUsers`.
- **SSE through IAP:** IAP streams responses; B4/step-2 explicitly confirms the run page still
  updates live (guards against edge buffering).
- **`agent_engines.create()` signature drift:** verify the installed SDK's signature (A3
  step 1 note) before running; some versions want a positional agent.
- **OAuth brand deprecation warning:** the IAP OAuth Admin *APIs* are being retired, but the
  existing brand + direct Cloud Run `--iap` path do not depend on them; ignore the warning.

## Out of scope (deferred)

Cloud SQL/relational session store (chose Agent Engine); per-user/group IAP allowlists (chose
whole-domain); context-aware access levels; custom domain + managed TLS; Artifact Registry +
Cloud Build CI/CD; autoscaling/session-affinity tuning.
