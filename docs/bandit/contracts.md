# Bandit experiments: interface contracts

This is the single source of truth that PRs 1–5 of
[`docs/plans/2026-10-02-bandit-experiments.md`](../plans/2026-10-02-bandit-experiments.md)
build against. A change to any contract here must update this file in the same PR.

**Terminology:**
- **round:** one impression and one decision.
- **batch:** the rounds between two posterior updates.
- **horizon (T):** the number of rounds per episode.
- **episode:** one independent run that starts from a reset posterior and has its own seed.

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
class LinTSParams: prior_var: float = 1.0; noise_var: float = 0.25; exploration_scale: float = 1.0
                   propensity_samples: int = 1000; min_propensity: float = 0.02; discount: float = 1.0
@dataclass(frozen=True)
class ExperimentConfig: experiment_id: str; arms: tuple[ArmSpec, ...]; scenario: str
                        ctr_mode: str = "demo"; reward_mode: str = "click"; horizon: int = 20000
                        batch_size: int = 100; episodes: int = 20; seed: int = 0
                        policy: LinTSParams = LinTSParams()
def load_experiment_config(src: str | Path | dict) -> ExperimentConfig   # validates 2–4 arms etc.
def experiment_config_to_dict(cfg) -> dict                               # JSON round-trip of load_*

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
def propensities(key, state, x, params, eligible=None) -> Array      # (K,) sums to 1, floor-clipped
def update(state, arms, X, rewards, params) -> LinTSState            # batched; params.discount<1 decays toward prior
```

## 2. Endpoint instances (CPR `BanditPredictor`, PR 2; client in PR 3)

Requests go to `POST …:predict` with `{"instances": [...], "parameters": {...}}`, and responses come back as `{"predictions": [...]}`, in order and one per instance. A bad instance returns
`{"type":"error","request_id":..., "error":"..."}` without failing the rest of the batch.

| `type` | Instance fields | Prediction fields |
|---|---|---|
| `decision` | `request_id`, `ts`, `context` (§4), `eligible_arms?` (creative ids) | `request_id`, `type`, `chosen_arm` (creative_id), `arm_index`, `propensity`, `arm_probabilities` {creative_id: p}, `explored` (bool: chosen ≠ posterior-mean argmax), `model_version`, `policy:"linear_ts"`, `episode`, `step`, `latency_ms` |
| `reward` | `request_id`, `arm` (creative_id), `reward` (float), `clicked` (0/1), `dwell_s?` | `request_id`, `type`, `accepted` (bool; false for a duplicate or unknown id), `model_version` |
| `reset` | `episode` (int), `seed` (int) | `type`, `episode`, `model_version` |
| `state` | — | `type`, `episode`, `step`, `model_version`, `pulls` {creative_id: n}, `posterior_mean` {creative_id: [d floats]}, `feature_spec_version` |

`parameters` (optional, bounded): `exploration_scale` [0.1, 5], `propensity_samples` [100, 5000].

Reward instances carry the **unscaled** reward (`click` mode: 0/1; `engaged` mode: click × dwell seconds, with `dwell_s` set). Like the simulator, the predictor divides engaged rewards by the scenario's `dwell_base_s` before its `update`. A reward is accepted only for a pending decision whose `arm` matches the chosen arm.

The traffic job (PR 3) sends `request_id = "{experiment_id}-e{episode}-r{round}"`, where `round` is the 0-based round index within the episode (also `bandit_events.round`). It splits a batch into requests of at most 500 instances and about 1.2 MB, sends one `reset` per episode with `seed = (experiment seed × 1000003 + episode) mod 2³¹`, and sends a batch's rewards only after all of that batch's decisions.

The CPR container is always deployed with `VERTEX_CPR_WEB_CONCURRENCY=1`, i.e. one worker process holding a single in-memory posterior. `AIP_STORAGE_URI` holds `experiment.json` (a §1 `experiment_config_to_dict`) and `checkpoints/`.

## 3. BigQuery tables (DDL in `deployment/create_bq_tables.sh`, PR 4; written by PR 3 and PR 4)

**`bandit_experiments`**, one row per experiment, upserted by the api with MERGE on `experiment_id`:
experiment_id STRING, user_id STRING, session_id STRING, app_name STRING, created_at TIMESTAMP,
updated_at TIMESTAMP, status STRING, scenario STRING, ctr_mode STRING, reward_mode STRING,
arms STRING (JSON list of §5 arm objects), config_uri STRING, model_resource STRING, endpoint_id STRING,
deployed_model_id STRING, ttl_expires_at TIMESTAMP, stopped_at TIMESTAMP, traffic_execution STRING,
progress STRING (JSON {episodes_done, episodes_total}), error STRING

**`bandit_events`**, one row per round for the endpoint policy, written by the traffic job (`insertId = request_id`).
It is partitioned by DATE(ts) and clustered on experiment_id:
experiment_id STRING, episode INT64, round INT64, batch INT64, request_id STRING, ts TIMESTAMP,
policy STRING, segment STRING, context STRING (JSON), arm STRING, propensity FLOAT64, reward FLOAT64,
clicked INT64, dwell_s FLOAT64, p_chosen FLOAT64, p_optimal FLOAT64, optimal_arm STRING,
regret FLOAT64, model_version STRING, latency_ms FLOAT64

**`bandit_episode_metrics`**, one row per (episode, policy), written by the traffic job:
experiment_id STRING, episode INT64, policy STRING, horizon INT64, total_reward FLOAT64,
total_clicks INT64, cumulative_regret FLOAT64, pct_optimal FLOAT64, steps_to_converge INT64,
curve STRING (JSON), arm_share STRING (JSON), per_segment STRING (JSON), arm_stats STRING (JSON),
created_at TIMESTAMP

The JSON payloads:
- `curve`: `{"checkpoints":[r1..rm], "cum_avg_reward":[...], "cum_regret":[...], "pct_optimal":[...]}`. Checkpoints are about 50 log-spaced round indices, the same for every policy and episode in one experiment.
- `arm_share`: `{creative_id: [share of pulls in each checkpoint window]}`.
- `per_segment`: `{segment: {"optimal_arm": creative_id, "pct_optimal": f, "avg_reward": f, "rounds": n}}`.
- `arm_stats`: `{creative_id: {"impressions": n, "clicks": n, "estimated_ctr": f, "true_ctr": f}}`.

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
  - Body: `{userId, appName, sessionId, creativeIndices: number[], scenario, ctrMode, rewardMode, ttlMinutes?}`.
  - Success: **201** `{experimentId, status: "deploying"}`.
  - Errors: 400 for an empty or invalid selection (fewer than 2 arms, or an index out of range), 409 `{detail:{reason:"active_experiment"}}` if the user already has an active experiment.
- `GET /experiments/{user_id}` returns `{experiments: ExperimentSummary[]}`, newest first.
- `GET /experiments/{user_id}/{experiment_id}` returns `ExperimentSummary`, with status reconciled against the endpoint and the TTL.
- `GET /experiments/{user_id}/{experiment_id}/metrics` returns `ExperimentMetrics`. It is empty (`episodes: 0`) until traffic runs.
- `POST /experiments/{user_id}/{experiment_id}/traffic`
  - Body: `{episodes: 1..100, horizon?: 1000..400000}`.
  - Success: `{status: "running_traffic", execution}`.
  - Error: 409 unless status is `ready`.
- `POST /experiments/{user_id}/{experiment_id}/stop` returns `{status: "stopping" | "stopped"}`.

**Status enum:** `deploying → ready → running_traffic → ready → stopping → stopped`; also `failed` and `expired` (the TTL reaper).

```ts
type Arm = { creativeId: string; index: number; label: string; conceptName: string;
             imageUri: string | null; scores: Record<string, number>; overallScore: number | null };
type ExperimentSummary = { experimentId: string; userId: string; sessionId: string; appName: string;
  createdAt: string; updatedAt: string; status: Status; scenario: string; ctrMode: string;
  rewardMode: string; ttlExpiresAt: string | null; arms: Arm[]; endpointId: string | null;
  trafficExecution: string | null; progress: { episodesDone: number; episodesTotal: number } | null;
  error: string | null };
type Band = { mean: number[]; lo: number[]; hi: number[] };          // mean ± 95% CI across episodes
type ExperimentMetrics = { experimentId: string; episodes: number; horizon: number | null;
  checkpoints: number[]; policies: string[];                         // order: linear_ts first, oracle last
  curves: Record<string, { cumAvgReward: Band; cumRegret: Band; pctOptimal: Band }>;
  totals: Record<string, { mean: number; std: number }>;             // expected total reward ± std
  armShare: Record<string, number[]>;                                // linear_ts only, by creativeId
  perSegment: Record<string, { optimalArm: string;
                               policies: Record<string, { pctOptimal: number; avgReward: number }> }>;
  arms: { creativeId: string; impressions: number; estimatedCtr: number; trueCtr: number }[] };
```

## 6. Resolved decisions (PR 4 + PR 5, 2026-10-02)

- **IDs:** `experimentId` is 16 lowercase hex characters (`[0-9a-f]{16}`). The frontend proxy accepts `^[a-z0-9][a-z0-9-]{2,63}$`. `endpointId` / `endpoint_id` and the job's `ENDPOINT_ID` are the **full resource name** `projects/P/locations/R/endpoints/N`.
- **Arms:** `creativeIndices` index `final_visual_concepts.visual_concepts` (the results page's `Proof.index`).
  - A selection must have 2–4 unique, in-range indices.
  - `Arm.label` is the headline; charts display `conceptName`.
  - Arm `scores` keys are the 12 judge dimensions (score/10, clipped to 0–1) plus `ad_copy_overall` and `visual_overall`. `overallScore` is the mean of the two overall scores.
  - Ad-copy evals are matched headline first, then id, then position.
  - `imageUri` is null when `_generated_artifact_keys` exists and doesn't include the concept's image.
- **Error reasons** (`detail.reason`):
  - 400: `too_few_arms`, `too_many_arms`, `duplicate_index`, `index_out_of_range`, `no_creatives`, `invalid_scenario`, `invalid_ctr_mode`, `invalid_reward_mode`, `invalid_episodes`, `invalid_horizon`;
  - 409: `active_experiment`, `not_ready`;
  - 404: `session_not_found`, `not_found`;
  - 502: `config_write_failed`, `traffic_start_failed`.
- **Lifecycle:**
  - The api moves `running_traffic` back to `ready` on a detail GET once `progress.episodes_done >= episodes_total` or the job execution has finished. A failed job sets `error` and keeps the status `ready`.
  - Stop waits up to 2 s, so it returns `stopped` if teardown finishes in time, otherwise `stopping`.
  - `ttlMinutes` is clamped to 10–480.
  - Store updates only rewrite the columns that changed, so the api never overwrites the `progress` written by the traffic job.
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
- **Engaged reward scale:** in `engaged` mode, rewards are divided by the scenario's `dwell_base_s` (30 s) before every policy update, both in the simulator (`TrueModel.reward_scale`) and in the predictor (`bandit.config.reward_scale`). Clients send **unscaled** rewards (click × dwell seconds).
- **`ArmSpec.overall` fallback:** use `scores["overall"]` if present. Otherwise use the mean of `ad_copy_overall` / `visual_overall` (api arms, §6). Otherwise use the mean of all scores, and 0.5 if there are none.
- **Policy spec strings:** `name` or `name:key=value[,key=value]`, for example `linear_ts:discount=0.98` or `ucb1:c=0.01`. The spec string becomes the policy label in the output. The CLI aliases are `lints`, `egreedy` and `bbts`.
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
- **Reset:** fresh prior, PRNG `key(seed)` (per-call `fold_in`), empty pending/seen maps, then an immediate checkpoint.
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
  }[];                        // ordered by finalShare desc (ties keep the experiment's arm order)
};
```

- **Source:** `bandit_events` rows with `policy = 'linear_ts'` only (the endpoint policy; the traffic job logs nothing else there).
- **Windows:** `nw = min(20, horizon)` windows. A round's window is `DIV(round * nw, horizon)` (exact integer floor, clamped to `nw - 1`), so window `w` is `[ceil(w·H/nw), ceil((w+1)·H/nw))`. With the default horizons there are always 20.
- **Numbers** are rounded to 4 decimal places; `share` is renormalized per window before rounding.
- **Empty** (no events yet): `episodes: 0`, `horizon: null`, `windows: []`, and one entry per experiment arm with empty arrays, zero counts, `trueCtr: null`, `segmentsWon: []`, `finalShare: 0`. Creatives seen in events but not in the experiment's arms are appended.
- **Caching** (in-process LRU, 64 experiments): 30 s while the experiment is `running_traffic`; indefinitely once `stopped` or `expired` (the data is final); not cached in any other status.
- **Implementation:** three parameterized queries (`runserver/experiments_store.py`: `build_creative_series_sql`, `build_segment_winners_sql`, `build_true_ctr_sql`) run concurrently; `runserver/experiments_series.py::build_creative_series` does the cross-episode aggregation in pure Python. A 20-episode × 40k-round experiment (800k events) processes about 134 MB across the three queries in about 1 s each.
