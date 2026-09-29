# P4a: Infrastructure as Code (Foundation Bootstrap) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` (or `subagent-driven-development`) to implement this plan task-by-task.

**Status:** proposal (not started)
**Goal:** Replace the click-ops + runbook provisioning in `deployment/README.md` with a Terraform root module (`infra/terraform/`). The module declares every long-lived GCP resource the example needs: APIs, service accounts, IAM, Pub/Sub topics, the BigQuery dataset and tables, the GCS bucket, the sessions Reasoning Engine, the four Cloud Run services (web, api and two functions) with their config, the Eventarc triggers and IAP. A new user can bootstrap a project with one `terraform apply`. The author's live project can be adopted with `import` blocks so that `terraform plan` shows **no changes**, and nothing gets recreated.
**Architecture:** Terraform owns each resource's *shape*: identity, IAM, scaling, CPU, timeouts, env, triggers and IAP. Scripts and CI keep owning the *artifacts*: container images and function source (`gcloud run deploy --source`) and the served Agent Engine agents (`deployment/deploy_agent.py`). Cloud Run resources `ignore_changes` the image, build and traffic fields, so the two owners never fight. State lives in a versioned GCS backend. Existing resources are adopted with `import` blocks gated by `var.adopt_existing`.
**Tech Stack:** Terraform >= 1.9 (for_each on `import` blocks needs >= 1.7), `hashicorp/google` provider `~> 8.0` (latest release is 8.3.0, 2026-09-15; the floor for everything used here is 7.21.0), GCS backend, GitHub Actions (`hashicorp/setup-terraform`). The module avoids any google-beta-only field, so it should also run under OpenTofu, but this is untested.

---

## Conventions

- **Commits:** conventional prefixes (`feat(infra):`, `docs:`, `ci:`). Commit messages and PR bodies must **never** include `Co-Authored-By` trailers, "Generated with Claude Code", or any other AI attribution. This follows CODE_STANDARDS §1 and overrides any tool default.
- Branch off `main` (e.g. `feat/p4a-terraform-foundation`). One commit per task.
- **Plan-only by default.** No task runs `terraform apply` against the live project except Task 9 step 4, and that step only after a human reviews the plan output. Any `apply` there is import-only.
- **No hardcoded author identifiers.** Project ID, project number, bucket, IAP domain, engine IDs and the CRF service account come only from `terraform.tfvars`. Commit `terraform.tfvars.example` and gitignore `*.tfvars` (except the example) and `backend.hcl`. This extends refresh Task 3.5 (#148).
- **Shared-project safety.** The author's project hosts about 20 unrelated Cloud Run services, and the project IAM policy has about 140 bindings. Use only **non-authoritative** IAM (`*_iam_member`). Never use `google_project_iam_binding`/`_policy` or `google_*_iam_policy`.

## Resource inventory

The inventory was checked live on 2026-09-29 with read-only `gcloud … describe/list/get-iam-policy` and `bq show` in the author's project, region `us-central1`. "Runbook §" refers to `deployment/README.md`. "QS" is `README.md` Quickstart step 4.

| # | Resource | Name (variable → current value) | Created today by | IaC scope | Live |
|---|---|---|---|---|---|
| 1 | Enabled APIs | run, cloudbuild, artifactregistry, aiplatform, bigquery, pubsub, eventarc, iap, storage, iam, cloudresourcemanager, logging | Frontend §0 + §7 (`services enable`) | **In** (`google_project_service`, `disable_on_destroy=false`) | yes, all enabled |
| 2 | Pub/Sub topic (orchestrator) | `creative_topic_name` → `creative-eventarc-topic` | CRF §2 | **In** | yes |
| 3 | Pub/Sub topic (worker) | `creative_worker_topic_name` → `creative-worker-queue-topic` | CRF §2 | **In** | yes |
| 4 | Eventarc push subscriptions | `eventarc-us-central1-<trigger>-sub-NNN` | auto-created by the triggers | Out (owned by Eventarc) | yes |
| 5 | BigQuery dataset | `bq_dataset_id` → `trend_trawler`, location `US` | QS (`bq mk --dataset`) | **In** | yes (US) |
| 6 | Table `target_trends_crf` | `bq_table_targets` | QS `bq mk`, CRF §3.0 `ALTER`, trend-scout `research_gaps` `ALTER` | **In** (schema JSON matches live column order) | yes, 15 cols (see drift) |
| 7 | Table `trend_creatives` | `bq_table_creatives` | QS | **In** | yes, 8 cols |
| 8 | Table `creative_evals` | `bq_table_evals` | QS (+ ws3 `research_gaps` `ALTER`) | **In** | yes, 16 cols |
| 9 | Table `all_trends` | `BQ_TABLE_ALL_TRENDS` | nothing (env var only; no code writes it) | **Out** (see drift) | **missing** |
| 10 | GCS artifacts bucket | `gcs_bucket` → `$GOOGLE_CLOUD_STORAGE_BUCKET` (us-central1, UBLA) | manual | **In** | yes |
| 11 | Terraform state bucket | `<project>-tfstate` | new (Task 1 one-liner) | Bootstrap script, not the module | n/a |
| 12 | SA `tt-api-sa` | `api_sa_id` | Frontend §1 | **In** | yes |
| 13 | `tt-api-sa` project roles | aiplatform.user, bigquery.dataEditor, bigquery.jobUser, storage.objectAdmin, logging.logWriter | Frontend §1 | **In** | yes, exact match |
| 14 | SA `tt-web-sa` | `web_sa_id` | Frontend §1 | **In** | yes |
| 15 | `tt-web-sa` project roles | storage.objectViewer, logging.logWriter | Frontend §1 | **In** | yes, exact match |
| 16 | CRF runtime + trigger SA | today the **default compute SA** | CRF §1 | **In**, via `crf_service_account` (empty = create `tt-crf-sa`) | yes (see drift) |
| 17 | CRF SA roles | eventarc.eventReceiver, run.invoker (+ aiplatform.user, bigquery.dataEditor/jobUser, pubsub.publisher, logging.logWriter for a dedicated SA) | CRF §1 (only the first two) | **In** | yes (compute SA also has `roles/owner`) |
| 18 | Agent Engine service agent roles | `service-<num>@gcp-sa-aiplatform-re` → bigquery.dataEditor/jobUser, storage.objectAdmin | implicit (has `roles/editor` live) | **In** (additive, greenfield need) | yes |
| 19 | Sessions Reasoning Engine | `sessions_engine_display_name` → `trend-trawler-sessions` (no spec) | Frontend §6 `create_session_engine.py` | **In** (`google_vertex_ai_reasoning_engine`) | yes |
| 20 | Served Agent Engine agents | `trend-scout-agent-v2`, `creative-trend-agent-v7` | `deploy_agent.py` | **Out** (app deploy) | yes (`interactive_creative` not deployed) |
| 21 | Cloud Run `trend-trawler-api` | 4 CPU / 8Gi, min 1, max 100, conc 320, timeout 900, `cpu_idle=false`, startup boost, private | Frontend §2, §6, §8 | **In** (shape; image ignored) | yes |
| 22 | api `run.invoker` → `tt-web-sa` | service IAM member | Frontend §3 | **In** | yes |
| 23 | Cloud Run `trend-trawler-web` | 1 CPU / 1Gi, min 0, max 100, conc 80, timeout 300, env `ADK_API_BASE` | Frontend §4 | **In** | yes |
| 24 | IAP on web | `iap_enabled=true`; IAP agent `run.invoker`; `iap.httpsResourceAccessor` → `iap_members` | Frontend §7 | **In** | yes (`run.googleapis.com/iap-enabled: true`) |
| 25 | CRF `creative-trawler-crf` | fn `crf_entrypoint`, python313, 4/8Gi, min 0, conc 100, timeout 600 | CRF §3.1 | **In** (shape; build ignored) | yes |
| 26 | CRF `creative-worker-crf` | fn `agent_worker_entrypoint`, 4/8Gi, **max 1**, **conc 1**, **timeout 1800** | CRF §3.3 | **In** | yes, exact |
| 27 | Eventarc trigger (orchestrator) | `creative-eventarc-trigger` → topic #2 → #25 | CRF §3.2 | **In** | yes |
| 28 | Eventarc trigger (worker) | `creative-worker-starter-trigger` → topic #3 → #26 | CRF §3.4 | **In** | yes |
| 29 | Artifact Registry `cloud-run-source-deploy` | auto-created by `--source` deploys | gcloud | Out | yes |

**Total: 29 inventory rows, 22 in module scope.** That comes to about 55 Terraform resource addresses once each API and role is expanded.

### Drift found (runbook/code vs live)

1. **CRF env vars are missing live.** Both functions run with **no env vars**, but since #148 the runbook's §3.1/§3.3 `--set-env-vars` makes `GOOGLE_CLOUD_PROJECT` required. The live images predate #148 and still hardcode the project, so they work. The next source redeploy without the env list will raise `RuntimeError` at first use. Terraform declares the env vars, so this is an *intended* diff (see Task 9).
2. **api has a leftover `BUCKET` env var.** #147 dropped it from the code, but the live revision may predate #147, so removing it is not provably safe. Keep it via `api_extra_env` until the next api deploy.
3. **api traffic is pinned** to one revision (`latestRevision` is not set). §8 claims "Both services now follow LATEST". Web follows LATEST. Terraform ignores `traffic`, and §8 must be corrected.
4. **The §8 rollback-tag table is stale.** `main-clean` and `main-current` now point at newer revisions than the ones listed.
5. **Web `run.invoker` includes a `user:` principal** besides the IAP agent. It is not in the runbook. Member-level IAM leaves it alone. Decide whether to keep it (open question).
6. **BigQuery `target_trends_crf`:**
   - The live table has an extra `orchestrator_claim_id STRING` column (2nd position). No code references it.
   - The QS `bq mk` **omits `research_gaps`**, but `trend_scout/tools.py` INSERTs it. A greenfield user following QS gets a failing `trend_scout` persist step. Terraform fixes this.
7. **`all_trends` doesn't exist live**, yet it is in `.env.example`, the api env and `agent_common/config.py`. Nothing writes to it. Leave it out of IaC and remove the env var in a follow-up.
8. **The CRFs run as the default compute SA, which holds `roles/owner`.** Greenfield defaults to a least-privilege `tt-crf-sa`. The live project keeps the compute SA via tfvars until a deliberate migration.
9. **Stray topic `creative-trigger-topic`** is not in `.env.example` and nothing uses it. Out of scope and a cleanup candidate.
10. **Legacy tables** (`*_v1/_v2/_v3`, `target_trends_crf_p95`, `target_trends`, `count`) are out of scope and never imported.
11. **§7 hardcodes the author's IAP domain.** This becomes `var.iap_members`.

## Tooling decision

**Terraform, not a gcloud bootstrap script.** The verified facts:

| Question | Answer | Source |
|---|---|---|
| Is the Reasoning Engine in the GA `google` provider? | **Yes.** `google_vertex_ai_reasoning_engine`, new in **7.6.0 (2025-10-07)**. `display_name` is required and `spec` is **optional**, so a bare sessions-only engine works. `deletion_policy` accepts `PREVENT`/`ABANDON`/`FORCE`. Import ID: `projects/{project}/locations/{region}/reasoningEngines/{id}`. Only `traffic_config`/`url` are beta. | registry `r/vertex_ai_reasoning_engine`, provider CHANGELOG 7.6.0 / 7.13.0 |
| Is Cloud Run direct IAP in Terraform? | **Yes, GA.** `iap_enabled` on `google_cloud_run_v2_service` was promoted to GA in **7.21.0 (2026-02-24)** after beta in 6.30.0. Access goes through `google_iap_web_cloud_run_service_iam_member` (6.31.0). Caveat: you can't combine it with IAP on a load balancer, and non-org projects need a console OAuth setup first. | registry `r/cloud_run_v2_service`, `r/iap_web_cloud_run_service_iam`; https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run |
| Can Terraform build Cloud Run functions from source? | **Only indirectly.** `build_config` (`function_target`, `base_image`, `source_location` = a **GCS** object, `enable_automatic_updates`) on `google_cloud_run_v2_service` since 6.18.0. The docs' Terraform path expects a pre-staged source zip or pre-built image. The local-dir `gcloud run deploy --source . --function` flow has no Terraform equivalent. | https://docs.cloud.google.com/run/docs/deploy-functions, registry `build_config` block |
| Other resources | `google_eventarc_trigger` (`destination.cloud_run_service`, `transport.pubsub.topic`), `google_bigquery_table` (`deletion_protection`, JSON `schema`), `google_storage_bucket`, `google_pubsub_topic` and `google_project_service` are all GA with import support. | registry |

**Why Terraform wins:**
1. **Import and drift detection.** Adopting a live project with a zero-diff proof is the key requirement here. A bash script can only ever do "create-if-missing".
2. **Every resource is supported GA**, including the two that were in doubt (Reasoning Engine and direct IAP).
3. **Declarative IAM** with `_member` semantics is safe in a shared project.
4. It is the idiom readers expect from an "end-to-end example".

**Recommended split:**
- **Terraform (`infra/terraform`):** APIs, SAs, IAM, topics, dataset and tables, the artifacts bucket, the sessions engine, the Cloud Run/function *service shells* (config, env, SA, scaling, IAP), Eventarc triggers and IAP access.
- **Scripts/CI (unchanged):** `gcloud run deploy <svc> --source …` for images (new revisions inherit Terraform-managed config, because gcloud only changes the flags it's given), `deploy_agent.py` for served agents, and SQL migrations for future schema changes on *existing* data (Terraform handles additive columns too; see Task 4).
- **Bootstrap one-liner:** the Terraform state bucket (chicken-and-egg), plus the IAP OAuth consent screen if the project has no org (console step).

## Directory layout

```
infra/terraform/
  versions.tf            # required_version, google provider pin
  backend.tf             # backend "gcs" {} (partial config)
  providers.tf
  variables.tf           # every identifier; no defaults that name the author
  terraform.tfvars.example
  backend.hcl.example
  apis.tf
  iam.tf                 # SAs + project/service IAM members
  pubsub.tf
  bigquery.tf
  schemas/{target_trends_crf,trend_creatives,creative_evals}.json
  storage.tf
  sessions_engine.tf
  cloud_run.tf           # web + api
  functions.tf           # orchestrator + worker
  eventarc.tf
  iap.tf
  imports.tf             # import blocks, for_each-gated by var.adopt_existing
  outputs.tf
  README.md              # short: greenfield vs adopt, link to deployment/README.md
infra/bootstrap-state.sh # creates versioned tfstate bucket (idempotent)
.github/workflows/terraform-ci.yml
```

A flat root module is deliberate. There is one environment and about 55 resources, and modules would add indirection without any reuse. Split into modules only if a second environment appears.

---

## Tasks

### Task 1: Skeleton, provider pin, backend, variables

**Files:** `infra/terraform/{versions,backend,providers,variables}.tf`, `terraform.tfvars.example`, `backend.hcl.example`, `infra/bootstrap-state.sh`, `.gitignore`.

```hcl
# versions.tf
terraform {
  required_version = ">= 1.9"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 8.0" } # >= 7.21 needed (iap_enabled GA)
  }
}
# backend.tf — values supplied via `terraform init -backend-config=backend.hcl`
terraform {
  backend "gcs" {}
}
# providers.tf
provider "google" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
  default_labels        = { agent-workflow = "trend-trawler", managed-by = "terraform" }
}
```

`default_labels` changes every labelled resource on import. If that breaks the "No changes" goal in Task 9, set `default_labels = {}` for adoption and add the labels in a separate reviewed apply. Decide during Task 9.

```hcl
# variables.tf (excerpt — every value is a variable)
variable "project_id"      { type = string }
variable "project_number"  { type = string }
variable "region"          { type = string, default = "us-central1" }
variable "bq_location"     { type = string, default = "US" }
variable "gcs_bucket"      { type = string }                       # bare name, no gs://
variable "bq_dataset_id"   { type = string, default = "trend_trawler" }
variable "bq_table_targets"   { type = string, default = "target_trends_crf" }
variable "bq_table_creatives" { type = string, default = "trend_creatives" }
variable "bq_table_evals"     { type = string, default = "creative_evals" }
variable "creative_topic_name"        { type = string, default = "creative-eventarc-topic" }
variable "creative_worker_topic_name" { type = string, default = "creative-worker-queue-topic" }
variable "crf_service_account" {
  type        = string
  default     = ""   # empty => create least-privilege tt-crf-sa
  description = "Existing SA email for the CRFs + triggers (adoption: the compute default SA)."
}
variable "iap_members"     { type = list(string) }                 # e.g. ["domain:example.com"]
variable "bootstrap_image" { type = string, default = "us-docker.pkg.dev/cloudrun/container/hello" }
variable "api_extra_env"   { type = map(string), default = {} }    # adoption shim (e.g. BUCKET)
variable "adopt_existing"  { type = bool, default = false }
variable "existing_sessions_engine_id" { type = string, default = "" }
variable "manage_ae_service_agent_roles" { type = bool, default = true }
```

`bootstrap-state.sh`:

```bash
gcloud storage buckets create "gs://${TF_STATE_BUCKET:-$GOOGLE_CLOUD_PROJECT-tfstate}" --location="$GCP_REGION" --uniform-bucket-level-access
gcloud storage buckets update "gs://…" --versioning
```

It exits 0 if the bucket already exists. `backend.hcl.example` holds `bucket = "<project>-tfstate"` and `prefix = "trend-trawler/foundation"`.

**Gitignore:** `infra/terraform/.terraform/`, `*.tfstate*`, `*.tfvars`, `!terraform.tfvars.example`, `backend.hcl`. Commit `.terraform.lock.hcl`.

**Validate:** `terraform -chdir=infra/terraform fmt -check -recursive && terraform -chdir=infra/terraform init -backend=false && terraform -chdir=infra/terraform validate`.
**Commit:** `feat(infra): terraform skeleton, provider pin, gcs backend, variables`

### Task 2: APIs, service accounts, IAM

**Files:** `apis.tf`, `iam.tf`.

```hcl
locals {
  apis = toset(["run", "cloudbuild", "artifactregistry", "aiplatform", "bigquery", "pubsub",
                "eventarc", "iap", "storage", "iam", "cloudresourcemanager", "logging"])
  crf_sa  = var.crf_service_account != "" ? var.crf_service_account : google_service_account.crf[0].email
  api_roles = toset(["roles/aiplatform.user", "roles/bigquery.dataEditor", "roles/bigquery.jobUser",
                     "roles/storage.objectAdmin", "roles/logging.logWriter"])
  web_roles = toset(["roles/storage.objectViewer", "roles/logging.logWriter"])
  crf_base_roles  = toset(["roles/eventarc.eventReceiver", "roles/run.invoker"]) # runbook CRF §1
  crf_extra_roles = toset(["roles/aiplatform.user", "roles/bigquery.dataEditor", "roles/bigquery.jobUser",
                           "roles/pubsub.publisher", "roles/logging.logWriter"])   # dedicated SA only
}
resource "google_project_service" "apis" {
  for_each           = local.apis
  service            = "${each.key}.googleapis.com"
  disable_on_destroy = false
}
resource "google_service_account" "api" { account_id = "tt-api-sa", display_name = "trend-trawler api_server" }
resource "google_service_account" "web" { account_id = "tt-web-sa", display_name = "trend-trawler web frontend" }
resource "google_service_account" "crf" {
  count        = var.crf_service_account == "" ? 1 : 0
  account_id   = "tt-crf-sa"
  display_name = "trend-trawler CRF orchestrator/worker + eventarc"
}
resource "google_project_iam_member" "api" {
  for_each = local.api_roles
  project  = var.project_id
  role     = each.key
  member   = "serviceAccount:${google_service_account.api.email}"
}
# ... same shape for web (local.web_roles) and crf (base ∪ extra when dedicated)
resource "google_project_iam_member" "ae_service_agent" {
  for_each = var.manage_ae_service_agent_roles ? toset(["roles/bigquery.dataEditor",
             "roles/bigquery.jobUser", "roles/storage.objectAdmin"]) : toset([])
  project    = var.project_id
  role       = each.key
  member     = "serviceAccount:service-${var.project_number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
  depends_on = [google_vertex_ai_reasoning_engine.sessions] # agent is provisioned lazily
}
```

The CRF role set is `setunion(local.crf_base_roles, var.crf_service_account == "" ? local.crf_extra_roles : toset([]))`. In adoption mode that is exactly the two bindings CRF §1 grants.

For the dedicated SA, the Pub/Sub service agent needs `iam.serviceAccountTokenCreator` only on projects created before 2021-04-08. Leave it out; it is already present live.

**Validate:** `validate`. `terraform plan` in a scratch project shows about 25 creates.
**Commit:** `feat(infra): APIs, service accounts and non-authoritative IAM`

### Task 3: Pub/Sub topics and GCS bucket

```hcl
resource "google_pubsub_topic" "creative" {
  for_each = { orchestrator = var.creative_topic_name, worker = var.creative_worker_topic_name }
  name     = each.value
}
resource "google_storage_bucket" "artifacts" {
  name                        = var.gcs_bucket
  location                    = upper(var.region)
  uniform_bucket_level_access = true
  force_destroy               = false
  lifecycle { prevent_destroy = true }
}
```

Leave `soft_delete_policy`, `versioning` and `public_access_prevention` unset, so the computed live values are adopted. Live has the 7-day default soft delete and PAP `inherited`.

**Commit:** `feat(infra): pubsub topics and artifacts bucket`

### Task 4: BigQuery dataset and tables

**Files:** `bigquery.tf`, `schemas/*.json`.

Generate each schema from live, so column order and modes match exactly. A reordered schema diffs, and can force table replacement.

```bash
for t in target_trends_crf trend_creatives creative_evals; do
  bq show --schema --format=prettyjson "$GOOGLE_CLOUD_PROJECT:trend_trawler.$t" > infra/terraform/schemas/$t.json
done
```

Commit the JSON. `target_trends_crf.json` keeps `orchestrator_claim_id` for import fidelity; see open questions.

```hcl
resource "google_bigquery_dataset" "trend_trawler" {
  dataset_id = var.bq_dataset_id
  location   = var.bq_location
  # no `access` blocks: Optional+Computed, so live ACLs are left untouched
  lifecycle { prevent_destroy = true }
}
locals {
  tables = {
    targets   = { id = var.bq_table_targets,   schema = "target_trends_crf.json" }
    creatives = { id = var.bq_table_creatives, schema = "trend_creatives.json" }
    evals     = { id = var.bq_table_evals,     schema = "creative_evals.json" }
  }
}
resource "google_bigquery_table" "t" {
  for_each            = local.tables
  dataset_id          = google_bigquery_dataset.trend_trawler.dataset_id
  table_id            = each.value.id
  schema              = file("${path.module}/schemas/${each.value.schema}")
  deletion_protection = true
  lifecycle { prevent_destroy = true }
}
```

**Future migrations:** append the new column to the **end** of the JSON (nullable). The provider applies an in-place additive update, which is the Terraform equivalent of `ADD COLUMN IF NOT EXISTS`. Any other schema change stays a hand-run SQL migration followed by a JSON sync.

**Docs:** replace the QS `bq mk` block in `README.md` with "run `terraform apply` (see `infra/terraform/README.md`)". Keep a collapsed fallback `bq mk` block, corrected to include `research_gaps` on `target_trends_crf` (drift #6).
**Commit:** `feat(infra): bigquery dataset + tables from live schemas; fix quickstart research_gaps`

### Task 5: Sessions Reasoning Engine

```hcl
resource "google_vertex_ai_reasoning_engine" "sessions" {
  display_name    = var.sessions_engine_display_name  # default "trend-trawler-sessions"
  region          = var.region
  deletion_policy = "PREVENT"   # holds every persisted ADK session
  # no `spec`: a bare engine that serves no agent (same as create_session_engine.py)
  lifecycle { prevent_destroy = true }
}
locals {
  sessions_engine_id  = reverse(split("/", google_vertex_ai_reasoning_engine.sessions.id))[0]
  session_service_uri = "agentengine://projects/${var.project_number}/locations/${var.region}/reasoningEngines/${local.sessions_engine_id}"
}
```

Keep `deployment/create_session_engine.py` as the non-Terraform path, and point its docstring at Terraform as the preferred path. Check that `id` ends in the numeric engine ID. If it doesn't, derive the ID from `name`.

**Commit:** `feat(infra): sessions reasoning engine`

### Task 6: Cloud Run web + api (service shells)

**File:** `cloud_run.tf`. The shared lifecycle block is the contract with the `gcloud run deploy --source` flow:

```hcl
resource "google_cloud_run_v2_service" "api" {
  name                = "trend-trawler-api"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = true
  template {
    service_account                  = google_service_account.api.email
    timeout                          = "900s"
    max_instance_request_concurrency = 320
    scaling { min_instance_count = 1, max_instance_count = 100 }
    containers {
      image = var.bootstrap_image          # replaced by the first --source deploy
      resources {
        limits            = { cpu = "4", memory = "8Gi" }
        cpu_idle          = false          # == --no-cpu-throttling (REQUIRED: detached runs)
        startup_cpu_boost = true
      }
      dynamic "env" {
        for_each = merge(local.api_env, var.api_extra_env)
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }
  lifecycle {
    prevent_destroy = true
    ignore_changes = [
      client, client_version, build_config, traffic,
      template[0].revision, template[0].containers[0].image,
      template[0].containers[0].base_image_uri,
    ]
  }
}
locals {
  api_env = {
    GOOGLE_GENAI_USE_VERTEXAI = "1", GOOGLE_CLOUD_PROJECT = var.project_id,
    GOOGLE_CLOUD_PROJECT_NUMBER = var.project_number, GCP_REGION = var.region,
    GOOGLE_CLOUD_STORAGE_BUCKET = var.gcs_bucket, BQ_PROJECT_ID = var.project_id,
    BQ_DATASET_ID = var.bq_dataset_id, BQ_TABLE_TARGETS = var.bq_table_targets,
    BQ_TABLE_CREATIVES = var.bq_table_creatives, BQ_TABLE_EVALS = var.bq_table_evals,
    BQ_TABLE_ALL_TRENDS = "all_trends",  # live parity; drop with drift #7 follow-up
    SESSION_SERVICE_URI = local.session_service_uri,
  }
}
resource "google_cloud_run_v2_service_iam_member" "web_invokes_api" {
  name     = google_cloud_run_v2_service.api.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.web.email}"
}
```

`trend-trawler-web` has the same shape: `tt-web-sa`, 1 CPU / 1Gi, `cpu_idle = true`, min 0 / max 100, concurrency 80, timeout 300s, env `ADK_API_BASE = google_cloud_run_v2_service.api.uri`, and `iap_enabled = true` (Task 8).

**Env ownership:** Terraform now owns the env. The runbook's §2/§8 redeploy becomes `gcloud run deploy trend-trawler-api --source . --region $REGION`, with **no** `--set-env-vars` or scaling flags. Passing them would create drift that the next `plan` reverts.

The two literal `name`s become `var.api_service_name` / `var.web_service_name`, with the current values as defaults.

**Commit:** `feat(infra): cloud run web + api service definitions (image owned by deploy)`

### Task 7: Cloud Run functions + Eventarc triggers

**Files:** `functions.tf`, `eventarc.tf`. The same lifecycle contract applies, and `build_config` is ignored.

```hcl
locals {
  crf_env = {
    GOOGLE_CLOUD_PROJECT = var.project_id, GOOGLE_CLOUD_PROJECT_NUMBER = var.project_number,
    GCP_REGION = var.region, CREATIVE_WORKER_TOPIC_NAME = var.creative_worker_topic_name,
    AGENT_WORKER_USER_ID = var.agent_worker_user_id,
  }
  functions = {
    orchestrator = { name = var.creative_crf_name, entry = "crf_entrypoint",
                     min = 0, max = 100, conc = 100, timeout = "600s", topic = "orchestrator",
                     trigger = var.creative_trigger_name, label = "creative-orchestrator" }
    worker       = { name = var.creative_worker_crf_name, entry = "agent_worker_entrypoint",
                     min = 0, max = 1, conc = 1, timeout = "1800s", topic = "worker",
                     trigger = var.creative_worker_trigger_name, label = "creative-worker" }
  }
}
resource "google_cloud_run_v2_service" "fn" {
  for_each            = local.functions
  name                = each.value.name
  location            = var.region
  deletion_protection = true
  labels              = { function = each.value.label }
  template {
    service_account                  = local.crf_sa
    timeout                          = each.value.timeout
    max_instance_request_concurrency = each.value.conc
    scaling {
      min_instance_count = each.value.min
      max_instance_count = each.value.max # worker max=1 SERIALIZES runs (5-RPM pro / 2-RPM image quota)
    }
    containers {
      image = var.bootstrap_image
      resources {
        limits            = { cpu = "4", memory = "8Gi" }
        cpu_idle          = true
        startup_cpu_boost = true
      }
      dynamic "env" {
        for_each = local.crf_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }
  lifecycle { prevent_destroy = true, ignore_changes = [/* as Task 6 */] }
}
resource "google_eventarc_trigger" "fn" {
  for_each        = local.functions
  name            = each.value.trigger
  location        = var.region
  service_account = local.crf_sa
  matching_criteria {
    attribute = "type"
    value     = "google.cloud.pubsub.topic.v1.messagePublished"
  }
  transport {
    pubsub { topic = google_pubsub_topic.creative[each.value.topic].id }
  }
  destination {
    cloud_run_service {
      service = google_cloud_run_v2_service.fn[each.key].name
      region  = var.region
    }
  }
}
```

For the dedicated `tt-crf-sa` only, grant `roles/run.invoker` per service with `google_cloud_run_v2_service_iam_member`, instead of the project-wide grant in CRF §1.

**Function deploy script:** CRF §3.1/§3.3 shrinks to:

```bash
gcloud run deploy $CREATIVE_CRF_NAME --source . --function crf_entrypoint --base-image python313 --region $GCP_REGION
```

Terraform owns memory, CPU, concurrency, timeout, max instances, SA and env.

**Greenfield check:** in a scratch project, confirm that `gcloud run deploy --source --function` converts a Terraform-created service still running the placeholder image into a function. If it doesn't, the fallback is to stage the zip in GCS and set `build_config { source_location, function_target, base_image }` in Terraform for greenfield only.

**Commit:** `feat(infra): CRF orchestrator/worker services and eventarc triggers`

### Task 8: IAP on the web service

```hcl
# on google_cloud_run_v2_service.web:
iap_enabled = true

resource "google_cloud_run_v2_service_iam_member" "iap_invokes_web" {
  name     = google_cloud_run_v2_service.web.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${var.project_number}@gcp-sa-iap.iam.gserviceaccount.com"
}
resource "google_iap_web_cloud_run_service_iam_member" "access" {
  for_each               = toset(var.iap_members)
  project                = var.project_id
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.web.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.key
}
```

The module never creates an `allUsers` binding. Pair this with `invoker_iam_disabled = false`, the default.

**Greenfield prerequisite:** on a project without an organization, configure the OAuth consent screen in the console first (per the IAP doc). Verify with `curl -sI $WEB_URL/`, expecting a `302` to `accounts.google.com`.

**Commit:** `feat(infra): direct IAP on trend-trawler-web`

### Task 9: Adopt the live project (import) — zero-diff proof

**File:** `imports.tf`. The blocks are inert unless `adopt_existing = true`, so greenfield users never import anything.

```hcl
locals {
  p   = var.project_id
  r   = var.region
  adopt = var.adopt_existing
}
import {
  for_each = local.adopt ? local.apis : toset([])
  to       = google_project_service.apis[each.key]
  id       = "${local.p}/${each.key}.googleapis.com"
}
import {
  for_each = local.adopt ? { orchestrator = var.creative_topic_name, worker = var.creative_worker_topic_name } : {}
  to       = google_pubsub_topic.creative[each.key]
  id       = "projects/${local.p}/topics/${each.value}"
}
import {
  for_each = local.adopt ? local.tables : {}
  to       = google_bigquery_table.t[each.key]
  id       = "projects/${local.p}/datasets/${var.bq_dataset_id}/tables/${each.value.id}"
}
import {
  for_each = local.adopt ? local.functions : {}
  to       = google_cloud_run_v2_service.fn[each.key]
  id       = "projects/${local.p}/locations/${local.r}/services/${each.value.name}"
}
import {
  for_each = local.adopt ? local.functions : {}
  to       = google_eventarc_trigger.fn[each.key]
  id       = "projects/${local.p}/locations/${local.r}/triggers/${each.value.trigger}"
}
import {
  for_each = local.adopt ? { x = 1 } : {}
  to       = google_vertex_ai_reasoning_engine.sessions
  id       = "projects/${local.p}/locations/${local.r}/reasoningEngines/${var.existing_sessions_engine_id}"
}
# + single-resource imports (for_each over {x=1} when adopting):
#   google_bigquery_dataset.trend_trawler     "projects/P/datasets/D"
#   google_storage_bucket.artifacts           "P/BUCKET"
#   google_service_account.api / .web         "projects/P/serviceAccounts/tt-api-sa@P.iam.gserviceaccount.com"
#   google_cloud_run_v2_service.api / .web    "projects/P/locations/R/services/NAME"
#   google_project_iam_member.api[role]       "P ROLE serviceAccount:EMAIL"   (same for web, crf, ae_service_agent)
#   google_cloud_run_v2_service_iam_member.*  "projects/P/locations/R/services/NAME roles/run.invoker MEMBER"
#   google_iap_web_cloud_run_service_iam_member.access[m]
#       "projects/P/iap_web/cloud_run-R/services/NAME roles/iap.httpsResourceAccessor MEMBER"
```

**Author's `terraform.tfvars`** (gitignored; values come from `.env` and the inventory commands):
- `adopt_existing = true`
- `crf_service_account = "<PROJECT_NUMBER>-compute@developer.gserviceaccount.com"`
- `existing_sessions_engine_id = "<id from SESSION_SERVICE_URI>"`
- `iap_members = ["domain:<IAP_DOMAIN>"]`
- `api_extra_env = { BUCKET = "gs://<bucket>" }`

**Steps:**
1. `infra/bootstrap-state.sh`, then `terraform init -backend-config=backend.hcl`.
2. `terraform plan -out=adopt.tfplan`. Expect about 55 "to import", **0 to destroy**, and **0 to replace**. **Stop if any `-/+` or `destroy` appears.**
3. Iterate on the HCL and `ignore_changes` until the only non-import diffs are the **intended** ones:
   - (a) CRF env vars added (drift #1). The images are unchanged, and old code ignores the extra vars.
   - (b) Possibly `default_labels`.
4. **Two-phase apply.** First apply with the CRF env vars temporarily listed in `ignore_changes`. The result must be import-only ("N imported, 0 added, 0 changed, 0 destroyed"). Then remove the ignore and apply the env-var change on its own, reviewed.
5. Re-run `terraform plan` and expect **"No changes. Your infrastructure matches the configuration."** Record the output in the PR description.
6. Leave `adopt_existing = true` in the author's tfvars. The blocks are no-ops after import. Delete them later if you like.

**Commit:** `feat(infra): gated import blocks to adopt an existing deployment`

### Task 10: Greenfield path

In a **scratch project**, never the author's:
1. `cp terraform.tfvars.example terraform.tfvars` and fill in `project_id`, `project_number`, `gcs_bucket` and `iap_members`.
2. Run `bootstrap-state.sh`, then `init`, then `apply`.
3. Run the four `gcloud run deploy --source` commands.
4. Run `deploy_agent.py --create` for the served agents.
5. Run `integration_test.py --check health`.
6. Publish the orchestrator message (CRF §5).

Record the wall-clock time and any manual steps in `infra/terraform/README.md`. Then run `terraform destroy`. It must fail on `prevent_destroy`, which proves the guard works. Remove the scratch project with `gcloud projects delete`.

**Commit:** `docs(infra): greenfield bootstrap walkthrough`

### Task 11: CI

**File:** `.github/workflows/terraform-ci.yml`. It triggers on push/PR to `main` touching `infra/**` or the workflow itself.

```yaml
permissions: { contents: read }
jobs:
  terraform:
    runs-on: ubuntu-latest
    defaults: { run: { working-directory: infra/terraform } }
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
        with: { terraform_version: "1.9.8" }
      - run: terraform fmt -check -recursive
      - run: terraform init -backend=false -input=false
      - run: terraform validate
```

No credentials and no `plan` in CI. A WIF-based `plan` job is an open question. Add `github-actions` coverage for `infra/` to Dependabot, plus the `terraform` ecosystem for provider bumps.

**Commit:** `ci: terraform fmt + validate workflow`

### Task 12: Docs

- `deployment/README.md`:
  - Add a new top section "Provision with Terraform (recommended)" linking `infra/terraform/README.md`.
  - Mark CRF §1–§3.4 and Frontend §0–§4, §6 and §7 as "manual equivalent / reference". Shrink the deploy commands to `--source`-only (Tasks 6–7).
  - Fix §8: api traffic is pinned, and the rollback-tag table is stale (drifts #3/#4).
  - Replace the hardcoded IAP domain with `<IAP_DOMAIN>`.
  - Add a note that env and scaling flags are owned by Terraform.
- `README.md` QS: see Task 4.
- `CLAUDE.md`: add `infra/terraform/` to Key Files and Commands (`terraform -chdir=infra/terraform plan`). Note that Terraform owns service config, and that `gcloud run deploy` must not pass config flags.
- `.env.example`: add a comment that the Terraform variables mirror these names.

**Validate:** relative links resolve, and `grep -rn "<author domain>\|<author project>" infra deployment/README.md` returns nothing.
**Commit:** `docs: terraform-first provisioning; correct runbook drift`

## Rollback / safety

- **Plan-only by default.** Every live apply goes through `terraform plan -out` plus human review. The first live apply is import-only (Task 9 step 4).
- **`prevent_destroy = true`** on the bucket, dataset, all tables, the sessions engine and all four Cloud Run services. Also `deletion_protection = true` on tables and services, and `deletion_policy = "PREVENT"` on the engine. Add `disable_on_destroy = false` on APIs.
- **IAM is non-authoritative** (`_member` only), so Terraform can't strip unrelated bindings in a shared project.
- **Image and traffic stay out of Terraform** (`ignore_changes`), so an apply can't roll back or re-route a deployed revision. The rollback-tag procedure in §8 stays valid.
- **Undo adoption:** `terraform state rm <addr>` (or `removed { lifecycle { destroy = false } }` blocks) stops managing a resource without touching it.
- **State:** the GCS backend is versioned, so you can recover a bad state by restoring the previous object generation. State contains env values but no secrets; the repo has none today.
- **Stop conditions:** any planned `destroy` or `replace` on a stateful resource. Any change to the api's `cpu_idle` or min instances. Any change to the worker's `max_instance_count`, `max_instance_request_concurrency` or `timeout`.

## Open questions

1. Drop the dead `target_trends_crf.orchestrator_claim_id` column? That needs `ALTER TABLE … DROP COLUMN` plus a JSON sync. Import first, drop later.
2. Migrate the live CRFs off the owner-privileged compute SA to `tt-crf-sa`? The switch is one tfvars change plus a reviewed apply, and it also moves the trigger identity.
3. Keep the extra `user:` `run.invoker` on `trend-trawler-web`, or remove it by hand? Terraform won't touch it either way.
4. Delete the unused `creative-trigger-topic` and the `all_trends` env var?
5. Add a WIF-authenticated `terraform plan` job, with drift detection on a nightly schedule?
6. Should Terraform also manage the `cloud-run-source-deploy` Artifact Registry repo (cleanup policies), or keep it gcloud-auto-created?
7. Is the api's leftover `BUCKET` env var safe to remove after the next api deploy? The `api_extra_env` shim then goes away.
8. Does `default_labels` produce acceptable label churn on adoption, or should labels be added later?
