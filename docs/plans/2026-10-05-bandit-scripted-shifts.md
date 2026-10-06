# Scripted behaviour shifts: steer the simulated readers during a traffic run

> **Status:** COMPLETE (2026-10-06). #243 (bandit core), #244 (traffic job + predictor, incl. exact endpoint↔simulator LinTS parity), #248 (api), #249 (frontend) merged and rolled out: BigQuery columns added (events/metrics before the traffic image, `traffic_runs` after the api), CPR + traffic images `60b58c7`, api `00152-cuw`, web `00038-zlv`. Live end-to-end run with shifts done (experiment `ad8df4f0e8f14d14`: run 1 forgetting on, run 2 off). Docs done (README, frontend/README, CLAUDE.md, bandit guide + screenshots 18/19); the experiments journey GIF is not being extended (not needed, per the user).

## Context
The deep research (2026-10-05) recommended a forward-only, scripted shift timeline as phase 1 of "orient and influence the simulation". Live "apply now" steering comes later.

**User decisions (2026-10-05):**
- **All four shift types:** promote a challenger, demote the leader, audience mix shift, and a temporary shock.
- **Shifts belong to a traffic run.** They are edited on the experiment page next to **Start traffic**, so the same endpoint can be rerun with different scripts.
- **Same world for every strategy.** Shifts apply to the endpoint and to every baseline, which keeps the comparison fair. A **ghost line** shows Linear TS on the same random draws *without* the shifts.
- **Forgetting toggle.** "Let the endpoint forget old evidence" is **on by default when shifts exist** and is applied per run.
- **Numbered traffic runs** with a run selector. Today a second Start traffic mixes its rows into the first run's.

**Research findings this design relies on:**
- **CRN needs keyed random streams.** Draws must be keyed on a stable identity, never taken sequentially off one shared stream.
  - *Already true here.* `simulate.batch_draws` uses `fold_in(k_ctx|k_rew, batch)`. Segment and level draws are Gumbel-max arrays of fixed shape (n,S) and (n,G,Lmax), and click uniforms have fixed shape (n,K). So changing click rates or the segment mix keeps every random draw aligned.
- **Compare against a matched baseline, not before vs after.**
- **Show per-arm adaptation** in the style of a TS-Insight barcode.
- **State the effect explicitly** with evidence volume and uncertainty. No live significance readouts.
- **Introduce one lever at a time.**

**Constraints found while exploring:**
- **Time already flows into the model.** It enters as a per-row `t` in `environment.click_probs`, and drift is a logit-column permutation from `drift_start`.
- **`TrueModel` takes schedule arrays.** It is a jitted pytree, so new arrays trace without change.
- **One environment for everyone.** `TrafficRunner` shares `env.model` between the endpoint and the baseline replays, and it already checks that segments are identical.
- **The reporting hides mid-run change today.** It uses ~50 log-spaced checkpoints and log-x charts. Per-segment "best creative", true click rates and `segmentsWon` are whole-run modes and averages.
- **Discount is fixed at load.** The endpoint reads its discount from `experiment.json` once, so per-run forgetting needs a predictor change and an image rebuild.
- **No run id in the data.** `bandit_events` and `bandit_episode_metrics` don't record which traffic run a row came from, and `metrics_row_id` = `{exp}-e{ep}-{policy}`.

---

## Shift contract (new contracts §10; binding for all layers)
The traffic body gains `shifts` (max 4) and `forget` (bool, default true when there is at least one shift):

```jsonc
"shifts": [
  {"kind": "promote", "atFrac": 0.4, "segment": "mobile_scrollers" | null, "creativeId": "aae3f6b4", "liftPp": 0.015},
  {"kind": "demote",  "atFrac": 0.5, "segment": null, "creativeId": "leader" | "<id>", "dropPp": 0.015},
  {"kind": "mix",     "atFrac": 0.3, "segmentMix": [0.6, 0.2, 0.2]},
  {"kind": "shock",   "atFrac": 0.6, "untilFrac": 0.7, "segment": null, "creativeId": "<id>", "ctrMultiplier": 0.6}
]
```

**Field semantics:**
- **`segment: null`** means everyone.
- **`promote`:** from `atFrac`, the creative beats the best other creative by `liftPp` (segment-level logit, like the existing segment-winner lift). Bounds `[0.005, 0.03]` demo, ×0.2 realistic.
- **`demote`:** from `atFrac`, the creative (`"leader"` is resolved at build time) falls `dropPp` below the second best. Same bounds as `liftPp`.
- **`mix`:** same rules as `segment_mix` in §9.
- **`shock`:** the click probability is multiplied by `ctrMultiplier` (`[0.3, 2.0]`) over `[atFrac, untilFrac)`, which must be at least 2% of the run.

**Ordering and resolution:**
- `atFrac` is in `[0.05, 0.95]`.
- Shifts resolve **in time order** on top of earlier shifts, so "leader" means the leader at that moment.
- Shifts are abrupt in v1.

**Snake_case form.** The same structure in snake_case goes to the job through the `SHIFTS_JSON` env override plus `TRAFFIC_RUN=N`.

**Resolved record.** It is stored per run with the concrete creative IDs, rounds and resolved targets:
- the experiment row gets `traffic_runs` JSON;
- GCS gets `{id}/runs/{n}.json`, so a run can be replayed exactly.

---

## PR A: bandit core (`feat/bandit-shifts-core`)
- **`bandit/config.py`:**
  - frozen `ShiftSpec` with `SHIFT_KINDS` and `SHIFT_BOUNDS` (exported for parity);
  - `shifts_from_dict` / `shifts_to_dict` (strict, no bools, bounds, kind-specific fields);
  - `validate_shifts(shifts, scenario, arms, ctr_mode)`;
  - `default_shift_discount(ctr_mode, batch)` = `discount_for_memory(horizon/8)`, reusing the drift memory rule and the existing `discount_for_memory`.
- **`bandit/environment.py`:**
  - `build_true_model(..., shifts=())` resolves the shifts into new `TrueModel` leaves:
    - `shift_start (P,)`, `shift_end (P,)` (`inf` when open-ended);
    - `shift_logit (P,S,K)` for promote and demote, solved numerically at segment level against the model including earlier shifts (same approach as the segment-winner lift; α is not recalibrated);
    - `shift_mult (P,S,K)` for shocks;
    - `shift_seg_logits (P,S)` and `shift_is_mix (P,)`.
  - `click_probs(model, X, seg, t)` adds the active logit offsets, applies the existing drift permutation, takes the sigmoid, multiplies by active shocks and clips to (0,1).
  - `sample_contexts(key, model, n, t)` takes per-row segment logits from the latest active mix shift. The Gumbel shape is unchanged, so draws stay aligned.
  - `optimal_arms` and the oracle already follow `p`.
  - `resolved_shifts(env)` returns concrete targets and rounds for the record.
- **`bandit/simulate.py`:** `batch_draws` passes `t` to `sample_contexts`. `build_environment(cfg, scenario=, shifts=)`.
- **`bandit/metrics.py`:**
  - `shift_response(arrays, shift_rounds, window)` reports, per shift, the % optimal in the 2k rounds before and after the shift, `recovery_rounds` and regret rate before and after:
    - `recovery_rounds` = rounds until the trailing 1k-round optimal-choice rate reaches 80% of its pre-shift level, or null;
    - "pre-shift level" is the rate in the window just before that shift.
  - `merge_checkpoints(cps, shift_rounds)` adds `r-1`, `r`, `r+0.5%`, `r+2%` and `r+5%` of the horizon.
  - `make_checkpoints(spacing="linear")` is used whenever there are shifts.
- **`bandit/cli.py`:** `--shifts <json|path>` and `--forget`.
- **Tests** (`test_bandit_environment.py`, `test_bandit_simulate_metrics.py`, `test_bandit_config.py`):
  - **CRN alignment:** a shifted vs unshifted env with the same keys draws identical reward uniforms, identical levels and identical segments, except rows whose segment flips under a mix shift.
  - **Shift effects:** promote and demote change the per-segment oracle at the round; a shock multiplies only inside its window and recovers after; a mix shift changes segment frequencies from the round on.
  - **Resolution:** "leader" is resolved in time order.
  - **Calibration:** α is unchanged.
  - **`shift_response`** on synthetic series.
  - **Validation and bounds.**
- **Preview parity:** extend `scripts/gen_scenario_presets.py` (bounds) and `tests/test_scenario_preview_golden.py` with after-shift matrices for the TS port in PR D.

## PR B: traffic job + predictor (`feat/bandit-shifts-traffic`)
- **`bandit_traffic/main.py`:** read `SHIFTS_JSON`, `TRAFFIC_RUN` and `FORGET` (env or flags), then validate with `bandit.config`.
- **`bandit_traffic/traffic.py`:**
  - Build the env with the shifts.
  - Use linear checkpoints merged with the shift rounds when there are shifts.
  - Write `traffic_run` on every event and metrics row.
  - `metrics_row_id` → `{exp}-r{run}-e{ep}-{policy}`; event `request_id` gets `-r{run}`.
  - **Ghost replay:** after each episode, run a local LinTS (`bandit.linear_ts` with the experiment's policy params, plus the discount when `forget` is on) through `simulate.run_episodes` on the **unshifted** env with the same episode key. Store it as policy `linear_ts_unshifted` in `bandit_episode_metrics`.
  - Add `shift_response` (JSON column) to every policy's metrics row.
  - When shifts exist, `per_segment` and per-arm true CTR in each metrics row are also computed **per regime** (`regimes: [{start, end, per_segment, true_ctr}]`), so the api can split the winners table and the click-rate table without new queries.
- **`bandit_serving/predictor.py`:** the `reset` instance accepts optional `discount` (bounds `[0.95, 1.0]`), which applies until the next reset. This follows the per-request `parameters` pattern, and the traffic job sends it when `forget` is on.
- **`bandit_traffic/bq.py`:** new columns (`traffic_run INT64` on events and metrics, `shift_response STRING` JSON on metrics) and an updated `METRICS_COLUMN_TYPES`.
- **Tests:**
  - `test_bandit_traffic.py`: an in-process run with 2 shifts writes run-numbered rows. The ghost differs from the endpoint only after the first shift (identical before it, up to policy randomness, which is checked statistically). `shift_response` is present, and segments match the baselines.
  - `test_bandit_predictor.py`: the reset discount applies, and a later reset without one restores the config value.
  - `test_bandit_bq_rows.py`.

## PR C: api (`feat/experiments-shifts-api`)
- **`runserver/experiments.py`:**
  - `_TrafficBody` gets `shifts` and `forget`.
  - `validate_shifts` uses duplicated `SHIFT_KINDS` / `SHIFT_BOUNDS`, parity-tested against `bandit.config`, the same way as the existing override parity. It checks creative IDs against the experiment's arms and segment names against `SCENARIO_SEGMENTS`. 400 `invalid_shifts` with `field`.
  - Start traffic allocates `run = len(traffic_runs)+1`, writes `{id}/runs/{n}.json`, passes `SHIFTS_JSON` / `TRAFFIC_RUN` / `FORGET` through `experiments_jobs.build_env_overrides`, and appends to the `traffic_runs` JSON on the row.
  - `to_summary` exposes `trafficRuns[] {run, startedAt, episodes, horizon, shifts, forget, status}`.
- **`/metrics` and `/creatives` take `?run=N`** (default latest; legacy rows with NULL `traffic_run` count as run 1). The cache keys include the run.
  - `experiments_metrics.py` aggregates `shift_response` (mean ± 95% CI across episodes for the endpoint, ghost and baselines) and includes the ghost curve.
  - `experiments_metrics.py` splits `perSegment` (optimal arm and per-strategy found-rate) and per-arm true CTR by regime when the run has shifts. The traffic job writes per-regime `per_segment` in the metrics row so no extra query is needed.
  - `experiments_series.py` / `experiments_store.py` add `regimes[]`: for each `[shift_k, shift_k+1)` round range, the per-segment optimal creative and per-creative CTR. This is one extra GROUP BY on the regime index computed from the shift rounds, which are passed as a query parameter.
- **BigQuery migration** (both datasets; run **after** the api deploy, since #239 makes newer code tolerate new columns):
  - `bandit_events.traffic_run INT64`;
  - `bandit_episode_metrics.traffic_run INT64`, `shift_response STRING`;
  - `bandit_experiments.traffic_runs STRING`.

  DDL goes in `deployment/create_bq_tables.sh` and the procedure in `deployment/README.md`.
- **Contracts:** new §10, with §3, §5 and §8 updated.
- **Tests:** shift validation (every bound, unknown creative or segment, too many shifts, shock window too short); run allocation and the env overrides; `?run=` filtering, including legacy NULL rows; parity; regimes aggregation.

## PR D: frontend (`feat/experiment-shift-timeline`), with the `frontend-design` + `dataviz` skills
- **Shift editor** (`src/app/experiments/[experimentId]/shift-timeline.tsx`), in the control panel above Start traffic:
  - **Timeline bar:** a 0–100% bar of the run with event pins; each pin is a native range input, keeping it accessible.
  - **"Add shift" menu:** the 4 kinds, plus 3 presets (Demote the leader at halfway, Mobile surge at 30%, Ad fatigue on the leader 60–75%).
  - **One compact form per event:** a segment select with "Everyone"; a creative select with colour dots and a "Current leader" option for demote; a magnitude slider using the existing `ui/slider`; a two-slider window for a shock. Remove and reorder are included.
  - **A plain-language sentence per event,** e.g. "At 40% of the run, mobile scrollers start preferring The Jackpot Reveal (+1.5 pts)."
  - **Forgetting toggle,** default on when shifts exist, with help copy.
  - **Live before/after preview:** `scenario-preview.ts` gains `applyShifts(base logits, shifts)`, which reuses the pre-sigmoid `base` and skips re-bisecting α. It is golden-tested in PR A. The preview is a mini creative × segment grid per regime ("Before", "After shift 1", …).
- **Charts:**
  - `components/charts/line-chart.tsx` gets a `markers?: {x, label}[]` prop: vertical rule plus label, inside the clip, mentioned in the hover readout.
  - Runs with shifts use a **linear** x axis.
  - The ghost series `linear_ts_unshifted` is drawn dashed as "Linear TS without your shifts" on the reward, regret and % optimal charts.
  - The scoreboard share strips get shift ticks.
  - The segment grid gets a regime switcher (Before / After shift N).
  - Cumulative charts get a recovery band.
- **Where shifts show up in every existing view** (none is left showing a misleading whole-run blend):

  | View (today) | With shifts |
  |---|---|
  | Overview headline + supporting line | Shift-aware: names the shifts and how the endpoint responded; the drift wording generalised |
  | Creative scoreboard (traffic now, click rate, segments won, share strip) | Strip gets shift ticks and per-regime hover. "Segments won" and "click rate" show the **latest regime**, with a small "before: …" line |
  | Creative × segment click-rate grid | Regime switcher (Before / After shift N); "Best" outlines per regime |
  | Creative detail drawer (reading, key numbers, segment bars, click rate over time) | Shift markers on the click-rate-over-time line; segment bars and reading per regime (defaults to latest) |
  | Analysis: cumulative average reward vs optimum | Markers, linear x, ghost line |
  | Analysis: cumulative regret | Markers, linear x, ghost line, recovery band |
  | Analysis: share of rounds on the best creative | Markers, linear x, ghost line (the clearest view of adaptation) |
  | Analysis: where the endpoint sends traffic (arm share) | Markers, linear x (per-window shares already show the switch) |
  | Analysis: winners by reader segment table | One column group per regime (best creative and how often each strategy found it, per regime); whole-run modes removed when there are shifts |
  | Analysis: expected total reward per episode (bars) | Adds a "Linear TS without your shifts" bar |
  | Analysis: impressions and click rates table | True click rate per regime (latest regime by default, with a "before" value) |
  | Per-chart readings + Explain notes | Shift-aware sentences and "how to read this" copy |

  The data comes from `regimes[]` (series), the regime-split `perSegment` (metrics) and `shift_response`. The PR C api returns per-regime versions of the per-segment and true-CTR fields when the run has shifts.
- **Docs and media:** `docs/bandit/README.md` (feature section + a screenshot), README Experiments screenshots, and `docs/screenshots/experiments-journey.gif` extended with a shift step. The PaperBanana architecture diagram for the bandit (if present in docs) gets the control flow: shifts → traffic job → ghost replay.
- **Shift result cards** under the headline, one per shift. They are generated in `experiment-insights.ts` from the aggregated `shift_response`, carry an episode count and an interval, and say "Too early to call" under 5 episodes. No p-values or live significance badges. Example: "After you demoted The Tone Dividend Bailout at round 20,000, the endpoint's best-creative rate fell from 62% to 18% and took 3,400 rounds (± 600) to recover to 50%; without the shift it would have earned 140 more clicks per episode (± 30)."
- **Run selector** (`?run=N`): "Run 2 of 3 · 2 shifts · forgetting on". Explain-mode copy goes in `experiment-explain.ts` / `experiment-help.ts`.
- **Tests (Vitest):** shift-editor payload mapping and validation mirroring the bounds; `applyShifts` golden parity; marker placement; the result-card readings (including thin data); run-selector URL state.
- **Screenshots:**
  - `18-shift-timeline.png`: editor with 2 shifts and the preview.
  - `19-shift-results.png`: markers, ghost line, result cards, regime grid.

  They need a fixture built from a simulator run with shifts.

## Rollout (after merge; nothing active, checked first)
1. Rebuild and push the CPR image (reset `discount`) and the traffic image. Redeploy the traffic job; its IAM binding is kept.
2. Deploy the api with `BANDIT_SERVING_IMAGE` updated. Verify on the `verify` tag, pin it, and remove the tag.
3. Run the BigQuery migration on both datasets.
4. Deploy and pin the web.
5. Live end to end: segment_winners, 3 creatives, shifts (demote the leader at 50%, a shock at 70–80%), forgetting on, 5 episodes. Check the markers, ghost line, result cards and regime grid. Run a second traffic run with forgetting off and compare through the run selector. Stop.

## Verification
- **Python:** `ruff format --check`, `ruff check`, `ty check`, `pytest -n 4` (`GOOGLE_CLOUD_PROJECT=test-project`). `requirements.txt` is unchanged (JAX stays dev-only).
- **Offline:** `uv run python -m bandit.cli simulate --scenario segment_winners --shifts '[{"kind":"demote","at_frac":0.5,"segment":null,"creative_id":"leader","drop_pp":0.015}]' --forget --episodes 5`. Check that regret jumps at the shift, that the ghost doesn't, and that `recovery_rounds` is reported.
- **In-process traffic:** `python -m bandit_traffic.main --in-process --dry-run` with `SHIFTS_JSON`. Rows carry `traffic_run`, and a ghost row exists.
- **Local stack:** api with `BANDIT_DEPLOY_MODE=fake` + `npm run dev`. Edit a timeline, check that the preview updates live, start traffic, and check the run selector.
- **Frontend:** `npm run lint && npm test && npm run build`.

## Conventions
- **Branches:** one per PR, squash-merged after CI.
  - **Never add Co-Authored-By or any AI attribution.**
  - Never stage `.agents/`, `.claude/`, `skills-lock.json` or `notebook-examples/`.
- **Order:** PR A first. B and C run in parallel on A, with C written against the §10 contract. D runs in parallel against the contract and rebases onto A for the golden test.
- **Review:** screenshots go to the user before anything merges or deploys.
- **Deploys:** never while a run or experiment is active. Pin traffic, and no api rollback tag.
