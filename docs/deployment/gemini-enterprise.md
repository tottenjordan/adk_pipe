# Gemini Enterprise Integration Guide

Trend Trawler integrates with **Gemini Enterprise** (Google Cloud Discovery Engine) in two independent, opt-in directions:

1. **Inbound Enterprise Grounding** — Grounds `creative_agent` and `interactive_creative` research and briefs against private brand guidelines, past campaign playbooks, and product specs stored in Discovery Engine data stores alongside public Google Search.
2. **Outbound Agent Exposure** — Publishes `trend_scout`, `creative_agent`, and `interactive_creative` as first-class agents inside Gemini Enterprise (`default_assistant`) via either **Mode A: Vertex AI Agent Engine (`reasoning-engine`)** or **Mode B: Cloud Run A2A (`a2a`)**, with seamless deep-link handoff into Trend Trawler Studio.

Both directions are **off by default**: leaving the environment variables unset preserves the existing Studio, CLI, and Cloud Run Functions workflows with zero runtime overhead and no required Gemini Enterprise `app_id`.

---

## 1. Inbound Enterprise Grounding

When `ENTERPRISE_DATA_STORES` or `ENTERPRISE_SEARCH_ENGINE` is configured, `combined_research_pipeline` runs an additional parallel `enterprise_researcher` branch using ADK's `DiscoveryEngineSearchTool`. Retrieved passages are indexed under `enterprise_sources` with `[E-...]` citation tags so the brief writer, research report, and Proof Room UI distinguish internal enterprise sources from public web sources.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ENTERPRISE_DATA_STORES` | Comma-separated Discovery Engine data store resource paths (`projects/<project>/locations/<loc>/collections/default_collection/dataStores/<store_id>`). | Unset (disabled) |
| `ENTERPRISE_SEARCH_ENGINE` | Optional Discovery Engine search app resource path (`projects/<project>/locations/<loc>/collections/default_collection/engines/<engine_id>`). | Unset |
| `ENTERPRISE_RESEARCH_MODEL` | Model used by the enterprise research sub-agent. | `gemini-3.1-flash-lite` |

### Required IAM

Grant the runtime service account (Agent Engine runtime SA and/or Cloud Run API SA) read access to Discovery Engine:

```bash
gcloud projects add-iam-policy-binding "$GOOGLE_CLOUD_PROJECT" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/discoveryengine.viewer"
```

---

## 2. Outbound Registration Mode A: Vertex AI Agent Engine (`reasoning-engine`)

If your Trend Trawler agents are deployed to Vertex AI Agent Engine (`deployment/deploy_agent.py`), you can register them directly on a Gemini Enterprise assistant via `adkAgentDefinition.provisionedReasoningEngine`.

### Prerequisites

1. Deploy one or more agents to Agent Engine:
   ```bash
   uv run python deployment/deploy_agent.py --version=v1 --agent=trend_scout --create
   uv run python deployment/deploy_agent.py --version=v1 --agent=creative_agent --create
   ```
2. Ensure the Discovery Engine service agent (`service-${GOOGLE_CLOUD_PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com`) has permission to query Vertex AI Reasoning Engines (`roles/aiplatform.user`).

### Register an Agent Engine Instance

```bash
uv run python deployment/register_gemini_enterprise.py \
  --project "$GOOGLE_CLOUD_PROJECT" \
  --location global \
  --engine-location "${GCP_REGION:-us-central1}" \
  --app-id "$GEMINI_ENTERPRISE_APP_ID" \
  --agent trend_scout \
  --mode reasoning-engine \
  --reasoning-engine-id "$SCOUT_AGENT_ENGINE_ID" \
  --register
```

To register all three agents at once using `SCOUT_AGENT_ENGINE_ID`, `CREATIVE_AGENT_ENGINE_ID`, and `INTERACTIVE_AGENT_ENGINE_ID` from `.env`:

```bash
uv run python deployment/register_gemini_enterprise.py \
  --app-id "$GEMINI_ENTERPRISE_APP_ID" \
  --agent all \
  --mode reasoning-engine \
  --register
```

---

## 3. Outbound Registration Mode B: Cloud Run A2A (`a2a`)

When `ENABLE_A2A_SERVER=1` is set on the FastAPI backend (`deployment/async_app.py`), `runserver/a2a_mount.py` mounts shared-session A2A endpoints for all three agents while preserving `App`-level `ResumabilityConfig`, `ScopedModelArmorPlugin`, and Vertex AI session compatibility:

- `/a2a/trend_scout/` (card: `/a2a/trend_scout/.well-known/agent-card.json`)
- `/a2a/creative_agent/` (card: `/a2a/creative_agent/.well-known/agent-card.json`)
- `/a2a/interactive_creative/` (card: `/a2a/interactive_creative/.well-known/agent-card.json`)

### Cloud Run Environment Variables

| Variable | Description |
|----------|-------------|
| `ENABLE_A2A_SERVER` | Set to `1` (`true` / `yes`) to mount `/a2a/<agent>` endpoints on `deployment.async_app:app`. |
| `A2A_PUBLIC_BASE_URL` | Public HTTPS origin of the Cloud Run API service advertised in each `AgentCard`. |
| `A2A_ALLOWED_CALLER_SAS` | Comma-separated allowlist of caller service accounts verified via Google OIDC Bearer tokens on `/a2a/*` in `USER_AUTHZ_MODE=enforce` (for example, `service-<project-number>@gcp-sa-discoveryengine.iam.gserviceaccount.com`). Fails closed (`401`) when empty. **Trust boundary:** allowlisted SAs are trusted to assert end-user session ownership via A2A `user_email` metadata/auth headers — list only trusted first-party platform service agents, never untrusted third-party callers. |
| `A2A_AUDIENCES` | Comma-separated expected OIDC `aud` values for `/a2a/*` (falls back to `TRUSTED_PROXY_AUDIENCES` when unset). |
| `A2A_PUBLIC_AGENT_CARD` | Optional (`1` / `0`, default `0`). When `1`, allows unauthenticated `GET /a2a/<agent>/.well-known/agent-card.json` discovery while keeping `POST /a2a/<agent>` OIDC-enforced. |
| `A2A_HANDOFF_SECRET` | Random HMAC-SHA256 secret (32+ bytes recommended) used to sign one-time `&handoff=<token>` URLs for sessions started under the shared `a2a-service` user. |
| `STUDIO_PUBLIC_URL` | Public HTTPS origin of the Next.js Trend Trawler Studio frontend; when set, A2A task responses include a deep-link to `/results/<sid>?app=<agent>&from=ge` (or `/run/<sid>` when paused at an interactive checkpoint). |

### Cloud Run IAM

Grant the Gemini Enterprise / Discovery Engine service agent `roles/run.invoker` on the Cloud Run API service:

```bash
gcloud run services add-iam-policy-binding trend-trawler-api \
  --region="${GCP_REGION:-us-central1}" \
  --member="serviceAccount:service-${GOOGLE_CLOUD_PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

### Register Cloud Run A2A Endpoints in Gemini Enterprise

```bash
uv run python deployment/register_gemini_enterprise.py \
  --project "$GOOGLE_CLOUD_PROJECT" \
  --location global \
  --app-id "$GEMINI_ENTERPRISE_APP_ID" \
  --agent all \
  --mode a2a \
  --a2a-base-url "https://trend-trawler-api.example.com" \
  --register
```

### Listing, Updating, and Deleting Registered Agents

```bash
# List agents registered on the assistant
uv run python deployment/register_gemini_enterprise.py \
  --app-id "$GEMINI_ENTERPRISE_APP_ID" \
  --list

# Update an existing agent definition
uv run python deployment/register_gemini_enterprise.py \
  --app-id "$GEMINI_ENTERPRISE_APP_ID" \
  --agent creative_agent \
  --mode a2a \
  --a2a-base-url "https://trend-trawler-api.example.com" \
  --agent-resource-id "<agent_resource_id>" \
  --update

# Delete a registered agent
uv run python deployment/register_gemini_enterprise.py \
  --app-id "$GEMINI_ENTERPRISE_APP_ID" \
  --agent-resource-id "<agent_resource_id>" \
  --delete
```

---

## 4. Offline `--dry-run` & Local Smoke Verification

You do **not** need a live Gemini Enterprise `app_id` or GCP credentials to validate registration payloads or test the A2A + Studio handoff flow locally.

### Offline Registration Payload Preview (`--dry-run`)

```bash
# Preview A2A registration payloads for all three agents offline
uv run python deployment/register_gemini_enterprise.py \
  --project demo-project \
  --location global \
  --app-id placeholder-app-id \
  --agent all \
  --mode a2a \
  --a2a-base-url "https://trend-trawler-api.example.com" \
  --register \
  --dry-run

# Preview Reasoning Engine registration payload offline
uv run python deployment/register_gemini_enterprise.py \
  --project demo-project \
  --location us-central1 \
  --engine-location us-central1 \
  --app-id placeholder-app-id \
  --agent trend_scout \
  --mode reasoning-engine \
  --reasoning-engine-id 1234567890 \
  --register \
  --dry-run
```

### Local A2A Server + Studio Handoff Smoke Test

Start the backend locally with `ENABLE_A2A_SERVER=1` and `TRUST_CLIENT_USER_ID=1`:

```bash
ENABLE_A2A_SERVER=1 \
TRUST_CLIENT_USER_ID=1 \
SESSION_SERVICE_URI=memory:// \
A2A_PUBLIC_BASE_URL=http://localhost:8000 \
STUDIO_PUBLIC_URL=http://localhost:3000 \
A2A_HANDOFF_SECRET=local-dev-handoff-secret \
ALLOW_ORIGINS=http://localhost:3000 \
  uv run uvicorn deployment.async_app:app --port 8000
```

Verify the Agent Cards are served immediately while existing Studio endpoints stay unchanged:

```bash
curl -s http://localhost:8000/a2a/trend_scout/.well-known/agent-card.json | jq .
curl -s http://localhost:8000/a2a/creative_agent/.well-known/agent-card.json | jq .
curl -s http://localhost:8000/a2a/interactive_creative/.well-known/agent-card.json | jq .
curl -s http://localhost:8000/list-apps | jq .
```
