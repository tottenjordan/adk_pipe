"""Build the scripted-shift screenshot fixtures from a REAL simulator run.

Runs ``bandit`` (dev group: JAX) on the first live experiment's three creatives
(segment_winners, demo, 40k rounds, 10 episodes) with the shift script the
editor screenshot shows (contracts §10):

1. at 50%, late night casual readers go off The Tone Dividend Bailout (demote, 2 pts);
2. from 60% to 75%, Ergonomic Lumbar Relief gets 40% fewer clicks (shock ×0.6);

with forgetting on, plus the ghost replay: Linear TS with the same policy and
episode keys on the same draws without the shifts, so it is identical to the
endpoint before the first shift. It writes the payloads in the api's exact
shapes (contracts §5 / §8 / §10 on the PR C branch):

- ``screenshot-fixtures/shift-experiment.json``           ExperimentSummary + trafficRuns
- ``screenshot-fixtures/shift-experiment-metrics.json``   ExperimentMetrics (run, shiftResponse, regimes, shiftCost)
- ``screenshot-fixtures/shift-experiment-creatives.json`` CreativeSeries (run, regimes)

Regenerate with::

    GOOGLE_CLOUD_PROJECT=test-project PYTHONPATH="$PWD" \
        uv run python frontend/scripts/build_shift_fixture.py
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import numpy as np

from bandit import environment as envm
from bandit.config import (
    LinTSParams,
    arm_from_dict,
    build_sim_config,
    default_noise_var,
    default_shift_discount,
    load_scenario,
    shifts_from_dict,
    validate_shifts,
)
from bandit.metrics import make_checkpoints, merge_checkpoints, shift_response
from bandit.policies import canonical_spec
from bandit.simulate import run_experiment

FIX = Path(__file__).parent / "screenshot-fixtures"
EPISODES = 10
SEED = 0
POLICIES = ["linear_ts", "ucb1", "epsilon_greedy", "beta_bernoulli_ts", "uniform", "oracle"]
GHOST = "linear_ts_unshifted"
EXPERIMENT_ID = "5d1f0c2a9b7e4f61"

# The script shown in 18-shift-timeline.png (REST camelCase, as trafficRuns records it).
SHIFTS_REST: list[dict[str, Any]] = [
    {"kind": "demote", "atFrac": 0.5, "segment": "late_night_casual", "creativeId": "aae3f6b4", "dropPp": 0.02},
    {"kind": "shock", "atFrac": 0.6, "untilFrac": 0.75, "segment": None, "creativeId": "8c0e9ee8", "ctrMultiplier": 0.6},
]
SNAKE = {
    "atFrac": "at_frac",
    "untilFrac": "until_frac",
    "creativeId": "creative_id",
    "dropPp": "drop_pp",
    "liftPp": "lift_pp",
    "ctrMultiplier": "ctr_multiplier",
    "segmentMix": "segment_mix",
}


def r4(v: float) -> float:
    return float(round(float(v), 4))


def band(x: np.ndarray) -> dict:
    """mean ± 95% CI across episodes (axis 0)."""
    m = x.mean(0)
    half = 1.96 * x.std(0, ddof=1) / np.sqrt(x.shape[0])
    return {"mean": [r4(v) for v in m], "lo": [r4(v) for v in m - half], "hi": [r4(v) for v in m + half]}


def stat(xs: list[float] | np.ndarray) -> dict | None:
    """contracts §5 Stat: {mean, lo, hi} (95% CI across episodes)."""
    a = np.asarray(xs, np.float64)
    if not a.size:
        return None
    half = 1.96 * a.std(ddof=1) / np.sqrt(len(a)) if len(a) > 1 else 0.0
    return {"mean": r4(a.mean()), "lo": r4(a.mean() - half), "hi": r4(a.mean() + half)}


def curves(out: dict, cps: np.ndarray) -> dict:
    arm = out["arm"]
    reward = out["reward"].astype(np.float64)
    gap = out["mean_opt"].astype(np.float64) - out["mean_chosen"].astype(np.float64)
    is_opt = (arm == out["opt_arm"]).astype(np.float64)
    t = np.arange(1, arm.shape[1] + 1)
    idx = cps - 1
    return {
        "cumAvgReward": band((np.cumsum(reward, 1) / t)[:, idx]),
        "cumRegret": band(np.cumsum(gap, 1)[:, idx]),
        "pctOptimal": band((np.cumsum(is_opt, 1) / t)[:, idx]),
    }


def per_segment(results: dict, segs: list[str], ids: list[str], sl: slice) -> dict:
    """§3 perSegment: optimal arm from the endpoint's rows (the ghost never votes)."""
    lin = results["linear_ts"]
    out: dict = {}
    for s, name in enumerate(segs):
        opt = lin["opt_arm"][:, sl][lin["segment"][:, sl] == s]
        if not opt.size:
            continue
        pols = {}
        for p, o in results.items():
            mm = o["segment"][:, sl] == s
            hit = (o["arm"][:, sl] == o["opt_arm"][:, sl])[mm]
            pols[p] = {"pctOptimal": r4(hit.mean()), "avgReward": r4(o["reward"][:, sl][mm].astype(np.float64).mean())}
        out[name] = {"optimalArm": ids[int(np.bincount(opt, minlength=len(ids)).argmax())], "policies": pols}
    return out


def seg_stats(lin: dict, a: int, sl: slice, segs: list[str], winners: dict[str, str], cid: str) -> list[dict]:
    rows = []
    for s, name in enumerate(segs):
        m = (lin["arm"][:, sl] == a) & (lin["segment"][:, sl] == s)
        imps = int(m.sum())
        clicks = int(lin["clicked"][:, sl][m].sum())
        rows.append(
            {
                "segment": name,
                "impressions": imps,
                "clicks": clicks,
                "ctr": r4(clicks / imps) if imps else None,
                "trueCtr": r4(lin["p_all"][:, sl, a][m].mean()) if imps else None,
                "isBest": winners.get(name) == cid,
            }
        )
    return sorted(rows, key=lambda r: r["segment"])


def main() -> None:
    live = json.loads((FIX / "live-experiment.json").read_text())
    arms = [{"creative_id": a["creativeId"], "label": a["conceptName"], "scores": a["scores"]} for a in live["arms"]]
    ids = [a["creative_id"] for a in arms]
    scenario = "segment_winners"
    sc = load_scenario(scenario)
    cfg = build_sim_config(
        scenario,
        arms=tuple(arm_from_dict(a) for a in arms),
        episodes=EPISODES,
        seed=SEED,
        policy=LinTSParams(noise_var=default_noise_var(sc.target_ctr["demo"], "click")),
        experiment_id=EXPERIMENT_ID,
    )
    horizon = cfg.horizon
    snake = [{SNAKE.get(k, k): v for k, v in s.items() if v is not None or k == "segment"} for s in SHIFTS_REST]
    shifts = validate_shifts(shifts_from_dict(snake), sc, cfg.arms, "demo")
    discount = default_shift_discount("demo", cfg.batch_size, horizon)
    cfg = dataclasses.replace(cfg, policy=dataclasses.replace(cfg.policy, discount=discount))

    shifted = run_experiment(cfg, [canonical_spec(p) for p in POLICIES], scenario=sc, shifts=shifts)
    # Same cfg, seed and episode keys, no shifts: the ghost replay.
    ghost = run_experiment(cfg, [canonical_spec("linear_ts")], scenario=sc)
    env = shifted.env
    resolved = envm.resolved_shifts(env)
    rounds = [rec["round"] for rec in resolved]
    boundaries = sorted({*rounds, *(rec["end_round"] for rec in resolved if rec.get("end_round"))})
    cps = np.asarray(merge_checkpoints(make_checkpoints(horizon, 50, "linear"), rounds, horizon))
    segs = list(env.segment_names)

    results = {p: {k: np.asarray(v) for k, v in shifted.results[p].items()} for p in shifted.policies}
    g = {k: np.asarray(v) for k, v in ghost.results["linear_ts"].items()}
    lin = results["linear_ts"]
    first = rounds[0]
    assert np.array_equal(lin["arm"][:, :first], g["arm"][:, :first]), "ghost must match the endpoint before shift 1"
    everyone = {**results, GHOST: g}
    order = ["linear_ts", GHOST, *POLICIES[1:-1], "oracle"]

    def ep(p: str, e: int) -> dict:
        return {k: v[e] for k, v in everyone[p].items()}

    # ── /metrics (contracts §5) ──
    edges = np.concatenate([[0], cps])
    arm_share = {}
    for a, cid in enumerate(ids):
        cs = np.concatenate([np.zeros((EPISODES, 1)), np.cumsum((lin["arm"] == a).astype(np.float64), 1)], 1)
        share = (cs[:, edges[1:]] - cs[:, edges[:-1]]) / np.maximum(np.diff(edges), 1)
        arm_share[cid] = [r4(v) for v in share.mean(0)]
    arm_rows = []
    for a, cid in enumerate(ids):
        m = lin["arm"] == a
        imps, clicks = int(m.sum()), int(lin["clicked"][m].sum())
        arm_rows.append(
            {
                "creativeId": cid,
                "impressions": imps,
                "estimatedCtr": r4(clicks / imps if imps else 0),
                "trueCtr": r4(lin["p_all"][..., a].mean()),
            }
        )
    shift_resp: dict[str, list[dict]] = {}
    for p in order:
        per_ep = [shift_response(ep(p, e), rounds) for e in range(EPISODES)]
        entries = []
        for j, r in enumerate(rounds):
            rows = [x[j] for x in per_ep]
            rec = [x["recovery_rounds"] for x in rows if x["recovery_rounds"] is not None]
            entries.append(
                {
                    "round": r,
                    "episodes": EPISODES,
                    "pctOptimalBefore": stat([x["pct_optimal_before"] for x in rows]),
                    "pctOptimalAfter": stat([x["pct_optimal_after"] for x in rows]),
                    "regretRateBefore": stat([x["regret_rate_before"] for x in rows]),
                    "regretRateAfter": stat([x["regret_rate_after"] for x in rows]),
                    "recoveryRounds": stat(rec),
                    "recoveredEpisodes": len(rec),
                }
            )
        shift_resp[p] = entries
    reg_edges = [0, *boundaries, horizon]
    regimes_m = [
        {
            "start": s0,
            "end": s1,
            "perSegment": per_segment(everyone, segs, ids, slice(s0, s1)),
            "arms": [{"creativeId": cid, "trueCtr": r4(lin["p_all"][:, s0:s1, a].mean())} for a, cid in enumerate(ids)],
        }
        for s0, s1 in zip(reg_edges[:-1], reg_edges[1:], strict=True)
    ]
    metrics = {
        "experimentId": EXPERIMENT_ID,
        "episodes": EPISODES,
        "horizon": horizon,
        "checkpoints": [int(c) for c in cps],
        "policies": order,
        "curves": {p: curves(everyone[p], cps) for p in order},
        "totals": {
            p: {"mean": r4(everyone[p]["reward"].sum(1).mean()), "std": r4(everyone[p]["reward"].sum(1).std(ddof=1))}
            for p in order
        },
        "armShare": arm_share,
        "perSegment": per_segment(everyone, segs, ids, slice(0, horizon)),
        "arms": arm_rows,
        "run": 2,
        "shiftResponse": shift_resp,
        "regimes": regimes_m,
        # Paired per-episode difference, ghost − endpoint (same readers in both).
        "shiftCost": {
            "episodes": EPISODES,
            "clicksPerEpisode": stat(g["clicked"].sum(1) - lin["clicked"].sum(1)),
            "rewardPerEpisode": stat(g["reward"].sum(1) - lin["reward"].sum(1)),
        },
    }

    # ── /creatives (contracts §8) ──
    nw = 20
    win = [(int(np.ceil(w * horizon / nw)), int(np.ceil((w + 1) * horizon / nw))) for w in range(nw)]
    whole = slice(0, horizon)

    def winners(sl: slice) -> dict[str, str]:
        out = {}
        for s, name in enumerate(segs):
            opt = lin["opt_arm"][:, sl][lin["segment"][:, sl] == s]
            out[name] = ids[int(np.bincount(opt, minlength=len(ids)).argmax())]
        return out

    w_all = winners(whole)
    creatives: list[dict[str, Any]] = []
    for a, cid in enumerate(ids):
        m = lin["arm"] == a
        share, ctr, cum = [], [], []
        running = 0.0
        for lo, hi in win:
            mm = m[:, lo:hi]
            share.append(r4(mm.mean()))
            imps = int(mm.sum())
            clicks = int(lin["clicked"][:, lo:hi][mm].sum())
            ctr.append(r4(clicks / imps) if imps else None)
            running += clicks / EPISODES
            cum.append(r4(running))
        regret = (lin["mean_opt"] - lin["mean_chosen"])[m]
        creatives.append(
            {
                "creativeId": cid,
                "share": share,
                "ctr": ctr,
                "cumClicks": cum,
                "impressions": int(m.sum()),
                "clicks": int(lin["clicked"][m].sum()),
                "trueCtr": r4(lin["p_all"][..., a][m].mean()),
                "segmentsWon": sorted(n for n in segs if w_all[n] == cid),
                "finalShare": share[-1],
                "segments": seg_stats(lin, a, whole, segs, w_all, cid),
                "missedClicks": r4(regret.sum() / EPISODES),
                "engagedSecondsPer1k": None,
            }
        )
    creatives.sort(key=lambda c: -c["finalShare"])
    regimes_c = []
    for k, (s0, s1) in enumerate(zip(reg_edges[:-1], reg_edges[1:], strict=True)):
        sl = slice(s0, s1)
        wins = winners(sl)
        total = int(lin["arm"][:, sl].size)
        cr = []
        for a, cid in enumerate(ids):
            m = lin["arm"][:, sl] == a
            imps = int(m.sum())
            clicks = int(lin["clicked"][:, sl][m].sum())
            cr.append(
                {
                    "creativeId": cid,
                    "impressions": imps,
                    "clicks": clicks,
                    "share": r4(imps / total),
                    "ctr": r4(clicks / imps) if imps else None,
                    "trueCtr": r4(lin["p_all"][:, sl, a][m].mean()) if imps else None,
                    "segmentsWon": sorted(n for n in segs if wins[n] == cid),
                    "segments": seg_stats(lin, a, sl, segs, wins, cid),
                }
            )
        regimes_c.append(
            {"index": k, "start": s0, "end": s1, "impressions": total, "segmentWinners": wins, "creatives": cr}
        )
    series = {
        "experimentId": EXPERIMENT_ID,
        "episodes": EPISODES,
        "horizon": horizon,
        "windows": [{"start": lo, "end": hi} for lo, hi in win],
        "creatives": creatives,
        "run": 2,
        "regimes": regimes_c,
    }

    # ── summary (contracts §5) ──
    summary = {
        **{k: v for k, v in live.items() if k != "progress"},
        "experimentId": EXPERIMENT_ID,
        "status": "ready",
        "progress": {"episodesDone": EPISODES, "episodesTotal": EPISODES},
        "policyDiscount": 1.0,
        "trafficRuns": [
            {
                "run": 1,
                "startedAt": "2026-10-05T17:02:11Z",
                "episodes": 5,
                "horizon": horizon,
                "shifts": [],
                "forget": False,
                "status": "finished",
            },
            {
                "run": 2,
                "startedAt": "2026-10-05T17:31:40Z",
                "episodes": EPISODES,
                "horizon": horizon,
                "shifts": SHIFTS_REST,
                "forget": True,
                "status": "finished",
            },
        ],
    }
    (FIX / "shift-experiment.json").write_text(json.dumps(summary, indent=1) + "\n")
    (FIX / "shift-experiment-metrics.json").write_text(json.dumps(metrics) + "\n")
    (FIX / "shift-experiment-creatives.json").write_text(json.dumps(series) + "\n")
    print("boundaries", boundaries, "| shiftCost", metrics["shiftCost"]["clicksPerEpisode"])


if __name__ == "__main__":
    main()
