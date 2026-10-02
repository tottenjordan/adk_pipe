"""Pure aggregation of ``bandit_episode_metrics`` rows into ``ExperimentMetrics``.

Input rows follow docs/bandit/contracts.md §3 (one per (episode, policy); the JSON
columns may arrive as strings from BigQuery or as already-parsed objects). Output is
the §5 ``ExperimentMetrics`` shape (camelCase). Pure Python on purpose: the api image
has no numpy/JAX.

Statistics, across episodes, per policy:
- curves: per checkpoint, the mean with a 95% confidence interval on the mean
  (Student t, sample std); with one episode the band collapses to the mean.
- totals: mean and sample std of ``total_reward``.
- armShare: ``linear_ts`` only, the per-window mean share by creative id.
- perSegment: per segment, the mean ``pct_optimal`` / ``avg_reward`` per policy.
- arms: ``linear_ts`` arm stats summed across episodes (CTR = clicks / impressions).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

ENDPOINT_POLICY = "linear_ts"
# Contracts order: the endpoint policy first, the oracle last, the baselines between.
_POLICY_ORDER = ("linear_ts", "ucb1", "epsilon_greedy", "beta_bernoulli_ts", "uniform")
_LAST = "oracle"
_CURVE_KEYS = {
    "cum_avg_reward": "cumAvgReward",
    "cum_regret": "cumRegret",
    "pct_optimal": "pctOptimal",
}
# Two-sided 95% Student t critical values by degrees of freedom (1..30); 1.96 beyond.
_T95 = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)  # fmt: skip


def empty_metrics(experiment_id: str) -> dict:
    return {
        "experimentId": experiment_id,
        "episodes": 0,
        "horizon": None,
        "checkpoints": [],
        "policies": [],
        "curves": {},
        "totals": {},
        "armShare": {},
        "perSegment": {},
        "arms": [],
    }


def _json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value


def order_policies(names: Iterable[str]) -> list[str]:
    """``linear_ts`` first, then the known baselines, unknown ones (sorted), oracle last."""
    present = set(names)
    known = [p for p in _POLICY_ORDER if p in present]
    extra = sorted(present - set(_POLICY_ORDER) - {_LAST})
    return known + extra + ([_LAST] if _LAST in present else [])


def t_critical(n: int) -> float:
    """Two-sided 95% t critical value for a sample of size ``n`` (df = n - 1)."""
    df = n - 1
    if df < 1:
        return 0.0
    return _T95[df - 1] if df <= len(_T95) else 1.96


def mean_std(values: Sequence[float]) -> tuple[float, float]:
    """Mean and sample std (0.0 for fewer than two values)."""
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    mean = math.fsum(values) / n
    if n < 2:
        return mean, 0.0
    var = math.fsum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, math.sqrt(var)


# Natural bounds per curve: a confidence interval on a bounded quantity is clamped
# to its range (a rate can't be < 0 or > 1; reward and regret are never negative).
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


def band(
    series: Sequence[Sequence[float]],
    bounds: tuple[float | None, float | None] = (None, None),
) -> dict:
    """``{mean, lo, hi}`` per position across ``series`` (95% CI on the mean).

    Series are truncated to the shortest one so every position has every episode.
    ``lo``/``hi`` are clamped to ``bounds`` (see ``CURVE_BOUNDS``)."""
    if not series:
        return {"mean": [], "lo": [], "hi": []}
    length = min(len(s) for s in series)
    out: dict[str, list[float]] = {"mean": [], "lo": [], "hi": []}
    t = t_critical(len(series))
    for i in range(length):
        mean, std = mean_std([float(s[i]) for s in series])
        half = t * std / math.sqrt(len(series)) if len(series) > 1 else 0.0
        out["mean"].append(mean)
        out["lo"].append(_clamp(mean - half, *bounds))
        out["hi"].append(_clamp(mean + half, *bounds))
    return out


def _mean_lists(lists: Sequence[Sequence[float]]) -> list[float]:
    if not lists:
        return []
    length = min(len(v) for v in lists)
    return [math.fsum(float(v[i]) for v in lists) / len(lists) for i in range(length)]


def _per_segment(by_policy: Mapping[str, list[dict]], policies: list[str]) -> dict:
    optimal_votes: dict[str, dict[str, int]] = {}
    acc: dict[str, dict[str, dict[str, list[float]]]] = {}
    for policy in policies:
        for row in by_policy[policy]:
            for seg, stats in _json(row.get("per_segment"), {}).items():
                if not isinstance(stats, Mapping):
                    continue
                if arm := stats.get("optimal_arm"):
                    votes = optimal_votes.setdefault(seg, {})
                    votes[arm] = votes.get(arm, 0) + 1
                slot = acc.setdefault(seg, {}).setdefault(
                    policy, {"pct": [], "reward": []}
                )
                slot["pct"].append(float(stats.get("pct_optimal") or 0.0))
                slot["reward"].append(float(stats.get("avg_reward") or 0.0))
    out = {}
    for seg in sorted(acc):
        votes = optimal_votes.get(seg, {})
        optimal = max(votes, key=lambda a: (votes[a], a)) if votes else ""
        out[seg] = {
            "optimalArm": optimal,
            "policies": {
                p: {
                    "pctOptimal": mean_std(acc[seg][p]["pct"])[0],
                    "avgReward": mean_std(acc[seg][p]["reward"])[0],
                }
                for p in policies
                if p in acc[seg]
            },
        }
    return out


def _arm_stats(rows: list[dict], arm_order: Sequence[str] | None) -> list[dict]:
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        for cid, stats in _json(row.get("arm_stats"), {}).items():
            if not isinstance(stats, Mapping):
                continue
            t = totals.setdefault(
                cid, {"impressions": 0, "clicks": 0.0, "est": [], "true": []}
            )
            impressions = int(stats.get("impressions") or 0)
            t["impressions"] += impressions
            if "clicks" in stats:
                t["clicks"] += float(stats.get("clicks") or 0)
            else:  # no click count: reconstruct from the per-episode estimate
                t["clicks"] += float(stats.get("estimated_ctr") or 0.0) * impressions
            t["true"].append(float(stats.get("true_ctr") or 0.0))
    order = [c for c in (arm_order or []) if c in totals]
    order += [c for c in totals if c not in order]
    return [
        {
            "creativeId": cid,
            "impressions": totals[cid]["impressions"],
            "estimatedCtr": (
                totals[cid]["clicks"] / totals[cid]["impressions"]
                if totals[cid]["impressions"]
                else 0.0
            ),
            "trueCtr": mean_std(totals[cid]["true"])[0],
        }
        for cid in order
    ]


def aggregate_episode_metrics(
    rows: Iterable[Mapping[str, Any]],
    experiment_id: str = "",
    arm_order: Sequence[str] | None = None,
) -> dict:
    """§3 ``bandit_episode_metrics`` rows -> §5 ``ExperimentMetrics``.

    ``arm_order`` (creative ids) orders the ``arms`` list; unknown ids follow in
    first-seen order. Duplicate (episode, policy) rows keep the last one."""
    dedup: dict[tuple[int, str], dict] = {}
    for raw in rows:
        row = dict(raw)
        if not experiment_id:
            experiment_id = str(row.get("experiment_id") or "")
        dedup[(int(row.get("episode") or 0), str(row.get("policy") or ""))] = row
    if not dedup:
        return empty_metrics(experiment_id)

    ordered = [dedup[k] for k in sorted(dedup)]
    policies = order_policies(r["policy"] for r in ordered)
    by_policy: dict[str, list[dict]] = {p: [] for p in policies}
    for row in ordered:
        by_policy[row["policy"]].append(row)

    curves_json = {id(r): _json(r.get("curve"), {}) for r in ordered}
    checkpoints: list[int] = []
    for row in by_policy.get(ENDPOINT_POLICY) or ordered:
        if cps := curves_json[id(row)].get("checkpoints"):
            checkpoints = [int(c) for c in cps]
            break

    curves = {}
    totals = {}
    for policy in policies:
        prow = by_policy[policy]
        curves[policy] = {
            camel: band(
                [
                    curves_json[id(r)][snake]
                    for r in prow
                    if isinstance(curves_json[id(r)].get(snake), list)
                ],
                CURVE_BOUNDS.get(snake, (None, None)),
            )
            for snake, camel in _CURVE_KEYS.items()
        }
        mean, std = mean_std([float(r.get("total_reward") or 0.0) for r in prow])
        totals[policy] = {"mean": mean, "std": std}

    share_lists: dict[str, list[list[float]]] = {}
    for row in by_policy.get(ENDPOINT_POLICY, []):
        for cid, shares in _json(row.get("arm_share"), {}).items():
            if isinstance(shares, list):
                share_lists.setdefault(cid, []).append(shares)
    share_order = [c for c in (arm_order or []) if c in share_lists]
    share_order += [c for c in share_lists if c not in share_order]

    horizons = [int(r["horizon"]) for r in ordered if r.get("horizon") is not None]
    return {
        "experimentId": experiment_id,
        "episodes": len({ep for ep, _ in dedup}),
        "horizon": max(horizons) if horizons else None,
        "checkpoints": checkpoints,
        "policies": policies,
        "curves": curves,
        "totals": totals,
        "armShare": {cid: _mean_lists(share_lists[cid]) for cid in share_order},
        "perSegment": _per_segment(by_policy, policies),
        "arms": _arm_stats(by_policy.get(ENDPOINT_POLICY, []), arm_order),
    }
