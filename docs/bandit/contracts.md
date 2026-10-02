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
