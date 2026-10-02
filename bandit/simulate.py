"""Offline simulator: run any ``Policy`` against a synthetic ``Environment``.

**Terminology** (contracts): a *round* is one impression + decision; a *batch*
is the rounds between two policy updates (``batch_size``); the *horizon* T is
rounds per episode; an *episode* is an independent run from a reset policy with
its own key.

An episode is a ``lax.scan`` over batches. Per batch it samples users, computes
the true click probabilities, pre-samples every arm's counterfactual outcome,
lets the policy choose, and updates the policy once with the chosen rewards.

**Common random numbers.** An episode key is split into independent
``fold_in`` streams (0 = contexts, 1 = rewards, 2 = policy), each folded with
the batch index. Contexts and reward coin flips therefore never depend on the
policy: every policy sees identical users, and if two policies pick the same
arm in the same round they get the same outcome (click = U < p with one shared
U per (round, arm)). Episodes are vmapped (in chunks) for speed.

Per-round outputs (numpy, shape (T,) unless noted): ``segment``, ``arm``,
``reward``, ``clicked``, ``p_chosen``/``p_opt`` (true click probs),
``mean_chosen``/``mean_opt`` (true expected reward; = p in click mode),
``opt_arm``, ``reward_opt`` (realized reward the optimal arm would have got),
``propensity`` and ``p_all`` (T, K) true click probs for every arm.
"""

from __future__ import annotations

import functools
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import jax
import jax.numpy as jnp
import numpy as np
from jax import Array

from bandit import environment as envm
from bandit.baselines import Policy
from bandit.config import ExperimentConfig, ScenarioConfig
from bandit.policies import make_policy

#: ``[(start_round, eligible_arm_indices), ...]``; first entry must start at 0.
ArmSchedule = Sequence[tuple[int, Sequence[int]]]

OUTPUT_KEYS = (
    "segment",
    "arm",
    "reward",
    "clicked",
    "p_chosen",
    "p_opt",
    "mean_chosen",
    "mean_opt",
    "opt_arm",
    "reward_opt",
    "propensity",
    "p_all",
)


def schedule_masks(
    schedule: ArmSchedule | None, num_arms: int, num_batches: int, batch_size: int
) -> np.ndarray:
    """Per-batch eligibility masks (num_batches, K). A batch uses the schedule
    entry active at its first round (expiry/injection take effect at batch
    boundaries)."""
    if not schedule:
        return np.ones((num_batches, num_arms), bool)
    entries = sorted((int(s), tuple(int(a) for a in arms)) for s, arms in schedule)
    if entries[0][0] != 0:
        raise ValueError("arm_schedule must start at round 0")
    masks = np.zeros((num_batches, num_arms), bool)
    starts = np.array([s for s, _ in entries])
    for b in range(num_batches):
        _, arms = entries[
            int(np.searchsorted(starts, b * batch_size, side="right")) - 1
        ]
        if not arms or min(arms) < 0 or max(arms) >= num_arms:
            raise ValueError(f"bad eligible arms {arms} for {num_arms} arms")
        masks[b, list(arms)] = True
    return masks


def episode_streams(key: Array) -> tuple[Array, Array, Array]:
    """An episode key's independent ``(contexts, rewards, policy)`` streams."""
    k_ctx, k_rew, k_pol = (jax.random.fold_in(key, i) for i in range(3))
    return k_ctx, k_rew, k_pol


def batch_draws(
    model: envm.TrueModel,
    k_ctx: Array,
    k_rew: Array,
    b: Array | int,
    batch_size: int,
    reward_mode: str,
    eligible: Array,
) -> dict[str, Array]:
    """Everything the environment draws for batch ``b`` (policy-independent).

    ``segment`` (n,), ``levels`` (n, G), ``X`` (n, d), ``t`` (n,) round indices,
    ``p`` / ``mean`` (n, K) true click probs / expected rewards, ``clicked_all`` /
    ``reward_all`` (n, K) pre-sampled counterfactual outcomes (common random
    numbers) and ``opt`` (n,) the optimal eligible arm. The simulator scans this;
    the traffic job (``bandit_traffic``) calls it per batch so the endpoint and
    the locally replayed baselines see identical users and coin flips.
    """
    seg, levels, X = envm.sample_contexts(
        jax.random.fold_in(k_ctx, b), model, batch_size
    )
    t = b * batch_size + jnp.arange(batch_size)
    p = envm.click_probs(model, X, seg, t)
    mean = envm.expected_rewards(model, p, seg, reward_mode)
    clicked_all, reward_all = envm.sample_rewards(
        jax.random.fold_in(k_rew, b), p, reward_mode, model.dwell_means[seg]
    )
    return {
        "segment": seg,
        "levels": levels,
        "X": X,
        "t": t,
        "p": p,
        "mean": mean,
        "clicked_all": clicked_all,
        "reward_all": reward_all,
        "opt": envm.optimal_arms(mean, eligible),
    }


def _episode(
    policy: Policy,
    batch_size: int,
    num_batches: int,
    reward_mode: str,
    reward_scale: float,
    num_arms: int,
    dim: int,
    model: envm.TrueModel,
    masks: Array,
    key: Array,
) -> dict[str, Array]:
    k_ctx, k_rew, k_pol = episode_streams(key)

    def step(state: Any, xs: tuple[Array, Array]) -> tuple[Any, dict[str, Array]]:
        b, elig = xs
        d = batch_draws(model, k_ctx, k_rew, b, batch_size, reward_mode, elig)
        seg, X, p, mean = d["segment"], d["X"], d["p"], d["mean"]
        clicked_all, reward_all, opt = d["clicked_all"], d["reward_all"], d["opt"]
        arms, prop = policy.select(jax.random.fold_in(k_pol, b), state, X, elig, mean)

        def pick(m: Array, a: Array) -> Array:
            return jnp.take_along_axis(m, a[:, None], axis=1)[:, 0]

        reward = pick(reward_all, arms)
        state = policy.update(state, arms, X, reward / reward_scale)
        out = {
            "segment": seg,
            "arm": arms,
            "reward": reward,
            "clicked": pick(clicked_all, arms),
            "p_chosen": pick(p, arms),
            "p_opt": pick(p, opt),
            "mean_chosen": pick(mean, arms),
            "mean_opt": pick(mean, opt),
            "opt_arm": opt,
            "reward_opt": pick(reward_all, opt),
            "propensity": prop.astype(jnp.float32),
            "p_all": p,
        }
        return state, out

    xs = (jnp.arange(num_batches), masks)
    _, outs = jax.lax.scan(step, policy.init(num_arms, dim), xs)
    return {
        k: v.reshape((num_batches * batch_size, *v.shape[2:])) for k, v in outs.items()
    }


@functools.lru_cache(maxsize=64)
def _compiled(
    policy: Policy,
    batch_size: int,
    num_batches: int,
    reward_mode: str,
    reward_scale: float,
    num_arms: int,
    dim: int,
):
    fn = functools.partial(
        _episode,
        policy,
        batch_size,
        num_batches,
        reward_mode,
        reward_scale,
        num_arms,
        dim,
    )
    return jax.jit(jax.vmap(fn, in_axes=(None, None, 0)))


def run_episodes(
    policy: Policy,
    env: envm.Environment,
    keys: Array,
    horizon: int,
    batch_size: int,
    arm_schedule: ArmSchedule | None = None,
    chunk: int = 8,
) -> dict[str, np.ndarray]:
    """Run one policy for every key in ``keys`` (E,): arrays shaped (E, T, ...)."""
    num_batches = -(-horizon // batch_size)
    masks = jnp.asarray(
        schedule_masks(arm_schedule, env.num_arms, num_batches, batch_size)
    )
    fn = _compiled(
        policy,
        batch_size,
        num_batches,
        env.reward_mode,
        float(env.reward_scale),
        env.num_arms,
        int(env.model.theta.shape[1]),
    )
    parts: list[dict[str, np.ndarray]] = []
    for i in range(0, keys.shape[0], chunk):
        out = fn(env.model, masks, keys[i : i + chunk])
        parts.append({k: np.asarray(v[:, :horizon]) for k, v in out.items()})
    return {k: np.concatenate([p[k] for p in parts]) for k in OUTPUT_KEYS}


def run_episode(
    policy: Policy,
    env: envm.Environment,
    key: Array,
    horizon: int | None = None,
    batch_size: int = 100,
    arm_schedule: ArmSchedule | None = None,
) -> dict[str, np.ndarray]:
    """Run one episode; returns per-round arrays (see module docstring)."""
    out = run_episodes(
        policy, env, key[None], horizon or env.horizon, batch_size, arm_schedule
    )
    return {k: v[0] for k, v in out.items()}


def scenario_key(seed: int, scenario: str) -> Array:
    """Experiment base key: ``fold_in(key(seed), crc32(scenario))``."""
    return jax.random.fold_in(
        jax.random.key(seed), zlib.crc32(scenario.encode()) & 0x7FFFFFFF
    )


def episode_keys(seed: int, scenario: str, episodes: int) -> Array:
    """Episode ``e`` key = ``fold_in(fold_in(base, 1), e)`` (shared by all policies)."""
    stream = jax.random.fold_in(scenario_key(seed, scenario), 1)
    return jax.vmap(lambda e: jax.random.fold_in(stream, e))(jnp.arange(episodes))


def build_environment(
    cfg: ExperimentConfig, *, scenario: ScenarioConfig | None = None
) -> envm.Environment:
    """The experiment's ground truth (model key = ``fold_in(base, 0)``)."""
    return envm.build_true_model(
        cfg,
        jax.random.fold_in(scenario_key(cfg.seed, cfg.scenario), 0),
        scenario=scenario,
    )


@dataclass
class ExperimentResult:
    cfg: ExperimentConfig
    env: envm.Environment
    policies: list[str]
    results: dict[str, dict[str, np.ndarray]] = field(default_factory=dict)
    arm_schedule: ArmSchedule | None = None


def run_experiment(
    cfg: ExperimentConfig,
    policies: Sequence[str | Policy],
    episodes: int | None = None,
    *,
    arm_schedule: ArmSchedule | None = None,
    scenario: ScenarioConfig | None = None,
    log_propensity: bool = True,
) -> ExperimentResult:
    """Run every policy for ``episodes`` (default ``cfg.episodes``) episodes on the
    ground truth built from ``cfg`` (model key = ``fold_in(base, 0)``), with
    common random numbers across policies."""
    env = build_environment(cfg, scenario=scenario)
    keys = episode_keys(cfg.seed, cfg.scenario, episodes or cfg.episodes)
    result = ExperimentResult(cfg=cfg, env=env, policies=[], arm_schedule=arm_schedule)
    for spec in policies:
        pol = (
            spec
            if isinstance(spec, Policy)
            else make_policy(
                spec,
                lints_params=cfg.policy,
                reward_mode=cfg.reward_mode,
                log_propensity=log_propensity,
            )
        )
        if pol.name in result.results:
            raise ValueError(f"duplicate policy {pol.name!r}")
        result.policies.append(pol.name)
        result.results[pol.name] = run_episodes(
            pol, env, keys, cfg.horizon, cfg.batch_size, arm_schedule
        )
    return result
