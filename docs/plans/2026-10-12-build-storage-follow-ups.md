# Build/Storage Follow-ups Implementation Plan (aiplatform 2.4 · cleanup policies · bandit images)

**Goal:** Close the three optional follow-ups with the shortest wall-clock time:
1. Move the root env from google-cloud-aiplatform 2.2.0 to 2.4.0.
2. Add retention to trend-trawler's own registry images and buckets.
3. Slim the bandit CPR image and build both bandit images on cached Cloud Build.

**Architecture:**
- **A** is a lockfile bump.
- **B** is committed policy files plus an apply script, applied right after merge.
- **C** replaces the SDK-generated CPR Dockerfile with a checked-in multi-stage Dockerfile, and builds it and the traffic image in one BuildKit Cloud Build config with a registry cache, modelled on the root `cloudbuild.yaml`.
- A and C converge in one deploy round: engines, the api (deps and the new `BANDIT_SERVING_IMAGE`), and the traffic job.

**Tech Stack:** uv, Artifact Registry cleanup policies, GCS lifecycle JSON, Docker BuildKit on Cloud Build, Vertex CPR model server, pytest.

---

## Context

**Why now:** these were left over from the Cloud Build migration (#343–#351). Exploration findings:

- **aiplatform:**
  - The root lock has never been bumped from 2.2.0. #349 and #247 only bumped `bandit_traffic/requirements.txt`, and nothing records why.
  - 2.4.0 (2026-10-07) has `requires_dist` **identical** to 2.2.0.
  - google-adk 2.10 caps aiplatform only in extras the project doesn't use.
  - The CRF (`google-cloud-agentplatform`) and `bandit_traffic` are already on 2.4.0.
- **Storage (user decision: trend-trawler only, never other projects in `hybrid-vertex`):**
  - No cleanup policy exists on `cloud-run-source-deploy` or `cpr`.
  - `trend-trawler-api` has 332 untagged versions and `trend-trawler-web` 187.
  - `gs://trend-trawler-deploy-ae` has **no lifecycle at all**, even though deployment/README.md:2116-2146 and CLAUDE.md claim a 365-day `person-refs/` rule.
  - `run-sources-hybrid-vertex-us-central1/services/trend-trawler-api/` holds 7.7 GB of source zips.
  - Must never expire:
    - `adk-pipe/` (the live engines' `packageSpec` points at `adk-pipe/<agent>/<ver>/staging/`)
    - `bandit/` (experiment configs and posterior checkpoints)
    - run folders (BigQuery rows, galleries and the revoke cascade reference them)
    - `shares/`
- **Bandit:**
  - The CPR image (281 MB compressed, ~1.4 GB) comes from `LocalModel.build_cpr_model`'s generated Dockerfile, which does `pip install --force-reinstall 'google-cloud-aiplatform[prediction]>=1.27.0'` (309 MB, unpinned).
  - It then runs `COPY . .` and `pip install --force-reinstall -r requirements.txt` (666 MB). Shared deps are stored twice, and any code change rebuilds both layers.
  - The traffic image (263 MB) already builds on Cloud Build (`deployment/bandit/cloudbuild.traffic.yaml`) but has no cache, no digest output and pip-only installs (904 MB layer).

**Constraints (every PR):**
- Worktree with `.python-version` copied (3.13), `unset VIRTUAL_ENV`.
- Gates: `uv run ruff format --check . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 8`.
- No Co-Authored-By or AI attribution.
- Merge only after CI passes; dispatch `python-ci.yml` for docs/JSON-only changes.
- Each PR edits only **its own** section of `deployment/README.md` (separate hunks merge cleanly).
- No CLAUDE.md edits except the one line PR B fixes.

## File ownership (disjoint)

| PR | Branch | Files |
|---|---|---|
| **A** | `chore/aiplatform-2.4` | `pyproject.toml`, `uv.lock`, `requirements.txt` |
| **B** | `ops/storage-retention` | `deployment/cleanup/**` (new), `tests/test_cleanup_policies.py` (new), `deployment/README.md` (lifecycle section ~2116-2153), CLAUDE.md line 319 wording, plan copy |
| **C** | `build/bandit-images-cloudbuild` | `bandit_serving/Dockerfile` (new), `bandit_serving/requirements.txt`, `bandit_traffic/Dockerfile`, `deployment/bandit/build_image.py`, `deployment/bandit/cloudbuild.bandit.yaml` (new, replaces `cloudbuild.traffic.yaml` via `git mv`), `deployment/bandit/bandit.gcloudignore` (`git mv` of `traffic.gcloudignore`), `tests/test_build_image.py`, `tests/test_bandit_cloudbuild.py` (new), `docs/bandit/README.md` build section, `bandit_serving/README.md`, `deployment/README.md` bandit build section (~1628-1639) |

**Critical path:** C (~1.5 h incl. a local CPR test), so start it first. A (~20 min) and B (~40 min) finish well inside that window.

---

## PR A — aiplatform 2.2.0 → 2.4.0

**Task A1:**
1. `uv lock --upgrade-package google-cloud-aiplatform` (expect 2.2.0 → 2.4.0 and nothing else; if other packages move, stop and report).
2. Raise both floors to `>=2.4.0,<3.0.0` (pyproject.toml lines 15 and 50).
3. `uv lock` again, then `uv export --format requirements-txt --no-hashes --no-dev -o requirements.txt`.
4. Run the full gates. These tests matter most:
   - `test_sdk_versions.py` (runtimes/sessions/AdkApp/vertexai compat)
   - `test_dependency_groups.py`, `test_deploy_utils.py`, `test_create_session_engine.py`
   - `test_build_image.py`, `test_bandit_endpoint_lib.py`
5. Check for new deprecation warnings: `pytest -W error::DeprecationWarning tests/test_sdk_versions.py`. If 2.4 adds a warning that pytest's filters turn into errors, add a narrow filter next to `pyproject.toml:100` with a comment.

Commit: `chore(deps): google-cloud-aiplatform 2.4.0 (root lock; matches the CRF and traffic job)`.

**Note:** A changes `requirements.txt`, which ships to all three engines and the api image, so it needs the deploy round below.

## PR B — trend-trawler storage retention

**Task B1: policy files and a validating test (TDD)**

Write `tests/test_cleanup_policies.py` first. It loads every JSON in `deployment/cleanup/` and asserts:
- Every Artifact Registry `Delete` rule has `condition.packageNamePrefixes` that are a subset of `TT_PACKAGES = {"trend-trawler-api", "trend-trawler-web", "trend-trawler-share", "creative-trawler-crf", "creative-worker-crf", "trend-trawler-bandit"}`. Never empty: an empty filter would hit omnimash, nova and janus.
- Every `Delete` rule uses `tagState: UNTAGGED` plus `olderThan` ≥ 14 days.
- A `Keep` `mostRecentVersions` rule (≥ 10 versions) covers the same packages.
- Every bucket lifecycle rule has `matchesPrefix`, all from an allowlist.
- No rule's prefix starts with `adk-pipe/`, `bandit/`, `shares/` or a run-folder pattern (`^\d{4}_`).
- `person-refs/` has `age: 365`.

Then create the files:

- `deployment/cleanup/ar-cloud-run-source-deploy.json`:
  ```json
  [
    {"name": "tt-delete-untagged", "action": {"type": "Delete"},
     "condition": {"tagState": "untagged", "olderThan": "14d",
                   "packageNamePrefixes": ["trend-trawler-api", "trend-trawler-web", "trend-trawler-share",
                                           "creative-trawler-crf", "creative-worker-crf"]}},
    {"name": "tt-keep-recent", "action": {"type": "Keep"},
     "mostRecentVersions": {"keepCount": 10,
                            "packageNamePrefixes": ["trend-trawler-api", "trend-trawler-web", "trend-trawler-share",
                                                    "creative-trawler-crf", "creative-worker-crf"]}}
  ]
  ```
  - **Why this is safe:** tagged images (`latest`, `<sha>`, `buildcache`) are never deleted by an untagged rule.
  - The 10 newest versions are kept whatever their age, so the live CRF buildpack digests (always the newest) and the web `prev` rollback revision survive.
  - The api's live revision is pinned by a tagged digest.
- `deployment/cleanup/ar-cpr.json`: the same shape for `["trend-trawler-bandit"]`. It is a prefix, so it also covers `-traffic`. Use `keepCount: 5`.
- `deployment/cleanup/gcs-trend-trawler-deploy-ae.json`:
  ```json
  {"rule": [
    {"action": {"type": "Delete"}, "condition": {"age": 365, "matchesPrefix": ["person-refs/"]}},
    {"action": {"type": "Delete"}, "condition": {"age": 90, "matchesPrefix": ["adk-pipe/cloudbuild/"]}}
  ]}
  ```
  `adk-pipe/cloudbuild/` holds only the handoff `engine_ids.json`.
- `deployment/cleanup/gcs-run-sources.json`: `age: 30` for `matchesPrefix` `services/trend-trawler-api/`, `services/trend-trawler-web/`, `services/trend-trawler-share/`, `services/creative-trawler-crf/`, `services/creative-worker-crf/`. These are source zips for `--source` deploys and aren't needed after the build.

Commit `ops: trend-trawler-only retention policies (registry + buckets)`.

**Task B2: `deployment/cleanup/apply.sh`**

`set -euo pipefail`, `--dry-run` (default) or `--apply`:
- **Registry:** `gcloud artifacts repositories set-cleanup-policies <repo> --location us-central1 --policy <file> --dry-run|--no-dry-run`.
- **Before setting registry policies:** abort if the repo already has policies that don't match the file (`describe --format=json(cleanupPolicies)`). The repo is shared, so never clobber another team's rules.
- **Before setting bucket lifecycle:** abort if the bucket already has a lifecycle that differs from the file, because `--lifecycle-file` replaces the whole config. If it does, print a diff and require a merged file.
- **Preview mode:**
  - Print the untagged versions older than 14 days that fall outside the newest 10, per package: `gcloud artifacts docker images list <repo>/<pkg> --include-tags --format=json`, then python3 to apply the same rule.
  - Print the object counts and bytes under each lifecycle prefix older than its age.
  - This shows the effect immediately; Artifact Registry's own dry-run only logs after its daily run.

**Task B3: docs**
- deployment/README.md lifecycle section: replace "run this" with `deployment/cleanup/apply.sh`, list each rule and why the protected prefixes are excluded, and add a registry retention subsection.
- CLAUDE.md:319: "365-day lifecycle backstop" becomes "(`deployment/cleanup/`)".
- Add the plan copy.

**Ops, right after B merges (not blocked by A/C):**
1. `deployment/cleanup/apply.sh --dry-run`. Review the preview, and confirm no live digest is listed: api/web/share/CRF live digests from `gcloud run services describe … --format='value(spec.template.spec.containers[0].image)'`, traffic job and `BANDIT_SERVING_IMAGE`.
2. `deployment/cleanup/apply.sh --apply`. Registry policies start with `--no-dry-run`, since the preview already showed the effect.
3. After the next daily run, verify the version counts dropped and the services still resolve their images.

## PR C — bandit images: checked-in Dockerfiles + cached Cloud Build

**Task C1: CPR Dockerfile (TDD on the staged tree)**

Update `tests/test_build_image.py` first:
- The staged tree now contains `Dockerfile`.
- A new test asserts `bandit_serving/Dockerfile`:
  - has exactly one dependency-install `RUN`, before `COPY bandit_serving`
  - pins aiplatform through `requirements.txt`
  - sets `PREDICTOR_MODULE=bandit_serving.predictor`, `PREDICTOR_CLASS=BanditPredictor`, `HANDLER_MODULE=google.cloud.aiplatform.prediction.handler` and `HANDLER_CLASS=PredictionHandler`
  - has `ENTRYPOINT ["python", "-m", "google.cloud.aiplatform.prediction.model_server"]`
  - has `EXPOSE 8080` and a non-root `USER`

Then:
- `bandit_serving/requirements.txt`: add `google-cloud-aiplatform[prediction]==2.4.0` (the explicit pin replaces the SDK's unpinned `>=1.27.0` and matches PR A and the traffic job). Keep the protobuf pin.
- `bandit_serving/Dockerfile`, multi-stage:
  - **Builder stage** on the same pinned uv image digest `deployment/cloudbuild.agents.yaml` uses (`_UV_IMAGE`):
    ```
    uv venv /opt/venv --python 3.13
    uv pip install --python /opt/venv --no-cache --compile-bytecode -r requirements.txt
    ```
    Install requirements only, so the layer is cached until requirements change.
  - **Final stage** on `python:3.13-slim`:
    - `COPY --from=builder /opt/venv /opt/venv`
    - `ENV PATH=/opt/venv/bin:$PATH HOME=/home/app` plus the four handler/predictor env vars
    - a non-root user (uid 10001, as `bandit_traffic/Dockerfile` does)
    - `COPY bandit/ … bandit_serving/` last, limited to the files `build_image.py`'s staging list names
    - `EXPOSE 8080` and the `ENTRYPOINT` above
  - Single install means no duplicated packages. If `uv pip install` warns on the `[prediction]` extra's `docker` dependency, keep it: the model server imports only fastapi/uvicorn, but dropping extras is a later optimisation, not this PR.
- `deployment/bandit/build_image.py`:
  - `stage_src_dir` also copies the Dockerfile.
  - `build()` runs `docker build -f Dockerfile -t <uri> <staged>` (honouring `--no-cache` and `--base-image` as a `--build-arg BASE_IMAGE`) instead of `LocalModel.build_cpr_model`.
  - Add `--image <uri>`, which skips the build and runs `--local-test` against a prebuilt image (for example a Cloud Build digest after `docker pull`).
  - The local test wraps the image in `LocalModel(serving_container_spec=…ModelContainerSpec(image_uri=uri, predict_route=…, health_route=…, ports=[8080], env=SERVING_ENV))`, matching what `build_cpr_model` returned. Keep every existing local-test assertion.
- **Run locally:**
  1. `uv run python deployment/bandit/build_image.py --local-test` must pass all checks: single worker, reset/decision/reward/duplicate/second batch/state/mixed batch, and no checkpoint-write failure.
  2. Record `image_size_mb` before (1.39 GB) and after in the PR body, plus the compressed size after push in C3.

Commit `build(bandit): checked-in multi-stage CPR Dockerfile (single pinned install)`.

**Task C2: traffic Dockerfile on the same pattern**
- Same builder/final split (uv venv in the builder, slim final, existing non-root `app` user, deps before code). Keep `bandit_traffic/requirements.txt` unchanged.
- Test: extend `tests/test_bandit_cloudbuild.py` (new) to assert that both Dockerfiles install deps before copying code.
- Smoke: `docker build -f bandit_traffic/Dockerfile .`, then `docker run --rm <img> python -m bandit_traffic.main --in-process --dry-run`.

Commit.

**Task C3: `deployment/bandit/cloudbuild.bandit.yaml`**

`git mv cloudbuild.traffic.yaml cloudbuild.bandit.yaml` and `git mv traffic.gcloudignore bandit.gcloudignore`. Add `/bandit_serving/` to the ignore file's allowlist.

Copy the root `cloudbuild.yaml` structure:
- the `buildx-setup` step
- two parallel `docker buildx build` steps (CPR with context `.`, `-f bandit_serving/Dockerfile`; traffic with `-f bandit_traffic/Dockerfile`)
- tags `${_CPR_IMAGE}:${SHORT_SHA}` / `${_TRAFFIC_IMAGE}:${SHORT_SHA}` plus `latest`
- `--cache-from/--cache-to type=registry,ref=…:buildcache,mode=max,image-manifest=true,oci-mediatypes=true --provenance=false --push --metadata-file`
- the same `sed` digest extraction, printing `CPR_IMAGE=…@sha256:…` and `TRAFFIC_IMAGE=…@sha256:…`
- `_CPR_IMAGE` / `_TRAFFIC_IMAGE` defaults under `us-central1-docker.pkg.dev/$PROJECT_ID/cpr/`
- `logging: CLOUD_LOGGING_ONLY`, default machine

Tests (`tests/test_bandit_cloudbuild.py`, `yaml.safe_load`):
- both build steps wait only on `buildx-setup`
- each has `--cache-to` with `buildcache`
- each prints its digest line
- the ignore file includes `bandit_serving` and excludes `.env`/`frontend`

Run a real build: `gcloud builds submit --region us-central1 --config deployment/bandit/cloudbuild.bandit.yaml --ignore-file deployment/bandit/bandit.gcloudignore --substitutions=SHORT_SHA=$(git rev-parse --short HEAD) .`, then read the digests with `gcloud builds log <ID> --region us-central1 | grep -aoE '(CPR|TRAFFIC)_IMAGE=[^ ]+'`. Do this twice: the second, no-change build measures the warm-cache time.

Pull the CPR digest and run `build_image.py --image <digest> --local-test`. This verifies the image Cloud Build produced, not just the local one.

Commit.

**Task C4: docs**
- Replace the CPR and traffic build commands (`docs/bandit/README.md:512-529`, `deployment/README.md:1628-1639`, `bandit_serving/README.md`) with the Cloud Build command, the digest retrieval, `build_image.py --image <digest> --local-test`, setting the api's `BANDIT_SERVING_IMAGE` to the **digest**, and `IMAGE_TAG=<sha> deploy_traffic_job.sh`.
- `build_image.py --local-test` without `--image` stays as the local dev path.

Commit.

---

## Execution and merge order

1. **t=0:** dispatch the A, B and C implementers in parallel; C starts first.
2. **Merge each PR as soon as its CI passes and its review is clean.** Expected order: A → B → C, any order is safe.
3. **B ops:** right after B merges, run `apply.sh --dry-run`, then `--apply`. This runs in parallel with C's remaining work.
4. **Shared deploy round** once A and C are merged (`main` = HEAD). Steps a–c start in parallel:
   - **a.** Engines, expedited: **creative first, alone** (`"--substitutions=^;^_REF=<full sha>;_AGENTS=creative_agent;_CREATIVE_VERSION=v41"`, ~3 min), and as soon as it lands, `engine_handoff.py apply` + the creative smoke test (~9–10 min, the critical path). Submit the **interactive + scout** build (`_AGENTS=interactive_creative,trend_scout;_INTERACTIVE_VERSION=v37;_SCOUT_VERSION=v20`) at the same time as the creative one, so it runs while the smoke does; its handoff + health check follow.
   - **b.** api+web images via the root `cloudbuild.yaml`. Web is a cache hit.
   - **c.** Bandit images via `cloudbuild.bandit.yaml`. Then:
     - `build_image.py --image <CPR digest> --local-test`
     - `IMAGE_TAG=<sha> BQ_DATASET_ID=trend_trawler deployment/bandit/deploy_traffic_job.sh`
   - **d.** After the creative smoke test (`integration_test.py --check smoke --agent creative_agent`) passes and the interactive + scout handoff and health check pass, delete the superseded engines.
   - **e.** Deploy the api digest with `--no-traffic --update-env-vars BANDIT_SERVING_IMAGE=<CPR digest>`. Pin it only after an idle check (no "run start" in the last 15 minutes). Web stays on its current revision unless its digest changed. One api revision change covers both A and C.
   - **f.** Update the runbook memory and MEMORY.md.

**Estimated total:** about 1.5–2 h (C's critical path), plus about 20 min for the deploy round.

## Verification

- **A:** `uv tree --package google-cloud-aiplatform` shows 2.4.0; the full suite passes; engine smoke 5/5 and health 3/3; the api's startup logs are clean; `test_deployment.py --agent=trend_scout` works.
- **B:**
  - The preview lists only trend-trawler packages and prefixes.
  - `describe` shows the policies and the lifecycle on both buckets.
  - The next day the untagged counts are down and every live digest still resolves (`gcloud artifacts docker images describe <digest>`).
  - `adk-pipe/`, `bandit/`, `shares/` and the run folders are untouched.
- **C:**
  - Local and Cloud-Build image local tests pass.
  - The size before/after is recorded; the target is roughly half of 1.39 GB uncompressed, with no duplicate packages (`docker history` shows one dependency layer).
  - A warm-cache Cloud Build is under 60 s.
  - The traffic job runs: execute it once with `--in-process --dry-run`-equivalent env, or launch a short live experiment from the results-page Deploy panel (2 creatives, `clear_winner`). That exercises a real endpoint upload with the new `BANDIT_SERVING_IMAGE` and the new traffic image. Optional but recommended, about 25 min, and it can run after the round.
