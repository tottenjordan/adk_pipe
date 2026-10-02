"""Per-episode bandit metrics -> contracts §3 payloads (numpy only, no JAX).

Terminology (fixed here, as in the contracts): *round* = one impression and
decision (1-based counts below), *batch* = rounds between two policy updates,
*horizon* T = rounds per episode, *episode* = one independent run.

Definitions, for one episode of one policy (per-round arrays from
``bandit.simulate``):

- **cumulative reward** R(t) = Σ_{i≤t} r_i; **cumulative average reward**
  R(t)/t (the notebooks' convergence plot).
- **pseudo-regret** Σ_{i≤t} (μ*_i - μ_{a_i,i}) with μ the true expected reward
  of round i (click prob in click mode, prob × dwell mean in engaged mode) and
  μ* its max over eligible arms. Always ≥ 0 and non-decreasing; this is the
  ``cumulative_regret`` / ``cum_regret`` everywhere.
- **realized regret** Σ (r*_i - r_i), r*_i = the reward the optimal arm would
  have produced in that round (same coin flips; common random numbers).
- **% optimal** = fraction of rounds where the chosen arm is the round's
  optimal arm (overall, cumulative in the curve, and per segment).
- **suboptimal pulls** per arm = pulls of that arm in rounds where it was not
  optimal; **regret by arm** = Σ gap over that arm's pulls (Σ over arms =
  pseudo-regret).
- **arm share** = share of pulls per arm in each checkpoint window
  (c_{j-1}, c_j].
- **steps_to_converge** (notebook): with a moving average of window w over the
  realized rewards and over μ*, the first round t ≥ w whose trailing
  w-round average reward reaches the trailing average optimum; ``None`` if it
  never does. w defaults to clamp(T // 10, 10, 2000).
- **checkpoints**: ~50 log-spaced 1-based round counts ending at T, shared by
  all policies/episodes of one experiment (``log_checkpoints``).

Cross-episode aggregation (mean ± 95% CI bands, expected total reward ± std)
lives in the dependency-free ``bandit.aggregate``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np


def _f(x: float) -> float:
    """Compact float for JSON payloads (6 significant digits)."""
    return float(f"{float(x):.6g}")


def log_checkpoints(horizon: int, num: int = 50) -> list[int]:
    """About ``num`` unique log-spaced round counts in [1, horizon], ending at T."""
    if horizon <= num:
        return list(range(1, horizon + 1))
    count = num
    while True:
        pts = np.unique(np.round(np.geomspace(1, horizon, count)).astype(int))
        if len(pts) >= num or count > 4 * num:
            break
        count += 1
    pts = pts.tolist()
    if pts[-1] != horizon:
        pts.append(horizon)
    return [int(p) for p in pts]


def cumulative_average(x: np.ndarray) -> np.ndarray:
    """R(t)/t for t = 1..T."""
    x = np.asarray(x, np.float64)
    return np.cumsum(x) / np.arange(1, len(x) + 1)


def pseudo_regret(mean_opt: np.ndarray, mean_chosen: np.ndarray) -> np.ndarray:
    """Cumulative pseudo-regret Σ (μ* - μ_a) for t = 1..T."""
    return np.cumsum(np.asarray(mean_opt, np.float64) - np.asarray(mean_chosen))


def default_window(horizon: int) -> int:
    return int(min(2000, max(10, horizon // 10)))


def steps_to_converge(
    rewards: np.ndarray, optimum: np.ndarray | float, window: int
) -> int | None:
    """First round t ≥ window where the trailing ``window``-round mean reward
    reaches the trailing mean of ``optimum`` (scalar or per-round); else None."""
    rewards = np.asarray(rewards, np.float64)
    if len(rewards) < window:
        return None
    opt = np.broadcast_to(np.asarray(optimum, np.float64), rewards.shape)
    kernel = np.ones(window) / window
    ma = np.convolve(rewards, kernel, mode="valid")
    ma_opt = np.convolve(opt, kernel, mode="valid")
    hits = np.flatnonzero(ma >= ma_opt - 1e-12)
    return int(hits[0] + window) if len(hits) else None


def episode_metrics(
    out: Mapping[str, np.ndarray],
    *,
    policy: str,
    episode: int,
    arm_ids: Sequence[str],
    segment_names: Sequence[str],
    checkpoints: Sequence[int],
    window: int | None = None,
) -> dict[str, Any]:
    """One ``bandit_episode_metrics`` row (§3) from one episode's per-round arrays.

    JSON payload fields (``curve``, ``arm_share``, ``per_segment``,
    ``arm_stats``) are plain dicts here; the traffic job serialises them to
    JSON strings for BigQuery. Extra keys beyond the §3 columns:
    ``realized_regret``, ``optimal_avg_reward``, ``suboptimal_pulls``,
    ``regret_by_arm``.
    """
    arm = np.asarray(out["arm"])
    reward = np.asarray(out["reward"], np.float64)
    clicked = np.asarray(out["clicked"])
    opt_arm = np.asarray(out["opt_arm"])
    seg = np.asarray(out["segment"])
    mean_opt = np.asarray(out["mean_opt"], np.float64)
    gap = mean_opt - np.asarray(out["mean_chosen"], np.float64)
    horizon = len(arm)
    k = len(arm_ids)
    cps = np.asarray(checkpoints, int)
    if cps[-1] != horizon:
        raise ValueError(f"last checkpoint {cps[-1]} != horizon {horizon}")
    idx = cps - 1
    is_opt = arm == opt_arm

    curve = {
        "checkpoints": [int(c) for c in cps],
        "cum_avg_reward": [_f(v) for v in cumulative_average(reward)[idx]],
        "cum_regret": [_f(v) for v in np.cumsum(gap)[idx]],
        "pct_optimal": [_f(v) for v in cumulative_average(is_opt)[idx]],
    }

    pulls = np.zeros((len(cps), k))
    edges = np.concatenate([[0], cps])
    onehot = np.zeros((horizon, k))
    onehot[np.arange(horizon), arm] = 1.0
    csum = np.concatenate([np.zeros((1, k)), np.cumsum(onehot, axis=0)])
    pulls = csum[edges[1:]] - csum[edges[:-1]]
    share = pulls / np.maximum(pulls.sum(axis=1, keepdims=True), 1)
    arm_share = {cid: [_f(v) for v in share[:, a]] for a, cid in enumerate(arm_ids)}

    per_segment: dict[str, dict[str, Any]] = {}
    for s, name in enumerate(segment_names):
        m = seg == s
        n = int(m.sum())
        if n == 0:
            continue
        seg_opt = int(np.bincount(opt_arm[m], minlength=k).argmax())
        per_segment[name] = {
            "optimal_arm": arm_ids[seg_opt],
            "pct_optimal": _f(is_opt[m].mean()),
            "avg_reward": _f(reward[m].mean()),
            "rounds": n,
        }

    p_all = np.asarray(out["p_all"], np.float64)
    arm_stats: dict[str, dict[str, Any]] = {}
    for a, cid in enumerate(arm_ids):
        m = arm == a
        imps, clicks = int(m.sum()), int(clicked[m].sum())
        arm_stats[cid] = {
            "impressions": imps,
            "clicks": clicks,
            "estimated_ctr": _f(clicks / imps) if imps else 0.0,
            "true_ctr": _f(p_all[:, a].mean()),
        }

    return {
        "policy": policy,
        "episode": int(episode),
        "horizon": horizon,
        "total_reward": _f(reward.sum()),
        "total_clicks": int(clicked.sum()),
        "cumulative_regret": _f(gap.sum()),
        "realized_regret": _f(np.sum(np.asarray(out["reward_opt"]) - reward)),
        "pct_optimal": _f(is_opt.mean()),
        "steps_to_converge": steps_to_converge(
            reward, mean_opt, window or default_window(horizon)
        ),
        "optimal_avg_reward": _f(mean_opt.mean()),
        "curve": curve,
        "arm_share": arm_share,
        "per_segment": per_segment,
        "arm_stats": arm_stats,
        "suboptimal_pulls": {
            cid: int(np.sum((arm == a) & ~is_opt)) for a, cid in enumerate(arm_ids)
        },
        "regret_by_arm": {
            cid: _f(gap[arm == a].sum()) for a, cid in enumerate(arm_ids)
        },
    }


def experiment_rows(result: Any, num_checkpoints: int = 50) -> list[dict[str, Any]]:
    """Per-(episode, policy) rows for a ``bandit.simulate.ExperimentResult``."""
    env = result.env
    horizon = result.cfg.horizon
    cps = log_checkpoints(horizon, num_checkpoints)
    rows = []
    for policy in result.policies:
        outs = result.results[policy]
        for e in range(outs["arm"].shape[0]):
            ep = {k: v[e] for k, v in outs.items()}
            row = episode_metrics(
                ep,
                policy=policy,
                episode=e,
                arm_ids=env.arm_ids,
                segment_names=env.segment_names,
                checkpoints=cps,
            )
            row["experiment_id"] = result.cfg.experiment_id
            rows.append(row)
    return rows
