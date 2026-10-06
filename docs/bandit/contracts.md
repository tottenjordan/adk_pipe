# Bandit experiments: interface contracts

This is the single source of truth that PRs 1–5 of
[`docs/plans/2026-10-02-bandit-experiments.md`](../plans/2026-10-02-bandit-experiments.md)
build against. A change to any contract here must update this file in the same PR.

**Terminology:**
- **round:** one impression and one decision.
- **batch:** the rounds between two posterior updates.
- **horizon (T):** the number of rounds per episode.
- **episode:** in per-episode mode (the default), one independent run that starts from a reset posterior and has its own seed. In continuous learning mode (§11) a traffic run is **one** episode of E × T rounds, cut into E **segments** of T rounds for progress and storage.
- **segment:** a T-round slice of a continuous run (§11); its metrics rows use `episode` = the segment index.

**Policy names:** `linear_ts` (the endpoint policy), `ucb1`, `epsilon_greedy`, `beta_bernoulli_ts`, `uniform`, `oracle`.
**Scenarios:** `clear_winner`, `segment_winners`, `drift`.
**CTR modes:** `demo` (about 4% mean) or `realistic` (about 0.8% mean).
**Reward modes:** `click` (0/1) or `engaged` (click × dwell seconds).

## 1. Core Python API (`bandit/`, PR 1). JAX only, never imported by `runserver/` or the agents

```python
# bandit/config.py
@dataclass(frozen=True)
class ArmSpec: creative_id: str; label: str; scores: dict[str, float]; visual_style: str = ""
@dataclass(frozen=True)
class LinTSParams: prior_var: float = 1.0; noise_var: float = 0.25; exploration_scale: float = 0.5
                   propensity_samples: int = 1000; min_propensity: float = 0.02; discount: float = 1.0
@dataclass(frozen=True)
class ExperimentConfig: experiment_id: str; arms: tuple[ArmSpec, ...]; scenario: str
                        ctr_mode: str = "demo"; reward_mode: str = "click"; horizon: int = 20000
                        batch_size: int = 100; episodes: int = 20; seed: int = 0
                        policy: LinTSParams = LinTSParams()
                        scenario_overrides: ScenarioOverrides | None = None   # §9
@dataclass(frozen=True)
class ScenarioOverrides: segment_mix: tuple[float, ...] | None = None; gap_scale: float | None = None
                         judge_wrong: float | None = None; noise_scale: float | None = None
                         drift_at_frac: float | None = None                    # §9
OVERRIDE_BOUNDS: dict[str, tuple[float, float]]                          # §9 bounds, inclusive
def load_experiment_config(src: str | Path | dict) -> ExperimentConfig   # validates 2–4 arms, §9 overrides etc.
def experiment_config_to_dict(cfg) -> dict                               # JSON round-trip of load_*; omits unset overrides
def validate_scenario_overrides(ov, scenario: ScenarioConfig) -> ScenarioOverrides   # ValueError naming the field
def apply_scenario_overrides(sc: ScenarioConfig, ov | None) -> ScenarioConfig
def resolve_scenario(cfg) -> ScenarioConfig     # load_scenario(cfg.scenario) + cfg.scenario_overrides
@dataclass(frozen=True)
class ShiftSpec: kind: str; at_frac: float; segment: str | None = None; creative_id: str | None = None
                 lift_pp: float | None = None; drop_pp: float | None = None
                 segment_mix: tuple[float, ...] | None = None; until_frac: float | None = None
                 ctr_multiplier: float | None = None                            # §10
SHIFT_KINDS: tuple[str, ...]; SHIFT_BOUNDS: dict[str, tuple[float, float]]     # §10, inclusive
MAX_SHIFTS = 4; SHIFT_MIN_WINDOW = 0.02; LEADER = "leader"; LEADER_KINDS = ("demote", "shock")
def shifts_from_dict(data: list | None) -> tuple[ShiftSpec, ...]         # strict snake_case parse (SHIFTS_JSON)
def shifts_to_dict(shifts) -> list[dict]                                 # JSON round-trip of shifts_from_dict
def validate_shifts(shifts, scenario: ScenarioConfig, arms, ctr_mode) -> tuple[ShiftSpec, ...]  # ValueError "shifts[i].<field>"
def default_shift_discount(ctr_mode, batch_size, horizon) -> float      # discount_for_memory(horizon / 8, batch_size)
LEARNING_MODES = ("per_episode", "continuous"); MAX_CONTINUOUS_ROUNDS = 2_000_000   # §11
def validate_continuous_run(episodes, horizon, batch_size) -> int        # §11: total rounds E·T, ValueError naming the field
def continuous_world_config(cfg) -> ExperimentConfig                     # §11: cfg with horizon E·T, episodes 1 (world of one long run)

# bandit/environment.py + bandit/simulate.py (simulator; §10)
def build_environment(cfg, *, scenario=None, shifts=()) -> Environment  # shifts resolved in time order; same keys
def resolved_shifts(env) -> list[dict]                                   # concrete rounds / creatives / targets
def sample_contexts(key, model, n, t=None) -> (segments, levels, X)      # t (n,): latest started mix shift applies
def run_segment(policy, env, key, start_batch, num_batches, batch_size, init_state=None) -> (outs, final_state)  # §11

# bandit/metrics.py (§10)
def shift_response(out, shift_rounds, window=None, recovery_window=None, recovery_level=0.8) -> list[dict]
def merge_checkpoints(checkpoints, shift_rounds, horizon) -> list[int]
def regime_stats(out, boundaries, arm_ids, segment_names) -> list[dict]  # per-regime per_segment + true_ctr

# bandit/features.py
FEATURE_SPEC_VERSION: str            # e.g. "ctx-v1"
def feature_names() -> list[str]     # length d (bias first)
def encode_context(ctx: dict) -> np.ndarray      # shape (d,), raises ValueError on unknown/sensitive keys
def encode_batch(ctxs: list[dict]) -> np.ndarray # shape (n, d)

# bandit/linear_ts.py  (pure, jit-able)
class LinTSState(NamedTuple): precision: Array  # (K,d,d)
                              b: Array          # (K,d)
                              n: Array          # (K,) pulls
                              step: Array       # () rounds seen
def init_state(num_arms: int, dim: int, prior_var: float) -> LinTSState
def posterior_mean(state) -> Array                                   # (K,d)
def select(key, state, X, params, eligible=None) -> tuple[Array, Array]   # arms (n,), scores (n,K)
def select_with_noise(state, X, z, params, eligible=None) -> tuple[Array, Array]  # select with its N(0,1) draws z (n,K) given
def propensities(key, state, x, params, eligible=None) -> Array      # (K,) sums to 1, floor-clipped
def update(state, arms, X, rewards, params) -> LinTSState            # batched; params.discount<1 decays toward prior
```

## 2. Endpoint instances (CPR `BanditPredictor`, PR 2; client in PR 3)

Requests go to `POST …:predict` with `{"instances": [...], "parameters": {...}}`, and responses come back as `{"predictions": [...]}`, in order and one per instance. A bad instance returns
`{"type":"error","request_id":..., "error":"..."}` without failing the rest of the batch.

| `type` | Instance fields | Prediction fields |
|---|---|---|
| `decision` | `request_id`, `ts`, `context` (§4), `eligible_arms?` (creative ids), `batch?` + `row?` (ints ≥ 0, both or neither; see below) | `request_id`, `type`, `chosen_arm` (creative_id), `arm_index`, `propensity`, `arm_probabilities` {creative_id: p}, `explored` (bool: chosen ≠ posterior-mean argmax), `model_version`, `policy:"linear_ts"`, `episode`, `step`, `latency_ms` |
| `reward` | `request_id`, `arm` (creative_id), `reward` (float), `clicked` (0/1), `dwell_s?` | `request_id`, `type`, `accepted` (bool; false for a duplicate or unknown id), `model_version` |
| `reset` | `episode` (int), `seed` (int), `discount?` (float in [0.95, 1.0]; §10), `policy_key?` + `batch_size?` (both or neither; see below) | `type`, `episode`, `model_version`, `discount` (the γ the episode's updates use) |
| `state` | — | `type`, `episode`, `step`, `model_version`, `pulls` {creative_id: n}, `posterior_mean` {creative_id: [d floats]}, `feature_spec_version` |

`parameters` (optional, bounded): `exploration_scale` [0.1, 5], `propensity_samples` [100, 5000].

A `reset`'s optional `discount` (added 2026-10-05 for the §10 forgetting toggle; bounds `bandit.config.RESET_DISCOUNT_BOUNDS`) replaces `policy.discount` for every update until the next reset; a reset without it (or with `null`) restores the `experiment.json` value. It follows the per-request `parameters` pattern: a finite non-bool number, quantized to 0.001 (a static jit argument, so the compile cache stays bounded), and out of bounds or non-numeric is a per-instance `error` that leaves the current episode untouched. It is checkpointed (`latest.json` `discount`), so a restart mid-run keeps it. A predictor image older than this ignores the field (the reset still succeeds, at the config γ).

**Policy stream (2026-10-05).** A `reset` can carry the simulator's policy stream for the episode: `policy_key` is the `jax.random.key_data` of `bandit.simulate.episode_streams(episode_key)[2]` (2 uint32 words for the default threefry key; integral floats accepted), and `batch_size` (int in [1, 10000]) is the simulator batch size. While it is set, a decision with `batch` and `row` (its batch index and its row within that batch, `row < batch_size`) is drawn exactly like `simulate._episode` draws round `batch × batch_size + row`: key `fold_in(k_pol, batch)` → `split` → `k_sel`, and noise `normal(k_sel, (batch_size, K))[row]` (`_decide_rows_kernel`, on top of `linear_ts.select_with_noise`). So the chosen arm depends only on (batch, row, posterior), whatever the padding, request splitting or retries. Accepted rewards are applied as one `update` per run of rewards from the same decision batch. A run that continues the previous update's batch (a batch whose rewards were split over requests) doesn't decay again, so γ is applied once per simulator batch. Rewards are divided by the reward scale in float32 inside the update kernel, the simulator's own op. Errors are per instance: a bad `policy_key`/`batch_size` leaves the episode untouched; a decision with only one of `batch`/`row`, a negative value or `row ≥ batch_size` is rejected. Backward compatible both ways: a reset without the fields (an older traffic image) keeps the legacy key (`fold_in(key(seed), call counter)`, padded draws) and ignores decision `batch`/`row`, and an older predictor image ignores all four fields. The stream is checkpointed (`latest.json` `policy_stream`). It needs a predictor image rebuild, like the reset `discount`.

Reward instances carry the **unscaled** reward (`click` mode: 0/1; `engaged` mode: click × dwell seconds, with `dwell_s` set). Like the simulator, the predictor divides engaged rewards by the scenario's `dwell_base_s` before its `update`. A reward is accepted only for a pending decision whose `arm` matches the chosen arm.

The traffic job (PR 3) sends `request_id = "{experiment_id}-r{traffic_run}-e{episode}-r{round}"`, where `traffic_run` is the 1-based traffic run (§10; always present, run 1 included, since 2026-10-05; earlier runs sent `{experiment_id}-e{episode}-r{round}`) and `round` is the 0-based round index within the episode (also `bandit_events.round`). It splits a batch into requests of at most 500 instances and about 1.2 MB, sends one `reset` per episode with `seed = (experiment seed × 1000003 + episode) mod 2³¹` plus the episode's `policy_key`/`batch_size` (`bandit_traffic.traffic.policy_stream_fields`), gives every decision its `batch` and `row`, and sends a batch's rewards only after all of that batch's decisions. A **continuous** run (§11) sends exactly **one** `reset` for the whole run (`episode` 0, episode 0's seed and policy stream); decision `batch` is then the **global** batch index over the run (0 … E·T/batch_size − 1) and `round` the global round. The predictor needs no change for this: `batch` has no upper bound, `model_version` (`…-e0-v{n}`) keeps counting, the pending/seen maps are bounded and a batch's rewards follow its decisions, and checkpoints keep their update-count / time cadence.

The CPR container is always deployed with `VERTEX_CPR_WEB_CONCURRENCY=1`, i.e. one worker process holding a single in-memory posterior. `AIP_STORAGE_URI` holds `experiment.json` (a §1 `experiment_config_to_dict`) and `checkpoints/`.

## 3. BigQuery tables (DDL in `deployment/create_bq_tables.sh`, PR 4; written by PR 3 and PR 4)

**`bandit_experiments`**, one row per experiment, upserted by the api with MERGE on `experiment_id`:
experiment_id STRING, user_id STRING, session_id STRING, app_name STRING, created_at TIMESTAMP,
updated_at TIMESTAMP, status STRING, scenario STRING, ctr_mode STRING, reward_mode STRING,
arms STRING (JSON list of §5 arm objects), config_uri STRING, model_resource STRING, endpoint_id STRING,
deployed_model_id STRING, ttl_expires_at TIMESTAMP, stopped_at TIMESTAMP, traffic_execution STRING,
progress STRING (JSON {episodes_done, episodes_total}), error STRING,
scenario_overrides STRING (§9 snake_case JSON; set only when the experiment has overrides,
added 2026-10-04 by `ALTER TABLE … ADD COLUMN IF NOT EXISTS`, see deployment/README.md),
deploy_lease_until TIMESTAMP, deploy_lease_owner STRING (the §6 single-deployer lease; written
only by the api's conditional lease UPDATEs, never by the MERGE; added 2026-10-05 the same way),
traffic_runs STRING (§10 JSON list, one snake_case entry per numbered traffic run:
`{run, started_at, episodes, horizon, forget, shifts, learning, execution, status?}`, where `shifts` is the
job form, `learning` the §11 mode (entries written before 2026-10-06 lack it and read as
`"per_episode"`) and `status` is frozen on an entry when the next run starts; added 2026-10-05; an
api ahead of the migration drops the column from its MERGE with an error log)

**`bandit_events`**, one row per round for the endpoint policy, written by the traffic job (`insertId = request_id`).
It is partitioned by DATE(ts) and clustered on experiment_id:
experiment_id STRING, episode INT64, round INT64, batch INT64, request_id STRING, ts TIMESTAMP,
policy STRING, segment STRING, context STRING (JSON), arm STRING, propensity FLOAT64, reward FLOAT64,
clicked INT64, dwell_s FLOAT64, p_chosen FLOAT64, p_optimal FLOAT64, optimal_arm STRING,
regret FLOAT64, model_version STRING, latency_ms FLOAT64, traffic_run INT64

**`bandit_episode_metrics`**, one row per (episode, policy), written by the traffic job:
experiment_id STRING, episode INT64, policy STRING, horizon INT64, total_reward FLOAT64,
total_clicks INT64, cumulative_regret FLOAT64, pct_optimal FLOAT64, steps_to_converge INT64,
curve STRING (JSON), arm_share STRING (JSON), per_segment STRING (JSON), arm_stats STRING (JSON),
created_at TIMESTAMP, traffic_run INT64, shift_response STRING (JSON), regimes STRING (JSON)

`traffic_run` (events and metrics, added 2026-10-05 by `ALTER TABLE … ADD COLUMN IF NOT EXISTS`, see
deployment/README.md) is the 1-based traffic run (§10) that wrote the row; rows written before it
have NULL, which readers count as run 1. `shift_response` and `regimes` (metrics only, same
migration) are set only for a run with shifts (§10), else NULL. The metrics streaming `insertId` is
`{experiment_id}-r{traffic_run}-e{episode}-{policy}`.

The JSON payloads:
- `curve`: `{"checkpoints":[r1..rm], "cum_avg_reward":[...], "cum_regret":[...], "pct_optimal":[...]}`. Checkpoints are about 50 log-spaced round indices, the same for every policy and episode in one traffic run. A run with shifts (§10) uses 50 linear ones merged around each shift round (`merge_checkpoints`). A continuous run (§11) uses 50 linear segment-local ones per segment (merged around shift rounds inside the segment) and adds `"segment_start"`, the global 0-based round where the segment starts.
- `arm_share`: `{creative_id: [share of pulls in each checkpoint window]}`.
- `per_segment`: `{segment: {"optimal_arm": creative_id, "pct_optimal": f, "avg_reward": f, "rounds": n}}`.
- `arm_stats`: `{creative_id: {"impressions": n, "clicks": n, "estimated_ctr": f, "true_ctr": f}}`.
- `shift_response` (runs with shifts): the §10 `bandit.metrics.shift_response` list for this episode and policy, one entry per shift in time order: `{"round", "pct_optimal_before", "pct_optimal_after", "regret_rate_before", "regret_rate_after", "recovery_rounds"}`. Since 2026-10-06 the traffic job also merges that shift's resolved record (§10, `bandit_traffic.traffic.resolved_shift_fields`) into each entry: `"index"`, `"kind"`, `"end_round"`, `"segment"`, `"creative_id"` (concrete, `"leader"` resolved), `"requested_creative_id"` and `"targets"` (`[{"segment", "ctr_before", "ctr_after"}]`; mix: null creative/segment fields and `[]`). No new column: this is how the api (which has no `bandit`) learns the resolution. Rows written earlier lack these keys.
- `regimes` (runs with shifts): `[{"start", "end", "per_segment", "true_ctr"}]`, the §10 `bandit.metrics.regime_stats` split of `per_segment` (same shape as above) and of the true CTR per creative `{creative_id: f}` at the shift rounds and shock end rounds (`[start, end)`, 0-based rounds).

## 4. Context object (decision `context`, `bandit_events.context`)

Synthetic only, and coarse:
```json
{"devicetype": "mobile|desktop|tablet", "os": "ios|android|other", "connectiontype": "wifi|cellular",
 "region": "northeast|midwest|south|west", "age_bucket": "21-34|35-54|55+",
 "daypart": "morning|afternoon|evening|night", "weekend": true,
 "topic_matches_trend": true, "interest_matches_product": false, "freq_24h": "0|1|2+"}
```
No IDs, IP addresses, latitude/longitude, ZIP code, city, or sensitive categories. Adults only (21+ buckets).

## 5. REST API (`runserver/experiments.py`, PR 4; client `frontend/src/lib/experiments.ts`, PR 5)

All routes are user-scoped. The proxy rewrites `userId` / the `{user_id}` segment, and the backend checks the owner, returning 404 for a foreign experiment.

- `POST /experiments`
  - Body: `{userId, appName, sessionId, creativeIndices: number[], scenario, ctrMode, rewardMode, ttlMinutes?, scenarioOverrides?}`.
  - `scenarioOverrides` (§9, optional): `{segmentMix?, gapScale?, judgeWrong?, noiseScale?, driftAtFrac?}`. `null` fields count as unset; empty or omitted writes no `scenario_overrides`.
  - Success: **201** `{experimentId, status: "deploying"}`.
  - Errors: 400 for an empty or invalid selection (fewer than 2 arms, or an index out of range) or invalid `scenarioOverrides` (`detail.field` names the camelCase field), 409 `{detail:{reason:"active_experiment"}}` if the user already has an active experiment.
- `GET /experiments/{user_id}` returns `{experiments: ExperimentSummary[]}`, newest first.
- `GET /experiments/{user_id}/{experiment_id}` returns `ExperimentSummary`, with status reconciled against the endpoint and the TTL.
- `GET /experiments/{user_id}/{experiment_id}/metrics[?run=N]` returns `ExperimentMetrics` for traffic run `N` (default the latest; §10). It is empty (`episodes: 0`) until traffic runs. A `run` outside `[1, latest]` (or not an integer) is a 400 `invalid_run`.
- `POST /experiments/{user_id}/{experiment_id}/traffic`
  - Body: `{episodes: 1..100, horizon?: 1000..400000, shifts?: Shift[] (≤ 4, §10), forget?: boolean | null, learning?: "per_episode" | "continuous"}`.
  - `learning` (§11, default `"per_episode"`): `"continuous"` keeps the endpoint's posterior across the run's episodes (then called segments). A continuous run is limited to `episodes × horizon ≤ 2,000,000` rounds (`bandit.config.MAX_CONTINUOUS_ROUNDS`) and needs `horizon` to be a multiple of the experiment's `batch_size`.
  - Success: `{status: "running_traffic", execution, run}` (`run` = the allocated 1-based run number).
  - Errors: 400 `invalid_shifts` (with `detail.field`, e.g. `shifts[1].untilFrac`) or `invalid_forget`; 400 `invalid_learning` with `detail.field`: `learning` (not one of the two modes), `episodes` (a continuous run over 2,000,000 rounds; a missing `horizon` counts as the experiment default) or `horizon` (not a multiple of `batch_size`); 409 unless status is `ready`.
- `POST /experiments/{user_id}/{experiment_id}/stop` returns `{status: "stopping" | "stopped"}`.

**Status enum:** `deploying → ready → running_traffic → ready → stopping → stopped`; also `failed` and `expired` (the TTL reaper).

```ts
type Arm = { creativeId: string; index: number; label: string; conceptName: string;
             imageUri: string | null; scores: Record<string, number>; overallScore: number | null };
type ExperimentSummary = { experimentId: string; userId: string; sessionId: string; appName: string;
  createdAt: string; updatedAt: string; status: Status; scenario: string; ctrMode: string;
  rewardMode: string; ttlExpiresAt: string | null; arms: Arm[]; endpointId: string | null;
  trafficExecution: string | null; progress: { episodesDone: number; episodesTotal: number } | null;
  error: string | null;
  scenarioOverrides: ScenarioOverrides | null;                       // §9; null = preset as-is
  policyDiscount: number;                                            // §7
  trafficRuns: TrafficRun[] };                                       // §10; oldest first, [] before any traffic
type TrafficRun = { run: number; startedAt: string | null; episodes: number;
  horizon: number | null;          // the run's horizon (the body's, else the experiment default)
  shifts: Shift[];                 // §10 REST form as validated (camelCase; [] = no shifts)
  forget: boolean;
  learning: "per_episode" | "continuous";                            // §11; older runs "per_episode"
  status: "running" | "finished" | "failed" | "stopped" };
  // An experiment that ran traffic before runs were numbered shows a legacy run 1
  // (startedAt/horizon null, shifts [], forget false, learning "per_episode").
type ScenarioOverrides = { segmentMix?: number[]; gapScale?: number; judgeWrong?: number;
  noiseScale?: number; driftAtFrac?: number };                       // only the fields that were set
type Band = { mean: number[]; lo: number[]; hi: number[] };          // mean ± 95% CI across episodes
type ExperimentMetrics = { experimentId: string; episodes: number; horizon: number | null;
  checkpoints: number[]; policies: string[];                         // order: linear_ts first, oracle last
  curves: Record<string, { cumAvgReward: Band; cumRegret: Band; pctOptimal: Band }>;
  totals: Record<string, { mean: number; std: number }>;             // expected total reward ± std
  armShare: Record<string, number[]>;                                // linear_ts only, by creativeId
  perSegment: Record<string, { optimalArm: string;
                               policies: Record<string, { pctOptimal: number; avgReward: number }> }>;
  arms: { creativeId: string; impressions: number; estimatedCtr: number; trueCtr: number }[];
  run: number;                                                       // §10: the run these numbers are from
  shiftResponse: Record<string, ShiftResponse[]>;                    // §10; {} without shifts
  regimes: MetricsRegime[];                                          // §10; [] without shifts
  shiftCost?: ShiftCost;                                             // §10; omitted without ghost rows
  resolvedShifts?: ResolvedShift[];                                  // §10; omitted for older runs / no shifts
  // §11, continuous runs only (a per-episode response has none of these keys):
  learning?: "continuous"; segmentHorizon?: number | null; segmentStarts?: number[];
  continuousSummary?: ContinuousSummary };                           // §11 shape and rules
type ResolvedShift = { index: number;                                // position in the requested list; time order
  kind: "promote" | "demote" | "mix" | "shock"; round: number; endRound: number | null;
  segment: string | null; creativeId: string | null;                 // concrete ("leader" resolved); null for mix
  requestedCreativeId: string | null;                                // as requested (may be "leader")
  targets: { segment: string; ctrBefore: number | null; ctrAfter: number | null }[] };
type ShiftCost = { episodes: number;                                 // episodes with both a linear_ts and a ghost row
  clicksPerEpisode: Stat | null;                                     // paired ghost − linear_ts total_clicks
  rewardPerEpisode: Stat };                                          // paired ghost − linear_ts total_reward
type Stat = { mean: number; lo: number; hi: number };               // mean ± 95% CI across episodes
type ShiftResponse = { round: number; episodes: number;              // one per shift, in time order
  pctOptimalBefore: Stat; pctOptimalAfter: Stat; regretRateBefore: Stat; regretRateAfter: Stat;
  recoveryRounds: Stat | null;                                       // over the episodes that recovered
  recoveredEpisodes: number };
type MetricsRegime = { start: number; end: number;                   // rounds [start, end)
  perSegment: ExperimentMetrics["perSegment"];                       // same shape and rules, this regime only
  arms: { creativeId: string; trueCtr: number }[] };                 // mean regime true CTR (linear_ts rows)
```

- **Policies:** a run with shifts adds the ghost policy `linear_ts_unshifted` (Linear TS on the same draws without the shifts, §10). It is ordered right after `linear_ts` and appears in `curves`, `totals`, `perSegment` and `shiftResponse`, but never votes on an `optimalArm` (its winners come from the unshifted world).
- **`shiftCost`** (2026-10-05): what the run's shifts cost the endpoint, as the **paired** per-episode difference `linear_ts_unshifted` − `linear_ts` of `total_clicks` and `total_reward`, matched by `episode` (unpaired rows on either side are ignored), with a 95% Student-t interval on the mean difference (not clamped; negative = the shifts helped; one pair collapses to the mean). Pairing is valid because the endpoint and the ghost replay the same episode keys, and it removes the between-episode variance, so the interval is much tighter than comparing the two `totals`. `clicksPerEpisode` is `null` when no pair has `total_clicks`. The key is omitted when there is no pair (no shifts, or ghost rows not written yet).
- **`resolvedShifts`** (2026-10-06): the run's shifts as the traffic job resolved them, camelCased from the resolved fields on the rows' `shift_response` entries (§3). They are identical across rows; the api takes the first row that has them (endpoint first), or, if rows disagree, the most common version (and logs a warning). Omitted when no row has them (runs before 2026-10-06, or no shifts). The frontend names a `"leader"` shift's creative from it (matched by time-order position, kind and round), falling back to the old heuristic (highest regime true CTR in the period ending at the shift) for older runs.
- **Whole-run fields are unchanged** with shifts (`perSegment`, `arms[].trueCtr` still span the whole run); the per-regime split is the additive `regimes` list. `regimes` comes from the metrics rows' `regimes` JSON (no extra query); `start`/`end` come from the first row that carries them (the endpoint's).

## 6. Resolved decisions (PR 4 + PR 5, 2026-10-02)

- **IDs:** `experimentId` is 16 lowercase hex characters (`[0-9a-f]{16}`). The frontend proxy accepts `^[a-z0-9][a-z0-9-]{2,63}$`. `endpointId` / `endpoint_id` and the job's `ENDPOINT_ID` are the **full resource name** `projects/P/locations/R/endpoints/N`.
- **Arms:** `creativeIndices` index `final_visual_concepts.visual_concepts` (the results page's `Proof.index`).
  - A selection must have 2–4 unique, in-range indices.
  - `Arm.label` is the headline; charts display `conceptName`.
  - Arm `scores` keys are the 12 judge dimensions (score/10, clipped to 0–1) plus `ad_copy_overall` and `visual_overall`. `overallScore` is the mean of the two overall scores.
  - Ad-copy evals are matched headline first, then id, then position.
  - `imageUri` is null when `_generated_artifact_keys` exists and doesn't include the concept's image.
- **Error reasons** (`detail.reason`):
  - 400: `too_few_arms`, `too_many_arms`, `duplicate_index`, `index_out_of_range`, `no_creatives`, `invalid_scenario`, `invalid_ctr_mode`, `invalid_reward_mode`, `invalid_scenario_overrides` (with `detail.field`, §9), `invalid_episodes`, `invalid_horizon`, `invalid_shifts` (with `detail.field`, §10), `invalid_forget`, `invalid_run`;
  - 409: `active_experiment`, `not_ready`;
  - 404: `session_not_found`, `not_found`;
  - 502: `config_write_failed` (also the §10 `runs/{n}.json` record), `traffic_start_failed`.
- **Lifecycle:**
  - The api moves `running_traffic` back to `ready` once `progress.episodes_done >= episodes_total` or the job execution has finished. A failed job sets `error` and keeps the status `ready`. The check runs on a detail GET, in the background on `/metrics` and `/creatives` GETs, on the reaper's full pass (every 5 min, which also adopts `running_traffic` rows another process started) and on a light pass every 60 s that point-reads only the `running_traffic` rows the process is watching (no query when idle; 2026-10-05, after a frozen browser poll left a finished run `running_traffic` for 43 min). Idempotent: the transition re-reads the row and is skipped if its `traffic_execution` changed since the check.
  - Stop waits up to 2 s, so it returns `stopped` if teardown finishes in time, otherwise `stopping`.
  - `ttlMinutes` is clamped to 10–480.
  - Store updates only rewrite the columns that changed, so the api never overwrites the `progress` written by the traffic job. A partial update ignores (logs once) columns in the fetched row that this code doesn't know, so an older revision keeps working on a table a newer one migrated; writing an unknown column still fails.
  - **Single deployer (2026-10-05):** a deploy (initial or resumed) runs only while its api process holds the row's lease: `UPDATE … SET deploy_lease_until = CURRENT_TIMESTAMP() + 180 s, deploy_lease_owner = '<K_REVISION>/<nonce>' WHERE experiment_id = @id AND status = 'deploying' AND (deploy_lease_until IS NULL OR deploy_lease_until < CURRENT_TIMESTAMP())`, won iff `num_dml_affected_rows = 1`. The holder renews it every 60 s and clears it when the deploy ends (success or failure). The reconcile GET and the reaper only resume a `deploying` row with no local task and no unexpired lease, so other instances or old revisions back off; a dead holder's lease expires and the next pass resumes. As a second layer the deployer adopts the oldest model/endpoint labelled `app=trend-trawler,experiment=<id>` before creating one (ids recorded on the row win), skips the deploy when the endpoint already has a deployed model, and teardown also deletes labelled extras.
- **Default horizons** (frontend): `clear_winner` 20k, `segment_winners` and `drift` 40k in demo mode; ×10 (max 400k) in realistic mode.
- **Local development:** `BANDIT_DEPLOY_MODE=fake` (in-memory store, fake deployer, fake jobs; `BANDIT_FAKE_DEPLOY_SECONDS`, default 2).

## 7. PR 1 + PR 2 decisions (2026-10-02)

**PR 1 (`bandit/` core):**
- **Features:** `d = 19` (bias plus one-hot groups with the first level of each dropped as the reference). A context must carry **exactly** the 10 §4 keys: missing, unknown or sensitive keys and unknown levels raise `ValueError`. An integer `freq_24h` is accepted (`>= 2` maps to `"2+"`).
- **Posterior parameterization:** precision form, `Λ_k = I/τ² + Σ x xᵀ / σ²` and `b_k = Σ r x / σ²`. The mean is `μ_k = Λ_k⁻¹ b_k`, and draws use covariance `s² Λ_k⁻¹` (`s = exploration_scale`).
- **Discount:** `γ` is applied once per `update` call (per batch, not per round): `Λ ← γΛ + (1−γ)I/τ²`, `b ← γb`. Pull counts `n` and `step` are never discounted.
- **Propensity floor:** `min_propensity` only affects the **logged** propensities (water-filled to the floor and renormalized over eligible arms). `select` never applies it.
- **Noise variance calibration:** the `LinTSParams.noise_var = 0.25` default over-explores at ad CTRs (about 6× the Bernoulli variance at 4%). The calibrated `σ²` is `p(1−p)` for `click` and `2p − p²` for `engaged`, where `p` is the scenario's `target_ctr[ctr_mode]` (for example `clear_winner` demo 0.0467, `segment_winners` demo 0.04 / realistic 0.008). It is rounded to 6 decimal places. The source of truth is `bandit.config.default_noise_var` / `scenario_noise_var`.
  - The api (`runserver/experiments.py`) always writes `policy.noise_var` explicitly. It duplicates the formula and the scenario CTR table, and a test checks parity with `bandit`.
  - `BanditPredictor` fills in the same value when `experiment.json` has no `policy.noise_var` or it is null.
- **Discount calibration (2026-10-05):** the endpoint forgets old evidence only in `drift`. `policy.discount = exp(−batch_size / N)` (3 decimals), where `N` is an effective memory of 1/8 of the preset horizon: 5k rounds in demo mode and 50k in realistic mode, so `γ = 0.98` and `0.998` at `batch_size = 100`. Every other scenario keeps `γ = 1` (a discount raises their regret). Source of truth: `bandit.config.DISCOUNT_MEMORY_ROUNDS` / `discount_for_memory` / `default_discount`; sweep in `docs/experiments/bandit-simulation.md`.
  - The api always writes `policy.discount` explicitly (it duplicates the table and the formula; a test checks parity with `bandit`). It also records a forgetting endpoint's γ in the `bandit_experiments.policy_discount` column (written only when `γ < 1`, so non-drift deploys never name it), and the §5 summary reports it as `policyDiscount` (`1.0` when the column is null: every experiment before this change ran with full memory).
  - `BanditPredictor` uses `policy.discount` for every reward batch. The traffic job sends one reward request per 100-round batch, and the predictor applies each request's run of rewards as one `update`, so γ is applied once per batch as in the simulator.
  - Even discounted (and with the tuned exploration below), LinTS doesn't beat UCB1 in `drift`: the 19-coefficient-per-arm model re-learns more slowly than one rate per arm. The frontend explains this when the endpoint trails (`experiment-insights.ts` `why`).
- **Exploration calibration (2026-10-05):** `exploration_scale = 0.5` for every scenario and ctr mode (it was 1.0). It is the `LinTSParams` default (`bandit.config.DEFAULT_EXPLORATION_SCALE`), so the simulator, the api and an `experiment.json` without `policy.exploration_scale` all use it. In the sweep (`docs/experiments/bandit-simulation.md`, "Exploration sweep": 3 scenarios × 2 ctr modes × 3/4 arms) it lowers LinTS regret in all 12 cells: 4–16 % in the stationary scenarios and 14–24 % in `drift` (with the unchanged discount), with no lock-in on a wrong creative. Smaller scales (≤ 0.35) gain a little more on average but double or triple the spread across episodes. The discount table is unchanged: at `s = 0.5` the drift optimum stays flat around γ = 0.98 / 0.998.
  - The api always writes `policy.exploration_scale` (`runserver/experiments.py` `DEFAULT_POLICY` duplicates the default; a test checks parity with `bandit`). No store column: `experiment.json` records it.
  - `BanditPredictor` samples with `policy.exploration_scale`. A request's `parameters.exploration_scale` (§2) overrides it for that request only, clamped to [0.1, 5] and quantized to 0.1; the traffic job sends none.
- **Engaged reward scale:** in `engaged` mode, rewards are divided by the scenario's `dwell_base_s` (30 s) before every policy update, both in the simulator (`TrueModel.reward_scale`) and in the predictor (`bandit.config.reward_scale`). Clients send **unscaled** rewards (click × dwell seconds).
- **`ArmSpec.overall` fallback:** use `scores["overall"]` if present. Otherwise use the mean of `ad_copy_overall` / `visual_overall` (api arms, §6). Otherwise use the mean of all scores, and 0.5 if there are none.
- **Policy spec strings:** `name` or `name:key=value[,key=value]`, for example `linear_ts:discount=0.98` or `ucb1:c=0.01`. The spec string becomes the policy label in the output. The CLI aliases are `lints`, `egreedy` and `bbts`. The CLI's `--policies` separates specs with `,` or `;`; a bare `key=value` continues the previous spec's options (`lints:discount=0.97,exploration_scale=0.1,ucb1` is two policies).
- **Extra episode-row keys:** beyond the §3 columns, simulator rows also carry `realized_regret`, `optimal_avg_reward`, `suboptimal_pulls` {creative_id: n} and `regret_by_arm` {creative_id: f}.

**PR 2 (`bandit_serving/`, CPR):**
- **Response shape:** CPR's `PredictionHandler` serializes whatever `postprocess` returns, so `postprocess` returns the whole `{"predictions": [...]}`.
- **Request validation:**
  - A malformed envelope is a 400: the body is not an object, `instances` is not a list, `parameters` is not an object, or there are more than **1000 instances**.
  - Anything else wrong with a single instance becomes a per-instance `error`. That includes a non-object instance, an unknown `type`, a bad context, unknown or empty `eligible_arms`, an unknown reward `arm`, a non-finite `reward`, `clicked ∉ {0,1}`, a negative or non-integer reset `episode`, and a `request_id` that is missing or longer than 256 characters.
- **Order:** instances are processed in order. Each maximal run of consecutive `decision` (or `reward`) instances is batched, so a `state` or `decision` that follows a reward in the same request sees the update.
- **Rewards:**
  - A reward is accepted only for a pending decision whose chosen arm equals `arm`. Otherwise the ack has `accepted:false` plus a `reason`: `duplicate`, `unknown request_id`, or `arm does not match the decision`.
  - The pending map (200k) and the seen-id set (400k) are bounded and evict oldest first. Neither is checkpointed, so rewards for decisions made before a restart are rejected.
  - A request whose rewards are all rejected does not bump the version.
- **`model_version`** is `{experiment_id}-e{episode}-v{n}`, where `n` is the number of reward batches applied this episode. A decision reports the version it was sampled from. `step` is the number of rewarded rounds the posterior has seen.
- **Reset:** fresh prior, PRNG `key(seed)` (per-call `fold_in`), empty pending/seen maps, then an immediate checkpoint. A continuous traffic run (§11) sends one reset per run, so the posterior then accumulates over all E × T rounds.
- **Serving parity (2026-10-05):** driven by the traffic job, the endpoint's LinTS **is** the simulator's: same params, reward scaling (float32, in the update kernel), one `update` (one γ) per batch, and, through the reset policy stream (§2), the same random draws. `tests/test_bandit_endpoint_parity.py` drives the real `BanditPredictor` through `TrafficRunner` (click and engaged, γ = 0.98, and 37-instance request splitting) and checks that the served arms match `simulate.run_episodes` round for round, so a live `linear_ts` episode equals a local replay of the same episode key. **Continuous parity (§11):** a continuous run is one long episode with episode 0's key and policy stream and the global batch index, so the endpoint's arms equal `simulate.run_segment` chained over the segments (equivalently `run_episodes` with horizon E·T), round for round (the `continuous` case in `tests/test_bandit_endpoint_parity.py`). Before the policy stream there was no algorithmic gap either: the reported 0.254 vs 0.317 % optimal (in-process fake, `segment_winners` episodes 0–7) was policy-PRNG noise on heavy-tailed episodes (an early lock-in can drop an episode below uniform). Over 64 episodes the regret difference was −0.00 ± 0.59, and the three live experiments landed within about 1 SE of a local replay, with the replayed UCB1 matching exactly.
- **Request `parameters`:** clamped to the §2 bounds and quantized (`exploration_scale` to 0.1, `propensity_samples` to 100) so the jit cache stays bounded.
- **Checkpoints:** `{AIP_STORAGE_URI}/checkpoints/{model_version}.npz` (`precision`, `b`, `n`, `step`) plus `checkpoints/latest.json` (`experiment_id`, `model_version`, `npz`, `episode`, `seed`, `n_updates`, `calls`, `feature_spec_version`, `saved_at`).
  - A checkpoint is written every `BANDIT_CHECKPOINT_EVERY` (50) reward batches or `BANDIT_CHECKPOINT_SECONDS` (120 s), checked on update, and on every reset.
  - Writes go through a background thread. A failure is logged and never blocks serving.
  - On load, a `latest.json` for a different `experiment_id` (or with mismatched shapes) is ignored.

## 8. Per-creative time series (`GET /experiments/{user_id}/{experiment_id}/creatives`, 2026-10-03)

Binding for the frontend client. Owner and 404 semantics are identical to the `/metrics` route (`detail.reason: "not_found"` for a missing or foreign experiment).

```ts
type CreativeSeries = {
  experimentId: string;
  episodes: number;                            // distinct episodes in bandit_events (linear_ts)
  horizon: number | null;                      // MAX(round) + 1; null when there are no events
  windows: { start: number; end: number }[];   // equal round windows [start, end) over the horizon
  creatives: {
    creativeId: string;
    share: number[];          // per window: mean over episodes of this creative's share of linear_ts impressions; sums to ~1 across creatives
    ctr: (number | null)[];   // per window: pooled clicks / impressions; null when the creative had no impressions in that window
    cumClicks: number[];      // per window end: mean cumulative clicks per episode
    impressions: number;      // total over all episodes
    clicks: number;
    trueCtr: number | null;   // simulator truth: mean p_chosen when this creative was chosen
    segmentsWon: string[];    // segments whose most frequent bandit_events.optimal_arm is this creative (ties → lowest id), sorted
    finalShare: number;       // share in the last window
    segments: {
      segment: string;        // bandit_events.segment
      impressions: number;    // total over all episodes
      clicks: number;         // SUM(clicked)
      ctr: number | null;     // clicks / impressions; null when 0 impressions
      trueCtr: number | null; // mean p_chosen for this creative in this segment; null when 0 impressions
      isBest: boolean;        // segment ∈ this creative's segmentsWon
    }[];                      // every segment seen in the experiment's events (zero-impression rows included, so every creative has the same list), sorted by name
    missedClicks: number;     // mean per episode of SUM(regret) over this creative's rows (expected clicks lost vs the best creative for those readers)
    engagedSecondsPer1k: number | null; // 1000 · SUM(dwell_s) / impressions when the experiment's reward_mode is "engaged"; null otherwise or with 0 impressions
  }[];                        // ordered by finalShare desc (ties keep the experiment's arm order)
  run: number;                // §10: the traffic run (`?run=N`, default the latest)
  regimes: {                  // §10: [] when the run has no shifts
    index: number;            // 0-based; regime k = RANGE_BUCKET(round, boundaries) = k
    start: number; end: number;          // rounds [start, end) of the run's horizon
    impressions: number;                 // linear_ts impressions in the regime, all episodes
    segmentWinners: Record<string, string>; // segment -> most frequent optimal_arm (ties → lowest id)
    creatives: {                         // every creative, in the experiment's arm order
      creativeId: string;
      impressions: number; clicks: number;
      share: number;                     // pooled impressions / regime impressions
      ctr: number | null;                // clicks / impressions; null with 0 impressions
      trueCtr: number | null;            // mean p_chosen; null with no p_chosen
      segmentsWon: string[];             // sorted
      segments: { segment: string; impressions: number; clicks: number; ctr: number | null;
                  trueCtr: number | null; isBest: boolean }[]; // same sorted list in every regime
    }[];
  }[];
};
```

- **Source:** `bandit_events` rows with `policy = 'linear_ts'` only (the endpoint policy; the traffic job logs nothing else there).
- **Windows:** `nw = min(20, horizon)` windows. A round's window is `DIV(round * nw, horizon)` (exact integer floor, clamped to `nw - 1`), so window `w` is `[ceil(w·H/nw), ceil((w+1)·H/nw))`. With the default horizons there are always 20.
- **Numbers** are rounded to 4 decimal places (including `missedClicks` and `engagedSecondsPer1k`); `share` is renormalized per window before rounding.
- **Empty** (no events yet): `episodes: 0`, `horizon: null`, `windows: []`, and one entry per experiment arm with empty arrays, zero counts, `trueCtr: null`, `segmentsWon: []`, `finalShare: 0`, `segments: []`, `missedClicks: 0`, `engagedSecondsPer1k: null`. Creatives seen in events but not in the experiment's arms are appended.
- **Caching** (in-process LRU, 64 entries keyed `{experimentId}:r{run}`): 30 s while the experiment is `running_traffic`; indefinitely once `stopped` or `expired`, or for a run older than the latest (the data is final); not cached otherwise.
- **Runs (2026-10-05, §10):** every query adds `AND IFNULL(traffic_run, 1) = @traffic_run`, so legacy rows count as run 1. Before the `traffic_run` column exists (unmigrated table) run 1 reads every row and later runs read nothing.
- **Regimes (2026-10-05, §10):** a fifth query, `build_regimes_sql`, runs only when the run has shifts: `GROUP BY RANGE_BUCKET(round, @boundaries), segment, optimal_arm, arm` with `COUNT(*)`, `SUM(clicked)`, `SUM(p_chosen)`, `COUNT(p_chosen)`. `@boundaries` (`ARRAY<INT64>`, sorted, unique, strictly inside `(0, horizon)`) are the run's shift rounds `round(atFrac · horizon)` plus each shock's end round `round(untilFrac · horizon)`, i.e. the same boundaries `bandit.metrics.regime_stats` uses, computed from the run's `traffic_runs` entry (its recorded horizon).
- **Implementation:** four parameterized queries (`runserver/experiments_store.py`: `build_creative_series_sql`, `build_segment_winners_sql`, `build_true_ctr_sql`, `build_creative_segments_sql`) run concurrently; `runserver/experiments_series.py::build_creative_series` does the cross-episode aggregation in pure Python. A 20-episode × 40k-round experiment (800k events) processes about 134 MB across the three queries in about 1 s each.
- **Per-segment fields** (added 2026-10-03, additive): `build_creative_segments_sql` groups the `linear_ts` rows by `(arm, segment)` and returns `COUNT(*)`, `SUM(clicked)` (`clicked` is INT64 0/1), `SUM(p_chosen)` + `COUNT(p_chosen)`, `SUM(regret)` and `SUM(dwell_s)` (NULLs as 0). `segments[].trueCtr` = `SUM(p_chosen) / COUNT(p_chosen)`; `missedClicks` = the creative's `SUM(regret)` / `episodes`; `engagedSecondsPer1k` uses the experiment row's `reward_mode`. `isBest` mirrors `segmentsWon` (the `optimal_arm` mode, not this query).

## 9. Scenario overrides (`experiment.json` `scenario_overrides`, 2026-10-04)

Binding for every layer: `bandit/`, the traffic job, the predictor, the api (`runserver/experiments.py`) and the frontend Deploy panel. Plan: [`docs/plans/2026-10-04-bandit-advanced-traffic-controls.md`](../plans/2026-10-04-bandit-advanced-traffic-controls.md).

`experiment.json` (§1 `experiment_config_to_dict`) gains **one optional key**. It is written only when at least one override is set. When `ScenarioOverrides` is `None` or every field is `None`, the key is omitted, so a default config serialises byte-for-byte as before and images built before §9 keep loading it. Inside the object, only the fields that are set are written.

```jsonc
"scenario_overrides": {            // every field optional
  "segment_mix":   [0.6, 0.2, 0.2], // one weight per scenario segment (clear_winner/drift 3, segment_winners 4); each in [0.05, 1]; renormalised to sum 1
  "gap_scale":     1.5,             // [0.25, 2.0]; see below
  "judge_wrong":   0.8,             // [0, 1]; 0 = judge right, 0.5 = uninformative, 1 = reversed (replaces the preset value)
  "noise_scale":   1.0,             // [0, 2]; multiplies the preset's noise_sd AND theta_sd
  "drift_at_frac": 0.3              // [0.2, 0.8]; only valid when scenario == "drift" (replaces drift.at_frac)
}
```

- **Bounds:** inclusive, `bandit.config.OVERRIDE_BOUNDS` = `{segment_mix: (0.05, 1.0), gap_scale: (0.25, 2.0), judge_wrong: (0.0, 1.0), noise_scale: (0.0, 2.0), drift_at_frac: (0.2, 0.8)}`. The `segment_mix` bound applies to each raw weight before renormalisation. Values must be finite JSON numbers (booleans and strings are rejected).
- **`gap_scale` g:**
  - `segment_winners` (`arm_effect: segment_winners`): `lift_pp × g`.
  - `rank_ctrs` scenarios (`clear_winner`, `drift`): each preset CTR becomes `sigmoid(mid + g·(logit(ctr) − mid))`, where `mid` is the mean of `logit(rank_ctrs)`. The spread happens in logit space, so the CTRs stay in (0, 1). The usual `ctr_mode` scaling and `target_ctr` calibration then apply.
- **Validation** (`load_experiment_config` → `validate_experiment_config` → `validate_scenario_overrides(ov, load_scenario(cfg.scenario))`) is strict:
  - unknown keys inside `scenario_overrides` are rejected;
  - so are a non-object value, a wrong `segment_mix` length, an out-of-bounds value, and `drift_at_frac` on a scenario other than `drift`.
  - Every error is a `ValueError` whose message names the field (`scenario_overrides.<field>`).
  - `null` or `{}` loads as no overrides.
- **Application:** `resolve_scenario(cfg)` = `apply_scenario_overrides(load_scenario(cfg.scenario), cfg.scenario_overrides)`.
  - The traffic job builds its ground truth from it (`simulate.build_environment(cfg, scenario=resolve_scenario(cfg))`), and the locally replayed baselines share that environment.
  - The predictor needs no change: it reads only `target_ctr` (noise-var calibration) and `dwell_base_s` (engaged reward scale), and no override touches either.
  - The episode and model keys still derive from the scenario **name** (`simulate.scenario_key`), so a tuned experiment sees the same random draws as its preset.
- **REST (PR B, implemented 2026-10-04 in `runserver/experiments.py`):**
  - `POST /experiments` takes an optional camelCase `scenarioOverrides {segmentMix, gapScale, judgeWrong, noiseScale, driftAtFrac}` with the same bounds and rules.
  - The api writes it to `experiment.json` as the snake_case object above, only when non-empty.
  - A failure is **400** with `detail.reason: "invalid_scenario_overrides"` and `detail.field`, the camelCase field name (for example `"segmentMix"`).
  - runserver never imports `bandit`; it duplicates `OVERRIDE_BOUNDS` and the per-scenario segment counts (`SCENARIO_SEGMENTS`), under parity tests (`tests/test_experiments_api.py`).
  - `segmentMix` is stored as sent (each weight bounds-checked); `bandit` renormalises it. `null` fields are dropped.
  - The `bandit_experiments.scenario_overrides` column (§3) holds the same snake_case object, and `ExperimentSummary.scenarioOverrides` (§5) returns it camelCase (`null` when unset).
- **CLI:** `python -m bandit.cli simulate` exposes `--segment-mix`, `--gap-scale`, `--judge-wrong`, `--noise-scale` and `--drift-at`, with the same bounds, applied through `apply_scenario_overrides`.
  - The bounded overrides are recorded in the output's `config.scenario_overrides`.
  - The one exception is a `--segment-mix` with weights outside [0.05, 1] (for example the notebook-parity single-segment `1,0,0,0` readers). It is applied directly as a lab-only escape hatch and isn't recorded.

## 10. Scripted behaviour shifts (per traffic run, 2026-10-05)

Binding for every layer: `bandit/` (PR A, implemented), the traffic job and predictor (PR B), the api (PR C) and the frontend shift editor (PR D). Plan: [`docs/plans/2026-10-05-bandit-scripted-shifts.md`](../plans/2026-10-05-bandit-scripted-shifts.md).

Shifts belong to a **traffic run**, not to the experiment: `experiment.json` is unchanged. They apply to the ground truth that the endpoint **and** every replayed baseline see.

**REST form** (the `POST …/traffic` body, camelCase). `shifts` holds at most 4 entries. `forget` is a bool that defaults to `true` when there is at least one shift (else `false`):

```jsonc
"shifts": [
  {"kind": "promote", "atFrac": 0.4, "segment": "mobile_scrollers" | null, "creativeId": "aae3f6b4", "liftPp": 0.015},
  {"kind": "demote",  "atFrac": 0.5, "segment": null, "creativeId": "leader" | "<id>", "dropPp": 0.015},
  {"kind": "mix",     "atFrac": 0.3, "segmentMix": [0.6, 0.2, 0.2]},
  {"kind": "shock",   "atFrac": 0.6, "untilFrac": 0.7, "segment": null, "creativeId": "leader" | "<id>", "ctrMultiplier": 0.6}
],
"forget": true
```

**Job form** (snake_case; `bandit.config.shifts_from_dict` / `shifts_to_dict`): the same structure with `at_frac`, `until_frac`, `creative_id`, `lift_pp`, `drop_pp`, `segment_mix`, `ctr_multiplier`. The traffic job gets three env overrides:
- `SHIFTS_JSON`: the snake_case list;
- `TRAFFIC_RUN=N`: the 1-based run number;
- `FORGET=true|false`.

**Fields and bounds** (`bandit.config.SHIFT_KINDS` / `SHIFT_BOUNDS` / `MAX_SHIFTS` / `SHIFT_MIN_WINDOW`; inclusive; finite JSON numbers only, booleans and strings rejected; unknown keys and another kind's fields rejected):

| Kind | Required | Optional | Bounds |
|---|---|---|---|
| all | `kind`, `at_frac` | | `at_frac` ∈ [0.05, 0.95] |
| `promote` | `creative_id` (an arm), `lift_pp` | `segment` (`null` = everyone) | `lift_pp` ∈ [0.005, 0.03] × s |
| `demote` | `creative_id` (an arm or `"leader"`), `drop_pp` | `segment` | `drop_pp` ∈ [0.005, 0.03] × s |
| `mix` | `segment_mix` (one weight per scenario segment, as §9) | | each weight ∈ [0.05, 1], renormalised |
| `shock` | `creative_id` (an arm or `"leader"`), `until_frac`, `ctr_multiplier` | `segment` | `until_frac` ∈ [0.07, 1.0] and ≥ `at_frac` + 0.02; `ctr_multiplier` ∈ [0.3, 2.0] |

- **`s`** is the scenario's `ctr_scale(ctr_mode)` = `target_ctr[ctr_mode] / target_ctr["demo"]` (1 in demo; 0.2 realistic for every preset scenario). So realistic `lift_pp` / `drop_pp` are in [0.001, 0.006].
- **`segment`** must be one of the scenario's segment names; `"leader"` is valid only for `demote` and `shock` (`bandit.config.LEADER_KINDS`; widened 2026-10-05 so a preset like "ad fatigue on the leader" needs no client-side resolution).
- **Errors:** `ValueError` naming the field as `shifts[i].<field>` (`shifts_from_dict` checks types, fields per kind and the scenario-independent bounds; `validate_shifts(shifts, scenario, arms, ctr_mode)` adds segment names, the mix length, creative ids and the scaled magnitude bounds). The api (PR C) duplicates `SHIFT_KINDS` / `SHIFT_BOUNDS` under a parity test and answers **400** `detail.reason: "invalid_shifts"` with `detail.field`.
- `scripts/gen_scenario_presets.py` writes the same constants (camelCase bound names) to `scenario-presets.generated.json` → `shifts`.

**Resolution** (`bandit.environment.build_true_model(..., shifts=)`, reached through `simulate.build_environment(cfg, scenario=, shifts=)`):
- A shift's **round** is `r = round(at_frac · T)` (0-based). It applies to rounds `t ≥ r`, so checkpoint `r` covers exactly the pre-shift rounds. A shock applies to `r ≤ t < round(until_frac · T)`. Shifts are abrupt.
- Shifts resolve **in time order** (stable on `at_frac`, so ties keep list order), each **on top of the earlier ones**. The state at round `r` is the segment-level logit `V[s, k]`: α + `b_k` + `u[s, k]` + `θ_k · E[x | s]` (the quantity the `segment_winners` lift uses), with the drift blend at `r` and every earlier promote/demote offset. The segment-level CTR is σ(V). Shock multipliers are temporary and are ignored when resolving later shifts.
- **`"leader"`** (demote and shock), resolved at the shift's own round on top of the earlier promote/demote/mix shifts and ignoring every shock, = argmax segment-level CTR in `segment`, or argmax of the pooled CTR (segment weights of the latest earlier mix, else the scenario mix) when `segment` is null. Ties go to the lowest arm index.
- **`promote`:** in each targeted segment, the creative gets a logit offset `δ ≥ 0` so that σ(V + δ) = best other creative's CTR + `lift_pp`. A creative already ahead by more is left alone.
- **`demote`:** `δ ≤ 0` so that σ(V + δ) = best other creative's CTR − `drop_pp` (for the leader, the best other is the runner-up). A creative already that far behind is left alone.
- **`segment: null`** applies the per-segment rule in every segment.
- **`mix`:** from `r`, segments are drawn from the latest started mix's weights.
- **`shock`:** from `r` to the end round, the creative's click probability is multiplied by `ctr_multiplier`.
- **α is not recalibrated.**

**Click model with shifts** (`environment.click_probs`): `p = min(σ(drift_blend(pre) + Σ started offsets) · Π in-window shock multipliers, 1 − 1e-6)`. Offsets are added **after** the drift blend, so a promote or demote always targets the creative it names, even after the drift's swap. The drift's own swap still uses the pre-drift (unshifted) marginal CTRs.

**Common random numbers:** shifts change no key and no draw shape. Segment draws are a Gumbel argmax of fixed shape (n, S) over per-row log-weights, levels (n, G, Lmax), and click uniforms (n, K). So a shifted and an unshifted environment with the same keys see identical uniforms and identical segments and levels, except on rows whose segment a mix shift flips. `sample_contexts(key, model, n, t)` takes the per-row round `t` (`simulate.batch_draws` passes it); `t=None` means the scenario mix.

**Resolved record** (`environment.resolved_shifts(env)`, JSON-ready, in time order). Each entry has:
- `index`: position in the requested list;
- `kind`, `at_frac`, `round`;
- `end_round`: shock only, else `null`;
- for promote, demote and shock: `segment`, `requested_creative_id` (may be `"leader"`) and `creative_id` (concrete);
- the kind's magnitude (`lift_pp` / `drop_pp` / `ctr_multiplier` + `until_frac`), or for mix `segment_mix` + `segment_weights` (renormalised);
- `targets`: per affected segment, `segment`, `ctr_before`, `ctr_after` (segment-level), plus `best_other_ctr` and `logit_offset` for promote and demote.

PR B/C store it per run: the `traffic_runs` JSON on the experiment row, and `{id}/runs/{n}.json` in GCS. (The api has no `bandit` and so records the **validated requested** script in snake_case, together with the run's horizon; the resolved record is a deterministic function of it, the experiment config and the horizon, so a run replays exactly.) The traffic job (PR B) also logs the resolved record once per run (`traffic run N: … resolved shifts [...]`) and returns it in its summary. Since 2026-10-06 it also writes the resolution into every metrics row's `shift_response` entries (§3), which the api surfaces as `/metrics` `resolvedShifts` (§5).

**REST (PR C, implemented 2026-10-05 in `runserver/experiments.py`):**
- **Validation** (`validate_shifts(scenario, ctr_mode, arms, shifts)`): the rules above on the camelCase form, `null` fields counting as unset. runserver duplicates `SHIFT_KINDS`, `SHIFT_BOUNDS`, `MAX_SHIFTS`, `SHIFT_MIN_WINDOW`, `LEADER`, `LEADER_KINDS`, the per-kind field tables, the per-scenario segment names (`SCENARIO_SEGMENT_NAMES`) and `ctr_scale` (from `SCENARIO_TARGET_CTR`), under parity tests (`tests/test_experiments_shifts.py`, which also checks that `bandit` rejects every case the api rejects, naming the same field). Creative ids must be the experiment's arms. A failure is **400** `{reason: "invalid_shifts", field}` with the camelCase field (`shifts[1].untilFrac`, `shifts[0].creativeId`; `shifts` for a non-list or more than 4). A non-boolean `forget` is **400** `invalid_forget`.
- **Run allocation:** `run = len(traffic_runs) + 1` (an experiment that ran traffic before runs were numbered counts as having a legacy run 1, so its next run is 2). Before starting the job the api writes `{artifacts}/{id}/runs/{n}.json`: `{experiment_id, run, started_at, episodes, horizon, forget, shifts (job form), learning (§11), request (the body as submitted)}`; `horizon` is the body's or the experiment default. It then starts the job with `TRAFFIC_RUN`, `FORGET` and (only when there are shifts) `SHIFTS_JSON`, and appends `{run, started_at, episodes, horizon, forget, shifts, learning, execution}` to `traffic_runs` in the same guarded `traffic_started` transition (skipped, 409, if another process started a run since the read). The previous entry's `status` is frozen at that point. The response adds `run`.
- **Reads:** `/metrics` and `/creatives` take `?run=N` (default the latest; `[1, latest]` else 400 `invalid_run`). Rows with a NULL `traffic_run` count as run 1. Both responses carry `run`; `/metrics` adds `shiftResponse`, `regimes` and (when ghost rows exist) `shiftCost`, the paired per-episode ghost − endpoint clicks / reward with a 95% t-interval (§5); `/creatives` adds `regimes` (§8).
- **Frontend proxy:** the `/api/adk` proxy forwards only the `since` / `version` query params today; PR D must add `run` to that allowlist (`frontend/src/lib/user-scoping.ts`).

**Forgetting:** when `forget` is true, the endpoint's discount for the run is `bandit.config.default_shift_discount(ctr_mode, batch_size, horizon)` = `discount_for_memory(horizon / 8, batch_size)`. This is the §7 drift memory rule: 0.98 for 40k demo rounds and 0.998 for 400k realistic rounds at `batch_size = 100`.
- The traffic job floors it at `RESET_DISCOUNT_BOUNDS[0]` = 0.95 (`bandit_traffic.traffic.run_discount`; short runs, below ~15.6k rounds at batch 100, would otherwise forget faster than the predictor accepts) and sends it as the `discount` of every `reset` (§2). The ghost uses the same value.
- `forget` without shifts is allowed (the discount is sent, no ghost is replayed).

**Metrics** (`bandit/metrics.py`):
- **`shift_response(out, shift_rounds)`:** one entry per shift for one episode. It has `round`, `pct_optimal_before` / `_after` (the w rounds before `r` / from `r`, with w = `default_window(T)`, 2000 at the presets, clipped to the episode), `regret_rate_before` / `_after` (mean pseudo-regret per round in the same windows) and `recovery_rounds`.
  - `recovery_rounds` = rounds from `r` until the trailing optimal-choice rate over post-shift rounds first reaches 80% of `pct_optimal_before`. The trailing window is w // 2, 1000 at the presets. It is `null` if the rate never gets there. Its floor is the trailing window itself: a policy the shift didn't dent reports exactly that.
- **`merge_checkpoints(cps, shift_rounds, T)`:** adds `r − 1`, `r`, `r + 0.5%·T`, `r + 2%·T` and `r + 5%·T` per shift (sorted, unique, within [1, T], ending at T). Runs with shifts use `make_checkpoints(spacing="linear")` merged this way.
- **`regime_stats(out, boundaries, arm_ids, segment_names)`:** per regime `[start, end)` between the boundary rounds (shift rounds and shock end rounds), the §3 `per_segment` and `true_ctr` {creative_id: mean true click prob}.

**CLI:** `python -m bandit.cli simulate --shifts '<json>' | <path>` (snake_case) and `--forget` (`linear_ts` gets `default_shift_discount`). The output adds:
- `shifts`: `{requested, resolved, forget, discount}`;
- a `shift_response` list on every row;
- a per-policy `shift_response` summary: means across episodes, with `recovery_rounds` averaged over the episodes that recovered (`recovered_episodes` / `episodes`).

Checkpoints default to linear spacing when there are shifts.

**Traffic job** (`bandit_traffic`, PR B):
- Reads `SHIFTS_JSON` (or `--shifts <json|path>`), `TRAFFIC_RUN` (`--traffic-run`, int ≥ 1, default 1) and `FORGET` (`--forget/--no-forget`; `true|false|1|0|yes|no|on|off`, default on iff there are shifts). The shifts are validated with `shifts_from_dict` + `validate_shifts` against the experiment's resolved scenario (§9 overrides included), arms and ctr mode; any error exits 2.
- The endpoint and every baseline share the shifted environment (their segments are checked identical). Checkpoints are 50 linear ones merged around each shift round; without shifts they stay log-spaced and nothing else changes except the run number.
- Every `bandit_events` / `bandit_episode_metrics` row carries `traffic_run` (§3). The metrics `insertId` is `{experiment_id}-r{run}-e{episode}-{policy}`, the event `request_id` `{experiment_id}-r{run}-e{episode}-r{round}` (§2).
- **Ghost** (`policy = "linear_ts_unshifted"`, only with shifts): after each episode, a local LinTS (`make_policy("linear_ts")` with the experiment's `policy`, plus the run's discount when `forget`) is replayed with `simulate.run_episodes` on the **unshifted** environment (same scenario and overrides) with the same episode key. Its users and coin flips match the endpoint's (segments differ only on rows a mix shift flips), so before the first shift its world is identical. With the §2 policy stream it also draws the endpoint's random numbers, so before the first shift it chooses the **same arms** as the endpoint in every round (tested in `tests/test_bandit_endpoint_parity.py`), and the post-shift gap between the two curves is purely the shift's effect.
- With shifts, every metrics row (endpoint, ghost and baselines) gets `shift_response` (against the shift rounds, the ghost's too, although its world is unshifted; each entry carries the shift's resolved fields, §3) and `regimes` (`regime_stats` at the shift rounds and shock end rounds).

**Continuous runs (§11):** shifts and the drift point are fractions of the **whole run**: a shift's round is `round(at_frac · E·T)` (global), a shock ends at `round(until_frac · E·T)`, and the drift flips once at `at_frac · E·T`, not once per segment. The forgetting discount uses the run's total: `default_shift_discount(ctr_mode, batch_size, E·T)`, floored at 0.95. The ghost is replayed continuously too (`run_segment` with its state carried across segments). `shift_response` and `regimes` are computed once, over the whole run's concatenated rounds, and ride only on the **last** segment's rows (§11).

**Preview parity:** `tests/test_scenario_preview_golden.py` writes `frontend/src/__tests__/fixtures/scenario-shifts-golden.json`: the resolved shifts plus the exact creative × segment CTR matrix in every regime, for 6 shift combinations (one a shock on `"leader"`) (`noise_scale = 0`, pre-drift truth). The format is documented in the test module. PR D's `applyShifts` must reproduce it.

## 11. Continuous learning mode (per traffic run, 2026-10-06)

Plan: [`docs/plans/2026-10-06-bandit-continuous-learning.md`](../plans/2026-10-06-bandit-continuous-learning.md). Binding for the traffic job (PR A, implemented), the api (PR B) and the frontend (PR C).

A traffic run's **learning mode** is `"per_episode"` (the default, every behaviour above) or `"continuous"`. A continuous run is **one long episode** of H = E × T rounds (E = `episodes`, T = `horizon`), cut into E **segments** of T rounds. The mode belongs to the traffic run (like shifts): `experiment.json` is unchanged, and each new run starts from a fresh posterior.

**Limits** (`bandit.config.validate_continuous_run`): `E × T ≤ MAX_CONTINUOUS_ROUNDS = 2_000_000` (job runtime and memory; e.g. 50 × 40k demo or 5 × 400k realistic) and `T` a multiple of `batch_size` (so no policy batch straddles a segment boundary). This replaces the per-episode `horizon ≤ 1_000_000` cap for the world's total only; T itself keeps its bounds. Errors are `ValueError`s naming `episodes` / `horizon`; the job exits 2.

**Job env:** `LEARNING_MODE=per_episode|continuous` (flag `--learning`; default `per_episode`; anything else exits 2). The api (PR B) sets it only for continuous runs.

**api record (PR B, implemented in `runserver/experiments.py`):** `validate_learning` checks the body (runserver duplicates `LEARNING_MODES` and `MAX_CONTINUOUS_ROUNDS` under a parity test, `tests/test_experiments_continuous.py`; the `batch_size` is the api's `ExperimentSettings.batch_size`, the one written into `experiment.json`). The validated mode is recorded as `learning` in `runs/{n}.json` (plus `request.learning` as submitted) and in the `traffic_runs` entry, and surfaced as `TrafficRun.learning` (§5). The `forget` default is unchanged (`true` iff there are shifts).

**Semantics** (`bandit_traffic.traffic.TrafficRunner`):
- **World:** `simulate.build_environment(continuous_world_config(cfg))`, i.e. the ground truth built with horizon H, so the environment's `t` is the global round and drift points / shifts resolve over the whole run (§10). The model key is unchanged.
- **Endpoint:** one `reset` (episode 0, `reset_seed(seed, 0)`, episode 0's `policy_key`, `batch_size`, plus the run's `discount` when forgetting, computed with H) before segment 0. Decisions carry the **global** `batch` (0 … H/batch_size − 1) and `row`. Every random stream (contexts, rewards, policy) is episode 0's, folded with the global batch index.
- **Baselines and ghost:** replayed segment by segment with `simulate.run_segment(policy, env, key_0, start_batch = s·T/batch_size, num_batches = T/batch_size, batch_size, init_state=<carried>)`, each policy's state (LinTS posterior, counts incl. `step`) carried to the next segment. Chaining `run_segment` over the segments equals `run_episodes` at horizon H round for round.
- **Rows:** one `bandit_episode_metrics` row per (segment, policy), `episode` = the segment index, `horizon` = T (the segment's rounds). Its `curve` holds the segment's own metrics on **segment-local**, **linear** checkpoints (50, merged around any shift round inside the segment), plus `"segment_start": s·T` (the global 0-based round where the segment starts). `insertId` stays `{experiment_id}-r{run}-e{segment}-{policy}`. Progress is updated after each segment (`episodes_done` = segments done).
- **Events:** `episode` = the segment, `round` = the **global** round, `batch` = the global batch, `request_id = {experiment_id}-r{run}-e{segment}-r{global round}` (unique within the run).
- **Shifts:** `shift_response` and `regimes` are computed once at the end from the whole run's concatenated per-round arrays (global rounds, windows of `default_window(H)`), and attached to the **last** segment's rows only, every entry with `"continuous": true`. Earlier segments' rows carry NULL for both.
- **Decision error threshold:** checked per segment, as per episode.

**`curve` concatenation rule (PR B):** sort a policy's rows by segment; a checkpoint `c` of segment `s` is global round count `segment_start + c`. Cumulative values carry across segments: cumulative reward at `c` = `cum_avg_reward[c] × c` + Σ earlier segments' `total_reward`; cumulative regret = `cum_regret[c]` + Σ earlier `cumulative_regret`; cumulative optimal count = `pct_optimal[c] × c` + Σ earlier `pct_optimal × horizon`. Rates divide by the global round count. Curves get no band (`lo = hi = mean`): pointwise intervals need independent replications.

**`continuousSummary`** (`/metrics`, continuous runs only; PR B): the endpoint − best-baseline (by whole-run total) per-segment paired difference of `total_clicks`, with a batch-means 95% interval over the post-warm-up segments, given only when the checks pass:

```ts
continuousSummary?: {
  segments: number; warmupSegments: number;
  pairedDiff: { policy: string; perSegment: number[]; mean: number;
                lo: number | null; hi: number | null; lag1: number | null;
                status: "ok" | "too_few_segments" | "autocorrelated" | "still_trending" };
}
```

**Batch means** (`runserver/batch_means.py::batch_means_summary(diffs, warmup_frac=0.5, min_batches=5, max_lag1=0.2)`, pure Python): the warm-up is the first `floor(warmup_frac · n)` segments (`warmupSegments`); the `m` kept segments are the batches. `mean` is their mean (over all segments when none are kept). `lag1` is the lag-1 autocorrelation of the kept values **around their OLS line** (null for fewer than 3 kept or zero variance). `status`, checked in order:
1. `too_few_segments`: `m < min_batches` (so 9 segments → 4 warm-up + 5 batches is the minimum);
2. `autocorrelated`: `|lag1| > max(max_lag1, 1.96/√m)`, i.e. above 0.2 **and** significant at about 5% (with ~20 batches `max_lag1` alone would flag ~40% of iid runs);
3. `still_trending`: the OLS slope of the kept values is significant (two-sided 5% t-test, df = m − 2);
4. `ok`: `lo` / `hi` = `mean ± t(m − 1) · s/√m` (`experiments_metrics.t_critical`); null for every other status.

Autocorrelation is checked before the trend because the naive slope test fires on most strongly autocorrelated series (an AR(1) with ρ = 0.8 wanders), while detrending keeps a genuine trend from reading as autocorrelation.

**`/metrics` for a continuous run (PR B, implemented in `runserver/experiments_metrics.py::aggregate_continuous`; the route passes the run's `traffic_runs` `learning`):**
- Only segments **every** policy has written are used (the contiguous run from segment 0), so a segment in flight never skews one curve.
- `episodes` = those segments; `horizon` = the rounds they cover (Σ segment `horizon`, i.e. global; the planned total is `TrafficRun.episodes × TrafficRun.horizon`); `checkpoints` = the stitched global round counts (the endpoint's).
- Additive keys, continuous only: `learning: "continuous"`, `segmentHorizon: number` (T), `segmentStarts: number[]` (each segment's `segment_start`, for boundary ticks), `continuousSummary` (omitted without an endpoint and a baseline sharing a segment). A per-episode response is byte-for-byte unchanged (no `learning` key).
- `curves`: the concatenation rule above, `lo = hi = mean`.
- `totals`, `perSegment`, `arms`: as per episode, over the segments (`totals` is the mean ± std **per segment**, not a CI; segments are equally long, so the means are whole-run means).
- `armShare`: the endpoint's segment windows concatenated in order (aligned with `checkpoints`), not a mean.
- `shiftResponse` / `regimes` / `resolvedShifts`: from the last segment's rows only (each `Stat` collapses to its single value, `episodes: 1`); `{}` / `[]` / omitted until the last segment is written.
- `shiftCost`: the **whole-run** ghost − endpoint total (Σ over paired segments) in `clicksPerEpisode` / `rewardPerEpisode` with `lo = hi = mean` (no interval); `episodes` = paired segments.

**`/creatives` for a continuous run:** the series query windows the **global** `round` (so `horizon` = `MAX(round) + 1` = the whole run) and groups by `(arm, window)` only, reporting `episode = 0`: one stream, so `episodes` is 1 and `cumClicks` / `missedClicks` are whole-run values. Regime boundaries use the run's total `episodes × horizon` (§10 continuous resolution).
