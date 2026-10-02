# Bandit Creative Experiments: Implementation Plan (JAX LinTS on Agent Platform CPR)

> **Status:** approved 2026-10-02. COMPLETE 2026-10-02: PRs #219–#227 merged; live rollout done (api 00137-sup, web 00045-vuf, traffic job, serving image 06f4717); first live experiment 0693ea62bb7144ef verified and torn down. **Pinned:** `jax[cpu]==0.11.2` / `jaxlib==0.11.2` (dev group only; `uv.lock`) — use the same pin in `bandit_serving/requirements.txt` and `bandit_traffic/requirements.txt`. jax ≥0.5 uses `jax_threefry_partitionable=True` by default (PRNG outputs differ from older versions).
> Execution: `subagent-driven-development`, one implementer per PR, with my review between PRs. PRs 1 and 2 can run in parallel worktrees.

## Context
Trend Trawler ends with 4 evaluated ad creatives per campaign. This feature adds an **optional deployment step**: the user approves all, some, or none of the creatives on the results page. The approved creatives become the arms of a **contextual multi-armed bandit**:
- the policy is **linear Thompson sampling written in JAX**;
- it is served from a **Gemini Enterprise Agent Platform (Vertex AI) online endpoint** built with **Custom Prediction Routines (CPR)**;
- a **synthetic traffic generator** (Cloud Run Job) runs "episodes" against the endpoint and logs request, response, reward and regret to BigQuery;
- an **experiment page** shows standard bandit metrics.

The outputs mirror the two reference notebooks in `notebook-examples/`, translated to JAX on an endpoint:
- **Toy notebook:** cumulative average reward vs the optimum on a log x-axis; small ε converging to a suboptimal arm; arm expiry and new-arm injection; per-cluster users weighted (1,0) / (0,1) / (0.5,0.5).
- **Realistic notebook:** ε-greedy, UCB and Beta-Bernoulli TS compared; expected total reward ± std over N episodes; impressions and estimated CTR per arm; steps to converge.

**Decisions confirmed by the user (2026-10-02):**
- **Use case:** a publisher page about the trend, with one display/native ad slot; context comes from coarse OpenRTB-style fields.
- **Learning:** online learning **inside a single-replica endpoint** (demo-grade MVP; we document that it is not HA).
- **Scale:** **demo click-rate mode** by default (~3–8%), with **realistic mode** as an option (~0.5–1%, with about 10× longer horizons).
- **Reward:** **click** by default. An optional **engaged-time** variant gives reward = click × dwell seconds, with dwell drawn from an exponential like the notebooks' watch time.
- **Scenarios:** clear winner, segment-specific winners, drift. **Reward delay is documented for later.**
- **Where it starts:** the **results-page Deploy panel** launches it, alongside a new **`/experiments`** list and **`/experiments/[id]`** page.
- **Charts:** **hand-drawn SVG**, no new frontend dependency.
- **Traffic:** a **Cloud Run Job** started from the UI, plus a local CLI.
- **Lifecycle:** **TTL auto-teardown** plus a Stop button.
- **Repo hygiene:** `notebook-examples/` must **never be pushed**, so it goes in `.gitignore`.

**Key constraints found:**
- **CPR worker count.** CPR's model server defaults to `max(cores, 2)` uvicorn workers (`set_number_of_workers_from_env`), and each worker would hold its own copy of the in-memory posterior. **The model must be deployed with `VERTEX_CPR_WEB_CONCURRENCY=1`.**
- **CPR contract:**
  - `Predictor.load(artifacts_uri)` receives `AIP_STORAGE_URI`;
  - the request flows `preprocess → predict → postprocess` on `AIP_PREDICT_ROUTE` with `{instances, parameters}` → `{predictions}`;
  - the health route must answer within 10 s;
  - shared public endpoints cap requests at 1.5 MB;
  - deploys take about 10–20 minutes.
- **Custom routes are out.** `invokeRoutePrefix` is preview only and disables `:predict`. So decisions, rewards and resets all go through `:predict` as typed instances.
- **Docker** is available locally, and Artifact Registry already has a **`cpr`** repo in `us-central1`. Cloud Build, Agent Platform, Run and Pub/Sub are enabled.
- **JAX stays out of the api image and Agent Engine.** Root `requirements.txt` and the api image install only non-dev dependencies, so JAX goes in the **dev group** for CI tests, plus **separate `requirements.txt` files** for the CPR and traffic images.
- **API authorization.** `runserver/authz.py` `decide()` user-scopes only paths that match `_PATH_USER_RES`, so the new `/experiments/{user}/…` routes need a regex there, plus `authorize_body_user` for the create POST. Frontend `user-scoping.ts` rules are needed too. The proof-room plan said "don't touch user-scoping.ts"; **this plan explicitly overrides that.**
- **Shared helpers.** The creative→image key is `creative_agent.gcs_tools.artifact_key_for`, imported lazily from runserver because importing `creative_agent` builds the whole agent graph. Row keys use `agent_common.idempotency.stable_row_id`. BigQuery DDL lives in `deployment/create_bq_tables.sh`.

## Architecture (MVP)
```
/results/[id] ── DeployPanel (pick arms, scenario, CTR mode, reward mode)
      │ POST /experiments
      ▼
trend-trawler-api (runserver/experiments*.py)
  • snapshot arms (creative_id, headline, image gs://, eval scores) → experiment.json in GCS
  • BQ bandit_experiments row (status machine)
  • detached task: Model.upload(CPR image, artifact_uri, env VERTEX_CPR_WEB_CONCURRENCY=1)
                   → Endpoint.create → deploy(min=max=1)        (reconciled on GET; TTL reaper)
  • POST …/traffic → Cloud Run Job execution (overrides: experiment, episodes, horizon)
  • POST …/stop    → undeploy + delete endpoint
      ▼                                             ▼
Agent Platform endpoint (CPR, 1 replica)      Cloud Run Job bandit_traffic
  BanditPredictor (JAX LinTS, in-memory)  ◄──  per episode: reset → for each batch:
  instance types: decision | reward |            decision batch → true p (env) → rewards
                  reset | state                  → reward batch; baselines replayed locally
  checkpoint → GCS                               with identical contexts/rewards (CRN)
                                                 → BQ bandit_events + bandit_episode_metrics
      ▼
/experiments/[id]: status, controls, SVG charts (cum. avg reward vs optimum (log-x),
  cum. regret ± CI, % optimal, arm share, per-segment, expected total reward ± std per policy)
```

---

## PR 0: housekeeping (`chore/bandit-plan`)
- [ ] Add `notebook-examples/` to `.gitignore`, with a comment: "internal reference notebooks; never commit". Verify with `git check-ignore notebook-examples` and `git status`.
- [ ] Copy this plan to `docs/plans/2026-10-02-bandit-experiments.md`.
- [ ] `uv add --dev "jax[cpu]"` (exact version pinned through `uv.lock`; record it in the plan doc) and `uv add --dev matplotlib` (for parity figures only).
- [ ] Regenerate `requirements.txt` with `uv export --no-dev` and confirm it is **unchanged**, so neither JAX nor matplotlib leaks into the api or Agent Engine.

## PR 1: JAX bandit core + offline simulator (`feat/bandit-core`)
New flat package **`bandit/`** at the repo root, following the flat-layout convention. It must **never** be imported by runserver or the agents.

- [ ] **`bandit/__init__.py`:** public API re-exports.
- [ ] **`bandit/config.py`:**
  - `@dataclass(frozen=True)` types: `ArmSpec(creative_id, label, scores: dict[str,float], visual_style)`, `ScenarioConfig`, `ExperimentConfig` (arms, scenario, `ctr_mode: "demo"|"realistic"`, `reward_mode: "click"|"engaged"`, horizon, batch_size, episodes, seed, `policy: LinTSParams(prior_var, noise_var, exploration_scale, propensity_samples=1000, min_propensity=0.02, discount=1.0)`).
  - `load_experiment_config(path_or_dict)` and `to_json()`, with validation: 2–4 arms, known scenario, numeric bounds.
- [ ] **`bandit/features.py`:**
  - `CONTEXT_SPEC`: ordered groups and levels. Device (mobile/desktop/tablet), OS (ios/android/other), connection (wifi/cellular), census region (NE/MW/S/W), age bucket (21-34/35-54/55+, adults only), daypart (morning/afternoon/evening/night), weekend, topic_matches_trend, interest_matches_product, frequency (0/1/2+). One reference level is dropped per group, plus a bias term, giving **d ≈ 18**.
  - `encode_context(ctx: dict) -> np.ndarray[d]`, which rejects unknown keys and sensitive fields (no IDs, latitude/longitude, IP, ZIP).
  - `encode_batch(list) -> [n,d]`, `feature_names()` and `FEATURE_SPEC_VERSION`.
- [ ] **`bandit/linear_ts.py`** (pure JAX, `jit`):
  - `LinTSState(NamedTuple)`: `precision[K,d,d]`, `b[K,d]`, `n[K]`, `step`.
  - `init_state(K, d, prior_var)`.
  - `posterior_mean(state)` via `jax.scipy.linalg.cho_solve`.
  - `sample_thetas(key, state, scale)`: per-arm draws from N(μ_k, scale²·σ²·Λ_k⁻¹), using a Cholesky factor of the precision.
  - `select(key, state, X[n,d], eligible_mask)` → `(arms[n], sampled_scores[n,K])`.
  - `propensities(key, state, x, M)`: Monte Carlo win frequencies with clipping and renormalisation.
  - `update(state, arms[n], X[n,d], rewards[n], discount)`: batched scatter-add of xxᵀ and r·x, with optional discounting toward the prior for drift.
- [ ] **`bandit/baselines.py`.** Same functional interface (`init`, `select`, `update`) as LinTS, so they all run in one simulator:
  - `uniform`;
  - `epsilon_greedy(ε)`;
  - `ucb1(c)`, the notebook's UCB (eq. 2.10, with the `epsilon` multiplier semantics);
  - `beta_bernoulli_ts` (non-contextual; notebook TS);
  - `oracle` (argmax of the true p).
- [ ] **`bandit/environment.py`.** Ground truth is a logistic model, so the policy is a misspecified linear-Gaussian fit, which the docs will say:
  - `build_true_model(cfg, key)` sets logit p = α + b_a + θ_aᵀx + u_{segment,a} + interaction terms. The parts are:
    - **Arm base** b_a = κ·(overall_a − mean), seeded from eval scores.
    - **Segment affinities** from `audience_fit`, and `trend_authenticity` × `topic_match`.
    - Interaction terms such as mobile × `stopping_power`.
    - **Seeded noise** (and an optional "judge-wrong" perturbation), so judge scores don't perfectly predict reward.
    - **α calibrated by bisection** to hit the target mean CTR (demo ≈ 4%, realistic ≈ 0.8%).
  - `sample_contexts(key, n)` samples a latent segment first (from the scenario mix), then conditional feature marginals (e.g. segment A is 80% mobile), plus a daily-cycle hour.
  - `click_probs(model, X, segments, t)` applies the drift schedule (abrupt at the change point, or a gradual variant).
  - `sample_rewards(key, p, mode, dwell_means)`: a click is Bernoulli(p); engaged mode multiplies by Exponential(dwell_mean[segment, arm]) seconds.
  - `optimal_arms(p)`.
  - Scenario presets match the research recipe:
    - **Clear winner:** 3 segments, best/middle/worst arms at 6.0 / 4.5 / 3.5% (demo).
    - **Segment winners:** 4 segments, a different oracle arm per segment, +1.5 pp each.
    - **Drift:** the best arm becomes the worst at T/2.
- [ ] **`bandit/scenarios/{clear_winner,segment_winners,drift}.yaml`:** segments and mix, κ/λ, gaps, drift point, T (20k / 40k / 40k demo), batch 100, 50 episodes.
- [ ] **`bandit/simulate.py`:**
  - `run_episode(policy, model, key, horizon, batch_size)`: `lax.scan` over batches. Returns per-round arrays (segment, arm, reward, clicked, p_chosen, p_opt, opt_arm, propensity).
  - **Common random numbers:** separate `fold_in` streams for contexts, rewards and policy, so every policy sees the same users and coin flips.
  - `run_experiment(cfg, policies, episodes)`: loops over episodes with keys `fold_in(key(seed), scenario, episode)`.
  - Arm injection and expiry (the toy notebook's "sparse graph update") as an optional `arm_schedule`.
- [ ] **`bandit/metrics.py`** (pure numpy/JAX). Every function documents its definition, and terminology is fixed here (round / batch / horizon / episode):
  - cumulative reward and cumulative average reward;
  - **pseudo-regret**, Σ(p_opt − p_chosen), and realized regret;
  - % optimal overall and per segment;
  - suboptimal pulls per arm and Σ gap × pulls;
  - arm share over windows;
  - `steps_to_converge` (moving average crossing the optimum, as in the notebook);
  - expected total reward ± std across episodes, with a mean ± 95% CI curve;
  - downsampled curves (log-spaced checkpoints) for storage and the UI.
- [ ] **`bandit/cli.py`:** `uv run python -m bandit.cli simulate --scenario segment_winners --ctr-mode demo --reward-mode click --policies lints,ucb1,egreedy,bbts,uniform,oracle --episodes 50 --out /tmp/sim.json`.
- [ ] **`experiments/bandit/notebook_parity.py` + `docs/experiments/bandit-simulation.md`.** Reads simulator output, or a BigQuery experiment through `--experiment-id` (added in PR 3), and writes `experiments/bandit/figures/*.png` reproducing the notebook plots:
  1. cumulative average reward vs optimum (log x) per policy;
  2. the ε too small → suboptimal convergence example;
  3. per-segment (cluster) curves vs segment optimum, for users weighted (1,0) / (0,1) / (0.5,0.5);
  4. expected total reward ± std per policy (error bars);
  5. impressions and estimated vs true CTR per arm;
  6. cumulative regret ± CI;
  7. % optimal arm;
  8. drift recovery;
  9. arm injection.

  Delay sweeps and steps-to-converge vs K are listed as later work.
- **Tests** (`tests/`, run in CI through the dev group):
  - [ ] `test_bandit_features.py`: dimension and order, one-hot correctness, reference levels dropped, unknown or sensitive keys rejected, version constant.
  - [ ] `test_bandit_linear_ts.py`:
    - posterior mean equals the numpy closed-form ridge solution;
    - a batched update equals sequential updates;
    - propensities sum to 1, respect the clip floor, and match empirical selection frequency within a tolerance;
    - the same key gives the same result under `jit`;
    - after enough updates, the clearly better arm is chosen more than 90% of the time;
    - discount = 1 is a no-op.
  - [ ] `test_bandit_baselines.py`:
    - UCB tries unseen arms first;
    - ε-greedy explores at rate ε;
    - BB-TS posterior counts are correct;
    - the oracle always picks argmax p.
  - [ ] `test_bandit_environment.py`:
    - calibration reaches the target mean CTR within ±10% in both modes;
    - the segment scenario has a different oracle arm per segment;
    - drift flips the best arm at the change point;
    - engaged reward is 0 without a click;
    - contexts and rewards are reproducible from the keys;
    - common random numbers give identical contexts across policies;
    - no sensitive fields appear.
  - [ ] `test_bandit_simulate_metrics.py`:
    - regret is ≥ 0 and non-decreasing;
    - the oracle's regret is 0;
    - uniform regret is roughly linear;
    - LinTS regret is below uniform's and below non-contextual TS in `segment_winners` (small T, fixed seed);
    - `steps_to_converge` works on a synthetic series;
    - CI shapes are right.
  - [ ] Notebook parity smoke test (marked, tiny T): figure files get produced.

## PR 2: CPR serving container (`feat/bandit-serving`)
- [ ] **`bandit_serving/predictor.py`: `BanditPredictor(Predictor)`.**
  - **`load(artifacts_uri)`:** reads `experiment.json` (arms, feature spec version, LinTS params, seed) from `AIP_STORAGE_URI` (gs://, or a local dir for tests). Restores `checkpoints/latest.npz` if present, then initialises the state and PRNG key. A `threading.Lock` guards state.
  - **`preprocess`:** validates `instances`. Each instance has a `type`:
    - `decision {request_id, ts, context, eligible_arms?}`;
    - `reward {request_id, arm, reward, clicked, dwell_s?}`;
    - `reset {episode, seed}`;
    - `state {}`.

    Request-level `parameters` (`exploration_scale`, `propensity_samples`) are bounded.
  - **`predict`:**
    - Decisions are batch-encoded, then `select` runs, then `propensities` is computed for each chosen arm. Each decision is recorded in a bounded `pending` map so its reward can be validated.
    - Rewards are deduplicated by `request_id`, applied as one batched `update`, and bump `model_version`.
    - Reset starts fresh state for a new episode.
    - State returns counts, posterior means, version and step.
  - **`postprocess`:** returns one prediction per instance, `{request_id, type, chosen_arm, propensity, arm_probabilities, explored, model_version, policy:"linear_ts", episode, step, latency_ms}`, or ack/error objects. A bad instance never fails the whole batch.
  - **Checkpointing:** writes `checkpoints/{version}.npz` plus `latest.json` to GCS every N updates or T seconds, and on reset.
- [ ] **`bandit_serving/requirements.txt`:** `jax[cpu]` (same pin as `uv.lock`), numpy, google-cloud-storage, pyyaml.
- [ ] **`deployment/bandit/build_image.py`** (absl CLI):
  1. Stage a temporary `src_dir` containing `predictor.py` and a copy of `bandit/`.
  2. Call `LocalModel.build_cpr_model(src_dir, output_image_uri="us-central1-docker.pkg.dev/$PROJECT/cpr/trend-trawler-bandit:<git-sha>", predictor=BanditPredictor, base_image="python:3.13-slim", requirements_path=…)`.
  3. `--local-test`: write a sample `experiment.json` to a temp dir, run `deploy_to_local_endpoint(artifact_uri=…, container_ready_timeout=…)` with env `VERTEX_CPR_WEB_CONCURRENCY=1`, then send reset, decision, reward and state requests and assert the shapes.
  4. `--push`: push the image.
- [ ] **`deployment/bandit/endpoint.py`.** Library plus CLI, also used by the api in PR 4:
  - `upload_model(image, artifact_uri, display_name)` with `serving_container_environment_variables={"VERTEX_CPR_WEB_CONCURRENCY":"1"}`;
  - `create_endpoint(display_name, labels)`;
  - `deploy(model, endpoint, machine_type="n2-standard-2", min=max=1, service_account=tt-bandit-endpoint-sa)`;
  - `undeploy_and_delete(endpoint_id)`;
  - `get_state(endpoint_id)`.

  Clients are lazy-imported, region from `GCP_REGION`, and the endpoint/model display name includes the experiment ID.
- **Tests:**
  - [ ] `tests/test_bandit_predictor.py`. Imports `BanditPredictor` with a tmp artifacts dir and needs no GCP or docker:
    - load works with and without a checkpoint;
    - decision returns valid arms and propensities;
    - a reward updates state and bumps the version;
    - a duplicate reward is ignored;
    - reset clears state;
    - mixed batches work;
    - an invalid instance produces a per-instance error;
    - checkpoint save and restore round-trips;
    - the lock is held during update (concurrency smoke test).
  - [ ] `tests/test_bandit_endpoint_lib.py`: a fake aiplatform module checks the exact upload/deploy arguments, including the `VERTEX_CPR_WEB_CONCURRENCY=1` env and min = max = 1.
- **Spike (gated live step, done in this PR):** `--local-test` passes. Then push, deploy one endpoint with a sample config, and record the measured deploy time, cold `predict` latency and hourly cost in the plan doc. Undeploy afterwards. **Stop condition:** if CPR on Python 3.13 or JAX fails, fall back to `python:3.12-slim`, then report.

## PR 3: Synthetic traffic generator + BigQuery logging (`feat/bandit-traffic`)
- [ ] **`bandit_traffic/main.py`** (Cloud Run Job entrypoint and CLI).
  - **Config:** env or flags `EXPERIMENT_ID`, `CONFIG_URI`, `ENDPOINT_ID` (or `--local-url`, `--in-process`), `EPISODES`, `HORIZON`, `BATCH_SIZE`, `REWARD_MODE`.
  - **Loop, for each episode:**
    1. `reset`;
    2. for each batch: sample contexts with the `bandit.environment` streams; send a decision request (≤ 1.5 MB per request; split batches); compute the true p and reward with common random numbers; send a reward request;
    3. accumulate per-round rows.
  - **Baselines:** after each episode, replay uniform, ε-greedy, UCB1, BB-TS and the oracle **locally** on the identical contexts and reward draws.
  - **Output:** write `bandit_events` rows (endpoint policy only, batched streaming inserts with `insertId=request_id`) and `bandit_episode_metrics` rows (every policy, with downsampled curves).
  - Progress and heartbeat go to `bandit_experiments` through a small status update.
- [ ] **`bandit_traffic/endpoint_client.py`:**
  - the `EndpointClient` protocol;
  - `VertexEndpointClient` (`aiplatform.Endpoint(...).predict` with retry and backoff);
  - `HttpClient` (for the local CPR endpoint);
  - `InProcessClient` (wraps `BanditPredictor` directly, for tests and `--in-process` runs).
- [ ] **`bandit_traffic/bq.py`:**
  - pure row builders `build_event_row` / `build_episode_metrics_row`, with a column-type map in the same style as `creative_agent/bq_tools.py` `EVAL_COLUMN_TYPES`;
  - batched insert, plus an injectable client for tests.
- [ ] **`bandit_traffic/requirements.txt`** and **`bandit_traffic/Dockerfile`** (python:3.13-slim, copies `bandit/` + `bandit_traffic/`).
- [ ] **`deployment/bandit/cloudbuild.traffic.yaml`** builds the image to `…/cpr/trend-trawler-bandit-traffic:<sha>`. **`deployment/bandit/deploy_traffic_job.sh`** runs `gcloud run jobs deploy trend-trawler-bandit-traffic --image … --service-account tt-bandit-traffic-sa --task-timeout 3600 --max-retries 0`.
- [ ] **BigQuery schemas** in `deployment/create_bq_tables.sh` (prod and `trend_trawler_eval`), plus a `deployment/README.md` section:
  - `bandit_experiments` (experiment_id, user_id, session_id, app_name, created_at, updated_at, status, scenario, ctr_mode, reward_mode, arms JSON, config_uri, model_resource, endpoint_id, deployed_model_id, ttl_expires_at, stopped_at, traffic_execution, progress JSON, error);
  - `bandit_events` (experiment_id, episode, round, batch, request_id, ts, policy, segment, context JSON, arm, propensity, reward, clicked, dwell_s, p_chosen, p_optimal, optimal_arm, regret, model_version, latency_ms), partitioned by `DATE(ts)` and clustered on experiment_id;
  - `bandit_episode_metrics` (experiment_id, episode, policy, horizon, total_reward, total_clicks, cumulative_regret, pct_optimal, per_segment JSON, suboptimal_pulls JSON, arm_share JSON, steps_to_converge, curve JSON, created_at).
- [ ] **Env vars:** `BQ_TABLE_BANDIT_EXPERIMENTS`, `BQ_TABLE_BANDIT_EVENTS`, `BQ_TABLE_BANDIT_METRICS`, `BANDIT_ARTIFACTS_PREFIX` (default `gs://$BUCKET/bandit/`), `BANDIT_SERVING_IMAGE`, `BANDIT_TRAFFIC_JOB`, `BANDIT_TTL_MINUTES` (default 120). Added to `.env.example` and the deployment README.
- **Tests:**
  - [ ] `tests/test_bandit_traffic.py`. Runs the full loop with `InProcessClient` and a fake BigQuery client, for 2 episodes × small T:
    - row counts are right;
    - event rows match the schema keys;
    - one episode-metrics row per policy;
    - oracle regret is 0;
    - LinTS regret is below uniform;
    - a reward request is sent per batch;
    - batches split under the size limit;
    - the per-instance error path works.
  - [ ] `tests/test_bandit_bq_rows.py`: row builders and the column-type map.

## PR 4: Experiment backend in the api (`feat/bandit-experiments-api`)
- [ ] **`runserver/experiments.py`.** `router` and `configure(store, deployer, jobs, authz_mode)` follow the `async_runs.configure` pattern; no JAX import.
  - `POST /experiments` with body `{userId, appName, sessionId, creativeIndices[], scenario, ctrMode, rewardMode, ttlMinutes?}`:
    1. `authorize_body_user`;
    2. load the session (via the shared session service);
    3. `snapshot_arms(state, indices)`;
    4. write `experiment.json` to `BANDIT_ARTIFACTS_PREFIX/{id}/`;
    5. insert the store row with status `deploying`;
    6. start the detached `_deploy` task;
    7. return `{experimentId, status}`.

    **Errors:** 400 for an empty or invalid selection, 409 if the user already has an active experiment (limit 1 active per user, for cost).
  - `GET /experiments/{user_id}` lists the user's experiments.
  - `GET /experiments/{user_id}/{experiment_id}` returns detail, with **status reconciled** against the real endpoint state and TTL. A foreign user gets 404.
  - `GET /experiments/{user_id}/{experiment_id}/metrics` returns episode metrics and curves from BigQuery: per policy, mean ± CI per checkpoint, plus a per-segment breakdown and arm share.
  - `POST …/traffic {episodes, horizon}` starts a Cloud Run Job execution with overrides. It is only allowed when the status is `ready`; the status then becomes `running_traffic`.
  - `POST …/stop` undeploys, deletes the endpoint, writes a final checkpoint and sets status `stopped`.
- [ ] **Pure helpers in `runserver/experiments.py`:**
  - `snapshot_arms(state, indices)` builds `creative_id = stable_row_id(session_id, concept_name)`, the headline, image `gs://{bucket}/{gcs_folder}/{agent_output_dir}/{artifact_key_for(name)}` (lazy import) and eval scores from `creative_evaluation_report`.
  - `next_status(current, event)` is the state machine: `deploying → ready → running_traffic → ready → stopping → stopped`, plus `failed` and `expired`.
  - `is_expired(row, now)`.
- [ ] **`runserver/experiments_store.py`:** BigQuery persistence through MERGE upsert. Pure SQL builders plus execution via `asyncio.to_thread(get_bigquery_client)`.
- [ ] **`runserver/experiments_deploy.py`:**
  - `Deployer` protocol; `VertexDeployer` wraps `deployment/bandit/endpoint.py`.
  - The `_deploy` task is idempotent: it records the model and endpoint IDs as each is created, so a reconcile resumes after an api revision change.
  - `reconcile(row)`.
  - `reap_expired()`: a TTL loop started from `deployment/async_app.py` lifespan, every 5 minutes, which tears down expired endpoints.
- [ ] **`runserver/experiments_jobs.py`:** `JobsRunner` protocol; `CloudRunJobsRunner` uses `google-cloud-run` `JobsClient.run_job` with env overrides. Add it with `uv add google-cloud-run`, since it's needed by the api.
- [ ] **`runserver/authz.py`:** add `^/experiments/(?P<user>[^/]+)(/.*)?$` to `_PATH_USER_RES`.
- [ ] **`deployment/async_app.py`:** `experiments.configure(...)`, `include_router`, and start the reaper in lifespan.
- [ ] **IAM and infra (gated live steps, documented in `deployment/README.md` §Bandit):**
  - create `tt-bandit-endpoint-sa` (storage.objectAdmin on `gs://$BUCKET/bandit/`) and `tt-bandit-traffic-sa` (aiplatform.user, BigQuery dataEditor on the dataset, jobUser, storage.objectViewer);
  - grant `tt-api-sa` `roles/run.jobsExecutorWithOverrides` on the job, plus `iam.serviceAccountUser` on both new service accounts.
- **Tests:**
  - [ ] `tests/test_experiments_api.py`. Uses a FastAPI TestClient with fake store, deployer, jobs and session service:
    - create → `deploying`, then the fake finishes → `ready`;
    - a foreign user gets 404 or 403 through authz;
    - an empty selection gets 400;
    - an active experiment gets 409;
    - traffic is refused unless `ready`;
    - stop triggers teardown;
    - the reaper expires a row;
    - reconcile recovers from a missing endpoint;
    - `snapshot_arms` works from a fixture state (`frontend/scripts/screenshot-fixtures/creative-state.json` + eval report).
  - [ ] `tests/test_experiments_store.py`: MERGE SQL builders and param types.
  - [ ] `tests/test_authz.py`: experiment path scoping cases.

## PR 5: Frontend: deploy panel, experiments pages, SVG charts (`feat/bandit-experiments-ui`)
- [ ] **`frontend/src/lib/user-scoping.ts` + tests:**
  - `POST ["experiments"]` with `bodyUser`;
  - `GET ["experiments", u]` and `["experiments", u, id]` / `["experiments", u, id, "metrics"]` with `userAt: 1`;
  - `POST ["experiments", u, id, "traffic"|"stop"]` with `userAt: 1`;
  - experiment IDs validated by regex.
- [ ] **`frontend/src/lib/experiments.ts`:**
  - types;
  - `createExperiment`, `listExperiments`, `getExperiment`, `getExperimentMetrics`, `startTraffic`, `stopExperiment`;
  - `pollExperiment`, an async generator modelled on `pollRun` (non-terminal: deploying, running_traffic);
  - pure helpers: status labels and colours, TTL countdown, and series shaping (mean ± CI, policy order, segment series).
- [ ] **`frontend/src/lib/chart.ts`:** linear and log scales, nice ticks, a path builder, a band (CI) path builder, and downsampling.
- [ ] **`frontend/src/components/charts/line-chart.tsx`:** multi-series lines, optional log x, a reference "optimum" line, CI bands, an accessible legend, `<title>`/`aria` labels, tabular numbers. The policy palette is validated with the dataviz skill; pass and fail colours are not used for series.
- [ ] **`frontend/src/app/results/[sessionId]/deploy-panel.tsx`.** Slots in after `ProofDetail` inside `hasCreativeView`:
  - creative checkboxes (all / some / none);
  - segmented controls for scenario, CTR mode and reward mode;
  - TTL;
  - "Deploy creatives" calls `createExperiment` and routes to `/experiments/[id]`;
  - disabled for stopped-early runs.
- [ ] **`frontend/src/app/experiments/page.tsx`:** a list in the style of `run-list` (status dot, scenario, arms, created, TTL).
- [ ] **`frontend/src/app/experiments/[experimentId]/page.tsx` + `experiment-charts.tsx`:**
  - a status header with Start traffic (episodes/horizon) and Stop;
  - arm cards with images;
  - charts:
    1. cumulative average reward vs optimum (log x) per policy;
    2. cumulative regret ± CI;
    3. % optimal;
    4. arm share over time;
    5. per-segment winners;
    6. expected total reward ± std per policy;
  - a table of impressions and estimated vs true CTR per arm;
  - a "demo CTRs are inflated" note.
- [ ] **`frontend/src/components/main-nav.tsx`:** an Experiments link, with the active state matched by `startsWith`.
- [ ] **Screenshot harness:** fixtures `experiments-list.json`, `experiment-detail.json` and `experiment-metrics.json`; captures `10-deploy-panel.png`, `11-experiments.png`, `12-experiment-detail.png`.
- **Tests (Vitest):**
  - [ ] `user-scoping.test.ts`: the new routes, the userId rewrite, rejected IDs.
  - [ ] `experiments.test.ts`: API wrappers with fetch spied, poll termination, series shaping, the TTL countdown.
  - [ ] `chart.test.ts`: scales, ticks, paths, downsampling.
  - [ ] `deploy-selection.test.ts`: pure selection-to-payload mapping.

## PR 6: Docs, parity report, live rollout (`docs/bandit-experiments`)
- [ ] **`docs/bandit/README.md`:**
  - architecture and the not-HA caveat (single replica, in-memory, checkpointed);
  - the API contract JSON for decision, reward, reset and state;
  - the feature spec and privacy rules (adults only, 21+ for alcohol brands, no IDs or precise location);
  - scenarios, metrics definitions and terminology;
  - demo vs realistic modes;
  - **future work:** reward delay and attribution windows, the stateless decision endpoint + learner job production pattern, off-policy evaluation (IPS/SNIPS/DR), logistic TS.
- [ ] CLAUDE.md, README and frontend/README sections; `docs/experiments/bandit-simulation.md` with committed parity figures.
- [ ] **Live rollout (gated, confirm each step):**
  1. create BigQuery tables;
  2. set up IAM;
  3. push the CPR image;
  4. deploy the traffic job;
  5. deploy api and web;
  6. live end-to-end: deploy 3 creatives from the PRS run, run 20 demo episodes of `segment_winners`, check BigQuery rows and the charts, then Stop. Confirm the endpoint is deleted, then let a second experiment expire through TTL.

## Conventions
- **Branches:** one branch and PR each; squash-merge after CI. **Never add `Co-Authored-By` or any AI attribution.** Never stage `notebook-examples/` (ignored in PR 0) or the repo-root `.agents/`, `.claude/`, `skills-lock.json`.
- **Python:** uv only (`uv add`). Loop: `ruff format --check`, `ruff check`, `ty check`, `pytest -n 4` with `GOOGLE_CLOUD_PROJECT=test-project`. Re-export `requirements.txt`, and it must stay JAX-free.
- **Frontend:** `npm run lint && npm test && npm run build`. No new dependencies.
- **JAX:** `jax.random.key` plus `fold_in` with separate streams; no global seeds. Pin the version in `uv.lock` and both container `requirements.txt` files.
- **Live steps:** no GCP resources outside the gated steps. Every endpoint is labelled `app=trend-trawler,experiment=<id>` so stray ones can be found and deleted.

## Verification (end to end)
1. **Unit:** the Python suite (new `test_bandit_*`, `test_experiments_*`, authz) and Vitest suites pass locally and in CI. `requirements.txt` has no JAX.
2. **Offline simulator:**
   - `uv run python -m bandit.cli simulate` for all 3 scenarios × 6 policies, then `experiments/bandit/notebook_parity.py`. The figures must look like the notebooks: curves approach the optimum on log x; UCB with small ε can stall on a suboptimal arm.
   - LinTS beats non-contextual TS in `segment_winners`, and discounted LinTS recovers in `drift`.
3. **Local endpoint:** `deployment/bandit/build_image.py --local-test` runs the CPR container in docker and passes the reset/decision/reward/state assertions with one worker. Then `bandit_traffic/main.py --local-url http://localhost:<port> --episodes 2` shows expected rows written to a fake BigQuery target, or a JSONL file with `--dry-run`.
4. **Local full stack, no endpoint:**
   - api with `TRUST_CLIENT_USER_ID=1 SESSION_SERVICE_URI=memory://`, plus `BANDIT_DEPLOY_MODE=local`, so the deployer points at the local CPR container and jobs run in-process;
   - `npm run dev`: deploy from a results page, watch status reach ready, start traffic, see the charts fill, stop.
5. **Live (PR 6):**
   - deploy and stop on the real endpoint;
   - measure deploy time and cost;
   - BigQuery `bandit_events` / `bandit_episode_metrics` populated;
   - the UI shows the charts;
   - TTL teardown confirmed with `gcloud ai endpoints list --filter=labels.app=trend-trawler` returning empty.
