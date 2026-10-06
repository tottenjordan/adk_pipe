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

Traffic runs with scripted shifts (contracts §10) add:
- shiftResponse: per policy (the ghost ``linear_ts_unshifted`` included), per
  shift in time order, each ``shift_response`` number as mean ± 95% CI across
  episodes; ``recoveryRounds`` over the episodes that recovered.
- regimes: ``perSegment`` and the per-arm true CTR split at the shift (and
  shock-end) rounds, from the rows' ``regimes`` JSON (no extra query). The
  whole-run fields keep their meaning.
- resolvedShifts: the run's shifts as the traffic job resolved them (concrete
  ``creativeId`` for ``"leader"``, targets), read from the resolved fields on the
  rows' ``shift_response`` entries; omitted for older runs.

The ghost's segment winners come from the unshifted world, so it never votes on
an ``optimalArm`` (whole run or per regime).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

log = logging.getLogger(__name__)

ENDPOINT_POLICY = "linear_ts"
# Linear TS replayed on the same draws without the run's shifts (contracts §10).
GHOST_POLICY = "linear_ts_unshifted"
# Contracts order: the endpoint policy (then its ghost) first, the oracle last, the
# baselines between.
_POLICY_ORDER = (
    "linear_ts",
    GHOST_POLICY,
    "ucb1",
    "epsilon_greedy",
    "beta_bernoulli_ts",
    "uniform",
)
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
        "shiftResponse": {},
        "regimes": [],
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


def _per_segment(
    by_policy: Mapping[str, list[dict]],
    policies: list[str],
    per_segment_of: Callable[[dict], Any] | None = None,
) -> dict:
    """``perSegment`` from each row's ``per_segment`` (or ``per_segment_of(row)``,
    e.g. one regime's). The ghost policy's rows never vote on ``optimalArm``."""
    optimal_votes: dict[str, dict[str, int]] = {}
    acc: dict[str, dict[str, dict[str, list[float]]]] = {}
    for policy in policies:
        for row in by_policy[policy]:
            raw = (
                per_segment_of(row)
                if per_segment_of is not None
                else _json(row.get("per_segment"), {})
            )
            if not isinstance(raw, Mapping):
                continue
            for seg, stats in raw.items():
                if not isinstance(stats, Mapping):
                    continue
                if policy != GHOST_POLICY and (arm := stats.get("optimal_arm")):
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


# Natural bounds of the shift_response numbers (CI clamped like CURVE_BOUNDS).
_SHIFT_KEYS: dict[str, tuple[str, tuple[float | None, float | None]]] = {
    "pct_optimal_before": ("pctOptimalBefore", (0.0, 1.0)),
    "pct_optimal_after": ("pctOptimalAfter", (0.0, 1.0)),
    "regret_rate_before": ("regretRateBefore", (0.0, None)),
    "regret_rate_after": ("regretRateAfter", (0.0, None)),
}


def stat(
    values: Sequence[float], bounds: tuple[float | None, float | None] = (None, None)
) -> dict:
    """``{mean, lo, hi}``: mean ± 95% CI across ``values`` (clamped to ``bounds``)."""
    b = band([[float(v)] for v in values], bounds)
    if not b["mean"]:
        return {"mean": 0.0, "lo": 0.0, "hi": 0.0}
    return {"mean": b["mean"][0], "lo": b["lo"][0], "hi": b["hi"][0]}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def aggregate_shift_response(by_policy: Mapping[str, list[dict]]) -> dict:
    """Per policy, per shift (the rows' ``shift_response`` order: time order): each
    number as ``stat`` across episodes. ``recoveryRounds`` is the ``stat`` over the
    episodes that recovered (``None`` when none did; ``recoveredEpisodes`` counts
    them). Rows without ``shift_response`` are skipped; a policy whose episodes
    disagree on the shift count is cut to the shortest list."""
    out: dict[str, list[dict]] = {}
    for policy, rows in by_policy.items():
        episodes = [
            [e for e in sr if isinstance(e, Mapping)]
            for sr in (_json(r.get("shift_response"), None) for r in rows)
            if isinstance(sr, list) and sr
        ]
        if not episodes:
            continue
        entries = []
        for j in range(min(len(ep) for ep in episodes)):
            per_ep = [ep[j] for ep in episodes]
            entry: dict[str, Any] = {
                "round": int(per_ep[0].get("round") or 0),
                "episodes": len(per_ep),
            }
            for snake, (camel, bounds) in _SHIFT_KEYS.items():
                vals = [v for e in per_ep if (v := _finite(e.get(snake))) is not None]
                entry[camel] = stat(vals, bounds)
            rec = [
                v
                for e in per_ep
                if (v := _finite(e.get("recovery_rounds"))) is not None
            ]
            entry["recoveryRounds"] = stat(rec, (0.0, None)) if rec else None
            entry["recoveredEpisodes"] = len(rec)
            entries.append(entry)
        out[policy] = entries
    return out


def _opt_int(value: Any) -> int | None:
    return int(value) if _finite(value) is not None else None


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _resolved_entry(j: int, e: Mapping[str, Any]) -> dict:
    """One ``shift_response`` entry's resolved-shift fields, camelCase."""
    targets = e.get("targets")
    return {
        "index": idx if (idx := _opt_int(e.get("index"))) is not None else j,
        "kind": str(e["kind"]),
        "round": _opt_int(e.get("round")) or 0,
        "endRound": _opt_int(e.get("end_round")),
        "segment": _opt_str(e.get("segment")),
        "creativeId": _opt_str(e.get("creative_id")),
        "requestedCreativeId": _opt_str(e.get("requested_creative_id")),
        "targets": [
            {
                "segment": str(t.get("segment")),
                "ctrBefore": _finite(t.get("ctr_before")),
                "ctrAfter": _finite(t.get("ctr_after")),
            }
            for t in (targets if isinstance(targets, list) else [])
            if isinstance(t, Mapping)
        ],
    }


def extract_resolved_shifts(
    rows: Sequence[Mapping[str, Any]], experiment_id: str = ""
) -> list[dict] | None:
    """The run's resolved shifts (contracts §5/§10), camelCase, from the
    resolved fields the traffic job adds to each ``shift_response`` entry
    (``kind``, ``creative_id`` with ``"leader"`` made concrete, ...). They are
    identical across rows; the first row (in ``rows`` order) that has them wins,
    or, if rows disagree, the most common version (logged). ``None`` when no
    row carries them (runs written before the traffic job recorded them)."""
    counts: dict[str, int] = {}
    first: dict[str, list[dict]] = {}
    for row in rows:
        sr = _json(row.get("shift_response"), None)
        if not isinstance(sr, list) or not sr:
            continue
        if not all(isinstance(e, Mapping) and e.get("kind") for e in sr):
            continue
        resolved = [_resolved_entry(j, e) for j, e in enumerate(sr)]
        key = json.dumps(resolved, sort_keys=True)
        counts[key] = counts.get(key, 0) + 1
        first.setdefault(key, resolved)
    if not counts:
        return None
    if len(counts) > 1:
        log.warning(
            "experiment %s: resolved shifts differ across %d metrics rows "
            "(%d versions); using the most common",
            experiment_id,
            sum(counts.values()),
            len(counts),
        )
    # max keeps the first-seen version on a tie (dicts preserve insertion order)
    return first[max(counts, key=lambda k: counts[k])]


def aggregate_shift_cost(by_policy: Mapping[str, list[dict]]) -> dict | None:
    """What the run's shifts cost the endpoint: the **paired** per-episode
    difference ghost (``linear_ts_unshifted``) − endpoint (``linear_ts``) of
    ``total_clicks`` and ``total_reward``, matched by episode, as ``stat`` (mean ±
    95% t-interval, unclamped: a negative cost means the shift helped). Pairing is
    valid because both replay the same episode keys, and it removes the
    between-episode variance. ``episodes`` counts the reward pairs; clicks pair
    only where both rows have ``total_clicks`` (``clicksPerEpisode`` is ``None``
    when no pair has them). ``None`` without any pair."""
    lin = {int(r.get("episode") or 0): r for r in by_policy.get(ENDPOINT_POLICY, [])}
    ghost = {int(r.get("episode") or 0): r for r in by_policy.get(GHOST_POLICY, [])}
    reward_diffs: list[float] = []
    click_diffs: list[float] = []
    for ep in sorted(lin.keys() & ghost.keys()):
        g, e = ghost[ep], lin[ep]
        rg, re_ = _finite(g.get("total_reward")), _finite(e.get("total_reward"))
        if rg is not None and re_ is not None:
            reward_diffs.append(rg - re_)
        cg, ce = _finite(g.get("total_clicks")), _finite(e.get("total_clicks"))
        if cg is not None and ce is not None:
            click_diffs.append(cg - ce)
    if not reward_diffs:
        return None
    return {
        "episodes": len(reward_diffs),
        "clicksPerEpisode": stat(click_diffs) if click_diffs else None,
        "rewardPerEpisode": stat(reward_diffs),
    }


def aggregate_regimes(
    by_policy: Mapping[str, list[dict]],
    policies: list[str],
    arm_order: Sequence[str] | None = None,
) -> list[dict]:
    """Per regime (the rows' ``regimes`` order, i.e. by round): ``start`` /
    ``end`` (from the first row carrying regimes, the endpoint's when present),
    ``perSegment`` (same shape and rules as the whole-run field) and ``arms``
    ``[{creativeId, trueCtr}]``: the mean regime ``true_ctr`` over the endpoint's
    episodes (any non-ghost policy's when the endpoint has none)."""
    regimes_of = {
        id(r): [g for g in reg if isinstance(g, Mapping)]
        for rows in by_policy.values()
        for r in rows
        if isinstance(reg := _json(r.get("regimes"), None), list)
    }
    ordered = [p for p in policies if p != GHOST_POLICY] + (
        [GHOST_POLICY] if GHOST_POLICY in policies else []
    )
    template: list = []
    for policy in ordered:
        for r in by_policy[policy]:
            if regimes_of.get(id(r)):
                template = regimes_of[id(r)]
                break
        if template:
            break
    if not template:
        return []
    truth_rows = [
        r for r in by_policy.get(ENDPOINT_POLICY, []) if regimes_of.get(id(r))
    ]
    if not truth_rows:
        truth_rows = [
            r
            for p in policies
            if p != GHOST_POLICY
            for r in by_policy[p]
            if regimes_of.get(id(r))
        ]
    out = []
    for k, head in enumerate(template):

        def regime_k(row: dict, k: int = k) -> Any:
            reg = regimes_of.get(id(row)) or []
            return reg[k].get("per_segment") if k < len(reg) else None

        ctr: dict[str, list[float]] = {}
        for row in truth_rows:
            reg = regimes_of[id(row)]
            truth = reg[k].get("true_ctr") if k < len(reg) else None
            if not isinstance(truth, Mapping):
                continue
            for cid, v in truth.items():
                if (f := _finite(v)) is not None:
                    ctr.setdefault(str(cid), []).append(f)
        order = [c for c in (arm_order or []) if c in ctr]
        order += [c for c in ctr if c not in order]
        out.append(
            {
                "start": int(head.get("start") or 0),
                "end": int(head.get("end") or 0),
                "perSegment": _per_segment(by_policy, policies, regime_k),
                "arms": [
                    {"creativeId": cid, "trueCtr": mean_std(ctr[cid])[0]}
                    for cid in order
                ],
            }
        )
    return out


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
    body: dict[str, Any] = {
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
        "shiftResponse": aggregate_shift_response(by_policy),
        "regimes": aggregate_regimes(by_policy, policies, arm_order),
    }
    # Only for a run with a ghost replay (i.e. with shifts); omitted otherwise.
    if (cost := aggregate_shift_cost(by_policy)) is not None:
        body["shiftCost"] = cost
    # Omitted for runs whose rows predate the resolved shift_response fields.
    endpoint_first = [r for p in policies for r in by_policy[p]]
    if (resolved := extract_resolved_shifts(endpoint_first, experiment_id)) is not None:
        body["resolvedShifts"] = resolved
    return body
