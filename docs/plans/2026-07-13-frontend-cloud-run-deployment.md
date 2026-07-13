# Frontend + api_server Cloud Run Deployment Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship the Next.js frontend and the ADK `api_server` as two containerized Cloud Run
services in `us-central1`, with the frontend privately calling the backend via a metadata-server
ID token — turning the "planned/target" box in `docs/diagrams/frontend_cloudrun_deployment.png`
into real, reproducible infrastructure.

**Architecture:** Two independent Cloud Run services. **Backend** (`trend-trawler-api`) runs
`adk api_server .` — it loads all three agent packages and calls Vertex/BigQuery/GCS in-process;
it is **private** (no unauthenticated access). **Frontend** (`trend-trawler-web`) runs the Next.js
16 standalone server; its existing same-origin Route Handlers proxy to the backend
(`/api/adk/*` → `ADK_API_BASE`) and to Cloud Storage (`/api/gcs`). The `/api/adk` proxy, running
server-side in the frontend container, mints a Google-signed **ID token** (audience = backend URL)
from the Cloud Run metadata server and attaches it as `Authorization: Bearer …`; the frontend
service account holds `roles/run.invoker` on the backend. This keeps the browser same-origin
(no CORS, no direct backend exposure) while the backend stays private.

**Tech Stack:** Next.js 16 (App Router, `output: "standalone"`), Node 22 (Debian slim), Python
3.13 + `uv` + `google-adk[eval]` 2.4, Docker (multi-stage), Cloud Run (`gcloud run deploy --source`),
Cloud Run metadata server (access + identity tokens), Vitest for the token/proxy unit tests.

---

## Design decisions (locked with the user)

1. **Backend target:** deploy `adk api_server` as its own Cloud Run service. The frontend's
   `/api/adk` proxy + `frontend/src/lib/api.ts` speak the ADK api_server REST+SSE surface
   (`list-apps`, session CRUD, `/run_sse`), which Agent Engine does **not** expose in that shape —
   so reusing the existing Agent Engine would mean rewriting the client. Not done.
2. **Backend auth:** backend is **private**; the frontend proxy attaches a metadata-server ID token.
   No `--allow-unauthenticated` on the backend.
3. **Build/deploy:** a `Dockerfile` per service + `gcloud run deploy --source .` (Cloud Run builds
   the Dockerfile). Matches the repo's existing gcloud-based Cloud Run deploys; no Artifact Registry
   pipeline for this first cut.

## Key facts confirmed against the codebase (do not re-derive)

- `adk api_server` accepts `--host` and `--port` (verified via `adk api_server --help`). Cloud Run
  injects `PORT` (default 8080); bind `--host 0.0.0.0 --port $PORT`.
- Next 16 supports `output: "standalone"` → `.next/standalone/server.js`, which honors `PORT` /
  `HOSTNAME` env (verified in `node_modules/next/dist/docs/.../output.md`). `server.js` does **not**
  bundle `public/` or `.next/static/` — the Dockerfile must copy them in.
- Backend env vars = the set in `deployment/deploy_agent.py:ENV_VAR_DICT`
  (`GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT_NUMBER`, `GOOGLE_CLOUD_STORAGE_BUCKET`,
  `BUCKET`, `BQ_PROJECT_ID`, `BQ_DATASET_ID`, `BQ_TABLE_TARGETS`, `BQ_TABLE_CREATIVES`,
  `BQ_TABLE_ALL_TRENDS`, `BQ_TABLE_EVALS`). **Caveat:** that dict omits `GOOGLE_CLOUD_PROJECT` /
  `GOOGLE_CLOUD_LOCATION` only because **Agent Engine reserves** them. Plain Cloud Run does **not**
  reserve them, so this deploy **does** set `GOOGLE_CLOUD_PROJECT` and `GCP_REGION=us-central1`.
  Models stay pinned to `global` in code (`agent_common` `MODEL_LOCATION` / `build_gemini`), so
  `GOOGLE_CLOUD_LOCATION` is left unset to avoid pushing model calls to a regional endpoint.
- `frontend/src/app/api/gcs/route.ts` already gets an **access token** from the metadata server
  (falling back to `gcloud` locally). On Cloud Run the metadata path works as-is; only the frontend
  SA needs `roles/storage.objectViewer`. Task 3 extracts this into a shared helper.
- The frontend's browser bundle only ever calls **same-origin** (`NEXT_PUBLIC_API_BASE` defaults to
  `/api/adk`), so **no CORS / `--allow_origins`** wiring is needed on the backend.
- `uv.lock` pins a private Google mirror that is inaccessible from some environments
  (see the `adk-pipe-dep-mirror-workaround` memory). The backend image build (Task 2) may hit this;
  the task has an explicit fallback step.

---

### Task 1: Frontend standalone build + Dockerfile

**Files:**
- Modify: `frontend/next.config.ts`
- Create: `frontend/Dockerfile`
- Create: `frontend/.dockerignore`

**Step 1: Enable standalone output**

Edit `frontend/next.config.ts` to add `output: "standalone"` to the config object (keep the
existing `allowedDevOrigins`):

```ts
const nextConfig: NextConfig = {
  output: "standalone",
  allowedDevOrigins: [
    "3000-jtotts-cc-station.cluster-bg7xwomlpbbfku2lmsogqimkzi.cloudworkstations.dev",
    "*.cluster-bg7xwomlpbbfku2lmsogqimkzi.cloudworkstations.dev",
  ],
};
```

**Step 2: Verify the standalone build is produced**

Run: `cd frontend && npm run build`
Expected: build succeeds and `frontend/.next/standalone/server.js` exists
(`ls .next/standalone/server.js`).

**Step 3: Smoke-run the standalone server locally (no Docker)**

Run:
```bash
cd frontend
cp -r public .next/standalone/ && cp -r .next/static .next/standalone/.next/
PORT=8081 HOSTNAME=0.0.0.0 node .next/standalone/server.js &
sleep 3 && curl -sS -o /dev/null -w "%{http_code}\n" http://localhost:8081/
kill %1
```
Expected: `200`. (Confirms `server.js` serves the app with `public/` + static copied in.)

**Step 4: Write `frontend/.dockerignore`**

```
node_modules
.next
.git
.env*
coverage
npm-debug.log*
Dockerfile
.dockerignore
```

**Step 5: Write `frontend/Dockerfile` (multi-stage, Debian slim)**

```dockerfile
# ---- deps ----
FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ---- builder ----
FROM node:22-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ---- runner ----
FROM node:22-slim AS runner
WORKDIR /app
ENV NODE_ENV=production
RUN groupadd -r nodejs && useradd -r -g nodejs nextjs
# Standalone server + assets it does not bundle itself.
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
ENV PORT=8080 HOSTNAME=0.0.0.0
EXPOSE 8080
CMD ["node", "server.js"]
```

**Step 6: Build the image locally to catch Dockerfile errors early**

Run: `cd frontend && docker build -t tt-web:local .`
Expected: image builds; final stage `CMD ["node","server.js"]`.
(If Docker is unavailable in this environment, note it and rely on the Step 3 standalone smoke +
the Cloud Build in Task 6.)

**Step 7: Commit**

```bash
git add frontend/next.config.ts frontend/Dockerfile frontend/.dockerignore
git commit -m "build(frontend): standalone output + Cloud Run Dockerfile"
```

---

### Task 2: Backend api_server Dockerfile

**Files:**
- Create: `Dockerfile` (repo root)
- Create: `.dockerignore` (repo root)

**Step 1: Write repo-root `.dockerignore`**

Keep the image lean and avoid shipping the frontend/node_modules or local venvs:

```
.git
.venv
__pycache__
frontend
node_modules
outputs
docs
tests
*.pyc
.env*
```

> Note: `frontend/` is excluded — the backend image is Python-only. `tests/` and `docs/` are
> excluded to shrink the build context.

**Step 2: Write repo-root `Dockerfile` (uv-based)**

```dockerfile
FROM python:3.13-slim

# uv from the official distroless image (pinned, no curl|sh).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependency layer first for caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# App source (agent packages: trend_scout, creative_agent, interactive_creative,
# creative_eval, agent_common).
COPY . .

ENV PORT=8080
# Cloud Run sets $PORT; bind all interfaces. Same-origin frontend proxy means no CORS needed.
CMD ["sh", "-c", "uv run adk api_server . --host 0.0.0.0 --port ${PORT}"]
```

**Step 3: Build the backend image locally**

Run: `docker build -t tt-api:local .`
Expected: build succeeds.

**Step 4: Handle the `uv.lock` private-mirror failure IF it occurs**

If Step 3 fails during `uv sync --frozen` with a 403/unreachable index (the private Google mirror,
per the `adk-pipe-dep-mirror-workaround` memory):
- First confirm whether Cloud Build (Task 6, runs inside the GCP project) can reach the mirror —
  it may have access that local builds lack. If so, this local step can be skipped and the image
  built via Cloud Run's build.
- Otherwise re-resolve the lock against public PyPI and use it for the image:
  `uv lock --upgrade` (verify it resolves from PyPI), then rebuild. Do **not** commit a
  PyPI-relocked `uv.lock` in this plan's PR unless the team agrees — flag it for the reviewer.

**Step 5: Smoke-run the backend container locally**

Run (mount ADC + set project so the agents can construct clients):
```bash
docker run --rm -p 8080:8080 \
  -e GOOGLE_CLOUD_PROJECT=hybrid-vertex \
  -e GOOGLE_GENAI_USE_VERTEXAI=TRUE \
  -v "$HOME/.config/gcloud:/home/root/.config/gcloud:ro" \
  tt-api:local &
sleep 8
curl -sS http://localhost:8080/list-apps
docker stop $(docker ps -q --filter ancestor=tt-api:local)
```
Expected: JSON array containing `trend_scout`, `creative_agent`, `interactive_creative`.
(If ADC mounting is fiddly, defer full verification to the live deploy in Task 6; the container
*starting* and serving `/list-apps` is the bar here.)

**Step 6: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "build: Dockerfile for the ADK api_server Cloud Run service"
```

---

### Task 3: Shared GCP token helper (TDD) + refactor the GCS route

**Files:**
- Create: `frontend/src/lib/gcp-auth.ts`
- Create: `frontend/src/__tests__/gcp-auth.test.ts`
- Modify: `frontend/src/app/api/gcs/route.ts`

**Step 1: Write the failing test**

`frontend/src/__tests__/gcp-auth.test.ts`:

```ts
import { describe, it, expect, vi, afterEach } from "vitest";
import { getAccessToken, getIdentityToken } from "@/lib/gcp-auth";

afterEach(() => vi.restoreAllMocks());

describe("gcp-auth", () => {
  it("getAccessToken reads access_token from the metadata server", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ access_token: "ya29.test" }),
      { status: 200, headers: { "content-type": "application/json" } },
    )));
    expect(await getAccessToken()).toBe("ya29.test");
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/service-accounts/default/token"),
      expect.objectContaining({ headers: { "Metadata-Flavor": "Google" } }),
    );
  });

  it("getIdentityToken requests an ID token for the given audience (plain-text JWT)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("eyJ.jwt.token", { status: 200 })));
    const token = await getIdentityToken("https://api.example.run.app");
    expect(token).toBe("eyJ.jwt.token");
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("audience=https%3A%2F%2Fapi.example.run.app"),
      expect.objectContaining({ headers: { "Metadata-Flavor": "Google" } }),
    );
  });
});
```

**Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run src/__tests__/gcp-auth.test.ts`
Expected: FAIL — cannot resolve `@/lib/gcp-auth`.

**Step 3: Implement `frontend/src/lib/gcp-auth.ts`**

```ts
const METADATA_BASE =
  "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default";
const METADATA_HEADERS = { "Metadata-Flavor": "Google" };

/** OAuth access token for calling Google APIs (e.g. Cloud Storage). ADC on Cloud Run;
 *  falls back to the gcloud CLI for local dev. */
export async function getAccessToken(): Promise<string> {
  try {
    const res = await fetch(`${METADATA_BASE}/token`, {
      headers: METADATA_HEADERS,
      signal: AbortSignal.timeout(1000),
    });
    if (!res.ok) throw new Error("metadata token non-ok");
    return (await res.json()).access_token as string;
  } catch {
    const { execSync } = await import("child_process");
    return execSync("gcloud auth application-default print-access-token", {
      encoding: "utf-8",
    }).trim();
  }
}

/** Google-signed ID token whose `aud` is `audience` — used to call a private Cloud Run
 *  service. The metadata `identity` endpoint returns the raw JWT as text. */
export async function getIdentityToken(audience: string): Promise<string> {
  try {
    const res = await fetch(
      `${METADATA_BASE}/identity?audience=${encodeURIComponent(audience)}`,
      { headers: METADATA_HEADERS, signal: AbortSignal.timeout(1000) },
    );
    if (!res.ok) throw new Error("metadata identity non-ok");
    return (await res.text()).trim();
  } catch {
    const { execSync } = await import("child_process");
    return execSync(`gcloud auth print-identity-token`, { encoding: "utf-8" }).trim();
  }
}
```

**Step 4: Run the test to verify it passes**

Run: `cd frontend && npx vitest run src/__tests__/gcp-auth.test.ts`
Expected: PASS (both cases).

**Step 5: Refactor `gcs/route.ts` to use the helper (DRY)**

Replace the inline metadata-token block (lines ~21-40) with:
```ts
import { getAccessToken } from "@/lib/gcp-auth";
// …
const accessToken = await getAccessToken();
```
Leave the rest of the route (GCS fetch, HTML URL-rewrite, caching) unchanged.

**Step 6: Run the full frontend suite**

Run: `cd frontend && npm test`
Expected: all tests pass (existing suite + new `gcp-auth.test.ts`).

**Step 7: Commit**

```bash
git add frontend/src/lib/gcp-auth.ts frontend/src/__tests__/gcp-auth.test.ts \
        frontend/src/app/api/gcs/route.ts
git commit -m "feat(frontend): shared GCP token helper (access + identity); reuse in /api/gcs"
```

---

### Task 4: Attach ID token in the `/api/adk` proxy (TDD)

**Files:**
- Modify: `frontend/src/app/api/adk/[...path]/route.ts`
- Create: `frontend/src/__tests__/adk-proxy-auth.test.ts`

**Step 1: Write the failing test for the pure predicate**

The proxy is a route handler; extract a pure, testable predicate so the auth decision is unit-tested.
`frontend/src/__tests__/adk-proxy-auth.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { backendNeedsAuth } from "@/app/api/adk/[...path]/route";

describe("backendNeedsAuth", () => {
  it("is true for a remote https backend (private Cloud Run)", () => {
    expect(backendNeedsAuth("https://trend-trawler-api-abc.run.app")).toBe(true);
  });
  it("is false for localhost dev backend", () => {
    expect(backendNeedsAuth("http://localhost:8000")).toBe(false);
  });
});
```

**Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run src/__tests__/adk-proxy-auth.test.ts`
Expected: FAIL — `backendNeedsAuth` is not exported.

**Step 3: Implement in `adk/[...path]/route.ts`**

Add the export and use it inside `proxy()` before the upstream `fetch`:

```ts
import { getIdentityToken } from "@/lib/gcp-auth";

const BACKEND = process.env.ADK_API_BASE ?? "http://localhost:8000";

/** A remote https backend is a private Cloud Run service → attach an ID token.
 *  A localhost backend (dev) is unauthenticated → skip. */
export function backendNeedsAuth(base: string): boolean {
  return base.startsWith("https://");
}

// …inside proxy(), after building `headers`, before fetch(target, …):
if (backendNeedsAuth(BACKEND)) {
  const token = await getIdentityToken(new URL(BACKEND).origin);
  headers.set("authorization", `Bearer ${token}`);
}
```

> Note: exporting a non-HTTP-method symbol from a route file is allowed — Next only treats the
> `GET`/`POST`/… exports as handlers. Keep `backendNeedsAuth` a plain function.

**Step 4: Run the test to verify it passes**

Run: `cd frontend && npx vitest run src/__tests__/adk-proxy-auth.test.ts`
Expected: PASS.

**Step 5: Guard the SSE path — confirm streaming is untouched**

Re-read the handler: the `Authorization` header is added to the *outgoing* request only; the
response is still `new Response(upstream.body, …)` with hop-by-hop headers stripped. No change to
streaming. (No automated test; verify by inspection.)

**Step 6: Run the full frontend suite**

Run: `cd frontend && npm test`
Expected: green.

**Step 7: Commit**

```bash
git add "frontend/src/app/api/adk/[...path]/route.ts" \
        frontend/src/__tests__/adk-proxy-auth.test.ts
git commit -m "feat(frontend): mint ID token for private Cloud Run backend in /api/adk proxy"
```

---

### Task 5: IAM — service accounts + role bindings (runbook, with-creds)

> This task changes cloud state. Commands are the deliverable; run them where GCP creds exist.
> Project: `hybrid-vertex`, region: `us-central1`. Substitute a real bucket for `$BUCKET`.

**Files:** none (documented in Task 6). This task defines the exact commands.

**Step 1: Create two service accounts**

```bash
gcloud iam service-accounts create tt-api-sa  --display-name="trend-trawler api_server"
gcloud iam service-accounts create tt-web-sa  --display-name="trend-trawler web frontend"
PROJECT=hybrid-vertex
API_SA=tt-api-sa@$PROJECT.iam.gserviceaccount.com
WEB_SA=tt-web-sa@$PROJECT.iam.gserviceaccount.com
```

**Step 2: Grant the backend SA the roles the agents need**

```bash
for ROLE in roles/aiplatform.user roles/bigquery.dataEditor roles/bigquery.jobUser \
            roles/storage.objectAdmin roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:$API_SA" --role="$ROLE" --condition=None
done
```

**Step 3: Grant the frontend SA GCS read (for `/api/gcs`) + logging**

```bash
for ROLE in roles/storage.objectViewer roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:$WEB_SA" --role="$ROLE" --condition=None
done
```

> `roles/run.invoker` on the backend service is granted in Task 6 **after** the backend exists
> (it is a per-service binding, not project-wide).

**Step 4: (verification)** `gcloud projects get-iam-policy $PROJECT --flatten=bindings \
  --filter="bindings.members:tt-*-sa" --format="table(bindings.role)"` shows the expected roles.

---

### Task 6: Deploy both services + docs

**Files:**
- Modify: `deployment/README.md` (new "Frontend + api_server on Cloud Run" section)
- Create: `frontend/.env.example`
- Modify: `CLAUDE.md` (frontend section: dev + deployed)
- Modify: `docs/diagrams/README.md` (drop the "planned" caveat on the deployment diagram row)

**Step 1: Deploy the backend (private)**

```bash
PROJECT=hybrid-vertex; REGION=us-central1
API_SA=tt-api-sa@$PROJECT.iam.gserviceaccount.com
# From repo root (uses the root Dockerfile):
gcloud run deploy trend-trawler-api \
  --source . --region $REGION --no-allow-unauthenticated \
  --service-account $API_SA \
  --memory 8Gi --cpu 4 --min-instances 0 --timeout 900 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT,GCP_REGION=$REGION,GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_STORAGE_BUCKET=$BUCKET,BUCKET=$BUCKET,BQ_PROJECT_ID=$PROJECT,BQ_DATASET_ID=trend_trawler,BQ_TABLE_TARGETS=target_trends_crf,BQ_TABLE_CREATIVES=trend_creatives,BQ_TABLE_ALL_TRENDS=all_trends,BQ_TABLE_EVALS=creative_evals"
```
> Env-var values must match `.env` / `deployment/deploy_agent.py:ENV_VAR_DICT`. `GOOGLE_CLOUD_LOCATION`
> is intentionally **omitted** (models pinned to `global` in code; setting it would push model calls
> to a regional endpoint). Confirm the actual table names against `.env` before running.

Capture the URL: `API_URL=$(gcloud run services describe trend-trawler-api --region $REGION --format='value(status.url)')`

**Step 2: Let the frontend SA invoke the backend**

```bash
gcloud run services add-iam-policy-binding trend-trawler-api --region $REGION \
  --member="serviceAccount:$WEB_SA" --role=roles/run.invoker
```

**Step 3: Deploy the frontend**

Decide frontend exposure. MVP: `--allow-unauthenticated` so users can reach it; **flag** that anything
non-demo should sit behind IAP (add a follow-up).

```bash
gcloud run deploy trend-trawler-web \
  --source ./frontend --region $REGION --allow-unauthenticated \
  --service-account $WEB_SA \
  --memory 1Gi --cpu 1 --min-instances 0 \
  --set-env-vars "ADK_API_BASE=$API_URL"
```

**Step 4: End-to-end verification (live)**

- `curl -sS -o /dev/null -w "%{http_code}" $(gcloud run services describe trend-trawler-web \
  --region $REGION --format='value(status.url)')/` → `200`.
- Open the web URL, submit a campaign for `trend_scout`, confirm the SSE stream renders (proves the
  ID-token'd `/api/adk` → private backend path).
- Open a completed run's results page, confirm an artifact loads (proves `/api/gcs` + `objectViewer`).
- Negative check: `curl $API_URL/list-apps` **without** a token → `403` (backend is private).

**Step 5: Write `frontend/.env.example`**

```bash
# ADK api_server base URL the /api/adk proxy forwards to.
# Local dev: leave unset (defaults to http://localhost:8000, unauthenticated).
# Cloud Run: the private backend service URL — the proxy attaches an ID token automatically.
ADK_API_BASE=
# Browser-side base for the ADK client; same-origin proxy by default. Leave unset.
# NEXT_PUBLIC_API_BASE=/api/adk
```

**Step 6: Document in `deployment/README.md`**

Add a "Frontend + api_server on Cloud Run" section: the two-service architecture, the private-backend
+ ID-token model, the exact `gcloud run deploy --source` commands from Steps 1-3, the IAM from Task 5,
and the local `docker build` smoke from Tasks 1-2. Cross-reference
`docs/diagrams/frontend_cloudrun_deployment.png`.

**Step 7: Update `CLAUDE.md` + diagram caveat**

- `CLAUDE.md` frontend section: note it now deploys to Cloud Run (two services), not dev-only.
- `docs/diagrams/README.md`: change the deployment-diagram row from "Target (Cloud Run, planned)"
  to reflect that the path is now implemented (keep IAP-for-frontend as the remaining gap).

**Step 8: Commit**

```bash
git add deployment/README.md frontend/.env.example CLAUDE.md docs/diagrams/README.md
git commit -m "docs: Cloud Run deploy runbook for frontend + api_server"
```

---

## Verification (whole plan)

**No-creds gate (CI-safe):**
- `cd frontend && npm test` — all Vitest suites green (incl. `gcp-auth`, `adk-proxy-auth`).
- `cd frontend && npm run build` — standalone build succeeds, `.next/standalone/server.js` present.
- `cd frontend && npx eslint` — clean.
- `frontend/.next/standalone/server.js` local smoke returns `200` (Task 1 Step 3).

**With-creds / live:**
- Both `docker build`s succeed (or Cloud Run source build succeeds).
- Backend `/list-apps` returns the three agents; unauthenticated call returns `403`.
- Frontend URL serves `200`; a full campaign run streams via SSE and an artifact loads via `/api/gcs`.

## Sequencing & PRs

Tasks 1→4 are code/build and independently testable (no cloud). Tasks 5-6 are the live deploy +
docs. Suggested single PR "feat: Cloud Run deployment for frontend + api_server" containing Tasks
1-4 + the docs/runbook from Task 6; Task 5 IAM and the actual `gcloud run deploy` (Task 6 Steps 1-4)
are run out-of-band by someone with project access and reported back. Open the PR only when asked.

## Risks / call-outs

- **`uv.lock` private mirror** (Task 2 Step 4) is the most likely build blocker; has a fallback.
- **Docker may be unavailable** in this workstation — Tasks 1-2 fall back to the standalone smoke +
  Cloud Run's own source build.
- **Frontend exposure**: MVP is `--allow-unauthenticated`. Front it with **IAP** for real use — the
  one deliberate gap left open (follow-up).
- **api_server statefulness**: default in-memory sessions mean a run must hit the same instance for
  its lifetime. Fine at `min-instances 0` / low traffic; if scaled out, add
  `--session_service_uri` (e.g. a database/Agent Engine session backend). Noted, not solved here.

## Out of scope (deferred)

IAP/identity-aware frontend auth; Artifact Registry + Cloud Build CI/CD pipeline; a persistent ADK
session service; autoscaling/load config beyond defaults; the `trawler_scheduler` Cloud Scheduler leg
(separate work item).
