# Bandit simulation: notebook parity (PR 1)

This page covers the offline simulator in the `bandit/` package. It is PR 1 of
[`docs/plans/2026-10-02-bandit-experiments.md`](../plans/2026-10-02-bandit-experiments.md),
and the interfaces are in [`docs/bandit/contracts.md`](../bandit/contracts.md). The figures
reproduce the two internal reference notebooks (the toy UCB notebook and the "more realistic
models" notebook) using JAX linear Thompson sampling (LinTS) and the five baselines, on
synthetic traffic. Nothing here touches GCP.

## How to run

```bash
# one simulation -> JSON (per-episode §3 rows + §5-shaped aggregate + scalar summary)
uv run python -m bandit.cli simulate --scenario segment_winners --ctr-mode demo \
  --reward-mode click \
  --policies linear_ts,ucb1,epsilon_greedy,beta_bernoulli_ts,uniform,oracle \
  --episodes 10 --horizon 20000 --out /tmp/sim.json

# every figure on this page (8 simulations, 20 episodes each, ~8 min on a laptop CPU)
uv run python experiments/bandit/notebook_parity.py --generate \
  --data-dir /tmp/bandit_parity --fig-dir experiments/bandit/figures
# redraw from existing JSON
uv run python experiments/bandit/notebook_parity.py --data-dir /tmp/bandit_parity
```

**Policy specs.** A policy spec can take options, for example `ucb1:c=0.01`,
`epsilon_greedy:epsilon=0.2` or `linear_ts:discount=0.98`. The spec string becomes the
policy's label in the output, so you can compare variants side by side. The aliases
`lints`, `egreedy` and `bbts` also work.

**Other CLI flags:**

| Flag | What it does |
|---|---|
| `--segment-mix 1,0,0,0` | Overrides the scenario's segment mix. |
| `--arm-schedule '[[0,[1,2,3]],[10000,[0,1,2]]]'` | Expires and injects arms at batch boundaries. |
| `--drift gradual` | Switches the drift scenario to the gradual variant. |
| `--judge-wrong 1` | Reverses the order the judge scores imply. |
| `--checkpoint-spacing linear --num-checkpoints 100` | Gives finer resolution late in the episode, for the drift and injection plots. The contract default is about 50 log-spaced checkpoints. |
| `--no-propensity` | Skips the Monte Carlo propensities for LinTS. They don't feed any metric, and skipping them makes the run about 10x faster. |

**Default settings.** All figures use demo CTRs, click reward, `batch_size` 100 (the
policy updates once per batch), seed 0 and 20 episodes. Every policy sees the same users
and the same coin flips (common random numbers), so differences between policies are paired.

## What each figure shows

The figures below come from the committed run, and the numbers in the text are means over
20 episodes. "Regret" means pseudo-regret, Σ(p_opt − p_chosen) in expected clicks.

### 1. Cumulative average reward vs optimum (log x)

![](../../experiments/bandit/figures/01_cum_avg_reward.png)

- **clear_winner** (arms at 6.0 / 4.5 / 3.5 % CTR):
  - The non-contextual policies converge on the winner, as in the toy notebook.
  - LinTS converges much more slowly. It fits 19 coefficients per arm where Beta-Bernoulli TS
    fits 1, and here the context carries no signal.
- **segment_winners**:
  - LinTS is the only policy that rises above the pooled-average plateau, toward the
    optimum line.
- **drift**: covered in figure 8.

### 2. A UCB multiplier that is too small settles on a suboptimal arm

![](../../experiments/bandit/figures/02_ucb_small_multiplier.png)

This is the toy notebook's "epsilon = 1" cell.

| UCB multiplier | Episodes with < 50 % optimal pulls | % optimal | Regret | Expected total reward |
|---|---|---|---|---|
| c = 0.01 | 8 / 20 | 59 % | 122 | 1084 ± 165 |
| c = 0.25 | 0 / 20 | 90 % | 37 | 1184 ± 34 |

The large variance at c = 0.01 is the signature of lock-in: some episodes are perfect and
others are stuck on a suboptimal arm.

### 3. Per-segment users (notebook cluster weights (1,0) / (0,1) / (0.5,0.5))

![](../../experiments/bandit/figures/03_segment_users.png)

This is `segment_winners` restricted to the `mobile_scrollers` and `trend_followers`
segments, with the segment-mix weights shown in each panel title.

- **Single-segment users** (the (1, 0) and (0, 1) panels): there is no heterogeneity to
  exploit, so Beta-Bernoulli TS wins. Its regret is 50 / 54, against 165 / 195 for LinTS.
- **The 50/50 user**: the two segments want different arms, so a pooled policy can't do
  well. LinTS and Beta-Bernoulli TS tie at T = 20k (regret 179 vs 175), and LinTS's curve
  is still rising, consistent with the notebook's remark that mixed users "need more data".

### 4. Expected total reward ± std per policy (error bars)

![](../../experiments/bandit/figures/04_expected_total_reward.png)

This is the realistic notebook's headline plot. In **segment_winners**, LinTS earns
1763 ± 51 clicks per episode. Every non-contextual policy earns about 1590–1600, uniform
included, and the oracle earns 2081.

### 5. Impressions and estimated vs true CTR per arm (LinTS)

![](../../experiments/bandit/figures/05_arm_impressions_ctr.png)

- **clear_winner**: LinTS gives the best arm 11.6k of 20k impressions, and its estimated
  CTRs match the true ones.
- **segment_winners**: impressions are split almost evenly, because each arm wins one
  segment.
  - Each arm's estimated CTR (about 4.3–4.6 %) sits above its traffic-weighted true CTR
    (about 3.9–4.1 %).
  - That gap is the selection effect a contextual policy should produce: it shows each arm
    to the users it suits.

### 6. Cumulative regret ± 95% CI

![](../../experiments/bandit/figures/06_cum_regret.png)

### 7. % optimal arm (cumulative)

![](../../experiments/bandit/figures/07_pct_optimal.png)

### 8. Drift recovery (the best arm becomes the worst at T/2)

![](../../experiments/bandit/figures/08_drift_recovery.png)

- **Before the drift**: discounting costs LinTS some accuracy. At T/2 the discounted
  variant reaches 59 % optimal, against 69 % for the undiscounted one.
- **After the drift**: the discounted variant (γ = 0.98 per batch, an effective memory of
  about 50 batches, or 5k rounds) recovers. The undiscounted posterior keeps favouring the
  old winner.
  - Total regret is 511 with discounting and 690 without.
- **UCB1**: recovers fastest (regret 172), because its log t bonus keeps re-checking arms
  whose means went stale.
- **Beta-Bernoulli TS**: recovers slowly, with large variance across episodes (regret
  459 ± 77, 95 % CI half-width).

### 9. Arm injection (the notebook's "sparse graph update")

![](../../experiments/bandit/figures/09_arm_injection.png)

The run uses 4 arms. Arms b, c and d are live until T/2, when arm a (the best one, at 6.0 %)
is injected and arm d expires.

- **LinTS**: picks up the new arm. Its share of pulls on a climbs to about 65 % by the end.
- **UCB1 and Beta-Bernoulli TS**: switch faster (regret 39 and 53, against 178 for LinTS).

## Findings

| Scenario | LinTS | UCB1 | ε-greedy | BB-TS | uniform | oracle |
|---|---|---|---|---|---|---|
| clear_winner (T = 20k) | 155 | 37 | 50 | 31 | 273 | 0 |
| segment_winners (T = 40k) | **322** | 473 | 482 | 479 | 482 | 0 |
| drift (T = 40k) | 690 (511 with γ = 0.98) | 172 | – | 459 | – | 0 |

The table shows final pseudo-regret, mean over 20 episodes, in demo mode. The 95 % CI
half-width is about 11 for LinTS in `clear_winner` and `segment_winners`, and 22 in `drift`.

**When context matters, LinTS wins.** In `segment_winners`, LinTS has 33 % less regret than
non-contextual TS (322 vs 479). LinTS reaches 47 % optimal pulls overall and 42–52 % within
each segment. Every pooled policy stays at the 25 % chance level, because the arms' pooled
CTRs are within 0.3 pp of each other.

**When context doesn't matter, LinTS pays for its parameters.** In `clear_winner`, LinTS has
about 5x the regret of Beta-Bernoulli TS (155 vs 31), and the 1-arm-per-segment users in
figure 3 show the same effect. With d = 19 and about 5k pulls per arm, LinTS's
posterior sd for a context's score is about 1.3 pp. That is close to the 1.5 pp arm gap, so
Thompson sampling keeps exploring.

**Steps to converge** use the notebook's definition: a moving average over a window of
`clamp(T/10, 10, 2000)` rounds crossing the optimum. In `clear_winner` the means are
3.0k rounds for BB-TS, 3.6k for UCB1 and 9.8k for LinTS. The minimum possible is the window,
2k; the oracle's value is 2.5k.

**Calibration** holds for every scenario in both CTR modes: α from bisection hits the target
mean CTR within ±10 % (tested). The realized `clear_winner` arm CTRs are 6.03 / 4.55 / 3.42 %.

## Caveats

- **Demo CTRs are inflated.** Demo mode averages about 4 % CTR, against about 0.8 % in
  realistic mode. Realistic mode needs about 10x the horizon for the same statistical
  power: the scenario presets use 200k / 400k rounds, and the gaps are scaled by 0.2.
- **The model is misspecified on purpose.** The truth is logistic with a latent segment.
  LinTS fits an independent linear-Gaussian model per arm on the observable context only,
  so it can never reach the segment oracle exactly. Least squares on the true
  probabilities caps a linear policy at about 86 % optimal in `segment_winners`.
- **Noise variance.** The simulator sets LinTS `noise_var` to the Bernoulli variance at the
  scenario's target CTR: p(1−p), about 0.045 in demo mode. In engaged mode it is 2p − p²
  on dwell-scaled rewards (`bandit.config.default_noise_var`). The `LinTSParams` default of
  0.25 is about 6x too wide at a 4 % CTR and over-explores. In an earlier probe at T = 20k (with
  a less separable segment layout) it reached 30 % optimal in `segment_winners`, against 36 % with the matched variance.
- **UCB and ε-greedy settings.** UCB uses c = 0.25 and ε-greedy uses ε = 0.1, both tuned
  for click-scale rewards. The notebooks' `epsilon` of 2 or 50 assumed CTRs around 0.2 or
  watch-time-scale rewards, which is why the defaults here are smaller.
- **Steps to converge are noisy.** The measure is computed on realized rewards, as in the
  notebook, so a lucky window can "converge" even for a poor policy: uniform did so in
  7 of 20 `clear_winner` episodes. Use it alongside regret and % optimal.
- **Out of scope here.** Reward delay is applied once per batch, as in the notebook's
  update delay with delay = `batch_size`. Delay sweeps and steps-to-converge vs K are left
  for later work. The propensity floor (`min_propensity`) clips the logged MC propensities
  but does not change the argmax selection, so off-policy estimates based on it are
  approximate.
