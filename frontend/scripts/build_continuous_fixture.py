"""Build the continuous-learning screenshot fixtures from a REAL traffic-job run.

Runs the synthetic-traffic job in process (``bandit_traffic.main --in-process
--dry-run --learning continuous``, contracts §11: one reset, the endpoint's
Linear TS posterior carried across every segment) on the first live
experiment's three creatives (segment_winners, demo click rates), 20 segments
of 40,000 rounds = 800,000 rounds in one stream. Then it shapes the job's JSONL
rows the way the api does for a continuous run (PR B, contracts §11):

- ``/metrics``: the api's own per-row aggregation
  (``runserver.experiments_metrics.aggregate_episode_metrics``) for totals (per
  segment), perSegment and arms, plus the continuous fields (``learning``,
  ``horizon`` = rounds covered, ``segmentHorizon``, ``segmentStarts``), with the
  curves stitched by the §11 concatenation rule
  onto global round checkpoints (cumulative values carried across segments,
  ``lo = hi = mean``: no bands) and ``continuousSummary``, the endpoint − best
  baseline per-segment paired ``total_clicks`` difference with a batch-means
  95% interval over the post-warm-up segments (the plan's
  ``batch_means_summary`` rules: warm-up = first half, ≥ 5 kept segments, no
  significant linear trend, |lag-1 autocorrelation| ≤ 0.2);
- ``/creatives``: ``runserver.experiments_series.build_creative_series`` over
  the events binned on the GLOBAL round (one stream: a single "episode").

It writes:

- ``screenshot-fixtures/continuous-experiment.json``           ExperimentSummary + trafficRuns (run 2 continuous)
- ``screenshot-fixtures/continuous-experiment-metrics.json``   ExperimentMetrics + continuousSummary
- ``screenshot-fixtures/continuous-experiment-creatives.json`` CreativeSeries (global windows)

Regenerate with (about 5 minutes, ~600 MB of JSONL under the work dir)::

    GOOGLE_CLOUD_PROJECT=test-project PYTHONPATH="$PWD" \\
        uv run python frontend/scripts/build_continuous_fixture.py [--work /tmp/tt-continuous]
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from bandit.config import (
    LinTSParams,
    arm_from_dict,
    build_sim_config,
    experiment_config_to_dict,
    scenario_noise_var,
)
from runserver.experiments_metrics import aggregate_episode_metrics, t_critical
from runserver.experiments_series import build_creative_series

FIX = Path(__file__).parent / "screenshot-fixtures"
SEGMENTS = 20
HORIZON = 40_000
SEED = 0
EXPERIMENT_ID = "c0a7e1b25f3d4e98"
SCENARIO = "segment_winners"
BASELINES = ("ucb1", "epsilon_greedy", "beta_bernoulli_ts", "uniform")
ROUND = 4


def r4(v: float) -> float:
    return float(round(float(v), ROUND))


# ── Batch means (the plan's runserver/batch_means.py spec, PR B) ──────────────


def batch_means_summary(
    diffs: list[float],
    warmup_frac: float = 0.5,
    min_batches: int = 5,
    max_lag1: float = 0.2,
) -> dict[str, Any]:
    """Drop the warm-up (first ``warmup_frac`` of segments), then mean ± t·s/√m."""
    n = len(diffs)
    warm = int(math.floor(warmup_frac * n))
    kept = diffs[warm:]
    m = len(kept)
    mean = sum(kept) / m if m else 0.0
    out: dict[str, Any] = {
        "warmup": warm,
        "mean": mean,
        "lo": None,
        "hi": None,
        "lag1": None,
    }
    if m < min_batches:
        return {**out, "status": "too_few_segments"}
    dev = [x - mean for x in kept]
    ss = sum(d * d for d in dev)
    lag1 = sum(dev[i] * dev[i + 1] for i in range(m - 1)) / ss if ss > 0 else 0.0
    out["lag1"] = lag1
    # Linear trend: OLS slope over the kept segments, t-test at 5% (df = m − 2).
    xs = list(range(m))
    xbar = sum(xs) / m
    sxx = sum((x - xbar) ** 2 for x in xs)
    slope = sum((x - xbar) * d for x, d in zip(xs, dev, strict=True)) / sxx
    resid = [d - slope * (x - xbar) for x, d in zip(xs, dev, strict=True)]
    se = math.sqrt(sum(r * r for r in resid) / (m - 2) / sxx) if m > 2 else 0.0
    if se > 0 and abs(slope / se) > t_critical(m - 1):
        return {**out, "status": "still_trending"}
    if abs(lag1) > max_lag1:
        return {**out, "status": "autocorrelated"}
    s = math.sqrt(ss / (m - 1))
    half = t_critical(m) * s / math.sqrt(m)
    return {**out, "lo": mean - half, "hi": mean + half, "status": "ok"}


# ── The real run ──────────────────────────────────────────────────────────────


def run_job(work: Path, live: dict) -> Path:
    arms = tuple(
        arm_from_dict(
            {
                "creative_id": a["creativeId"],
                "label": a["conceptName"],
                "scores": a["scores"],
            }
        )
        for a in live["arms"]
    )
    cfg = build_sim_config(
        SCENARIO,
        arms=arms,
        episodes=SEGMENTS,
        seed=SEED,
        policy=LinTSParams(noise_var=scenario_noise_var(SCENARIO, "demo", "click")),
        experiment_id=EXPERIMENT_ID,
    )
    work.mkdir(parents=True, exist_ok=True)
    config = work / "experiment.json"
    config.write_text(json.dumps(experiment_config_to_dict(cfg)))
    out = work / "out"
    if (out / "episode_metrics.jsonl").exists():
        print("reusing", out)
        return out
    cmd = [
        sys.executable,
        "-m",
        "bandit_traffic.main",
        "--in-process",
        "--config",
        str(config),
        "--episodes",
        str(SEGMENTS),
        "--horizon",
        str(HORIZON),
        "--learning",
        "continuous",
        "--traffic-run",
        "2",
        "--dry-run",
        "--out",
        str(out),
    ]
    print("running", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return out


# ── Shaping (contracts §11 concatenation rule) ────────────────────────────────


def flat(v: list[float]) -> dict:
    """One stream, no replications: lo = hi = mean (no band)."""
    xs = [r4(x) for x in v]
    return {"mean": xs, "lo": list(xs), "hi": list(xs)}


def stitch(
    rows: list[dict],
) -> tuple[list[int], dict[str, dict], dict[str, list[float]]]:
    """Per policy: one curve over global checkpoints; cumulative values carry across segments."""
    by_policy: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_policy[r["policy"]].append(r)
    checkpoints: list[int] = []
    curves: dict[str, dict] = {}
    for p, prow in by_policy.items():
        prow.sort(key=lambda r: r["episode"])
        reward_carry = regret_carry = optimal_carry = 0.0
        xs: list[int] = []
        avg: list[float] = []
        regret: list[float] = []
        optimal: list[float] = []
        for r in prow:
            c = json.loads(r["curve"])
            start = int(c["segment_start"])
            for k, cp in enumerate(c["checkpoints"]):
                g = start + int(cp)
                xs.append(g)
                avg.append((c["cum_avg_reward"][k] * cp + reward_carry) / g)
                regret.append(c["cum_regret"][k] + regret_carry)
                optimal.append((c["pct_optimal"][k] * cp + optimal_carry) / g)
            reward_carry += float(r["total_reward"])
            regret_carry += float(r["cumulative_regret"])
            optimal_carry += float(r["pct_optimal"]) * int(r["horizon"])
        curves[p] = {
            "cumAvgReward": flat(avg),
            "cumRegret": flat(regret),
            "pctOptimal": flat(optimal),
        }
        if p == "linear_ts" or not checkpoints:
            checkpoints = xs
    # The endpoint's arm share per checkpoint window, segment after segment.
    share: dict[str, list[float]] = defaultdict(list)
    for r in sorted(by_policy["linear_ts"], key=lambda r: r["episode"]):
        for cid, vals in json.loads(r["arm_share"]).items():
            share[cid].extend(r4(v) for v in vals)
    return checkpoints, curves, dict(share)


def series_from_events(
    path: Path, arm_ids: list[str], total: int, windows: int = 20
) -> dict:
    """§8 series over the GLOBAL round (one stream), via the api's pure aggregation."""
    agg: dict[tuple[str, int], list[int]] = defaultdict(lambda: [0, 0])
    winners: dict[tuple[str, str], int] = defaultdict(int)
    true: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
    cells: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    with path.open() as fh:
        for line in fh:
            e = json.loads(line)
            if e.get("policy") != "linear_ts":
                continue
            arm, seg = e["arm"], e["segment"]
            w = int(e["round"]) * windows // total
            a = agg[(arm, w)]
            a[0] += 1
            a[1] += int(e["clicked"])
            winners[(seg, e["optimal_arm"])] += 1
            t = true[arm]
            t[0] += float(e["p_chosen"])
            t[1] += 1
            c = cells[(arm, seg)]
            c["impressions"] += 1
            c["clicks"] += int(e["clicked"])
            c["p_sum"] += float(e["p_chosen"])
            c["p_n"] += 1
            c["regret_sum"] += float(e["regret"])
    rows = [
        {
            "arm": arm,
            "episode": 0,
            "win": w,
            "impressions": v[0],
            "clicks": v[1],
            "horizon": total,
            "n_windows": windows,
        }
        for (arm, w), v in agg.items()
    ]
    segment_rows = [
        {"segment": s, "optimal_arm": a, "n": n} for (s, a), n in winners.items()
    ]
    true_rows = [{"arm": a, "true_ctr": t[0] / t[1]} for a, t in true.items() if t[1]]
    creative_segment_rows = [
        {"arm": a, "segment": s, **c} for (a, s), c in cells.items()
    ]
    return build_creative_series(
        rows,
        segment_rows,
        true_rows,
        arm_ids,
        windows=windows,
        experiment_id=EXPERIMENT_ID,
        creative_segment_rows=creative_segment_rows,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--work",
        default="/tmp/tt-continuous",
        help="job config + JSONL output directory",
    )
    args = ap.parse_args()
    live = json.loads((FIX / "live-experiment.json").read_text())
    out = run_job(Path(args.work), live)
    rows = [json.loads(line) for line in (out / "episode_metrics.jsonl").open()]
    arm_ids = [a["creativeId"] for a in live["arms"]]

    metrics = aggregate_episode_metrics(rows, EXPERIMENT_ID, arm_ids)
    checkpoints, curves, share = stitch(rows)
    metrics["checkpoints"] = checkpoints
    metrics["curves"] = {p: curves[p] for p in metrics["policies"]}
    metrics["armShare"] = {cid: share[cid] for cid in arm_ids if cid in share}
    metrics["run"] = 2
    # PR B's continuous fields: `horizon` = rounds covered, plus the segment length and starts.
    starts = sorted({int(json.loads(r["curve"])["segment_start"]) for r in rows})
    metrics["learning"] = "continuous"
    metrics["horizon"] = checkpoints[-1]
    metrics["segmentHorizon"] = HORIZON
    metrics["segmentStarts"] = starts

    # continuousSummary: endpoint − best baseline (by whole-run total), per segment.
    clicks: dict[str, dict[int, float]] = defaultdict(dict)
    for r in rows:
        clicks[r["policy"]][int(r["episode"])] = float(r["total_clicks"])
    best = max(BASELINES, key=lambda p: sum(clicks[p].values()))
    segs = sorted(clicks["linear_ts"])
    diffs = [clicks["linear_ts"][s] - clicks[best][s] for s in segs]
    bm = batch_means_summary(diffs)
    metrics["continuousSummary"] = {
        "segments": len(segs),
        "warmupSegments": bm["warmup"],
        "pairedDiff": {
            "policy": best,
            "perSegment": [r4(d) for d in diffs],
            "mean": r4(bm["mean"]),
            "lo": None if bm["lo"] is None else r4(bm["lo"]),
            "hi": None if bm["hi"] is None else r4(bm["hi"]),
            "lag1": None if bm["lag1"] is None else r4(bm["lag1"]),
            "status": bm["status"],
        },
    }

    total = SEGMENTS * HORIZON
    series = series_from_events(out / "events.jsonl", arm_ids, total)
    series["run"] = 2

    summary = {
        **{k: v for k, v in live.items() if k != "progress"},
        "experimentId": EXPERIMENT_ID,
        "status": "ready",
        "progress": {"episodesDone": SEGMENTS, "episodesTotal": SEGMENTS},
        "policyDiscount": 1.0,
        "trafficRuns": [
            {
                "run": 1,
                "startedAt": "2026-10-06T14:02:11Z",
                "episodes": 20,
                "horizon": HORIZON,
                "shifts": [],
                "forget": False,
                "status": "finished",
                "learning": "per_episode",
            },
            {
                "run": 2,
                "startedAt": "2026-10-06T14:40:05Z",
                "episodes": SEGMENTS,
                "horizon": HORIZON,
                "shifts": [],
                "forget": False,
                "status": "finished",
                "learning": "continuous",
            },
        ],
    }
    (FIX / "continuous-experiment.json").write_text(
        json.dumps(summary, indent=1) + "\n"
    )
    (FIX / "continuous-experiment-metrics.json").write_text(json.dumps(metrics) + "\n")
    (FIX / "continuous-experiment-creatives.json").write_text(json.dumps(series) + "\n")
    print("best baseline", best, "| per-segment diffs", [round(d) for d in diffs])
    print(
        "continuousSummary",
        metrics["continuousSummary"]["pairedDiff"] | {"perSegment": "…"},
    )


if __name__ == "__main__":
    main()
