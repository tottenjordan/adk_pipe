"""Synthetic ground truth for offline bandit simulation.

Click probability for a round with context ``x`` (``bandit.features``), latent
segment ``s`` and arm ``a`` is logistic:

    logit p = α + b_a + u[s, a] + θ_aᵀx

- **Arm base ``b_a``**: from judge scores. Every score enters "judge-adjusted
  and centred": ``ŝ_a = (1 - 2·judge_wrong)·(score_a - mean)``, so
  ``judge_wrong=1`` reverses what the judge believes.
  - ``rank_ctrs`` scenarios (clear winner, drift): arms are ranked by
    ``κ·ŝ_overall + noise`` and get the preset marginal CTRs best -> worst
    (6.0 / 4.5 / 3.5 % demo; K≠3 interpolates in logit space), i.e.
    ``b_a = logit(ctr_rank(a)) - mean``.
  - ``segment_winners``: ``b_a = κ·ŝ_overall + noise``.
- **Segment affinity ``u[s, a]``**:
  ``λ·(ŝ_audience_fit·P_s(interest_matches_product) +
  ŝ_trend_authenticity·P_s(topic_matches_trend)) + noise``. In
  ``segment_winners`` each segment's winner (chosen greedily by its
  ``winner_key`` score among unassigned arms) is then lifted so its
  segment-level logit beats the best other arm by
  ``logit(base + lift) - logit(base)`` (+1.5 pp demo).
- **Context x arm interactions ``θ_a``**: ``η·ŝ_trend_authenticity`` on
  ``topic_matches_trend``, ``η·ŝ_audience_fit`` on ``interest_matches_product``,
  ``-η·ŝ_stopping_power`` on desktop/tablet (i.e. mobile × stopping power), plus
  ``N(0, theta_sd²)`` seeded noise on every non-bias feature.
- **α**: calibrated by bisection so the mean CTR under the scenario's context
  distribution and uniformly random arms equals ``target_ctr[ctr_mode]``
  (pre-drift).
- **Drift**: after the change point the best and worst arms (by pre-drift
  marginal CTR) swap their whole arm effect (``b``, ``u`` column, ``θ`` row);
  ``abrupt`` switches at ``at_frac·T``, ``gradual`` mixes the logits linearly over
  ``width_frac·T`` centred there.

Contexts are sampled hierarchically: segment first (scenario mix), then each
context group from the segment's conditional marginal (``BASE_MARGINALS``
overridden per segment). Engaged reward = click × Exponential(dwell mean
``dwell_base_s · dwell_factor_s · exp(dwell_score_scale·ŝ_overall)``).

The policies fit a linear-Gaussian model to this logistic truth on purpose
(misspecified, like production).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any, NamedTuple

import jax
import jax.numpy as jnp
import numpy as np
from jax import Array

from bandit import features
from bandit.config import (
    BASE_MARGINALS,
    ArmSpec,
    ExperimentConfig,
    ScenarioConfig,
    load_scenario,
)


class TrueModel(NamedTuple):
    """Ground-truth parameters (a JAX pytree, safe to pass through ``jit``)."""

    alpha: Array  # ()
    arm_base: Array  # (K,)
    seg_aff: Array  # (S, K)
    theta: Array  # (K, d)
    dwell_means: Array  # (S, K) seconds
    seg_logits: Array  # (S,)
    level_logits: Array  # (S, G, Lmax) log-probs, -inf padded
    drift_perm: Array  # (K,) post-drift arm a behaves like pre-drift arm perm[a]
    drift_start: Array  # () round index (inf = no drift)
    drift_width: Array  # () rounds (0 = abrupt)


@dataclass(frozen=True)
class Environment:
    model: TrueModel
    arms: tuple[ArmSpec, ...]
    segment_names: tuple[str, ...]
    scenario: ScenarioConfig
    ctr_mode: str
    reward_mode: str
    horizon: int
    target_ctr: float
    reward_scale: float  # divide rewards by this before policy updates
    winners: tuple[int, ...] | None = None  # segment_winners: oracle arm per segment

    @property
    def arm_ids(self) -> tuple[str, ...]:
        return tuple(a.creative_id for a in self.arms)

    @property
    def num_arms(self) -> int:
        return len(self.arms)


def _logit(p: np.ndarray | float) -> np.ndarray:
    p = np.asarray(p, np.float64)
    return np.log(p) - np.log1p(-p)


def _level_probs(marginals: dict[str, Any]) -> np.ndarray:
    """(G, Lmax) probabilities for one segment (BASE_MARGINALS + overrides)."""
    lmax = features.COLUMN_TABLE.shape[1]
    out = np.zeros((len(features.CONTEXT_SPEC), lmax))
    for g, (key, levels) in enumerate(features.CONTEXT_SPEC):
        spec = marginals.get(key, BASE_MARGINALS[key])
        if key in features.BOOL_KEYS:
            p_true = float(spec)  # bool groups hold P(True)
            out[g, :2] = (1.0 - p_true, p_true)
        else:
            w = np.array([float(spec.get(str(lv), 0.0)) for lv in levels])
            if w.sum() <= 0:
                raise ValueError(f"marginal for {key} has no mass")
            out[g, : len(levels)] = w / w.sum()
    return out


def _expected_features(level_probs: np.ndarray) -> np.ndarray:
    """E[x | segment] (d,) from the segment's level probabilities."""
    m = np.zeros(features.DIM + 1)
    for g in range(level_probs.shape[0]):
        np.add.at(m, features.COLUMN_TABLE[g], level_probs[g])
    m = m[: features.DIM]
    m[0] = 1.0
    return m


def _rank_ctrs(knots: tuple[float, ...], k: int, scale: float) -> np.ndarray:
    """K marginal CTRs best -> worst, interpolating ``knots`` in logit space."""
    lk = _logit(np.asarray(knots) * scale)
    return np.interp(np.linspace(0, 1, k), np.linspace(0, 1, len(knots)), lk)


def _greedy_winners(sc: ScenarioConfig, adj: dict[str, np.ndarray]) -> list[int]:
    k = len(next(iter(adj.values())))
    available: list[int] = []
    winners = []
    for seg in sc.segments:
        if not available:
            available = list(range(k))
        scores = adj[seg.winner_key]
        best = max(available, key=lambda a: (scores[a], -a))
        winners.append(best)
        available.remove(best)
    return winners


def _calibrate_alpha(logits_wo_alpha: np.ndarray, target: float) -> float:
    lo, hi = -20.0, 10.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if np.mean(1.0 / (1.0 + np.exp(-(mid + logits_wo_alpha)))) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def build_true_model(
    cfg: ExperimentConfig,
    key: Array,
    *,
    scenario: ScenarioConfig | None = None,
    calibration_samples: int = 50_000,
) -> Environment:
    """Build the ground truth for ``cfg`` (arms, scenario, CTR/reward mode, T).

    ``scenario`` overrides the preset named by ``cfg.scenario`` (e.g. a custom
    segment mix). Deterministic in ``key``.
    """
    sc = scenario or load_scenario(cfg.scenario)
    arms = cfg.arms
    k, d, s_count = len(arms), features.DIM, len(sc.segments)
    k_b, k_u, k_theta, k_calib = jax.random.split(key, 4)
    flip = 1.0 - 2.0 * sc.judge_wrong

    def adjusted(dim: str) -> np.ndarray:
        raw = np.array([a.overall if dim == "overall" else a.score(dim) for a in arms])
        return flip * (raw - raw.mean())

    dims = {"overall", "audience_fit", "trend_authenticity", "stopping_power"}
    dims |= {seg.winner_key for seg in sc.segments}
    adj = {dim: adjusted(dim) for dim in dims}
    noise_b = np.asarray(jax.random.normal(k_b, (k,)), np.float64) * sc.noise_sd
    noise_u = np.asarray(jax.random.normal(k_u, (s_count, k)), np.float64) * sc.noise_sd
    scale = sc.ctr_scale(cfg.ctr_mode)
    target = sc.target_ctr[cfg.ctr_mode]

    # arm base
    if sc.arm_effect == "rank_ctrs":
        order = np.argsort(-(sc.kappa * adj["overall"] + noise_b), kind="stable")
        rank_logits = _rank_ctrs(sc.rank_ctrs, k, scale)
        b = np.empty(k)
        b[order] = rank_logits
        b -= b.mean()
    else:
        b = sc.kappa * adj["overall"] + noise_b

    # context marginals per segment
    level_probs = np.stack([_level_probs(seg.marginals) for seg in sc.segments])
    m_seg = np.stack([_expected_features(lp) for lp in level_probs])  # (S, d)
    names = features.feature_names()
    col = names.index

    # segment affinity
    p_topic = m_seg[:, col("topic_matches_trend")]
    p_interest = m_seg[:, col("interest_matches_product")]
    u = (
        sc.lam
        * (
            p_interest[:, None] * adj["audience_fit"][None, :]
            + p_topic[:, None] * adj["trend_authenticity"][None, :]
        )
        + noise_u
    )

    # context x arm interactions
    theta = np.asarray(jax.random.normal(k_theta, (k, d)), np.float64) * sc.theta_sd
    theta[:, 0] = 0.0
    theta[:, col("topic_matches_trend")] += sc.eta * adj["trend_authenticity"]
    theta[:, col("interest_matches_product")] += sc.eta * adj["audience_fit"]
    for dev in ("devicetype=desktop", "devicetype=tablet"):
        theta[:, col(dev)] -= sc.eta * adj["stopping_power"]

    winners: tuple[int, ...] | None = None
    if sc.arm_effect == "segment_winners":
        winners = tuple(_greedy_winners(sc, adj))
        lift_logit = float(_logit(target + sc.lift_pp * scale) - _logit(target))
        for s_idx, w in enumerate(winners):
            v = b + u[s_idx] + theta @ m_seg[s_idx]
            best_other = np.max(np.delete(v, w))
            u[s_idx, w] += best_other + lift_logit - v[w]

    # alpha by bisection on a fixed calibration sample
    with np.errstate(divide="ignore"):  # zero-weight segments -> -inf logits
        seg_logits = np.log(np.array([seg.weight for seg in sc.segments]))
    with np.errstate(divide="ignore"):
        level_logits = np.log(level_probs)
    partial = TrueModel(
        alpha=jnp.zeros((), jnp.float32),
        arm_base=jnp.asarray(b, jnp.float32),
        seg_aff=jnp.asarray(u, jnp.float32),
        theta=jnp.asarray(theta, jnp.float32),
        dwell_means=jnp.zeros((s_count, k), jnp.float32),
        seg_logits=jnp.asarray(seg_logits, jnp.float32),
        level_logits=jnp.asarray(level_logits, jnp.float32),
        drift_perm=jnp.arange(k, dtype=jnp.int32),
        drift_start=jnp.asarray(jnp.inf, jnp.float32),
        drift_width=jnp.zeros((), jnp.float32),
    )
    seg, _, X = sample_contexts(k_calib, partial, calibration_samples)
    seg_np, X_np = np.asarray(seg), np.asarray(X, np.float64)
    base_logits = b[None, :] + u[seg_np] + X_np @ theta.T
    alpha = _calibrate_alpha(base_logits, target)

    # drift: best and worst arm (pre-drift marginal CTR) swap identities
    perm = np.arange(k)
    start, width = np.inf, 0.0
    if sc.drift.kind != "none":
        marg = np.mean(1.0 / (1.0 + np.exp(-(alpha + base_logits))), axis=0)
        best, worst = int(np.argmax(marg)), int(np.argmin(marg))
        perm[best], perm[worst] = worst, best
        center = sc.drift.at_frac * cfg.horizon
        if sc.drift.kind == "abrupt":
            start = center
        else:
            width = max(sc.drift.width_frac * cfg.horizon, 1.0)
            start = center - width / 2

    dwell = (
        sc.dwell_base_s
        * np.array([seg.dwell_factor for seg in sc.segments])[:, None]
        * np.exp(sc.dwell_score_scale * adj["overall"])[None, :]
    )
    model = partial._replace(
        alpha=jnp.asarray(alpha, jnp.float32),
        dwell_means=jnp.asarray(dwell, jnp.float32),
        drift_perm=jnp.asarray(perm, jnp.int32),
        drift_start=jnp.asarray(start, jnp.float32),
        drift_width=jnp.asarray(width, jnp.float32),
    )
    return Environment(
        model=model,
        arms=arms,
        segment_names=tuple(seg.name for seg in sc.segments),
        scenario=sc,
        ctr_mode=cfg.ctr_mode,
        reward_mode=cfg.reward_mode,
        horizon=cfg.horizon,
        target_ctr=target,
        reward_scale=sc.dwell_base_s if cfg.reward_mode == "engaged" else 1.0,
        winners=winners,
    )


_COLUMN_TABLE = jnp.asarray(features.COLUMN_TABLE)


@functools.partial(jax.jit, static_argnames=("n",))
def sample_contexts(key: Array, model: TrueModel, n: int) -> tuple[Array, Array, Array]:
    """Sample ``n`` users: ``(segments (n,), levels (n, G), X (n, d))``.

    Segment first (scenario mix), then each context group from that segment's
    conditional marginal. ``levels`` decode to contract §4 dicts via
    ``decode_contexts``; ``X`` is their ``ctx-v1`` encoding.
    """
    k_seg, k_lvl = jax.random.split(key)
    seg = jax.random.categorical(k_seg, model.seg_logits, shape=(n,))
    levels = jax.random.categorical(k_lvl, model.level_logits[seg], axis=-1)
    cols = _COLUMN_TABLE[jnp.arange(_COLUMN_TABLE.shape[0])[None, :], levels]
    X = jax.nn.one_hot(cols, features.DIM + 1, dtype=jnp.float32).sum(axis=1)
    X = X[:, : features.DIM].at[:, 0].set(1.0)
    return seg.astype(jnp.int32), levels.astype(jnp.int32), X


def decode_contexts(levels: Array | np.ndarray) -> list[dict[str, object]]:
    """Level indices -> contract §4 context dicts (no IDs / sensitive fields)."""
    return features.decode_levels(np.asarray(levels))


def drift_weight(model: TrueModel, t: Array) -> Array:
    """Post-drift mixing weight in [0, 1] for round indices ``t``."""
    t = jnp.asarray(t, jnp.float32)
    ramp = jnp.clip(
        (t - model.drift_start) / jnp.maximum(model.drift_width, 1e-6), 0, 1
    )
    step = (t >= model.drift_start).astype(jnp.float32)
    return jnp.where(model.drift_width > 0, ramp, step)


@jax.jit
def click_probs(model: TrueModel, X: Array, segments: Array, t: Array) -> Array:
    """True click probabilities (n, K) for contexts ``X``, segments, round ``t``."""
    pre = (
        model.alpha
        + model.arm_base[None, :]
        + model.seg_aff[segments]
        + X @ model.theta.T
    )
    post = pre[:, model.drift_perm]
    w = drift_weight(model, jnp.broadcast_to(t, segments.shape))[:, None]
    return jax.nn.sigmoid((1.0 - w) * pre + w * post)


def expected_rewards(model: TrueModel, p: Array, segments: Array, mode: str) -> Array:
    """True expected reward (n, K): p for clicks, p × dwell mean for engaged."""
    if mode == "engaged":
        return p * model.dwell_means[segments]
    return p


def sample_rewards(
    key: Array, p: Array, mode: str, dwell_means: Array
) -> tuple[Array, Array]:
    """Counterfactual rewards for every (row, arm): ``(clicked int32, reward)``.

    click = U < p with U ~ Uniform(0, 1); engaged reward = click × dwell_mean ×
    Exponential(1). The same key gives the same coin flips in both modes, and
    pre-sampling every arm's outcome gives common random numbers across
    policies (each policy reads the column it chose).
    """
    k_u, k_e = jax.random.split(key)
    clicked = (jax.random.uniform(k_u, p.shape) < p).astype(jnp.int32)
    if mode == "engaged":
        dwell = jnp.broadcast_to(dwell_means, p.shape) * jax.random.exponential(
            k_e, p.shape
        )
        return clicked, clicked * dwell
    return clicked, clicked.astype(jnp.float32)


def optimal_arms(values: Array, eligible: Array | None = None) -> Array:
    """Argmax arm per row of ``values`` (n, K), restricted to ``eligible``."""
    if eligible is not None:
        values = jnp.where(eligible, values, -jnp.inf)
    return jnp.argmax(values, axis=-1).astype(jnp.int32)


def marginal_ctrs(
    env: Environment, key: Array, n: int = 50_000, t: int = 0
) -> dict[str, np.ndarray]:
    """True CTR per arm at round ``t``: ``overall`` (K,) and ``by_segment`` (S, K)."""
    seg, _, X = sample_contexts(key, env.model, n)
    p = np.asarray(click_probs(env.model, X, seg, jnp.full(seg.shape, t)))
    seg_np = np.asarray(seg)
    by_seg = np.stack(
        [
            p[seg_np == s].mean(0)
            if np.any(seg_np == s)
            else np.full(p.shape[1], np.nan)
            for s in range(len(env.segment_names))
        ]
    )
    return {"overall": p.mean(0), "by_segment": by_seg}
