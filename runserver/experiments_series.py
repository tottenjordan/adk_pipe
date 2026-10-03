"""Pure aggregation of ``bandit_events`` rows into the contracts §8 ``CreativeSeries``.

Inputs are the raw rows of the three ``runserver.experiments_store`` series queries:
- ``rows``: one per (arm, episode, win) with ``impressions``, ``clicks``,
  ``horizon`` and ``n_windows`` (``build_creative_series_sql``);
- ``segment_rows``: ``(segment, optimal_arm, n)`` (``build_segment_winners_sql``);
- ``true_rows``: ``(arm, true_ctr)`` (``build_true_ctr_sql``).

Per window: ``share`` is the mean across episodes of each episode's share of that
window's impressions (normalized to sum to 1 across creatives); ``ctr`` is pooled
clicks / impressions (``None`` with no impressions); ``cumClicks`` is the mean
cumulative clicks per episode at the window end. Pure Python: the api image has no
numpy/JAX.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

_DIGITS = 4


def _r(value: float) -> float:
    return round(value, _DIGITS)


def _windows(horizon: int, n: int) -> list[dict]:
    """``n`` equal ``[start, end)`` round windows; matches the SQL ``DIV`` binning."""
    return [
        {"start": -(-w * horizon // n), "end": -(-(w + 1) * horizon // n)}
        for w in range(n)
    ]


def _arm_ids(arms: Iterable[Any]) -> list[str]:
    ids: list[str] = []
    for arm in arms:
        cid = arm.get("creativeId") if isinstance(arm, Mapping) else arm
        if cid and str(cid) not in ids:
            ids.append(str(cid))
    return ids


def segment_winners(segment_rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """``{segment: most frequent optimal_arm}`` (ties broken by arm id)."""
    counts: dict[str, Counter] = defaultdict(Counter)
    for row in segment_rows:
        seg, arm = row.get("segment"), row.get("optimal_arm")
        if seg is None or arm is None:
            continue
        counts[str(seg)][str(arm)] += int(row.get("n") or 0)
    return {
        seg: min(c.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        for seg, c in counts.items()
        if c
    }


def _empty_creative(cid: str) -> dict:
    return {
        "creativeId": cid,
        "share": [],
        "ctr": [],
        "cumClicks": [],
        "impressions": 0,
        "clicks": 0,
        "trueCtr": None,
        "segmentsWon": [],
        "finalShare": 0.0,
    }


def build_creative_series(
    rows: Iterable[Mapping[str, Any]],
    segment_rows: Iterable[Mapping[str, Any]],
    true_rows: Iterable[Mapping[str, Any]],
    arms: Sequence[Any],
    windows: int = 20,
    experiment_id: str = "",
) -> dict:
    """The §8 ``CreativeSeries`` (camelCase), creatives ordered by ``finalShare``
    desc (ties keep the experiment's arm order). ``arms`` is the experiment row's
    §5 arm list (or bare creative ids); arms seen only in the events are appended."""
    rows = list(rows)
    order = _arm_ids(arms)
    for row in rows:
        if row.get("arm") is not None and str(row["arm"]) not in order:
            order.append(str(row["arm"]))
    if not rows:
        return {
            "experimentId": experiment_id,
            "episodes": 0,
            "horizon": None,
            "windows": [],
            "creatives": [_empty_creative(cid) for cid in order],
        }

    horizon = int(rows[0]["horizon"])
    n = int(rows[0].get("n_windows") or min(windows, horizon))
    imps: dict[tuple[str, int, int], int] = defaultdict(int)
    clicks: dict[tuple[str, int, int], int] = defaultdict(int)
    ep_total: dict[tuple[int, int], int] = defaultdict(int)
    episodes: set[int] = set()
    for row in rows:
        cid, ep, w = str(row["arm"]), int(row["episode"]), int(row["win"])
        count = int(row.get("impressions") or 0)
        imps[(cid, ep, w)] += count
        clicks[(cid, ep, w)] += int(row.get("clicks") or 0)
        ep_total[(ep, w)] += count
        episodes.add(ep)
    n_eps = len(episodes)

    raw_share: dict[str, list[float]] = {}
    for cid in order:
        shares = []
        for w in range(n):
            per_ep = [
                imps[(cid, ep, w)] / ep_total[(ep, w)]
                for ep in episodes
                if ep_total[(ep, w)]
            ]
            shares.append(sum(per_ep) / len(per_ep) if per_ep else 0.0)
        raw_share[cid] = shares
    totals = [sum(raw_share[cid][w] for cid in order) for w in range(n)]

    true_ctr = {
        str(r["arm"]): float(r["true_ctr"])
        for r in true_rows
        if r.get("arm") is not None and r.get("true_ctr") is not None
    }
    won: dict[str, list[str]] = defaultdict(list)
    for seg, arm in sorted(segment_winners(segment_rows).items()):
        won[arm].append(seg)

    creatives: list[dict[str, Any]] = []
    for cid in order:
        share = [
            _r(raw_share[cid][w] / totals[w]) if totals[w] else 0.0 for w in range(n)
        ]
        w_imps = [sum(imps[(cid, ep, w)] for ep in episodes) for w in range(n)]
        w_clicks = [sum(clicks[(cid, ep, w)] for ep in episodes) for w in range(n)]
        cum: list[float] = []
        running = 0
        for c in w_clicks:
            running += c
            cum.append(_r(running / n_eps))
        creatives.append(
            {
                "creativeId": cid,
                "share": share,
                "ctr": [
                    _r(c / i) if i else None
                    for c, i in zip(w_clicks, w_imps, strict=True)
                ],
                "cumClicks": cum,
                "impressions": sum(w_imps),
                "clicks": sum(w_clicks),
                "trueCtr": _r(true_ctr[cid]) if cid in true_ctr else None,
                "segmentsWon": won.get(cid, []),
                "finalShare": share[-1] if share else 0.0,
            }
        )
    rank = {cid: i for i, cid in enumerate(order)}
    creatives.sort(key=lambda c: (-c["finalShare"], rank[c["creativeId"]]))
    return {
        "experimentId": experiment_id,
        "episodes": n_eps,
        "horizon": horizon,
        "windows": _windows(horizon, n),
        "creatives": creatives,
    }
