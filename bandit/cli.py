"""Offline simulator CLI.

    uv run python -m bandit.cli simulate --scenario segment_winners \\
        --ctr-mode demo --reward-mode click \\
        --policies linear_ts,ucb1,epsilon_greedy,beta_bernoulli_ts,uniform,oracle \\
        --episodes 10 --horizon 20000 --out /tmp/sim.json

Policy specs accept options (``ucb1:c=0.01``, ``linear_ts:discount=0.98``) and
the aliases ``lints``/``egreedy``/``bbts``. Separate policies with ``,`` or ``;``; a
bare ``key=value`` continues the previous spec, so multi-option specs work on the
command line (``--policies 'lints:discount=0.97,exploration_scale=0.1,ucb1'``).
``--segment-mix``, ``--gap-scale``, ``--judge-wrong``, ``--noise-scale`` and ``--drift-at`` tune the scenario with the
contracts §9 bounds (``bandit.config.apply_scenario_overrides``). The output JSON holds the config,
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
    OVERRIDE_BOUNDS,
    REWARD_MODES,
    SCENARIOS,
    LinTSParams,
    ScenarioOverrides,
    apply_scenario_overrides,
    arm_from_dict,
    build_sim_config,
    default_noise_var,
    experiment_config_to_dict,
    load_scenario,
    validate_scenario_overrides,
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
    gap_scale: float | None = None,
    noise_scale: float | None = None,
    drift_at: float | None = None,
    noise_var: float | None = None,
    log_propensity: bool = True,
    num_checkpoints: int = 50,
    checkpoint_spacing: str = "log",
    experiment_id: str = "sim",
) -> dict[str, Any]:
    """Run a simulation and return the JSON-ready output document."""
    t0 = time.time()
    preset = load_scenario(scenario)
    # contracts §9 overrides, with the same bounds the endpoint configs get. A
    # segment mix outside them (e.g. the notebook's single-segment 1,0,0,0 users)
    # is a lab-only escape hatch: applied directly and not recorded in the config.
    mix_lo, mix_hi = OVERRIDE_BOUNDS["segment_mix"]
    mix_in_bounds = segment_mix is not None and all(
        mix_lo <= w <= mix_hi for w in segment_mix
    )
    overrides = ScenarioOverrides(
        segment_mix=tuple(float(w) for w in segment_mix)
        if segment_mix is not None and mix_in_bounds
        else None,
        gap_scale=gap_scale,
        judge_wrong=judge_wrong,
        noise_scale=noise_scale,
        drift_at_frac=drift_at,
    )
    validate_scenario_overrides(overrides, preset)
    sc = apply_scenario_overrides(preset, overrides)
    if segment_mix is not None and not mix_in_bounds:
        sc = with_segment_mix(sc, segment_mix)
    if drift is not None:
        sc = dataclasses.replace(sc, drift=dataclasses.replace(sc.drift, kind=drift))
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
        scenario_overrides=overrides,
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


def split_policy_specs(text: str) -> list[str]:
    """Split a ``--policies`` value into policy specs.

    Policies are separated by ``,`` or ``;``. Because a spec's own options are
    also comma-separated (``name:key=value[,key=value]``), a comma-separated part
    that is a bare ``key=value`` (no ``:``) continues the previous spec's
    options: ``linear_ts:discount=0.97,exploration_scale=0.1,ucb1`` is two
    policies. ``;`` always starts a new policy. Empty parts are dropped.
    """
    specs: list[str] = []
    for group in text.split(";"):
        group_specs: list[str] = []
        for raw in group.split(","):
            part = raw.strip()
            if not part:
                continue
            if "=" in part and ":" not in part:
                if not group_specs:
                    raise ValueError(
                        f"policy option {part!r} must follow a policy name"
                    )
                group_specs[-1] += "," + part
            else:
                group_specs.append(part)
        specs.extend(group_specs)
    return specs


def _floats(text: str) -> list[float]:
    return [float(v) for v in text.split(",") if v.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bandit.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("simulate", help="run an offline simulation")
    s.add_argument("--scenario", choices=SCENARIOS, required=True)
    s.add_argument("--ctr-mode", choices=CTR_MODES, default="demo")
    s.add_argument("--reward-mode", choices=REWARD_MODES, default="click")
    s.add_argument(
        "--policies",
        default=",".join(POLICY_NAMES),
        help="policy specs separated by ',' or ';'; a bare key=value continues the "
        "previous spec, e.g. 'lints:discount=0.97,exploration_scale=0.1,ucb1'",
    )
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
    s.add_argument(
        "--judge-wrong",
        type=float,
        default=None,
        help="0 judge right, 0.5 uninformative, 1 reversed (bounds: contracts §9)",
    )
    s.add_argument(
        "--gap-scale",
        type=float,
        default=None,
        help="0.25-2: scale the gap between creatives (lift_pp / rank_ctrs spread)",
    )
    s.add_argument(
        "--noise-scale",
        type=float,
        default=None,
        help="0-2: multiply the scenario's noise_sd and theta_sd",
    )
    s.add_argument(
        "--drift-at",
        type=float,
        default=None,
        help="0.2-0.8: drift change point as a fraction of T (drift scenario only)",
    )
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
        policies=split_policy_specs(args.policies),
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
        gap_scale=args.gap_scale,
        noise_scale=args.noise_scale,
        drift_at=args.drift_at,
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
