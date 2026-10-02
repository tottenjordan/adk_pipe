"""Cross-episode aggregation -> contracts §5 ``ExperimentMetrics`` (snake_case).

Dependency-free (stdlib only, no numpy/JAX) so the api can vendor this module
as-is and convert keys to camelCase at its boundary. Input rows are
``bandit_episode_metrics`` rows (``bandit.metrics.episode_metrics`` output or
BigQuery rows whose JSON payload columns are still strings).

Output (snake_case mirror of §5)::

    {experiment_id, episodes, horizon, checkpoints, policies,
     curves: {policy: {cum_avg_reward: Band, cum_regret: Band, pct_optimal: Band}},
     totals: {policy: {mean, std}},           # total reward per episode
     arm_share: {creative_id: [...]},         # endpoint policy only
     per_segment: {segment: {optimal_arm, policies: {policy: {pct_optimal, avg_reward}}}},
     arms: [{creative_id, impressions, estimated_ctr, true_ctr}]}   # endpoint policy

- ``Band`` = ``{mean, lo, hi}`` per checkpoint: mean ± t₀.₉₇₅(n-1)·sd/√n
  (sample sd; lo = hi = mean when n = 1).
- ``totals.std`` is the sample std (ddof=1; 0 for one episode).
- ``policies`` order: ``linear_ts``, then the known baselines in contract order
  (``ucb1``, ``epsilon_greedy``, ``beta_bernoulli_ts``, ``uniform``), then any
  other labels (e.g. ``linear_ts:discount=0.98``) sorted, ``oracle`` last.
- ``arm_share`` and ``arms`` come from ``linear_ts`` rows only (empty if absent):
  ``impressions`` = summed over episodes, ``estimated_ctr`` = pooled clicks /
  pooled impressions, ``true_ctr`` = mean across episodes.
- ``per_segment.optimal_arm`` = most common per-episode value (ties -> the
  larger creative id); segments sorted by name.

These semantics deliberately match the api's own aggregator
(``runserver/experiments_metrics.py``, PR 4), which emits the camelCase form.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

_PAYLOADS = ("curve", "arm_share", "per_segment", "arm_stats")
_CURVE_KEYS = ("cum_avg_reward", "cum_regret", "pct_optimal")

# two-sided 95% Student-t quantiles for df = 1..30
_T975 = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)  # fmt: skip


def t_quantile_975(df: int) -> float:
    if df < 1:
        return 0.0
    return _T975[df - 1] if df <= len(_T975) else 1.96


def _r(x: float) -> float:
    return float(f"{x:.6g}")


def mean_ci(values: Sequence[float]) -> tuple[float, float, float]:
    """(mean, lo, hi) with a 95% Student-t interval."""
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, mean, mean
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    half = t_quantile_975(n - 1) * sd / math.sqrt(n)
    return mean, mean - half, mean + half


# Mirrors runserver/experiments_metrics.CURVE_BOUNDS: CIs on bounded curves are
# clamped to their natural range.
CURVE_BOUNDS: dict[str, tuple[float | None, float | None]] = {
    "cum_avg_reward": (0.0, None),
    "cum_regret": (0.0, None),
    "pct_optimal": (0.0, 1.0),
}


def _clamp(v: float, lo: float | None, hi: float | None) -> float:
    if lo is not None and v < lo:
        return lo
    if hi is not None and v > hi:
        return hi
    return v


def _band(
    series: Sequence[Sequence[float]],
    bounds: tuple[float | None, float | None] = (None, None),
) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {"mean": [], "lo": [], "hi": []}
    for col in zip(*series, strict=True):
        m, lo, hi = mean_ci(col)
        out["mean"].append(_r(m))
        out["lo"].append(_r(_clamp(lo, *bounds)))
        out["hi"].append(_r(_clamp(hi, *bounds)))
    return out


def _normalise(row: Mapping[str, Any]) -> dict[str, Any]:
    r = dict(row)
    for key in _PAYLOADS:
        if isinstance(r.get(key), str):
            r[key] = json.loads(r[key])
    return r


ENDPOINT_POLICY = "linear_ts"
_POLICY_ORDER = ("linear_ts", "ucb1", "epsilon_greedy", "beta_bernoulli_ts", "uniform")


def order_policies(names: Iterable[str]) -> list[str]:
    """``linear_ts``, known baselines, other labels (sorted), ``oracle`` last."""
    present = set(names)
    known = [p for p in _POLICY_ORDER if p in present]
    extra = sorted(present - set(_POLICY_ORDER) - {"oracle"})
    return known + extra + (["oracle"] if "oracle" in present else [])


def sample_std(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def aggregate_episode_metrics(
    rows: Sequence[Mapping[str, Any]], experiment_id: str | None = None
) -> dict[str, Any]:
    """Aggregate per-(episode, policy) rows into the §5 ``ExperimentMetrics`` shape."""
    rows = [_normalise(r) for r in rows]
    exp_id = experiment_id or (rows[0].get("experiment_id") if rows else None) or ""
    if not rows:
        return {
            "experiment_id": exp_id,
            "episodes": 0,
            "horizon": None,
            "checkpoints": [],
            "policies": [],
            "curves": {},
            "totals": {},
            "arm_share": {},
            "per_segment": {},
            "arms": [],
        }
    horizons = {int(r["horizon"]) for r in rows}
    checkpoints = rows[0]["curve"]["checkpoints"]
    if len(horizons) != 1:
        raise ValueError(f"rows mix horizons {sorted(horizons)}")
    if any(r["curve"]["checkpoints"] != checkpoints for r in rows):
        raise ValueError("rows have different checkpoints")
    policies = order_policies(r["policy"] for r in rows)
    by_policy = {p: [r for r in rows if r["policy"] == p] for p in policies}
    episodes = max(len({r["episode"] for r in rs}) for rs in by_policy.values())

    curves = {
        p: {k: _band([r["curve"][k] for r in rs], CURVE_BOUNDS[k]) for k in _CURVE_KEYS}
        for p, rs in by_policy.items()
    }
    totals = {}
    for p, rs in by_policy.items():
        vals = [float(r["total_reward"]) for r in rs]
        totals[p] = {"mean": _r(sum(vals) / len(vals)), "std": _r(sample_std(vals))}

    endpoint = by_policy.get(ENDPOINT_POLICY, [])
    arm_ids = list(endpoint[0]["arm_stats"]) if endpoint else []
    arm_share = {
        cid: [
            _r(sum(col) / len(col))
            for col in zip(*(r["arm_share"][cid] for r in endpoint), strict=True)
        ]
        for cid in arm_ids
    }
    arms = []
    for cid in arm_ids:
        stats = [r["arm_stats"][cid] for r in endpoint]
        imps = sum(int(s["impressions"]) for s in stats)
        clicks = sum(s["clicks"] for s in stats)
        arms.append(
            {
                "creative_id": cid,
                "impressions": imps,
                "estimated_ctr": _r(clicks / imps) if imps else 0.0,
                "true_ctr": _r(sum(s["true_ctr"] for s in stats) / len(stats)),
            }
        )

    per_segment: dict[str, Any] = {}
    segments: list[str] = []
    for r in rows:
        segments.extend(s for s in r["per_segment"] if s not in segments)
    for seg in sorted(segments):
        votes = Counter(
            r["per_segment"][seg]["optimal_arm"]
            for r in rows
            if seg in r["per_segment"]
        )
        opt = max(votes, key=lambda a: (votes[a], a))
        pols = {}
        for p, rs in by_policy.items():
            vals = [r["per_segment"][seg] for r in rs if seg in r["per_segment"]]
            if vals:
                pols[p] = {
                    "pct_optimal": _r(sum(v["pct_optimal"] for v in vals) / len(vals)),
                    "avg_reward": _r(sum(v["avg_reward"] for v in vals) / len(vals)),
                }
        per_segment[seg] = {"optimal_arm": opt, "policies": pols}

    return {
        "experiment_id": exp_id,
        "episodes": episodes,
        "horizon": horizons.pop(),
        "checkpoints": list(checkpoints),
        "policies": policies,
        "curves": curves,
        "totals": totals,
        "arm_share": arm_share,
        "per_segment": per_segment,
        "arms": arms,
    }


def summarize_totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-policy scalar summary (not part of §5): mean/std of total reward,
    clicks, pseudo-regret and % optimal, plus steps_to_converge (mean over
    converged episodes) and the converged-episode count."""
    out: dict[str, dict[str, Any]] = {}
    for p in order_policies(r["policy"] for r in rows):
        rs = [r for r in rows if r["policy"] == p]
        summary: dict[str, Any] = {"episodes": len(rs)}
        for key in ("total_reward", "total_clicks", "cumulative_regret", "pct_optimal"):
            vals = [float(r[key]) for r in rs]
            m, lo, hi = mean_ci(vals)
            std = sample_std(vals)
            summary[key] = {"mean": _r(m), "std": _r(std), "lo": _r(lo), "hi": _r(hi)}
        conv = [
            r["steps_to_converge"] for r in rs if r.get("steps_to_converge") is not None
        ]
        summary["steps_to_converge"] = {
            "mean": _r(sum(conv) / len(conv)) if conv else None,
            "converged": len(conv),
        }
        out[p] = summary
    return out
