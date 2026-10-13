# Deployment Required Fixes Implementation Plan (5 parallel PRs)

**Goal:** fix the six Required findings in `/home/user/adk_pipe/code_review.md` (the deployment module review) with minimal wall-clock time.

**Architecture:** five small PRs on disjoint files, all built in parallel. Every fix is a test, a script or a doc change. None changes live runtime behaviour, so **no deploy round is needed**: the `ENV_VAR_DICT` change takes effect at the next routine engine deploy, with identical values.

**Tech Stack:** Python 3.13 / pytest (xdist), bash, Cloud Build YAML, uv.

---

## Context

The five-axis review of `deployment/` found no Critical issues and six Required ones. All six were re-verified against the source; R4 was also reproduced in bash.

| # | Finding | Root cause (verified) |
|---|---|---|
| R1 | Smoke test can pass falsely | `integration_test.py:578` checks `k not in state`, but callbacks pre-seed every campaign key with `""` (`creative_agent/callbacks.py:87`). The run step also passes with 0 events. |
| R2 | `deploy.env` knobs silently ignored in the engines | Module globals are re-evaluated in the engine from the engine's env (cloudpickle pickles functions by reference). `IMAGE_SIZE` (`agent_common/config.py:139`), `MODEL_REQUEST_TIMEOUT_SECONDS`, `EVAL_MODEL`, `EVAL_MAX_WORKERS` and `IMAGE_ASPECT_RATIO_DEFAULT` are in `deploy.env` but not in `deploy_agent.py` `ENV_VAR_DICT` (`grep -c` = 0). |
| R3 | README tells readers to deploy a public agent | The `deployment/README.md:2618-2697` appendix: `cp .env …`, `chmod -R 777`, `adk deploy cloud_run --with_ui`, and answer `y` to "Allow unauthenticated invocations". It is linked from the TOC at `:22`. |
| R4 | Retention live-digest guard can fail open | `deployment/cleanup/apply.sh:60-76` uses `for rev in $(gcloud …)` and `echo "$(gcloud …)"`, which `set -euo pipefail` doesn't catch. Mutations to `retention.py`'s `LIVE_DIGEST_EXIT` path (`:200-215`) survived. |
| R5 | api authz wiring has no tests | No test imports `deployment.async_app`. Deleting `app.add_middleware(UserAuthzMiddleware, …)` (`:210`) plus the enforce boot guard (`:118-126`) passed all 3,888 tests. |
| R6 | `_DEPLOY` traffic/IAP safety has no tests | `tests/test_creative_fanout_image.py:138-152` asserts `--no-traffic` only for the two CRF services. Removing it from api/web and adding `--allow-unauthenticated` to web passed. |

**Constraints (every PR):**
- Worktree with `.python-version` copied (3.13), `unset VIRTUAL_ENV`, `uv sync`.
- Gates: `uv run ruff format --check . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 8`.
- TDD: failing test first, plus mutation checks on the guarded logic, restored with `git diff --exit-code`.
- No Co-Authored-By or AI attribution. Push and open a PR; I merge after CI is green. Dispatch `python-ci.yml` for shell/YAML/Markdown-only diffs, which are path-gated.
- No cloud mutations.

## File ownership (disjoint, so all five run in parallel)

| PR | Branch | Findings | Files |
|---|---|---|---|
| **Q** | `fix/smoke-assertions` | R1 (+ C7 image check) | `deployment/integration_test.py`, `tests/test_deploy_utils.py` (or a new `tests/test_integration_smoke.py` if cleaner) |
| **R** | `fix/engine-runtime-knobs` | R2 | `deployment/deploy_agent.py` (`ENV_VAR_DICT` only), `deployment/deploy.env` (comments/grouping), new `tests/test_deploy_env_contract.py` |
| **S** | `fix/retention-guard` | R4 | `deployment/cleanup/apply.sh`, `deployment/cleanup/retention.py`, `tests/test_cleanup_policies.py`, new `tests/test_cleanup_apply_sh.py` |
| **T** | `test/async-app-wiring` | R5 (+ C5 warning/docstring) | new `tests/test_async_app_wiring.py`, `deployment/async_app.py` (fallback warning + docstring only) |
| **U** | `fix/deploy-guard-docs` | R3 + R6 | `deployment/README.md` (delete appendix + TOC line), `tests/test_creative_fanout_image.py` (deploy-step assertions), plan copy |

---

## PR Q — R1: smoke tests that can actually fail

**Fix**
1. **Check values, not keys.** In `check_smoke`, replace `missing_keys = [k for k in expected_keys if k not in state]` with a check that each value is set and non-empty. Reuse the existing set/empty helper if the module has one; otherwise add a small `_is_set(v)` that treats `None`, `""`, `[]` and `{}` as unset.
2. **Per-agent output keys.** Add an `AGENT_OUTPUT_KEYS` map next to `CREATIVE_OUTPUT_KEYS`:
   - `trend_scout` → `selected_gtrends`;
   - `interactive_creative` → the outputs present when the smoke stops at checkpoint 1: `creative_brief` and `research_report_gcs_uri`. Confirm by reading what the interactive smoke awaits.

   Missing or empty outputs fail the smoke with the key names listed.
3. **The run step fails on 0 events.**
4. **The creative smoke requires renders (C7):** `generated_images` must be non-empty, with at least one entry carrying a `gcs_uri`, next to the existing `CREATIVE_OUTPUT_KEYS` check in `check_creative_outputs`.

**Tests (first, failing)**
- The state check fails for a state whose campaign keys are all `""`, and passes when they hold values.
- `trend_scout` fails without `selected_gtrends` and passes with a non-empty list.
- The creative outputs fail with an empty `generated_images` or no `gcs_uri`.
- The run step fails with 0 events.

Mutation: force `missing = []`, which must fail a test.

**Expected impact:** `--check smoke` / `--check all` and the Cloud Build `_SMOKE=true` step now fail when an agent produced nothing. Today they report green. Live behaviour is unchanged.

## PR R — R2: every knob the engine reads at runtime is shipped

**Fix**
1. **`deploy_agent.py` `ENV_VAR_DICT`:** add `MODEL_REQUEST_TIMEOUT_SECONDS`, `IMAGE_SIZE`, `IMAGE_ASPECT_RATIO_DEFAULT`, `EVAL_MODEL`, `EVAL_MODEL_LOCATION`, `EVAL_MAX_WORKERS`, `EVAL_MAX_VISUAL_WAVE_WORKERS`, `CRITIC_FALLBACK_MODEL` and `CAMPAIGN_RESEARCH_PLACEMENT`. Follow the existing entry style, and let the defaults mirror the config parsers.
   - **Unset vs empty:** keep `CRITIC_FALLBACK_MODEL=""` meaningful (it is the kill switch). Ship it only when it is set, so an unset value doesn't ship as `""`. Check how `ENV_VAR_DICT` handles None today and keep that behaviour.
2. **Audit the rest:** for every other key in `deploy.env`, decide "runtime-read → ship" or "deploy input only" (project, region, location, bucket and the other reserved ones). Grep how each is read.
3. **`deploy.env` comments:** say which knobs are captured at deploy time and which are read at runtime and shipped via `ENV_VAR_DICT`.

**Tests (first, failing)** in the new `tests/test_deploy_env_contract.py`:
- Parse `deployment/deploy.env` with the parser the existing profile test uses.
- Assert every key is either in `ENV_VAR_DICT` or in an explicit, commented `DEPLOY_INPUT_ONLY` allowlist.
- Assert the five named knobs are shipped.
- Assert shipped default values equal the config defaults, for at least `IMAGE_SIZE` and `MODEL_REQUEST_TIMEOUT_SECONDS`.

Mutation: drop `IMAGE_SIZE` from `ENV_VAR_DICT`, which must fail.

**Expected impact:** editing one of these knobs in `deploy.env` now changes the engines, as the file implies. The next routine engine deploy ships the same values as today, so behaviour is unchanged and no redeploy is needed now.

## PR S — R4: retention guard fails closed

**Fix**
1. **`apply.sh` `live_digests`:** capture every gcloud call into a variable before using it, e.g. `revs=$(gcloud …)`, which *does* abort under `set -e`, then iterate over `$revs`. Do the same for each revision digest and for the job image. After collection, validate:
   - every digest matches `^sha256:[0-9a-f]{64}$`;
   - each of the 5 services produced at least one revision;
   - the job image resolved.

   Otherwise print what failed and `exit 5` (a new, documented exit code), with `--apply` refusing to continue.
2. **`apply.sh:116` bucket listing:** tell "matched no objects" apart from a real failure instead of `2>/dev/null || true`.
3. **`retention.py`:** read the live-digest file with the same digest validation, so a malformed or empty token is a hard error, not silently ignored. Keep `LIVE_DIGEST_EXIT` behaviour.

**Tests (first, failing)**
- **`test_cleanup_policies.py`:** `ar-preview` with a live digest among the candidates returns `LIVE_DIGEST_EXIT` (kills both surviving mutations); a malformed digest in the live file fails.
- **New `test_cleanup_apply_sh.py`** (pattern: `tests/test_create_bq_tables.py`'s stub `bq` on `PATH`): a stub `gcloud` script returns canned output, and failure is chosen by an env var.
  - A failing `services describe` makes `apply.sh` (preview mode) exit non-zero, with no "OK: no live digest" line.
  - An empty revision digest exits non-zero.
  - A healthy stub reaches the preview summary.

Mutations: revert to `for rev in $(gcloud …)`, and drop the digest regex; both must fail.

**Expected impact:** the retention script can no longer approve deletions while blind to a live image. Any gcloud hiccup aborts the preview and `--apply` loudly. The live policies are unchanged.

## PR T — R5: tests for the api's authz wiring

**Fix (test-first, minimal code)**
- **New `tests/test_async_app_wiring.py`.** Run each scenario in a **subprocess** (`sys.executable -c …` with a controlled env). `async_app` builds the app at import from env, so a subprocess keeps xdist workers clean and allows a "reload with different env" case.
  - **Enforce scenario:** `USER_AUTHZ_MODE=enforce`, dummy `TRUSTED_PROXY_SA` / `TRUSTED_PROXY_AUDIENCES`, `SESSION_SERVICE_URI=memory://`, `RATINGS_STORE=memory`, `SHARES_STORE=memory`, `PERSON_REFS_STORE=memory`, `BANDIT_DEPLOY_MODE=fake`, `GOOGLE_CLOUD_PROJECT=test-project`, no `K_SERVICE`, no `TRUST_CLIENT_USER_ID`. Inside, import `deployment.async_app` and assert:
    1. `UserAuthzMiddleware` is in `app.user_middleware`;
    2. with a `TestClient`, `GET /apps/creative_agent/users/a@x.com/sessions` without `X-TT-User` → 401;
    3. `POST /run` → 404;
    4. `_find_canned_services(app)[0] is session_service` (the shared store).
  - **Boot-guard scenario:** enforce mode without `TRUSTED_PROXY_SA` → the import raises `RuntimeError`. Assert a non-zero exit and the message.
  - Mark the tests `slow` if the import takes more than about 2 s, and keep total runtime small.
- **`async_app.py` (C5, tiny):** `log.warning(...)` when `_find_canned_services` falls back to building its own services, and fix the stale docstring (local dev now runs this launcher with `memory://`).

Mutations: delete the `add_middleware` line, which must fail scenario 1; neuter the enforce guard, which must fail the boot-guard test.

**Expected impact:** CI now fails if anyone drops, reorders or disables the backend's P3 authz, or breaks the shared session store. Before, a refactor could make every user-scoped route callable as any user and stay green. No runtime change.

## PR U — R3 + R6: remove the public-deploy doc, guard the `_DEPLOY` step

**Fix**
1. **`deployment/README.md`:** delete "Alternative Deployment: deploy to Cloud Run instances" (`:2618-2697`) and its TOC entry (`:22`). Add one line where it was, pointing to "Frontend + api_server on Cloud Run". Grep the README for other links to the anchor.
2. **`tests/test_creative_fanout_image.py`:** extend the deploy-step loop to `trend-trawler-api` and `trend-trawler-web`. Assert for each:
   - the digest `--image`;
   - `--no-traffic`;
   - no `--allow-unauthenticated`, `--set-env-vars` or `--source`.

   For the api, also assert `--no-cpu-throttling` and `--min-instances 1`. Keep `"update-traffic" not in deploy` as the file-wide check.

**Tests (first):** the new assertions pass against the current `cloudbuild.yaml`; that's expected, because they're guards. Mutations: remove `--no-traffic` from the web deploy, and add `--allow-unauthenticated` to web; each must fail.

**Expected impact:**
- **README:** no README path leads anyone to an unauthenticated agent with secrets baked in.
- **Deploy step:** CI fails if the optional deploy step ever moves traffic, drops the api's always-on CPU settings, or makes web public, which would bypass IAP.
- No runtime change. Add the plan copy here.

---

## Execution

0. **First, before anything else: undo the global-skill mistake from 2026-10-12.** Only the project skills were meant to be pruned, and they were already exactly the nine kept.
   - `mv ~/.claude/skills-removed-2026-10-12/* ~/.claude/skills/`, then remove the empty backup directory.
   - Re-enable the plugin: `frontend-design@claude-plugins-official: true` in `~/.claude/settings.json`.
   - Rewrite the memory note `skill-set-trimmed.md` (and its MEMORY.md line): the project keeps nine skills, and the global skills and plugin are untouched.
   - Verify with `ls ~/.claude/skills` (11 entries) and the settings JSON.
   - Fix the plan header that references the moved writing-plans skill.
1. **t=0:** dispatch Q, R, S, T and U in parallel (worktrees). Each implementer restates the constraints and uses private mutation copies or restores with `git diff --exit-code`.
2. **Review:** review each PR with code-review-and-quality (tests first, mutation evidence, five axes). Merge as CI goes green, in any order. Dispatch path-gated CI for S and U (shell/Markdown).
3. **No deploy round.** Afterwards:
   - run `deployment/cleanup/apply.sh` (preview) once against live to confirm the hardened guard still passes;
   - mark the six findings addressed at the top of `/home/user/adk_pipe/code_review.md` (local file);
   - update memory.

**Estimated total:** about 45–60 min (the slowest PR is S or T), no deploy.

## Verification

- **All:** the full gates are green and every listed mutation fails a test.
- **Q:** in a scratch Python session, `check_smoke`-style helpers on a pre-seeded-empty state report failure. Optional live: `python deployment/integration_test.py --check smoke --agent trend_scout` still passes against the live engine, since it really gathers trends.
- **R:** `python -c "import deployment.deploy_agent as d; print({k: d.ENV_VAR_DICT[k] for k in ['IMAGE_SIZE','MODEL_REQUEST_TIMEOUT_SECONDS','EVAL_MAX_WORKERS']})"` shows the defaults (`2K`, `240`, `2`).
- **S:** a live `deployment/cleanup/apply.sh` preview still prints "OK: no live digest…". `PATH=<stub-dir>:$PATH` with a failing describe aborts non-zero.
- **T:** `uv run pytest tests/test_async_app_wiring.py -v` passes. With the middleware line deleted locally it fails; restore it.
- **U:** `grep -n "Alternative Deployment" deployment/README.md` returns nothing. The new deploy-step assertions fail under the two mutations.
