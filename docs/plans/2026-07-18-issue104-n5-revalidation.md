# Issue #104 N=5 Concurrency Re-Validation

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to run this runbook task-by-task.

**Goal:** Empirically confirm the merged #104 fixes drive the `creative_agent` N=5 concurrent-run failure rate from the original **15% (6/40, regional_25 2/20)** down to ~0, by re-running the DoE harness at N=5 against a fresh fixed-code revision and logging the result into the existing `quota-bucket-spread-doe` Vertex experiment as a distinct cohort.

**Approach:** No new code — the harness in `experiments/quota_spread/` already exists and is tested. This is an **ops runbook**: deploy a tagged `--no-traffic` revision from `main` (which carries the fixes), point a one-arm arm-map at it, smoke-test auth+endpoint, launch the 20-run N=5 batch **in the background**, then analyze + upload + tear down. Runs concurrently with other work.

**Tech Stack:** `gcloud run` (Cloud Run tagged revisions), the `experiments.quota_spread` harness (`run_doe`/`run_batch`/`analyze`/`upload_to_vertex`), impersonated ID-token auth, Vertex AI Experiments, `uv`.

---

## Context (why)

Issue #104 (now CLOSED as fixed) documented ~15% run failures under N=5 inter-run concurrency across three classes (artifact-export race, visual-JSON crash, unretried 503). All three fixes are merged on `main` @ `4c94276` and live on prod (`trend-trawler-api-00041-8k7`), and a subagent verified they're present + complete. **But the validating experiment was never re-run**, so the 15%→0 improvement is asserted by construction, not measured. This runbook closes that gap: it re-measures the exact metric the original DoE produced (`error_rate_by_cell` at N=5) on fixed code, using the same harness so the numbers are directly comparable, and drops the cohort into the same Vertex experiment for a side-by-side.

**Scope decision (user):** lean **regional_25-only, N=5, reps=4 → 20 runs (~1–1.5h)**, against a **fresh tagged `--no-traffic` revision** (not live prod), in a **clean co-tenant window** (novastorm + simulator paused). regional_25 is the prod default config; its original N=5 rate was 2/20 (10%).

**Honest caveats baked in:**
- A tagged rev isolates *traffic* + gives a clean `revision` label, but **inherits prod `BQ_DATASET_ID`/GCS env** → the ~20 runs still write rows to `trend_creatives`/`creative_evals` + artifacts to GCS (same as the original DoE). Accepted (no live users); optional post-hoc cleanup query in Teardown.
- N=5 batch tail inflates under contention (research p90 hit ~839s originally); wall-clock is gated by the slowest of each 5.

---

## Pre-flight facts (already confirmed this session)

- `main` @ `4c94276`, working tree clean; `gcloud` = `admin@jordantotten.altostrat.com` / project `hybrid-vertex`.
- api serving: `trend-trawler-api-00041-8k7` @100%; `main-clean` tag → `00037-5zg` @0%.
- The api service template env has **no `CAMPAIGN_RESEARCH_PLACEMENT`** → a fresh `--source` deploy inherits clean **regional_25** config (no `--remove-env-vars` needed; verify anyway).
- Existing `arms.json` is **stale + pre-fix** (points at deleted `00068-muz` / untagged pre-fix `00069-bik`) → do NOT reuse it; build a new one-arm map.
- Harness confirmed: `run_doe` fans `run_batch` per (arm,N,rep) cell; `write_batch_records` → `results/<arm>/N<k>/<batch_id>/`; `load_records` = `rglob("*.json")` from a **configurable** root (skips `manifest.json`/`summary.json`); `mint_token` impersonates `$EXP_INVOKER_SA` with audience = BASE URL; `upload_to_vertex` logs `revision` as a param and takes `--results-root`.

Constants: `EXP_INVOKER_SA=tt-web-sa@hybrid-vertex.iam.gserviceaccount.com`; BASE (audience) URL `https://trend-trawler-api-qqzji3hyoa-uc.a.run.app`; api SA `tt-api-sa@hybrid-vertex.iam.gserviceaccount.com`; new tag `exp-fixed-reg`; arm name `regional_25_fixed`; always `export PATH="$HOME/.local/bin:$PATH"` first.

---

## Task 0 — Persist this runbook to the repo

Copy this plan to `docs/plans/2026-07-18-issue104-n5-revalidation.md` (repo convention for plans). No commit unless asked.

---

## Task 1 — Deploy a fixed-code tagged `--no-traffic` revision

**Command** (from a clean `main` checkout; pass NO env flag → preserves all prod env incl. `SESSION_SERVICE_URI`):
```bash
export PATH="$HOME/.local/bin:$PATH"; cd /home/user/adk_pipe
gcloud run deploy trend-trawler-api --source . --region us-central1 \
  --tag exp-fixed-reg --no-traffic \
  --service-account tt-api-sa@hybrid-vertex.iam.gserviceaccount.com \
  --no-cpu-throttling --min-instances 1 --memory 8Gi --cpu 4 --timeout 900 \
  --no-allow-unauthenticated
```
**Verify:**
- New revision `Ready`; capture its name (e.g. `trend-trawler-api-000NN-xxx`) — `gcloud run revisions list --sort-by="~metadata.creationTimestamp" --limit 3`.
- **Prod untouched:** `describe --format='value(status.traffic)'` still shows `00041-8k7` @100% (and `main-clean` tag intact). The `--no-traffic` rev is at 0%.
- **Env clean:** the tagged rev has no `CAMPAIGN_RESEARCH_PLACEMENT` and retains `SESSION_SERVICE_URI` + `BQ_DATASET_ID=trend_trawler`. If `CAMPAIGN_RESEARCH_PLACEMENT` somehow present, redeploy with `--remove-env-vars CAMPAIGN_RESEARCH_PLACEMENT`.
- Tag URL up: `curl -s -o /dev/null -w '%{http_code}' https://exp-fixed-reg---trend-trawler-api-qqzji3hyoa-uc.a.run.app/list-apps` → `403` (private + up).

---

## Task 2 — Write the one-arm arm-map

Create `/tmp/arms_fixed.json` (scratch, not committed), filling `<NEW_REV>` from Task 1:
```json
{
  "regional_25_fixed": {
    "base_url": "https://exp-fixed-reg---trend-trawler-api-qqzji3hyoa-uc.a.run.app",
    "audience": "https://trend-trawler-api-qqzji3hyoa-uc.a.run.app",
    "revision": "<NEW_REV>",
    "tag": "exp-fixed-reg"
  }
}
```
Distinct arm name `regional_25_fixed` → results land in a separate `results/regional_25_fixed/` subtree (old cohort untouched) and the Vertex `arm` param cleanly separates cohorts.

---

## Task 3 — Auth + single-run smoke (de-risk the 90-min batch)

```bash
export PATH="$HOME/.local/bin:$PATH"; cd /home/user/adk_pipe
export EXP_INVOKER_SA=tt-web-sa@hybrid-vertex.iam.gserviceaccount.com
# a) token mint works (impersonation granted):
gcloud auth print-identity-token \
  --audiences=https://trend-trawler-api-qqzji3hyoa-uc.a.run.app \
  --impersonate-service-account=$EXP_INVOKER_SA --include-email >/dev/null && echo OK
# b) one real run end-to-end (~6 min): create_session -> start_run -> poll_to_terminal
PYTHONPATH=$PWD uv run --no-sync python -m experiments.quota_spread.run_batch \
  --base-url https://exp-fixed-reg---trend-trawler-api-qqzji3hyoa-uc.a.run.app \
  --audience https://trend-trawler-api-qqzji3hyoa-uc.a.run.app \
  --arm regional_25_fixed --concurrency 1 --batch-id smoke0 --revision <NEW_REV>
```
**Expected:** run status `done` (not `error`); a record under `results/regional_25_fixed/N1/smoke0/`. This N=1 fixed point is harmless to keep. If it errors, stop and diagnose before the batch.

---

## Task 4 — Launch the 20-run N=5 batch in the BACKGROUND

```bash
export PATH="$HOME/.local/bin:$PATH"; cd /home/user/adk_pipe
mkdir -p /tmp/doe_logs
EXP_INVOKER_SA=tt-web-sa@hybrid-vertex.iam.gserviceaccount.com PYTHONPATH=$PWD \
  uv run --no-sync python -m experiments.quota_spread.run_doe \
  --arm-map /tmp/arms_fixed.json --loads 5 --reps 4 --cool-secs 120 \
  > /tmp/doe_logs/revalidate.log 2>&1
```
Run via the Bash tool with `run_in_background: true` (detached; I get notified on completion and keep working meanwhile). `--loads 5 --reps 4` = 4 cells × N=5 = **20 runs**; 3× 120s cools; ~1–1.5h. Records → `results/regional_25_fixed/N5/regional_25_fixed_N5_r0..3/`.

**Monitor** (poll between other work): `tail -n 30 /tmp/doe_logs/revalidate.log`; count `run done status=done` vs `status=error`.

---

## Task 5 — Analyze (the headline number)

```bash
export PATH="$HOME/.local/bin:$PATH"; cd /home/user/adk_pipe
PYTHONPATH=$PWD uv run --no-sync python - <<'PY'
from experiments.quota_spread import analyze
recs = analyze.load_records("experiments/quota_spread/results/regional_25_fixed")
from collections import Counter
print("status:", Counter(r.get("status") for r in recs))
print("error_rate_by_cell:", analyze.error_rate_by_cell(recs))
print("N5 records:", sum(1 for r in recs if r.get("concurrency") == 5))
PY
```
**Success criterion:** N=5 `error_rate_by_cell` ≈ **0/20** (vs original regional_25 2/20). Inspect any `status=error` record's `error` field; classify whether it's one of the three fixed classes (regression — bad) or new/transient infra (e.g. isolated 503). Also sanity-check quality didn't regress (`eval_pass`/`eval_mean` in records).

---

## Task 6 — Upload the fixed cohort to Vertex (same experiment)

```bash
export PATH="$HOME/.local/bin:$PATH"; cd /home/user/adk_pipe
# dry-run first (offline shaping check):
PYTHONPATH=$PWD GOOGLE_CLOUD_PROJECT=hybrid-vertex uv run --no-sync \
  python -m experiments.quota_spread.upload_to_vertex \
  --results-root experiments/quota_spread/results/regional_25_fixed \
  --experiment quota-bucket-spread-doe --location us-central1 --dry-run
# then live (drop --dry-run)
```
`--results-root .../regional_25_fixed` uploads **only** the new cohort → run names `regional-25-fixed-n5-…`, `arm=regional_25_fixed` + `revision=<NEW_REV>` params → filterable against the original 48 in the `quota-bucket-spread-doe` console view. Idempotent (create-then-resume).

---

## Task 7 — Teardown (prod safety is paramount)

```bash
export PATH="$HOME/.local/bin:$PATH"
# remove the experiment tag; prod traffic split is NOT changed by this:
gcloud run services update-traffic trend-trawler-api --region us-central1 \
  --remove-tags exp-fixed-reg
# verify prod still 100% on 00041-8k7:
gcloud run services describe trend-trawler-api --region us-central1 \
  --format='value(status.traffic)'
```
- **Do NOT** run `--to-latest` (would route prod traffic onto the experiment rev). Prod stays pinned to `00041-8k7`.
- The tagged rev is now LATEST → Cloud Run **forbids deleting the latest revision**; leave it untagged @0% (harmless; superseded by the next real prod deploy). Attempt `gcloud run revisions delete <NEW_REV>` only if it's not latest.
- **Optional BQ/GCS cleanup** of the ~21 experiment runs (by time window of this session), if you want prod data pristine — draft, review before running:
  ```sql
  -- inspect first; rows carry per-run creative_uuid/session ids created during the batch window
  SELECT * FROM `hybrid-vertex.trend_trawler.creative_evals`
  WHERE <created_at within batch window>;
  ```

---

## Task 8 — Close-out

- Post a follow-up comment on (closed) issue **#104** with the empirical result: original regional_25 N=5 = 2/20 → fixed = `<X>/20`, linked to the Vertex cohort. (Outward-facing — only if asked.)
- Update memory `creative-agent-n5-concurrency-fix` (drop the "no N=5 re-run" caveat; record the measured rate + cohort revision).
- Remind: co-tenant jobs (novastorm + simulator) can resume.

---

## Verification (how we know it worked)

| Check | Where | Pass |
|---|---|---|
| Fixed code deployed, prod untouched | Task 1 | new rev `Ready` @0%; `00041-8k7` still @100%; env clean |
| Auth + endpoint healthy | Task 3 | token mints; 1-run smoke `done` |
| Failure rate collapsed | Task 5 | N=5 `error_rate_by_cell` ≈ 0/20 (vs 2/20) |
| Cohort logged, comparable | Task 6 | 20 runs in `quota-bucket-spread-doe`, filterable by `revision` |
| Prod safe after | Task 7 | traffic still `00041-8k7` @100%; tag removed |

**Standing rules:** branch off `main` (n/a — no code change); commit/push/PR/deploy/comment only when asked; the Task 1 deploy is user-authorized (tagged `--no-traffic`); no `Co-Authored-By`; no Claude attribution.
