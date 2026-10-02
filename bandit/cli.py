"""Offline simulator CLI.

    uv run python -m bandit.cli simulate --scenario segment_winners \\
        --ctr-mode demo --reward-mode click \\
        --policies linear_ts,ucb1,epsilon_greedy,beta_bernoulli_ts,uniform,oracle \\
        --episodes 10 --horizon 20000 --out /tmp/sim.json

Policy specs accept options (``ucb1:c=0.01``, ``linear_ts:discount=0.98``) and
the aliases ``lints``/``egreedy``/``bbts``. The output JSON holds the config,
the environment summary, one §3-shaped metrics row per (episode, policy)
(``rows``), the §5-shaped ``aggregate`` (snake_case) and a scalar ``summary``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import jax
import numpy as np

from bandit import environment as envm
from bandit.aggregate import aggregate_episode_metrics, summarize_totals
from bandit.config import (
    CTR_MODES,
    REWARD_MODES,
    SCENARIOS,
    LinTSParams,
    arm_from_dict,
    build_sim_config,
    default_noise_var,
    experiment_config_to_dict,
    load_scenario,
    with_segment_mix,
)
from bandit.features import FEATURE_SPEC_VERSION
from bandit.metrics import experiment_rows
from bandit.policies import POLICY_NAMES, canonical_spec
from bandit.simulate import ArmSchedule, run_experiment


def simulate(
    *,
    scenario: str,
    ctr_mode: str = "demo",
    reward_mode: str = "click",
    policies: Sequence[str] = POLICY_NAMES,
    episodes: int | None = None,
    horizon: int | None = None,
    batch_size: int | None = None,
    seed: int = 0,
    num_arms: int | None = None,
    arms: list[dict[str, Any]] | None = None,
    segment_mix: list[float] | None = None,
    arm_schedule: ArmSchedule | None = None,
    drift: str | None = None,
    judge_wrong: float | None = None,
    noise_var: float | None = None,
    log_propensity: bool = True,
    num_checkpoints: int = 50,
    checkpoint_spacing: str = "log",
    experiment_id: str = "sim",
) -> dict[str, Any]:
    """Run a simulation and return the JSON-ready output document."""
    t0 = time.time()
    sc = load_scenario(scenario)
    if segment_mix is not None:
        sc = with_segment_mix(sc, segment_mix)
    if drift is not None:
        sc = dataclasses.replace(sc, drift=dataclasses.replace(sc.drift, kind=drift))
    if judge_wrong is not None:
        sc = dataclasses.replace(sc, judge_wrong=judge_wrong)
    nv = (
        noise_var
        if noise_var is not None
        else default_noise_var(sc.target_ctr[ctr_mode], reward_mode)
    )
    cfg = build_sim_config(
        scenario,
        ctr_mode=ctr_mode,
        reward_mode=reward_mode,
        arms=tuple(arm_from_dict(a) for a in arms) if arms else None,
        num_arms=num_arms,
        horizon=horizon,
        batch_size=batch_size,
        episodes=episodes,
        seed=seed,
        policy=LinTSParams(noise_var=nv),
        experiment_id=experiment_id,
    )
    specs = [canonical_spec(p) for p in policies]
    result = run_experiment(
        cfg,
        specs,
        arm_schedule=arm_schedule,
        scenario=sc,
        log_propensity=log_propensity,
    )
    rows = experiment_rows(result, num_checkpoints, checkpoint_spacing)
    env = result.env
    pre = envm.marginal_ctrs(env, jax.random.key(seed + 1), t=0)
    post = envm.marginal_ctrs(env, jax.random.key(seed + 1), t=cfg.horizon - 1)
    return {
        "kind": "bandit_simulation",
        "feature_spec_version": FEATURE_SPEC_VERSION,
        "config": experiment_config_to_dict(cfg),
        "scenario": dataclasses.asdict(sc),
        "arm_schedule": [[int(s), [int(a) for a in arms_]] for s, arms_ in arm_schedule]
        if arm_schedule
        else None,
        "env": {
            "alpha": float(env.model.alpha),
            "arm_ids": list(env.arm_ids),
            "arm_labels": [a.label for a in env.arms],
            "segment_names": list(env.segment_names),
            "segment_weights": [s.weight for s in sc.segments],
            "winners": [env.arm_ids[w] for w in env.winners] if env.winners else None,
            "target_ctr": env.target_ctr,
            "true_ctr_start": _round(pre["overall"]),
            "true_ctr_end": _round(post["overall"]),
            "true_ctr_by_segment": {
                name: _round(pre["by_segment"][s])
                for s, name in enumerate(env.segment_names)
            },
        },
        "policies": result.policies,
        "checkpoints": rows[0]["curve"]["checkpoints"] if rows else [],
        "rows": rows,
        "aggregate": aggregate_episode_metrics(rows, experiment_id=experiment_id),
        "summary": summarize_totals(rows),
        "runtime_s": round(time.time() - t0, 2),
    }


def _round(a: np.ndarray) -> list[float]:
    return [float(f"{v:.6g}") for v in np.asarray(a)]


def _floats(text: str) -> list[float]:
    return [float(v) for v in text.split(",") if v.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bandit.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("simulate", help="run an offline simulation")
    s.add_argument("--scenario", choices=SCENARIOS, required=True)
    s.add_argument("--ctr-mode", choices=CTR_MODES, default="demo")
    s.add_argument("--reward-mode", choices=REWARD_MODES, default="click")
    s.add_argument("--policies", default=",".join(POLICY_NAMES))
    s.add_argument("--episodes", type=int, default=None)
    s.add_argument("--horizon", type=int, default=None)
    s.add_argument("--batch-size", type=int, default=None)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--num-arms", type=int, default=None, help="synthetic arms (2-4)")
    s.add_argument("--arms", type=Path, default=None, help="JSON list of ArmSpec dicts")
    s.add_argument("--segment-mix", type=_floats, default=None, help="e.g. 1,0,0,0")
    s.add_argument(
        "--arm-schedule",
        type=json.loads,
        default=None,
        help="JSON [[start_round, [arm indices]], ...], e.g. [[0,[1,2,3]],[10000,[0,1,2]]]",
    )
    s.add_argument("--drift", choices=("none", "abrupt", "gradual"), default=None)
    s.add_argument("--judge-wrong", type=float, default=None)
    s.add_argument("--noise-var", type=float, default=None)
    s.add_argument("--no-propensity", action="store_true", help="skip MC propensities")
    s.add_argument("--num-checkpoints", type=int, default=50)
    s.add_argument("--checkpoint-spacing", choices=("log", "linear"), default="log")
    s.add_argument("--experiment-id", default="sim")
    s.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    doc = simulate(
        scenario=args.scenario,
        ctr_mode=args.ctr_mode,
        reward_mode=args.reward_mode,
        policies=[p for p in args.policies.split(",") if p.strip()],
        episodes=args.episodes,
        horizon=args.horizon,
        batch_size=args.batch_size,
        seed=args.seed,
        num_arms=args.num_arms,
        arms=json.loads(args.arms.read_text()) if args.arms else None,
        segment_mix=args.segment_mix,
        arm_schedule=args.arm_schedule,
        drift=args.drift,
        judge_wrong=args.judge_wrong,
        noise_var=args.noise_var,
        log_propensity=not args.no_propensity,
        num_checkpoints=args.num_checkpoints,
        checkpoint_spacing=args.checkpoint_spacing,
        experiment_id=args.experiment_id,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc))
    print(f"wrote {args.out} ({doc['runtime_s']}s)", file=sys.stderr)
    for policy, s in doc["summary"].items():
        print(
            f"{policy:32s} reward {s['total_reward']['mean']:10.1f} ± "
            f"{s['total_reward']['std']:7.1f}  regret {s['cumulative_regret']['mean']:9.2f}"
            f"  %opt {s['pct_optimal']['mean']:.3f}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
