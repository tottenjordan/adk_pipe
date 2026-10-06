# Continuous Learning Mode (no per-episode resets) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (or `executing-plans`) to implement this plan task-by-task.

**Goal:** A per-traffic-run option, **"Keep learning across episodes"**. The endpoint's Linear TS posterior then accumulates across the whole run instead of resetting every episode. Comparisons stay fair, uncertainty is shown honestly, and the experiment page displays the result as one continuous timeline.

**Context (why):**
- **Today:** every episode starts with a `reset` (`bandit_serving/predictor.py` `_fresh`, lines 511-535). Each episode is an independent replay of learning from scratch. That's good for repeatable comparisons with confidence intervals, but it never shows long-run learning.
- **What the user wants:** learning that keeps accumulating, the way a production system would.
- **User decisions (2026-10-06):**
  - **Scope:** within a traffic run only; each new run still starts fresh.
  - **Uncertainty:** a single timeline, plus a batch-means interval after warm-up.
- **Research:**
  - **Episodes stop being independent.** Without resets, episodes are sequential, autocorrelated and still trending (learning). Cross-episode t-intervals (`runserver/experiments_metrics.band`) become invalid.
  - **What's valid instead:** the standard estimate for one long run is **batch means after deleting the warm-up**. It's valid only once the batch means stop trending, and the lag-1 autocorrelation of the batch means should be near 0 (|r1| < ~0.1–0.2). See [Batch means method](https://rossetti.github.io/RossettiArenaBook/ch5-BatchMeansMethod.html), [Alexopoulos & Goldsman, "To batch or not to batch?"](https://dl.acm.org/doi/10.1145/974734.974738) and [replicated batch means](https://onlinelibrary.wiley.com/doi/abs/10.1002/nav.20158).
  - **Learning curves:** pointwise intervals need independent replications, so continuous curves get no bands.
  - **Production pattern:** keep the sufficient statistics (Λ, b) and decay them with γ < 1 to adapt to drift, i.e. discounted TS ([Qi et al.](https://arxiv.org/abs/2305.10718), [non-stationary TS](https://doi.org/10.3390/e27010051)). Our `forget` discount already does this.

**Architecture (key idea): a continuous run is *one long episode* of H = E × T rounds, cut into E *segments* for progress and storage.** This means:
- **Endpoint:** gets a single `reset` at the start of the run, with episode-0 policy-stream keys. Batch indices continue globally (`b = 0 … E·T/batch − 1`), so `fold_in(k_pol, b)` keeps exact endpoint ↔ simulator parity with **no predictor change** and no new reset field.
- **World:** built with horizon H, and `t` is the global round. Drift points and shifts refer to the *whole run* instead of recurring every episode.
- **Baselines and ghost:** replayed segment by segment with the policy state carried over, by a new simulator function `run_segment(...) -> (outs, final_state)`. Common random numbers are preserved because contexts and rewards are keyed by `(episode-0 key, global batch)`.
- **Storage:** one metrics row per segment, as now (`episode` = segment index), and events carry the global `round`. The run's mode lives in `traffic_runs` and `runs/{n}.json` (`learning: "per_episode" | "continuous"`), so no BigQuery migration is needed.
- **api:** for continuous runs, concatenates segment curves into one timeline (cumulative values carried across segments, no CI). It adds `continuousSummary`: the endpoint − best-baseline per-segment paired difference with a batch-means 95% interval over the post-warm-up segments, shown only when the checks pass.
- **Limit:** continuous runs are capped at **2,000,000 total rounds** (job runtime and memory). That's 50 × 40k demo or 5 × 400k realistic.

**Tech Stack:** Python 3.13, JAX (`bandit/`, dev group), FastAPI `runserver/`, Next.js/TS frontend; uv, ruff, ty, pytest, Vitest.

**Conventions:**
- Three PRs (A core + traffic, B api, C frontend), TDD with a commit per task, written against the contract in Task A1.
- **Never** add Co-Authored-By or AI attribution. Don't stage `.agents/`, `.claude/` or `skills-lock.json`.
- Gate: `uv run ruff format . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 4`; frontend `npm run lint && npm test && npm run build`.
- If PR CI doesn't trigger, dispatch the workflows manually with `--ref`.
- Deploy only when nothing is active.

---

## PR A: core + traffic job (`feat/bandit-continuous-learning`)

### Task A1: Contract + plan doc
**Files:**
- Modify `docs/bandit/contracts.md`:
  - **Glossary (line 11):** an episode starts from a reset posterior *in per-episode mode*; in continuous mode, a run is one episode cut into segments.
  - **§2 (traffic-job reset rules, line 104):** continuous runs send **one** reset per run.
  - **§5 traffic body (line 176):** add `learning?: "per_episode" | "continuous"` (default `"per_episode"`) and the 2,000,000-round limit.
  - **§7 (reset semantics, line 296; serving parity, line 297):** state the continuous parity: one long episode, global batch index.
  - **§10:** shifts and drift in continuous mode are fractions of the whole run, the forgetting discount uses H = E·T, and the ghost is replayed continuously.
  - **New §11, "Continuous learning mode":** job env `LEARNING_MODE`, segment semantics (rows, `episode` = segment, global `round`), the `curve` concatenation rule, and the `continuousSummary` shape:
    ```ts
    continuousSummary?: {
      segments: number; warmupSegments: number;
      pairedDiff: { policy: string; perSegment: number[]; mean: number;
                    lo: number | null; hi: number | null; lag1: number | null;
                    status: "ok" | "too_few_segments" | "autocorrelated" | "still_trending" };
    }
    ```
- Copy this plan to `docs/plans/2026-10-06-bandit-continuous-learning.md`.

**Commit:** `docs(bandit): continuous learning mode contract (§11) and plan`

### Task A2: `simulate.run_segment` (carry state across segments)
**Files:**
- Modify `bandit/simulate.py`:
  - Add `start_batch` / `init_state` parameters to `_episode` (lines 132-178). It currently starts with `policy.init(...)` at line 175 and drops the final state; return `(final_state, outs)` instead.
  - Add `run_segment(policy, env, key, start_batch, num_batches, batch_size, init_state=None) -> tuple[dict[str, np.ndarray], Any]`, a jitted non-vmapped path that uses the global batch index for `batch_draws(..., b, ...)` and `fold_in(k_pol, b)`.
  - Keep `run_episodes` behaviour unchanged.
- Test: `tests/test_bandit_continuous.py`

**Step 1: failing tests:**
```python
def test_segments_equal_one_long_episode():
    # concatenating run_segment over 4 segments == run_episodes(horizon=4*T) on the same key, for linear_ts and ucb1
    ...
    assert np.array_equal(long["arm"], np.concatenate([s["arm"] for s in segs]))


def test_state_carries_across_segments():
    # segment 2 started from init_state=None differs from segment 2 started from segment 1's final state
    ...
```
Use small `HORIZON_S` / `BATCH` from `tests/_bandit_sizes.py`.

**Steps 2–5:** run the tests (they fail), implement, run them (they pass), then commit `feat(bandit): run_segment carries policy state across segments`.

### Task A3: Global-round world for continuous runs
**Files:**
- Modify `bandit/simulate.py` `build_environment` and `bandit/environment.py` as needed so the world can be built with horizon H = E·T. `t = b·batch + arange` is already global when `b` is global. Shifts' `at_frac` and the drift `at_frac` then resolve against H.
- Modify `bandit/config.py`:
  - add `MAX_CONTINUOUS_ROUNDS = 2_000_000`;
  - relax the horizon validation for the continuous total only.
- Test: a drift scenario in continuous mode flips once at `at_frac·H`, not every T. A shift at 0.5 resolves to round `0.5·H`.

**Commit:** `feat(bandit): continuous runs resolve drift and shifts over the whole run`

### Task A4: Traffic job continuous mode
**Files:**
- Modify `bandit_traffic/main.py`: add `--learning {per_episode,continuous}` / env `LEARNING_MODE` (default `per_episode`) to `build_parser`, `RunOptions` and `resolve_run_options` (lines 64-174), following the FORGET pattern.
- Modify `bandit_traffic/traffic.py` `TrafficRunner`:
  - In continuous mode, build `env` (and `ghost_env`) with horizon H.
  - Use `keys[0]` streams for the whole run.
  - Send **one** reset before segment 0. Use discount = `default_shift_discount(..., horizon=H)` floored at 0.95 when forgetting.
  - Loop over segments with a global batch offset.
  - After each segment, replay the baselines and ghost with `simulate.run_segment(..., init_state=<carried>)`, keeping each policy's carried state in a dict.
  - Write the segment's metrics rows: `episode` = segment, linear checkpoints within the segment, plus `segment_start` in the row's `curve` JSON so the api can offset checkpoints. Update progress after each segment.
  - Event `round` is global.
  - `shift_response` / `regimes`: compute once at the end from the concatenated per-round arrays (keep only the needed columns), and attach them to the **last** segment's rows with `"continuous": true`.
- Update the module docstring parity note.
- Tests in `tests/test_bandit_traffic.py`:
  - an in-process continuous run (3 segments × small T) sends exactly one reset;
  - the endpoint picks exactly the arms of `run_segment` over the same stream (round for round);
  - the baselines' and the ghost's segments match the endpoint's;
  - the metrics rows per segment carry `segment_start`;
  - global rounds in events;
  - a per-episode run is unchanged (regression).

**Commit:** `feat(bandit_traffic): continuous learning mode (one reset per run, carried baseline/ghost state)`

### Task A5: Fake endpoint + parity test
**Files:**
- `bandit_traffic/fake_endpoint.py`: confirm that one reset plus global batch indices behave identically, and add a test.
- `tests/test_bandit_endpoint_parity.py`: add a `continuous` case (the real `BanditPredictor` through `TrafficRunner`, 3 segments) asserting round-for-round equality with `run_segment`.

**Commit:** `test(bandit): endpoint↔simulator parity in continuous mode`

---

## PR B: api (`feat/experiments-continuous-learning`)

### Task B1: Body, validation, job env, run record
**Files:**
- `runserver/experiments.py`:
  - `_TrafficBody.learning: str | None` (lines 1441-1446).
  - Validate it in `http_start_traffic` (lines 1681-1797): must be `per_episode` or `continuous`, and in continuous mode `episodes × horizon ≤ MAX_CONTINUOUS_ROUNDS`. Use the duplicated constant with a parity test against `bandit.config`. Failure is 400 `invalid_learning` with `field`.
  - Record `learning` in `runs/{n}.json` and in the `traffic_runs` entry. `traffic_runs_of` (735-766) and `traffic_runs_summary` (792-814) expose `learning`, with legacy runs set to `per_episode`.
  - In continuous mode the default `forget` stays as is: `bool(shifts)`.
- `runserver/experiments_jobs.py`: `build_env_overrides(..., learning=None)` sets `LEARNING_MODE` only when `learning == "continuous"`. Thread it through the protocol and both runners (lines 22-205).
- Tests: validation cases, the env override, the run record, and the summary field.

**Commit:** `feat(experiments): learning mode on traffic runs (validation, job env, run record)`

### Task B2: Continuous aggregation + batch means
**Files:**
- Create `runserver/batch_means.py`. It's pure and has no numpy dependency in the api image:
  ```python
  def batch_means_summary(
      diffs: list[float],
      warmup_frac: float = 0.5,
      min_batches: int = 5,
      max_lag1: float = 0.2,
  ) -> dict:
      """Delete the warm-up (first warmup_frac of segments), then mean ± t·s/√m over the rest.
      status: too_few_segments (< min_batches after warm-up) | still_trending (a simple
      linear-trend test on the kept segments is significant at 5%) | autocorrelated
      (|lag-1 autocorr| > max_lag1) | ok (lo/hi set). lo/hi are None unless status == ok."""
  ```
  It reuses `experiments_metrics.t_critical`.
- Modify `runserver/experiments_metrics.py` `aggregate_episode_metrics` (lines 480-559). When the run is continuous (pass `learning` in from the route), it:
  - sorts rows by segment;
  - offsets checkpoints by `segment_start`;
  - carries cumulative values across segments (cumulative reward = `cum_avg_reward × rounds`, cumulative regret += previous segments' totals, cumulative optimal count = `pct_optimal × rounds`);
  - emits each curve as a single series with `lo = hi = mean`, i.e. no band;
  - computes `continuousSummary.pairedDiff` from the per-segment `total_clicks` differences (endpoint − best baseline by total), using `batch_means_summary`;
  - takes `shiftResponse` / `regimes` from the last segment's rows;
  - sets `shiftCost` to the whole-run ghost − endpoint total, with no CI.
- Modify `runserver/experiments_series.py` / `experiments_store.py` (series windows): for continuous runs, window over the **global** round and group by window only (one stream), not by (episode, window).
- Tests:
  - concatenation maths on synthetic rows (cumulative continuity across segment boundaries);
  - `batch_means_summary` statuses (iid noise gives `ok` with the correct t-interval; an AR(1) series with ρ=0.8 gives `autocorrelated`; a trending series gives `still_trending`; 4 segments gives `too_few_segments`);
  - series SQL builder params for continuous runs;
  - per-episode aggregation unchanged.

**Commit:** `feat(experiments): continuous-run aggregation with batch-means paired difference`

---

## PR C: frontend (`feat/experiment-continuous-learning`), using the `frontend-design` + `dataviz` skills

### Task C1: Control + payload
**Files:**
- `src/app/experiments/[experimentId]/page.tsx` traffic controls (around lines 384-411): add a segmented control, **"Learning: Reset each episode | Keep learning"**, with an InfoTip explaining the trade-off. Reset each episode means repeatable episodes with confidence bands; keep learning means one continuous stream, like production, with no bands and a batch-means check at the end. In continuous mode:
  - relabel "Episodes" to "Segments" and "Rounds per episode" to "Rounds per segment";
  - show the total (e.g. "800,000 rounds in one stream");
  - disable combinations over 2,000,000 rounds, with a note.
- `lib/shifts.ts` `trafficBody` (lines 393-400) and `TrafficBody` (382-387): send `learning` only when it's `"continuous"`. `lib/experiments.ts` `startTraffic` (357-371) passes it through.
- Update the `TrafficRun` type and `runLabel` ("· keeps learning").
- Help copy in `lib/experiment-help.ts`: fix `CONTROL_HELP.episodes` (lines 10-11), which currently says "reset and learns from scratch", and add `learning`.
- Tests: payload mapping, the total-rounds cap, and `runLabel`.

### Task C2: Continuous charts and readings
**Files:**
- `lib/experiments.ts` `curveSeries` (900-927): when the run is continuous, x is the global round, there are no bands, and the axis label is "Round (whole run)". Charts use a linear x axis plus subtle segment-boundary ticks through the existing `markers` prop.
- `lib/experiment-insights.ts`: in continuous mode, the headline and readings use `continuousSummary`.
  - When `status == "ok"`: "Over the last N segments the endpoint earned X more clicks per segment than UCB (± Y, batch means after warm-up)."
  - Otherwise, one honest status line: "still learning: the per-segment advantage is still changing", "not enough segments to estimate", or "segments too correlated to estimate".
  - "Too early to call" logic is replaced accordingly.
- Scoreboard strips and series windows use the global round with no changes, since the api returns global windows. Verify that.
- Tests: series shaping without bands, a reading for each status, marker placement.

### Task C3: Screenshot + docs
- **Screenshots:** a fixture from a real continuous simulator run (`bandit.cli` / in-process traffic, 5 segments), producing `docs/screenshots/20-continuous-learning.png`. Look at it and iterate on the design.
- **Docs:**
  - `docs/bandit/README.md`: a new "Continuous learning mode" section covering when to use each mode, why there are no bands, and the batch-means rules.
  - Fix the "Learning loop" text, i.e. episodes no longer always reset.
  - CLAUDE.md bandit bullet.

**Commits (PR C):** `feat(frontend): keep-learning traffic mode` / `feat(frontend): continuous charts and batch-means readings` / `docs: continuous learning mode`

---

## Rollout (after all three merge; nothing active)
1. **Traffic image:** rebuild and redeploy the traffic job. The traffic job is the only image that changes; the predictor is unchanged.
2. **api:** deploy with `--no-traffic --tag verify`, verify, pin, and remove the tag.
3. **web:** deploy and pin.
4. **BigQuery:** no migration.

## Verification
- **Unit:** all gates pass in CI. The parity tests prove endpoint == continuous simulator round for round.
- **Offline:** an in-process continuous run (`python -m bandit_traffic.main --in-process --dry-run --learning continuous`, 10 segments × 4k rounds) produces:
  - one reset;
  - cumulative regret that keeps flattening across segments, where per-episode mode restarts each episode;
  - `continuousSummary.status` that reaches `ok` once the advantage stabilises.
- **Live:**
  1. Deploy 3 creatives on segment_winners and start **Keep learning** with 20 segments × 40k rounds.
  2. Check the single continuous charts with segment ticks, and confirm the endpoint's best-creative rate keeps rising past what one episode reaches.
  3. Check the batch-means summary line.
  4. Compare with a per-episode run through the run selector.
  5. Stop the experiment.
