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
  all policies/episodes of one experiment (``log_checkpoints``). With scripted
  shifts (contracts §10): linear spacing, plus points around each shift round
  (``merge_checkpoints``).
- **shift response** (``shift_response``): per shift, % optimal and regret per
  round in the windows just before / after it, and the rounds needed to get
  back to 80 % of the pre-shift % optimal; ``regime_stats`` splits
  ``per_segment`` and the true CTR per arm at the shift rounds.

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


def _per_segment(
    seg: np.ndarray,
    opt_arm: np.ndarray,
    is_opt: np.ndarray,
    reward: np.ndarray,
    arm_ids: Sequence[str],
    segment_names: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """§3 ``per_segment``: modal optimal arm, % optimal, avg reward, rounds."""
    out: dict[str, dict[str, Any]] = {}
    for s, name in enumerate(segment_names):
        m = seg == s
        n = int(m.sum())
        if n == 0:
            continue
        seg_opt = int(np.bincount(opt_arm[m], minlength=len(arm_ids)).argmax())
        out[name] = {
            "optimal_arm": arm_ids[seg_opt],
            "pct_optimal": _f(is_opt[m].mean()),
            "avg_reward": _f(reward[m].mean()),
            "rounds": n,
        }
    return out


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

    per_segment = _per_segment(seg, opt_arm, is_opt, reward, arm_ids, segment_names)

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


def linear_checkpoints(horizon: int, num: int) -> list[int]:
    """``num`` evenly spaced round counts ending at T (offline drift/injection
    plots that need resolution late in the episode; the contract default is
    ``log_checkpoints``)."""
    pts = np.unique(np.round(np.linspace(horizon / num, horizon, num)).astype(int))
    return [int(p) for p in pts if p >= 1]


def make_checkpoints(horizon: int, num: int = 50, spacing: str = "log") -> list[int]:
    if spacing == "log":
        return log_checkpoints(horizon, num)
    if spacing == "linear":
        return linear_checkpoints(horizon, num)
    raise ValueError(f"unknown checkpoint spacing {spacing!r}")


def merge_checkpoints(
    checkpoints: Sequence[int], shift_rounds: Sequence[int], horizon: int
) -> list[int]:
    """``checkpoints`` plus, per shift round r (0-based first shifted round, so
    checkpoint r covers exactly the pre-shift rounds): r-1, r and r + 0.5 %,
    2 % and 5 % of the horizon. Sorted, unique, within [1, T], ending at T."""
    pts = {int(c) for c in checkpoints}
    for r in shift_rounds:
        pts |= {r - 1, r}
        pts |= {r + int(round(f * horizon)) for f in (0.005, 0.02, 0.05)}
    pts.add(horizon)
    return sorted(p for p in pts if 1 <= p <= horizon)


def shift_response(
    out: Mapping[str, np.ndarray],
    shift_rounds: Sequence[int],
    window: int | None = None,
    recovery_window: int | None = None,
    recovery_level: float = 0.8,
) -> list[dict[str, Any]]:
    """How one episode's policy reacted to each shift (contracts §10).

    Per shift round r (0-based first shifted round), with ``window`` w (default
    ``default_window(T)``, 2000 at the preset horizons) clipped to the episode:

    - ``pct_optimal_before`` / ``_after``: share of optimal choices in the w
      rounds before r / from r;
    - ``regret_rate_before`` / ``_after``: mean pseudo-regret per round there;
    - ``recovery_rounds``: rounds from r until the trailing
      ``recovery_window``-round optimal-choice rate (default w // 2, 1000 at
      the presets; post-shift rounds only) first reaches ``recovery_level``
      (80 %) of ``pct_optimal_before``; ``None`` if it never does.
    """
    arm = np.asarray(out["arm"])
    is_opt = (arm == np.asarray(out["opt_arm"])).astype(np.float64)
    gap = np.asarray(out["mean_opt"], np.float64) - np.asarray(
        out["mean_chosen"], np.float64
    )
    horizon = len(arm)
    w = window or default_window(horizon)
    wr = recovery_window or max(1, w // 2)
    csum = np.concatenate([[0.0], np.cumsum(is_opt)])
    rows = []
    for r in shift_rounds:
        r = int(r)
        lo, hi = max(0, r - w), min(horizon, r + w)
        before = is_opt[lo:r].mean() if r > lo else 0.0
        after = is_opt[r:hi].mean() if hi > r else 0.0
        recovery = None
        ends = np.arange(r + wr, horizon + 1)
        if len(ends):
            rate = (csum[ends] - csum[ends - wr]) / wr
            hits = np.flatnonzero(rate >= recovery_level * before - 1e-12)
            if len(hits):
                recovery = int(ends[hits[0]] - r)
        rows.append(
            {
                "round": r,
                "pct_optimal_before": _f(before),
                "pct_optimal_after": _f(after),
                "regret_rate_before": _f(gap[lo:r].mean()) if r > lo else 0.0,
                "regret_rate_after": _f(gap[r:hi].mean()) if hi > r else 0.0,
                "recovery_rounds": recovery,
            }
        )
    return rows


def regime_stats(
    out: Mapping[str, np.ndarray],
    boundaries: Sequence[int],
    arm_ids: Sequence[str],
    segment_names: Sequence[str],
) -> list[dict[str, Any]]:
    """Per-regime ``per_segment`` (§3 shape) and true CTR per arm for one
    episode. ``boundaries`` (0-based rounds, e.g. the shift and shock-end rounds)
    split [0, T) into regimes ``[start, end)``; empty regimes are dropped."""
    arm = np.asarray(out["arm"])
    horizon = len(arm)
    seg = np.asarray(out["segment"])
    opt_arm = np.asarray(out["opt_arm"])
    reward = np.asarray(out["reward"], np.float64)
    p_all = np.asarray(out["p_all"], np.float64)
    is_opt = arm == opt_arm
    edges = sorted({0, horizon} | {int(b) for b in boundaries if 0 < b < horizon})
    regimes = []
    for start, end in zip(edges[:-1], edges[1:], strict=True):
        sl = slice(start, end)
        regimes.append(
            {
                "start": start,
                "end": end,
                "per_segment": _per_segment(
                    seg[sl], opt_arm[sl], is_opt[sl], reward[sl], arm_ids, segment_names
                ),
                "true_ctr": {
                    cid: _f(p_all[sl, a].mean()) for a, cid in enumerate(arm_ids)
                },
            }
        )
    return regimes


def summarize_shift_response(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Per policy, per shift: the mean of each ``shift_response`` number across
    episodes; ``recovery_rounds`` averages the episodes that recovered
    (``recovered_episodes`` of ``episodes``; ``None`` when none did)."""
    by_policy: dict[str, list[list[dict[str, Any]]]] = {}
    for row in rows:
        if row.get("shift_response"):
            by_policy.setdefault(row["policy"], []).append(row["shift_response"])
    out: dict[str, list[dict[str, Any]]] = {}
    for policy, episodes in by_policy.items():
        summary = []
        for j, first in enumerate(episodes[0]):
            per_ep = [ep[j] for ep in episodes]
            entry: dict[str, Any] = {"round": first["round"], "episodes": len(per_ep)}
            for key in (
                "pct_optimal_before",
                "pct_optimal_after",
                "regret_rate_before",
                "regret_rate_after",
            ):
                entry[key] = _f(np.mean([e[key] for e in per_ep]))
            rec = [
                e["recovery_rounds"] for e in per_ep if e["recovery_rounds"] is not None
            ]
            entry["recovery_rounds"] = _f(np.mean(rec)) if rec else None
            entry["recovered_episodes"] = len(rec)
            summary.append(entry)
        out[policy] = summary
    return out


def experiment_rows(
    result: Any,
    num_checkpoints: int = 50,
    spacing: str = "log",
    shift_rounds: Sequence[int] = (),
) -> list[dict[str, Any]]:
    """Per-(episode, policy) rows for a ``bandit.simulate.ExperimentResult``.

    With ``shift_rounds`` the checkpoints are merged around each shift
    (``merge_checkpoints``) and every row gains ``shift_response``."""
    env = result.env
    horizon = result.cfg.horizon
    cps = make_checkpoints(horizon, num_checkpoints, spacing)
    if shift_rounds:
        cps = merge_checkpoints(cps, shift_rounds, horizon)
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
            if shift_rounds:
                row["shift_response"] = shift_response(ep, shift_rounds)
            row["experiment_id"] = result.cfg.experiment_id
            rows.append(row)
    return rows
