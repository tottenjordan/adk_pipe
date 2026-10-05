"""Build the scripted-shift screenshot fixtures from a REAL simulator run.

Runs ``bandit`` (dev group: JAX) on the first live experiment's three creatives
(segment_winners, demo, 40k rounds) with two scripted shifts (contracts §10) and
forgetting on, plus the ghost replay (Linear TS on the same random draws without
the shifts), and writes the api-shaped payloads the experiment page reads:

- ``screenshot-fixtures/shift-experiment.json``          ExperimentSummary + trafficRuns
- ``screenshot-fixtures/shift-experiment-metrics.json``  ExperimentMetrics + ghost, shiftResponse, regimes
- ``screenshot-fixtures/shift-experiment-creatives.json`` CreativeSeries + regimes

The shapes follow the PR C api contract as the frontend reads it
(``frontend/src/lib/shifts.ts``). Regenerate with::

    uv run python frontend/scripts/build_shift_fixture.py
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import jax
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
from bandit.metrics import make_checkpoints, merge_checkpoints, regime_stats, shift_response
from bandit.policies import canonical_spec
from bandit.simulate import build_environment, run_experiment

FIX = Path(__file__).parent / "screenshot-fixtures"
EPISODES = 10
SEED = 0
POLICIES = ["linear_ts", "ucb1", "epsilon_greedy", "beta_bernoulli_ts", "uniform", "oracle"]
GHOST = "linear_ts_unshifted"


def r4(v: float) -> float:
    return float(round(float(v), 4))


def band(x: np.ndarray) -> dict:
    """mean ± 95% CI across episodes (axis 0)."""
    m = x.mean(0)
    half = 1.96 * x.std(0, ddof=1) / np.sqrt(x.shape[0]) if x.shape[0] > 1 else np.zeros_like(m)
    return {"mean": [r4(v) for v in m], "lo": [r4(v) for v in m - half], "hi": [r4(v) for v in m + half]}


def stat(xs: list[float]) -> dict | None:
    if not xs:
        return None
    a = np.asarray(xs, np.float64)
    half = 1.96 * a.std(ddof=1) / np.sqrt(len(a)) if len(a) > 1 else 0.0
    return {"mean": r4(a.mean()), "lo": r4(a.mean() - half), "hi": r4(a.mean() + half), "n": len(a)}


def curves(out: dict, cps: np.ndarray) -> dict:
    arm = np.asarray(out["arm"])
    reward = np.asarray(out["reward"], np.float64)
    gap = np.asarray(out["mean_opt"], np.float64) - np.asarray(out["mean_chosen"], np.float64)
    is_opt = (arm == np.asarray(out["opt_arm"])).astype(np.float64)
    t = np.arange(1, arm.shape[1] + 1)
    idx = cps - 1
    return {
        "cumAvgReward": band((np.cumsum(reward, 1) / t)[:, idx]),
        "cumRegret": band(np.cumsum(gap, 1)[:, idx]),
        "pctOptimal": band((np.cumsum(is_opt, 1) / t)[:, idx]),
    }


def per_segment(results: dict, segs: list[str], ids: list[str], sl: slice) -> dict:
    out: dict = {}
    for s, name in enumerate(segs):
        lin = results["linear_ts"]
        m = np.asarray(lin["segment"])[:, sl] == s
        opt = np.asarray(lin["opt_arm"])[:, sl][m]
        if not opt.size:
            continue
        optimal = ids[int(np.bincount(opt, minlength=len(ids)).argmax())]
        pols = {}
        for p, o in results.items():
            mm = np.asarray(o["segment"])[:, sl] == s
            hit = (np.asarray(o["arm"])[:, sl] == np.asarray(o["opt_arm"])[:, sl])[mm]
            pols[p] = {
                "pctOptimal": r4(hit.mean()),
                "avgReward": r4(np.asarray(o["reward"], np.float64)[:, sl][mm].mean()),
            }
        out[name] = {"optimalArm": optimal, "policies": pols}
    return out


def main() -> None:
    live = json.loads((FIX / "live-experiment.json").read_text())
    arms = [
        {"creative_id": a["creativeId"], "label": a["conceptName"], "scores": a["scores"]}
        for a in live["arms"]
    ]
    ids = [a["creative_id"] for a in arms]
    scenario = "segment_winners"
    sc = load_scenario(scenario)
    cfg = build_sim_config(
        scenario,
        arms=tuple(arm_from_dict(a) for a in arms),
        episodes=EPISODES,
        seed=SEED,
        policy=LinTSParams(noise_var=default_noise_var(sc.target_ctr["demo"], "click")),
        experiment_id="5d1f0c2a9b7e4f61",
    )
    horizon = cfg.horizon

    # The editor's "Demote the leader at halfway" preset on segment_winners:
    # demote the pooled leader in the segment it wins, then "Ad fatigue on the leader".
    plain = build_environment(cfg, scenario=sc)
    marg = envm.marginal_ctrs(plain, jax.random.key(1), t=0)
    leader = int(np.argmax(marg["overall"]))
    by_seg = np.asarray([marg["by_segment"][s] for s in range(len(plain.segment_names))])
    won = [s for s in range(by_seg.shape[0]) if int(np.argmax(by_seg[s])) == leader]
    target = max(won, key=lambda s: by_seg[s, leader] - np.sort(by_seg[s])[-2]) if won else 0
    requested = [
        {
            "kind": "demote",
            "at_frac": 0.5,
            "segment": plain.segment_names[target],
            "creative_id": ids[leader],
            "drop_pp": 0.02,
        },
        {
            "kind": "shock",
            "at_frac": 0.6,
            "until_frac": 0.75,
            "segment": None,
            "creative_id": "leader",
            "ctr_multiplier": 0.6,
        },
    ]
    shifts = validate_shifts(shifts_from_dict(requested), sc, cfg.arms, "demo")
    discount = default_shift_discount("demo", cfg.batch_size, horizon)
    cfg = dataclasses.replace(cfg, policy=dataclasses.replace(cfg.policy, discount=discount))

    specs = [canonical_spec(p) for p in POLICIES]
    shifted = run_experiment(cfg, specs, scenario=sc, shifts=shifts)
    ghost = run_experiment(cfg, [canonical_spec("linear_ts")], scenario=sc)
    env = shifted.env
    resolved = envm.resolved_shifts(env)
    rounds = [rec["round"] for rec in resolved]
    boundaries = rounds + [rec["end_round"] for rec in resolved if rec.get("end_round")]
    cps = np.asarray(merge_checkpoints(make_checkpoints(horizon, 50, "linear"), rounds, horizon))
    segs = list(env.segment_names)

    results = {p: {k: np.asarray(v) for k, v in shifted.results[p].items()} for p in shifted.policies}
    results_all = {**results, GHOST: {k: np.asarray(v) for k, v in ghost.results["linear_ts"].items()}}

    def ep(p: str, e: int) -> dict:
        return {k: v[e] for k, v in results_all[p].items()}

    # ── /metrics ──
    lin = results["linear_ts"]
    arm_share = {}
    edges = np.concatenate([[0], cps])
    for a, cid in enumerate(ids):
        onehot = (lin["arm"] == a).astype(np.float64)
        cs = np.concatenate([np.zeros((EPISODES, 1)), np.cumsum(onehot, 1)], 1)
        share = (cs[:, edges[1:]] - cs[:, edges[:-1]]) / np.maximum(np.diff(edges), 1)
        arm_share[cid] = [r4(v) for v in share.mean(0)]
    arms_rows = []
    for a, cid in enumerate(ids):
        m = lin["arm"] == a
        imps, clicks = int(m.sum()), int(lin["clicked"][m].sum())
        arms_rows.append(
            {
                "creativeId": cid,
                "impressions": imps,
                "estimatedCtr": r4(clicks / imps if imps else 0),
                "trueCtr": r4(lin["p_all"][..., a].mean()),
            }
        )
    order = [*POLICIES[:-1], GHOST, "oracle"]
    shift_resp = []
    per_ep = {p: [shift_response(ep(p, e), rounds) for e in range(EPISODES)] for p in order}
    for j, rec in enumerate(resolved):
        pols = {}
        for p in order:
            rows = [x[j] for x in per_ep[p]]
            rec_rounds = [x["recovery_rounds"] for x in rows if x["recovery_rounds"] is not None]
            pols[p] = {
                "pctOptimalBefore": stat([x["pct_optimal_before"] for x in rows]),
                "pctOptimalAfter": stat([x["pct_optimal_after"] for x in rows]),
                "regretRateBefore": stat([x["regret_rate_before"] for x in rows]),
                "regretRateAfter": stat([x["regret_rate_after"] for x in rows]),
                "recoveryRounds": stat(rec_rounds),
                "recoveredEpisodes": len(rec_rounds),
                "episodes": EPISODES,
            }
        shift_resp.append({"index": rec["index"], "kind": rec["kind"], "round": rec["round"], "policies": pols})

    regimes_m = []
    reg_edges = sorted({0, horizon} | {b for b in boundaries if 0 < b < horizon})
    for start, end in zip(reg_edges[:-1], reg_edges[1:], strict=True):
        sl = slice(start, end)
        true = {cid: r4(lin["p_all"][:, sl, a].mean()) for a, cid in enumerate(ids)}
        regimes_m.append(
            {"start": start, "end": end, "perSegment": per_segment(results, segs, ids, sl), "trueCtr": true}
        )
    _ = regime_stats  # same split as bandit.metrics.regime_stats

    metrics = {
        "experimentId": cfg.experiment_id,
        "run": 2,
        "episodes": EPISODES,
        "horizon": horizon,
        "checkpoints": [int(c) for c in cps],
        "policies": order,
        "curves": {p: curves(results_all[p], cps) for p in order},
        "totals": {
            p: {
                "mean": r4(results_all[p]["reward"].sum(1).mean()),
                "std": r4(results_all[p]["reward"].sum(1).std(ddof=1)),
            }
            for p in order
        },
        "armShare": arm_share,
        "perSegment": per_segment(results, segs, ids, slice(0, horizon)),
        "arms": arms_rows,
        "shiftResponse": shift_resp,
        "regimes": regimes_m,
    }

    # ── /creatives ──
    nw = 20
    win = [(int(np.ceil(w * horizon / nw)), int(np.ceil((w + 1) * horizon / nw))) for w in range(nw)]
    creatives = []
    seg_mode = {}
    for s, name in enumerate(segs):
        opt = lin["opt_arm"][lin["segment"] == s]
        seg_mode[name] = int(np.bincount(opt, minlength=len(ids)).argmax())
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
        p_chosen = lin["p_all"][..., a][m]
        regret = (lin["mean_opt"] - lin["mean_chosen"])[m]
        seg_rows = []
        for s, name in enumerate(segs):
            ms = m & (lin["segment"] == s)
            imps = int(ms.sum())
            clicks = int(lin["clicked"][ms].sum())
            seg_rows.append(
                {
                    "segment": name,
                    "impressions": imps,
                    "clicks": clicks,
                    "ctr": r4(clicks / imps) if imps else None,
                    "trueCtr": r4(lin["p_all"][..., a][ms].mean()) if imps else None,
                    "isBest": seg_mode[name] == a,
                }
            )
        creatives.append(
            {
                "creativeId": cid,
                "share": share,
                "ctr": ctr,
                "cumClicks": cum,
                "impressions": int(m.sum()),
                "clicks": int(lin["clicked"][m].sum()),
                "trueCtr": r4(p_chosen.mean()),
                "segmentsWon": sorted(n for n in segs if seg_mode[n] == a),
                "finalShare": share[-1],
                "segments": sorted(seg_rows, key=lambda r: r["segment"]),
                "missedClicks": r4(regret.sum() / EPISODES),
                "engagedSecondsPer1k": None,
            }
        )
    creatives.sort(key=lambda c: -c["finalShare"])
    regimes_c = []
    for start, end in zip(reg_edges[:-1], reg_edges[1:], strict=True):
        sl = slice(start, end)
        seg_list = []
        for s, name in enumerate(segs):
            opt = lin["opt_arm"][:, sl][lin["segment"][:, sl] == s]
            seg_list.append(
                {"segment": name, "optimalCreativeId": ids[int(np.bincount(opt, minlength=len(ids)).argmax())]}
            )
        cr = []
        for a, cid in enumerate(ids):
            m = lin["arm"][:, sl] == a
            imps = int(m.sum())
            clicks = int(lin["clicked"][:, sl][m].sum())
            cr.append(
                {
                    "creativeId": cid,
                    "impressions": imps,
                    "ctr": r4(clicks / imps) if imps else None,
                    "trueCtr": r4(lin["p_all"][:, sl, a].mean()),
                }
            )
        regimes_c.append({"start": start, "end": end, "segments": sorted(seg_list, key=lambda r: r["segment"]), "creatives": cr})
    series = {
        "experimentId": cfg.experiment_id,
        "run": 2,
        "episodes": EPISODES,
        "horizon": horizon,
        "windows": [{"start": lo, "end": hi} for lo, hi in win],
        "creatives": creatives,
        "regimes": regimes_c,
    }

    # ── summary ──
    rest_shifts = [
        {
            "kind": s["kind"],
            "atFrac": s["at_frac"],
            **({"untilFrac": s["until_frac"]} if "until_frac" in s else {}),
            "segment": s.get("segment"),
            "creativeId": s["creative_id"],
            **({"dropPp": s["drop_pp"]} if "drop_pp" in s else {}),
            **({"ctrMultiplier": s["ctr_multiplier"]} if "ctr_multiplier" in s else {}),
        }
        for s in requested
    ]
    resolved_rest = [
        {
            **rest_shifts[rec["index"]],
            "round": rec["round"],
            "endRound": rec.get("end_round"),
            "requestedCreativeId": rec.get("requested_creative_id"),
            "creativeId": rec.get("creative_id"),
        }
        for rec in resolved
    ]
    summary = {
        **{k: v for k, v in live.items() if k not in ("progress",)},
        "experimentId": cfg.experiment_id,
        "status": "ready",
        "progress": {"episodesDone": EPISODES, "episodesTotal": EPISODES},
        "trafficRuns": [
            {
                "run": 1,
                "startedAt": "2026-10-05T17:02:11Z",
                "episodes": 5,
                "horizon": horizon,
                "shifts": [],
                "forget": False,
                "status": "done",
            },
            {
                "run": 2,
                "startedAt": "2026-10-05T17:31:40Z",
                "episodes": EPISODES,
                "horizon": horizon,
                "shifts": resolved_rest,
                "forget": True,
                "status": "done",
            },
        ],
    }
    (FIX / "shift-experiment.json").write_text(json.dumps(summary, indent=1) + "\n")
    (FIX / "shift-experiment-metrics.json").write_text(json.dumps(metrics) + "\n")
    (FIX / "shift-experiment-creatives.json").write_text(json.dumps(series) + "\n")
    print("leader", ids[leader], "demoted in", segs[target], "| resolved", resolved)


if __name__ == "__main__":
    main()
