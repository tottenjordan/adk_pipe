# Image Consolidation Implementation Plan (CRF: 2 → 1 · bandit: 2 → 1)

**Goal:** Go from 7 built images to 5:
- the two CRF Cloud Run services run one image;
- the bandit CPR endpoint and the traffic job run one image.

Each deployment picks its entry point at deploy time.

**Architecture:**
- **D (CRF):**
  - A checked-in Dockerfile for `cloud_functions/creative_fanout/` starts `functions-framework --source=main.py --target=$FUNCTION_TARGET`.
  - It is built once as a third parallel step of the root `cloudbuild.yaml`.
  - Both services deploy with `--image <digest>`, each with its own `FUNCTION_TARGET`.
- **E (bandit):**
  - `bandit_serving/Dockerfile` becomes the single bandit image, with the union of both requirement sets.
  - The CPR endpoint keeps the default entrypoint (model server).
  - The traffic job overrides the command (`python -m bandit_traffic.main`).

**Tech Stack:** Docker BuildKit on Cloud Build (registry cache), functions-framework, Cloud Run services/jobs, Eventarc, Vertex CPR.

---

## Context

The user asked to consolidate images after the build/storage follow-ups (#353–#355). Inventory after those PRs:
- api
- web (+ share, already shared)
- CRF orchestrator
- CRF worker
- bandit CPR
- bandit traffic

The 3 agent engines are built by Vertex, so there's nothing for us to consolidate there. Exploration findings:

**CRF:**
- `creative-trawler-crf` and `creative-worker-crf` are plain Cloud Run services (not Cloud Functions gen2 objects).
- Each is deployed with `gcloud run deploy --source . --function <entry> --base-image python313` (deployment/README.md §3, :556-616).
- So every deploy does its **own** buildpack build (~1m20s–2m) into its own package. Today the two run different source snapshots (orchestrator 00012-rmv has the sensitive-row filter; worker 00015-28f is older). These two packages held ~18 GB of deletable versions.
- **Entry points:** `main.py:646` `crf_entrypoint` and `main.py:822` `agent_worker_entrypoint`, both `@functions_framework.cloud_event`. The entry point is baked in by the buildpack (`run.googleapis.com/build-function-target`); there is no `FUNCTION_TARGET` env today.
- `main.py:53-54` uses relative imports, so it **must** start via `functions-framework --source=main.py` (package-style load), not `python main.py`.
- **Triggers:** Eventarc triggers target the service name, so `--image` redeploys keep them.
- **Settings to preserve** (an `--image` deploy without flags keeps them):
  - env: `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_PROJECT_NUMBER`, `GCP_REGION`, plus `CREATIVE_WORKER_TOPIC_NAME` / `CRF_MAX_ROWS_PER_RUN` on the orchestrator and `AGENT_WORKER_USER_ID` on the worker
  - concurrency 100 / 1, max instances 100 / 1, timeout 600 / 1800, 4 CPU / 8Gi
  - the default compute service account, labels, the TCP 8080 startup probe
- **Trade-off (accepted in this plan):** the services currently have Cloud Run **automatic base-image updates** (the `base-images` annotation and the `linux-base-image-update` runtime class), which only work for source/buildpack-built images.
  - Moving to a Dockerfile image on `python:3.13-slim` means OS/runtime patches arrive when we rebuild. The api, web and bandit images already work that way.
  - The deploy clears the base-image setting explicitly (`--clear-base-image`).
- **Bandit:**
  - `bandit_serving/requirements.txt` holds jax, numpy, pyyaml, google-cloud-storage, `google-cloud-aiplatform[prediction]==2.4.0` and protobuf.
  - `bandit_traffic/requirements.txt` holds the same core plus `google-cloud-bigquery==3.46.1` and `requests==2.34.2` (its aiplatform has no extra, so it's a subset of the serving pin).
  - The Dockerfiles are identical apart from requirements, the code copied, the workdir and the entrypoint. JAX dominates both (~931 MB layer), so a merged image is only marginally bigger.
  - Vertex `Model.upload` uses the image's default entrypoint (`deployment/bandit/endpoint.py:71-75`). The traffic job runs via `deployment/bandit/deploy_traffic_job.sh` (`--image`).

**Constraints (both PRs):**
- Worktree with `.python-version` copied (3.13), `unset VIRTUAL_ENV`.
- Gates: `uv run ruff format --check . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 8`.
- No Co-Authored-By or AI attribution.
- Open the PR without merging; no deploys. Pushing images via Cloud Build is OK.
- Each PR edits only its own sections of `deployment/README.md`.

## File ownership (disjoint)

| PR | Branch | Files |
|---|---|---|
| **D** | `build/crf-single-image` | `cloud_functions/creative_fanout/Dockerfile` + `Dockerfile.dockerignore` (new), `cloudbuild.yaml` (root; new `build-crf` step + optional deploy), `deployment/cloudbuild.gcloudignore`, `deployment/cleanup/ar-cloud-run-source-deploy.json` + `tests/test_cleanup_policies.py` (new package), `tests/test_crf_image.py` (new), `.github/workflows/crf-deps.yml` (only if a path filter needs the Dockerfile), `deployment/README.md` §3 (CRF deploy, ~540-620, :760) + the retention package list, plan copy |
| **E** | `build/bandit-single-image` | `bandit_serving/Dockerfile`, `bandit_serving/Dockerfile.dockerignore`, `bandit_serving/requirements.txt`, `bandit_traffic/Dockerfile` + `bandit_traffic/requirements.txt` (deleted), `deployment/bandit/cloudbuild.bandit.yaml`, `deployment/bandit/build_image.py` (staging adds `bandit_traffic/`), `deployment/bandit/deploy_traffic_job.sh`, `tests/test_build_image.py`, `tests/test_bandit_cloudbuild.py`, `docs/bandit/README.md`, `bandit_serving/README.md`, `deployment/README.md` bandit build/traffic sections (~1628-1650) |

**Task D2 owns the retention files.** The new CRF package needs adding to the allowlist; the bandit image keeps the `trend-trawler-bandit` package, which the policy already covers.

---

## PR D — one CRF image

**Task D1: Dockerfile (TDD)**
- **Write `tests/test_crf_image.py` first.** It parses `cloud_functions/creative_fanout/Dockerfile` and asserts:
  - a single dependency-install RUN from `requirements.txt` comes before the code COPY;
  - no `FUNCTION_TARGET` default is baked in, so a service without it fails fast;
  - the start command is `functions-framework --source=main.py --target=${FUNCTION_TARGET} --signature-type=cloudevent` (shell form so the env expands, or `exec` in a tiny entry script);
  - the port comes from `$PORT` (functions-framework honours it; Cloud Run sets 8080);
  - a non-root `USER` is set;
  - the `.dockerignore` allowlist names exactly `main.py`, `config.py`, `session.py`, `__init__.py` and `requirements.txt`.
- **Dockerfile:** follow the uv multi-stage pattern of `bandit_traffic/Dockerfile`:
  - a builder `FROM python:3.13-slim` plus `COPY --from=<pinned uv image>`, `uv venv /opt/venv`, `uv pip install -r requirements.txt`;
  - a final stage on slim with the venv;
  - `WORKDIR /app` with the 4 `.py` files;
  - `PYTHONUNBUFFERED=1`, user uid 10001.
  - Context is the repo root, filtered by `Dockerfile.dockerignore` (the root `.dockerignore` may drop `cloud_functions/`, as happened with `bandit_serving/` in #355).
- **Local smoke, both targets:**
  1. `docker build -f cloud_functions/creative_fanout/Dockerfile -t crf:test .`
  2. `docker run -e FUNCTION_TARGET=crf_entrypoint -e GOOGLE_CLOUD_PROJECT=test-project -p 8080:8080 crf:test`
  3. Check the log shows the function loading, then POST an obviously invalid CloudEvent and expect a handled error, with no GCP calls made before validation. Read `main.py` first to pick a payload that fails before any BigQuery or PubSub call.
  4. Repeat with `agent_worker_entrypoint`.
  5. Run without `FUNCTION_TARGET` and expect a fast failure.
- Commit.

**Task D2: build + retention + docs**
- **Root `cloudbuild.yaml`:**
  - Add a `build-crf` step mirroring `build-api` (`waitFor: [buildx-setup]`, `_CRF_IMAGE = ${_REPO}/creative-fanout-crf`, registry `buildcache`, `--metadata-file`) that prints `CRF_IMAGE=…@sha256:…`.
  - Extend the optional `_DEPLOY=true` step to deploy both services `--no-traffic` with `--update-env-vars FUNCTION_TARGET=<entry> --clear-base-image`. Check gcloud's flag name: if `--clear-base-image` doesn't exist, find the supported way to drop the source base-image setting on an `--image` deploy, and record it.
- **`deployment/cloudbuild.gcloudignore`:** allow `cloud_functions/creative_fanout/`.
- **Retention:**
  - Add `creative-fanout-crf` to `deployment/cleanup/ar-cloud-run-source-deploy.json` (both rules) and to `TT_PACKAGES` in `tests/test_cleanup_policies.py`.
  - The old packages keep their keep-10 rule and age out otherwise.
- **Extend tests** (an existing root-cloudbuild test if one exists, else `tests/test_crf_image.py`): `build-crf` waits only on `buildx-setup`, uses the buildcache, and prints its digest.
- **deployment/README.md §3:**
  - Replace both `--source` deploy commands with: build via `cloudbuild.yaml`, read the digest from `gcloud builds log`, then:
    ```bash
    gcloud run deploy creative-trawler-crf --image $CRF_IMAGE --update-env-vars FUNCTION_TARGET=crf_entrypoint --clear-base-image --region us-central1
    gcloud run deploy creative-worker-crf --image $CRF_IMAGE --update-env-vars FUNCTION_TARGET=agent_worker_entrypoint --clear-base-image --region us-central1
    ```
  - Note that no other flags are needed: `--image` keeps env, scaling, timeouts and triggers.
  - First-time creation keeps the full flag list, with `--image` in place of `--source --function --base-image`.
  - Mention the base-image trade-off.
  - Update the :540/:760 redeploy notes.
- **Real build:** run `gcloud builds submit --region us-central1 --config cloudbuild.yaml --ignore-file deployment/cloudbuild.gcloudignore --substitutions=SHORT_SHA=$(git rev-parse --short HEAD) .` and record the CRF step time (cold and warm) and the digest.
- Add the plan copy. Commit.

## PR E — one bandit image

**Task E1: merged image (TDD)**
- **Update the tests first:**
  - `tests/test_build_image.py` and `tests/test_bandit_cloudbuild.py`: one Dockerfile; the staged tree and `Dockerfile.dockerignore` also include `bandit_traffic/*.py`; requirements contain `google-cloud-bigquery` and `requests` pins; ENTRYPOINT still the model server; only **one** build step in `cloudbuild.bandit.yaml`, printing `BANDIT_IMAGE=…@sha256:…`.
  - `deploy_traffic_job.sh` passes `--command python --args=-m,bandit_traffic.main`, a `grep`-style test.
- **Implement:**
  - Merge `bandit_traffic/requirements.txt` into `bandit_serving/requirements.txt` (add the bigquery + requests pins; aiplatform stays `[prediction]==2.4.0`; fix the header comment).
  - `git rm bandit_traffic/Dockerfile bandit_traffic/requirements.txt`, after grepping for other users (Dependabot config, CI, docs) and updating them.
  - Dockerfile: also `COPY bandit_traffic/` (code layer only); ENTRYPOINT unchanged.
  - `build_image.py`: staging list adds the `bandit_traffic` modules.
  - `cloudbuild.bandit.yaml`: a single `build-bandit` step on `_BANDIT_IMAGE = …/cpr/trend-trawler-bandit`. Drop the traffic package; `trend-trawler-bandit-traffic` stops getting versions and ages out under the existing policy.
  - `deploy_traffic_job.sh`: `IMAGE=…/cpr/trend-trawler-bandit:${IMAGE_TAG}` (or `IMAGE` override) plus the command/args override; everything else unchanged.
- **Local checks:**
  - `uv run python deployment/bandit/build_image.py --local-test` (all CPR checks pass).
  - `docker run --rm --entrypoint python <img> -m bandit_traffic.main --in-process --dry-run --config <sample experiment.json mounted>`, as #355's smoke did, exits 0.
  - Record the size against 1.33 GB / 270 MB compressed.
- Commit.

**Task E2: Cloud Build + docs**
- Real build with `cloudbuild.bandit.yaml`; get the digest via `gcloud builds log`. Then pull it, run `build_image.py --image <digest> --local-test`, and run the traffic dry-run against the same digest.
- **Docs:**
  - `docs/bandit/README.md` build/deploy sections, `bandit_serving/README.md` and `deployment/README.md` bandit sections: one image, `BANDIT_SERVING_IMAGE=<digest>`, `IMAGE_TAG=<sha> deploy_traffic_job.sh`.
  - Update the contracts doc only if it names images; it shouldn't.
- Commit.

---

## Execution and deploy round

1. **t=0:** dispatch the D and E implementers in parallel. Merge each when CI is green and review is clean (any order).
2. **Deploy round** once both are merged (main = HEAD). Steps a and b start in parallel:
   - **a. CRF.** Root `cloudbuild.yaml` build (api/web are cache hits, so their digests are unchanged and need no deploy). Then:
     1. Deploy the **worker** first: `--image $CRF_IMAGE --update-env-vars FUNCTION_TARGET=agent_worker_entrypoint --clear-base-image --no-traffic --tag crfcheck`. Wait for Ready, check startup logs, and POST the invalid-event probe to the tagged URL (with an ID token): expect a handled error and no side effects.
     2. Route 100% to the new revision.
     3. Do the same for the **orchestrator**, with an extra no-op probe if `main.py` supports one (e.g. a message `max_rows: 0`, which can only lower the cap; confirm in the code that 0 dispatches nothing).
     4. Remove the `crfcheck` tags. Verify the env lists, scaling, timeouts and Eventarc triggers are unchanged (`describe` before/after diff).
   - **b. Bandit.** `cloudbuild.bandit.yaml` build, then:
     1. `build_image.py --image <digest> --local-test`.
     2. `IMAGE_TAG=<sha> deploy_traffic_job.sh`.
     3. api: deploy the **current** api digest with `--no-traffic --update-env-vars BANDIT_SERVING_IMAGE=<bandit digest>`, then pin after the idle check (no "run start" in the last 15 minutes). This is an env-only change.
   - **c.** Engines and web are untouched. Update the runbook memory (CRF revisions, image names) and MEMORY.md.

**Estimated total:** about 1–1.25 h (the parallel PRs) plus about 15 min for the deploy round.

## Verification
- **D:**
  - Unit tests pass; the local container serves both targets.
  - Both live services run the same `creative-fanout-crf@sha256:…` digest, with `FUNCTION_TARGET` set and env/scaling/triggers unchanged.
  - The probes are handled; the next real batch (or a hand-published orchestrator trigger with `max_rows` 1) runs end to end. That last step is optional and costs one creative run.
- **E:**
  - The local and Cloud-Build-image CPR tests pass; the traffic dry-run passes on the same digest.
  - The traffic job and the api's `BANDIT_SERVING_IMAGE` point at the same image package.
  - Optional: a short live experiment (2 creatives, `clear_winner`, about 25 min) exercises a real endpoint upload plus a traffic run on the merged image.
- **Retention:** re-run `deployment/cleanup/apply.sh` to preview, then `--apply`. This picks up the new package in the cloud-run-source-deploy policy; no live digest may be a deletion candidate.
