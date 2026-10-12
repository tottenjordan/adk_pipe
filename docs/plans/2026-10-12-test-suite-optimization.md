# Test Suite Optimization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` (or `subagent-driven-development`) to implement this plan task-by-task. Save a copy as `docs/plans/2026-10-12-test-suite-optimization.md` in the first PR.

**Goal:** cut the Python suite's CPU cost, which is what bounds CI (pytest step 139 s on GitHub's 4-vCPU runner), by fixing the measured hotspots. Also fix one test that reaches real GCS. Coverage stays the same, and CI gets only one small workflow change.

**Architecture:** surgical test-side fixes (smaller fixtures, no stray network or warm-up work, shared expensive objects, no fresh interpreters where they aren't needed), plus one optional production speed-up (`_calibrate_alpha`). No new workflows, no xdist-mode changes, no thread tuning: all three were measured and gave nothing or made things worse.

**Tech Stack:** pytest 8 + pytest-xdist 3.8, JAX (persistent compile cache in `tests/conftest.py`), Pillow, matplotlib, GitHub Actions (`python-ci.yml`).

---

## Context: what was measured (2026-10-12, main `69943f0`)

**[1] Where the time goes.** 3,610 tests. Wall time by worker count on a 32-core box:

| Workers | `-n 0` | `-n 2` | `-n 4` | `-n 6` | `-n 8` | `-n 16` | `-n auto` (32) | `-n 6 -m "not slow"` |
|---|---|---|---|---|---|---|---|---|
| Wall time | 132 s | 79 s | 53 s | 52 s | 51 s | **42 s** | 50 s | 37 s |

Collection alone takes 9 s, mostly agent imports (`creative_agent` ≈ 1.8 s, of which pandas/BigQuery ≈ 0.45 s and `google.genai.types` ≈ 0.39 s). Every xdist worker pays this.

Slowest items:

| Time | Test | Root cause (file:line) |
|---|---|---|
| 16.0 s | `test_bandit_notebook_parity::test_parity_figures_produced` | 9 simulations / 33 policy runs, horizons clamped to ≥ 400 (`experiments/bandit/notebook_parity.py:78`), `jax_disable_most_optimizations` so no shared compiles, 9 matplotlib figures, cold font cache in CI |
| 11.3 + 3.7 s | `test_scenario_preview_golden` (×2) | 22 × `build_true_model` at 400 k calibration samples; `_calibrate_alpha` bisection (~45 numpy passes over 400 k × K, `bandit/environment.py:176-193`) ≈ 3.5 s |
| 6.0 s | `test_build_image::test_staged_tree_is_self_contained` | the child process (`tests/test_build_image.py:65`) runs without `BANDIT_WARMUP=0`, so it compiles warm-up kernels for 4 batch sizes |
| 18.9 s (file) | `test_bandit_traffic` | 3 module fixtures each run a whole traffic job (≈ 58 XLA programs); inherent to what it tests |
| 10.7 s (file) | `test_shares_api` | every fake GCS object is a 2000×1000 PNG (`tests/test_shares_api.py:40-46`), so each share decodes, LANCZOS-shrinks and JPEG-encodes it; only one test needs a large image |
| 5.6 s (file) | `test_bandit_environment` | every test calls `build_true_model` from scratch; no shared fixture |
| 3.3 + 1.7 s | `test_public_api` | two `subprocess.run` calls; the second is a positive control that doesn't need a fresh interpreter (`tests/test_public_api.py:147-166`) |
| 2.1 s | `test_ratings_api::test_store_failure_is_502` | **Bug.** No `report_loader` is passed (`:385`), so the default `gcs_report_loader` reads the bucket from the repo `.env` and **tries to reach real GCS** (1.25 s inside `runserver/ratings.py:380`). In CI there's no `.env`, so the call is skipped there |
| ~1.2 s | `test_brand_history.py:350,583,676,680` | real `sleep`s used for timeout tests |

**[2] Root-cause categories found.**
- **Oversized fixtures:** shares PNGs, notebook-parity horizons.
- **Accidental work:** warm-up compile in a subprocess; a live GCS call.
- **A fresh interpreter that isn't needed:** the public_api positive control.
- **Per-test rebuilds of an expensive model:** `test_bandit_environment`.
- **Real sleeps:** `test_brand_history`.
- **Partial duplication:** two fake-endpoint parity tests (`tests/test_bandit_endpoint_parity.py:160,246`) overlap `test_bandit_traffic`'s fake-endpoint fixtures.

Nothing hits retry backoff unpatched (`no_sleep`/`noop_async` fixtures already exist). Module-scoped fixtures are already used where it matters.

**[3] I/O is not the bottleneck.**
- **Run-time evidence:** sys time is 18 s of 266 s CPU (pinned to 4 cores).
- **No sources of I/O load:** no Docker, no database (sqlite only in the `*_store` unit tests), no test writes outside `tmp_path`, and `logging.basicConfig(INFO)` is a no-op under pytest.
- **CPU fully explains CI:** the local run on 4 pinned cores takes ≈ 250 CPU-s (71 s wall). CI is the same work on ~2× slower vCPUs (139 s). So CI is CPU-bound, and the lever is total CPU seconds.
- **One CI-only cost:** matplotlib's font cache is rebuilt on every fresh runner. The JAX cache is already restored from cache.

**[4] xdist: already used well; no mode changes.**
- **CI:** `-n 4` matches the runner's 4 vCPU.
- **Locally:** `-n 16` is fastest (42 s). `-n auto` is slower (50 s) because each worker re-imports the agents, which confirms the existing CLAUDE.md note.
- **Tried and rejected:** `--dist loadscope` / `loadfile` made no difference (251 vs 248 CPU-s), because workers share the persistent JAX compile cache. Capping XLA/BLAS threads made the run **2.3× slower**.

**Frontend (vitest) is healthy:** 9 s locally, 25 s in CI, test bodies sum to 18.6 s across 77 files, already node-env by default. No changes planned.

**Expected outcome:** about −35 to −45 CPU-s out of ≈ 250, so the CI pytest step should drop from ≈ 139 s to ≈ 105-115 s. The live-GCS leak in a unit test goes away. Coverage is unchanged.

**Constraints:**
- No `Co-Authored-By` or AI attribution in commits or PRs.
- Merge only after CI passes.
- Gates: `unset VIRTUAL_ENV; uv run ruff format --check . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 4`.

**Benchmark command** (emulates the CI runner; use it before and after):
```bash
unset VIRTUAL_ENV; GOOGLE_CLOUD_PROJECT=test-project /usr/bin/time -f "%e s wall %U s user" \
  taskset -c 0-3 uv run pytest tests/ -q -p no:cacheprovider -n 4 --durations=15
```
Baseline: 71 s wall / 248 s user.

---

## PR A — quick test-side fixes (one branch `test/speed-quick-wins`, one commit per task)

### Task 1: Stop the ratings tests from reaching real GCS
**Files:** Modify `tests/test_ratings_api.py:385` and `:500`.

**Step 1:** Write a guard test that fails today. Put it in the same file:
```python
def test_store_failure_does_not_load_reports_from_gcs(monkeypatch):
    called = []
    monkeypatch.setattr(rt, "gcs_report_loader", lambda *a, **k: called.append(a))
    # same body as test_store_failure_is_502 …
    assert called == []
```
**Step 2:** Run `uv run pytest tests/test_ratings_api.py -k store_failure -v`. Expected: FAIL. The loader is called whenever `.env` provides a bucket; to reproduce in CI, set `GOOGLE_CLOUD_STORAGE_BUCKET=x`.

**Step 3:** In both tests, pass `rt.configure(session_service=h.svc, store=Broken(), report_loader=lambda uri: None)`. Check the parameter name in `runserver/ratings.py` `configure`.

**Step 4:** Re-run. Expected: PASS, and the test takes < 0.3 s (`--durations=5`).

**Step 5:** Commit `test(ratings): never reach real GCS from store-failure tests`.

### Task 2: Small images in the shares API tests
**Files:** Modify `tests/test_shares_api.py:40-46` and `:119`, and the re-encode test at `~:197`.

**Steps:**
1. Default `RENDER_PNG = _render_png((200, 100))`.
2. Add `LARGE_PNG = _render_png((2000, 1000))`, used only by `test_create_slate_share_reencodes_images…`, which asserts the 1600 px long-edge shrink.
3. Run `uv run pytest tests/test_shares_api.py -q --durations=5`. Expected: all pass; the file drops from ≈ 10.7 s to ≈ 3 s.
4. Grep for any other assertion on dimensions or size that relied on the large default, and switch those tests to `LARGE_PNG`.
5. Commit `test(shares): small fixture images except the re-encode test`.

### Task 3: No predictor warm-up in the build-image subprocess
**Files:** Modify `tests/test_build_image.py:65` (same pattern as `tests/test_bandit_endpoint_parity.py:93`).

**Steps:**
1. Add `env={**os.environ, "BANDIT_WARMUP": "0"}` to the `subprocess.run(...)` call, and import `os`.
2. Run `uv run pytest tests/test_build_image.py -q --durations=3`. Expected: pass, ≈ 6 s → ≈ 3 s.
3. Commit `test(build_image): skip predictor warm-up in the staged-tree subprocess`.

### Task 4: Run the deprecation positive control in-process
**Files:** Modify `tests/test_public_api.py:147-166`.

**Step 1:** Replace the subprocess with:
```python
def test_legacy_agent_deprecation_filter_actually_fires():
    from google.adk.agents import SequentialAgent

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        warnings.filterwarnings(
            "error", message=_LEGACY_AGENT_WARNING, category=DeprecationWarning
        )
        with pytest.raises(DeprecationWarning):
            SequentialAgent(name="probe", sub_agents=[])
```
Drop `@pytest.mark.subprocess`. The guarded test (`:140`) keeps its subprocess, because a fresh import is the point there.

**Step 2:** Run `uv run pytest tests/test_public_api.py -q`. Expected: pass. Also check it is order-independent: `-p no:randomly` doesn't apply here, so run the single test alone and then the whole file.

**Step 3:** Commit `test(public_api): in-process positive control for the deprecation filter`.

### Task 5: Shorten the brand-history sleeps
**Files:** Modify `tests/test_brand_history.py:350,583,676,680`.

**Steps:**
1. Read each test. Where a sleep simulates a slow query against a configured timeout, monkeypatch the timeout down (e.g. 10 s → 0.05 s) and scale the sleep to ≈ 2× the timeout. Where it waits for a concurrent task, wait on an `asyncio.Event` instead.
2. Run `uv run pytest tests/test_brand_history.py -q --durations=5`. Expected: pass, ≈ 1.2 s → < 0.3 s. Run it 5 times (`--count` isn't installed, so use a shell loop) to check for flakes.
3. Commit `test(brand_history): event/short-timeout waits instead of real sleeps`.

### Task 6 (moved to PR C so A and C share no files): Share built models in `test_bandit_environment`
**Files:** Modify `tests/test_bandit_environment.py`.

**Steps:**
1. Add a module-scoped, `functools.cache`-backed helper `_model(scenario, ctr_mode, seed=0)` that wraps `build_true_model(...)`.
2. Use it in tests that only read the model. Keep fresh builds in the tests that assert calibration across seeds (around `:290`) or that mutate the model.
3. Run `uv run pytest tests/test_bandit_environment.py -q --durations=5`. Expected: pass, ≈ 5.6 s → ≈ 3 s.
4. Commit `test(bandit): share read-only TrueModels across environment tests`.

**PR A gates:** the gates above plus the benchmark command; record before and after in the PR body. Dispatch nothing extra: Python CI is path-gated on `**.py`.

---

## PR B — notebook-parity smoke split + one CI cache step (`test/notebook-parity-split`)

### Task 7: Test the plotting on committed tiny data, and keep one small generation smoke
**Files:**
- Create: `tests/fixtures/bandit_parity/` (data JSON).
- Modify: `tests/test_bandit_notebook_parity.py`.
- Read: `experiments/bandit/notebook_parity.py` (`runs`, `main`, the plot functions).

**Step 1:** Generate the data once with the current script (`--generate --episodes 1 --horizon-scale 0.001`) into a tmp dir. Check its size. If it's under ~300 KB, commit it as `tests/fixtures/bandit_parity/`. If it's larger, keep only the checkpoints the plots read.

**Step 2:** Rewrite the test as two tests:
- `test_parity_figures_from_fixture_data`: `main(["--data-dir", <fixture>, "--fig-dir", tmp])` without `--generate`. Assert all 9 PNGs exist and are > 1000 bytes. Not `slow`.
- `test_parity_generation_smoke` (`slow`): monkeypatch `mod.runs` to return only the `clear_winner` entry with `policies=["linear_ts","oracle"]`, run `--generate --episodes 1`, and assert that the data file for that run is written. This keeps the generator path tested.

Check the CLI flags against `main`'s argparse before writing the commands.

**Step 3:** Run `uv run pytest tests/test_bandit_notebook_parity.py -q --durations=3`. Expected: both pass; 16 s → ≈ 3-4 s total.

**Step 4:** Add a fixture-regeneration note to `tests/README.md`: the command from Step 1, and when to rerun it (when a plot reads a new field).

**Step 5:** Commit `test(bandit): plot parity figures from fixture data; tiny generation smoke`.

### Task 8: Cache matplotlib's font cache in Python CI (the only workflow change)
**Files:** Modify `.github/workflows/python-ci.yml`, next to the existing JAX cache step.

**Step 1:** Add `MPLCONFIGDIR: ${{ runner.temp }}/mpl` to the pytest step's `env`, plus one step:
```yaml
- uses: actions/cache@v4   # match the version the JAX cache step already uses
  with:
    path: ${{ runner.temp }}/mpl
    key: mpl-${{ runner.os }}-${{ hashFiles('uv.lock') }}
```
Don't add a new workflow or any new jobs.

**Step 2:** Append `--durations=15` to the CI pytest command so slow tests stay visible in every run's log, at no cost.

**Step 3:** Commit `ci(python): cache the matplotlib font cache; print the 15 slowest tests`.

**PR B gates:** same gates as PR A. On the second CI run of the PR, check that the mpl cache hits and that the pytest step time drops.

---

## PR C (optional; changes production numerics) — Newton calibration (`perf/calibrate-alpha-newton`)

### Task 9: Replace the bisection in `_calibrate_alpha` with safeguarded Newton
**Files:** Modify `bandit/environment.py:176-193`. Test: `tests/test_bandit_environment.py` (the calibration guard near `:290`).

**Step 1:** Add a failing-by-design accuracy test: for random logits and targets in [0.005, 0.5], `abs(mean(sigmoid(alpha + L)) - target) < 1e-12` and the result matches the old bisection to within 1e-9. Keep a copy of the old function in the test as the reference.

**Step 2:** Implement Newton on `f(a) = mean(sigmoid(a+L)) - target` (`f' = mean(s(1-s))`). Start from `logit(target) - mean(L)`. Fall back to a bisection step whenever a Newton step leaves the `[-20, 10]` bracket. Stop at `|f| < 1e-13` or after 50 iterations. Keep the precomputed `exp(-L)`.

**Step 3:** Run the gates.
- **Must pass:** `tests/test_bandit_environment.py`, `tests/test_scenario_preview_golden.py` (`REPRO_TOL 1e-6` against the committed fixtures), `tests/test_bandit_endpoint_parity.py` (exact endpoint↔simulator parity; both sides call the same function), and the frontend `scenario-preview.test.ts` (`ACHIEVED_TOL 1e-5`).
- **If any golden diff exceeds tolerance:** stop. Don't regenerate the fixtures in this PR; abandon Task 9.

**Step 4:** Benchmark the golden file (11.3 + 3.7 s → expected ≈ −3 s), then commit `perf(bandit): Newton calibration for the scenario intercept`.

**Deploy note:** `bandit/` ships in the CPR and traffic images. A merge changes nothing live until those images are rebuilt; mention this in the PR.

---

## Deliberately not doing (measured or judged not worth it)
- **XLA/OMP thread caps:** 2.3× slower.
- **`--dist loadscope/loadfile`:** no change, because of the shared JAX cache.
- **`-n auto` in CI:** the runner has 4 vCPU, and locally `-n auto` loses to `-n 16`.
- **Lowering golden calibration samples or subsetting golden cases:** the TS parity tolerance needs them.
- **Global `jax_disable_most_optimizations`:** risks the 1e-6 golden and exact-parity tests.
- **Shrinking `test_bandit_simulate_metrics` T:** needs a statistical margin study.
- **Lazy-importing pandas/BigQuery in agents:** production code churn for ≈ 0.5 s per worker.
- **New workflows, nightly splits or matrix jobs.**

**Follow-up, if wanted:** remove the two fake-endpoint parity tests (`tests/test_bandit_endpoint_parity.py:160,246`) only after a line-by-line check that `test_bandit_traffic` asserts the same properties (≈ 2 s). This isn't in the PRs because the overlap is partial.

**Docs:** PR A updates CLAUDE.md Testing; PR B owns every `tests/README.md` edit. Both gain the local tip `-n 16` (fastest on a 32-core box; `-n auto` slower) and the benchmark command.

## Parallel execution (2026-10-12)
The PRs are built in parallel in separate worktrees (`/tmp/wt-a`, `/tmp/wt-b`, `/tmp/wt-c`) with disjoint files: A = ratings/shares/build_image/public_api/brand_history tests + CLAUDE.md + plan copy; B = notebook parity + fixtures + python-ci.yml + tests/README.md; C = bandit/environment.py + tests/test_bandit_environment.py (Tasks 6 + 9). Benchmarks run one PR at a time; merges go A → B → C, each rebased on main with CI re-run before merging.

## Verification
1. **Per task:** the targeted test passes, and its `--durations` shows the expected drop.
2. **Per PR:** the full gates (ruff, ty, pytest `-n 4`) plus the benchmark command, with before and after recorded in the PR body. Expected PR A ≈ −20 CPU-s, PR B ≈ −13 CPU-s (more in CI from the font cache), PR C ≈ −3 CPU-s.
3. **CI:** compare the pytest step time on each PR against the 139 s baseline (`gh run view <id> --json jobs`). Check that the mpl cache hits on re-runs.
4. **Network-leak check:** with `.env` present, run `uv run pytest tests/test_ratings_api.py -q` under `strace -f -e trace=connect` (or with `HTTPS_PROXY=http://127.0.0.1:9` set) and confirm there are no connections to storage.googleapis.com.
5. **Flake check:** run the full suite 3 times with `-n 4` and once with `-n 16`, all green.
