"""Baseline policies sharing one functional interface (``Policy``).

A ``Policy`` is a frozen bundle of three pure, jit-able functions so the
simulator can ``lax.scan`` any of them:

- ``init(num_arms, dim) -> state``
- ``select(key, state, X, eligible, mean_reward) -> (arms (n,), propensity (n,))``
  ``eligible`` is a (K,) bool mask; ``mean_reward`` (n, K) is the true expected
  reward, used **only** by ``oracle`` (every other policy must ignore it).
  ``propensity`` is P(chosen arm | state, x) (exact where cheap, Monte Carlo for
  the Thompson samplers).
- ``update(state, arms, X, rewards) -> state`` (batched; one call per batch).

Within a batch the state is frozen, i.e. feedback arrives once per batch (the
notebooks' "update delay" equals ``batch_size``). Rewards reach the update
already divided by the simulator's ``reward_scale`` (1 for clicks, the base
dwell for engaged time). In engaged mode ``clicked = reward > 0`` (dwell is
Exponential, so positive whenever there was a click).

The non-contextual baselines mirror the reference notebooks:

- ``uniform``: uniformly random eligible arm.
- ``epsilon_greedy(ε)``: with prob. ε a uniform eligible arm, otherwise the
  arm with the highest mean reward (random if every estimate is 0).
- ``ucb1(c)``: Sutton & Barto eq. 2.10 with the notebooks' ``epsilon``
  multiplier: ``mean_k + c·sqrt(ln t / n_k)`` with t = decisions so far
  (including this one); unseen arms score +inf with a random tie-break.
- ``beta_bernoulli_ts``: Beta(1 + clicks, 1 + n - clicks) per arm; in engaged
  mode the sampled CTR is multiplied by a sampled mean dwell
  (total_dwell / Gamma(clicks, 1), +inf before the first click), as in the
  notebook's ``TSModel``.
- ``oracle``: argmax of the true expected reward over eligible arms.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple

import jax
import jax.numpy as jnp
from jax import Array

SelectFn = Callable[[Array, Any, Array, Array, Array], tuple[Array, Array]]
UpdateFn = Callable[[Any, Array, Array, Array], Any]
InitFn = Callable[[int, int], Any]


@dataclass(frozen=True)
class Policy:
    name: str
    init: InitFn
    select: SelectFn
    update: UpdateFn


class CountsState(NamedTuple):
    n: Array  # (K,) pulls
    sum_r: Array  # (K,) summed (scaled) reward
    clicks: Array  # (K,) rewards > 0
    step: Array  # () decisions so far


def _counts_init(num_arms: int, dim: int) -> CountsState:
    del dim
    return CountsState(
        n=jnp.zeros((num_arms,), jnp.int32),
        sum_r=jnp.zeros((num_arms,), jnp.float32),
        clicks=jnp.zeros((num_arms,), jnp.int32),
        step=jnp.zeros((), jnp.int32),
    )


def _counts_update(
    state: CountsState, arms: Array, X: Array, rewards: Array
) -> CountsState:
    del X
    return CountsState(
        n=state.n.at[arms].add(1),
        sum_r=state.sum_r.at[arms].add(rewards.astype(jnp.float32)),
        clicks=state.clicks.at[arms].add((rewards > 0).astype(jnp.int32)),
        step=state.step + arms.shape[0],
    )


def _uniform_choice(key: Array, n: int, eligible: Array) -> Array:
    logits = jnp.where(eligible, 0.0, -jnp.inf)
    return jax.random.categorical(key, logits, shape=(n,)).astype(jnp.int32)


def _num_eligible(eligible: Array) -> Array:
    return jnp.sum(eligible).astype(jnp.float32)


def _means(state: CountsState) -> Array:
    return jnp.where(state.n > 0, state.sum_r / jnp.maximum(state.n, 1), 0.0)


def beta_posterior(state: CountsState) -> tuple[Array, Array]:
    """Beta(α, β) click posterior per arm: α = 1 + clicks, β = 1 + n - clicks."""
    return 1 + state.clicks, 1 + state.n - state.clicks


def uniform() -> Policy:
    def select(key, state, X, eligible, mean_reward):
        del state, mean_reward
        n = X.shape[0]
        arms = _uniform_choice(key, n, eligible)
        return arms, jnp.full((n,), 1.0) / _num_eligible(eligible)

    return Policy("uniform", _counts_init, select, _counts_update)


def epsilon_greedy(epsilon: float = 0.1) -> Policy:
    def select(key, state, X, eligible, mean_reward):
        del mean_reward
        n = X.shape[0]
        k_explore, k_rand, k_tie = jax.random.split(key, 3)
        means = jnp.where(eligible, _means(state), -jnp.inf)
        all_zero = ~jnp.any(jnp.where(eligible, _means(state), 0.0) != 0)
        greedy = jnp.argmax(means).astype(jnp.int32)
        tie = _uniform_choice(k_tie, n, eligible)
        greedy_rows = jnp.where(all_zero, tie, greedy)
        explore = jax.random.uniform(k_explore, (n,)) < epsilon
        random_arms = _uniform_choice(k_rand, n, eligible)
        arms = jnp.where(explore, random_arms, greedy_rows)
        k_e = _num_eligible(eligible)
        p_greedy = jnp.where(all_zero, 1.0 / k_e, 1.0 - epsilon + epsilon / k_e)
        p_other = jnp.where(all_zero, 1.0 / k_e, epsilon / k_e)
        prop = jnp.where(arms == greedy_rows, p_greedy, p_other)
        return arms, prop

    return Policy(
        f"epsilon_greedy:epsilon={epsilon:g}", _counts_init, select, _counts_update
    )


def ucb1(c: float = 0.25) -> Policy:
    def select(key, state, X, eligible, mean_reward):
        del mean_reward
        n = X.shape[0]
        t = (state.step + 1 + jnp.arange(n)).astype(jnp.float32)  # (n,)
        seen = state.n > 0
        bonus = c * jnp.sqrt(jnp.log(t)[:, None] / jnp.maximum(state.n, 1)[None, :])
        ucb = jnp.where(eligible, _means(state)[None, :] + bonus, -jnp.inf)
        unseen = eligible & ~seen
        tie = jax.random.uniform(key, (n, eligible.shape[0]))
        unseen_scores = jnp.where(unseen[None, :], 1.0 + tie, -jnp.inf)
        has_unseen = jnp.any(unseen)
        arms = jnp.where(
            has_unseen, jnp.argmax(unseen_scores, -1), jnp.argmax(ucb, -1)
        ).astype(jnp.int32)
        prop = jnp.where(has_unseen, 1.0 / jnp.maximum(jnp.sum(unseen), 1), 1.0)
        return arms, jnp.full((n,), prop)

    return Policy(f"ucb1:c={c:g}", _counts_init, select, _counts_update)


def beta_bernoulli_ts(engaged: bool = False, propensity_samples: int = 1000) -> Policy:
    def _sample_scores(key, state, shape):
        k_beta, k_gamma = jax.random.split(key)
        alpha, beta = beta_posterior(state)
        ctr = jax.random.beta(k_beta, alpha, beta, shape=shape)
        if not engaged:
            return ctr
        g = jax.random.gamma(k_gamma, jnp.maximum(state.clicks, 1), shape=shape)
        dwell = jnp.where(state.clicks > 0, state.sum_r / g, jnp.inf)
        return ctr * dwell

    def select(key, state, X, eligible, mean_reward):
        del mean_reward
        n = X.shape[0]
        k_sel, k_prop = jax.random.split(key)
        k = eligible.shape[0]
        scores = jnp.where(eligible, _sample_scores(k_sel, state, (n, k)), -jnp.inf)
        # random tie-break among +inf (unclicked arms in engaged mode)
        tie = jax.random.uniform(jax.random.fold_in(k_sel, 1), (n, k)) * 1e-6
        arms = jnp.argmax(
            jnp.where(jnp.isinf(scores) & (scores > 0), 1e30 + tie, scores), -1
        )
        # non-contextual: one MC propensity table per batch
        mc = jnp.where(
            eligible, _sample_scores(k_prop, state, (propensity_samples, k)), -jnp.inf
        )
        mc_tie = jax.random.uniform(jax.random.fold_in(k_prop, 1), mc.shape) * 1e-6
        wins = jnp.argmax(jnp.where(jnp.isinf(mc) & (mc > 0), 1e30 + mc_tie, mc), -1)
        freq = jnp.zeros((k,)).at[wins].add(1.0) / propensity_samples
        return arms.astype(jnp.int32), freq[arms]

    name = "beta_bernoulli_ts"
    return Policy(name, _counts_init, select, _counts_update)


def oracle() -> Policy:
    def select(key, state, X, eligible, mean_reward):
        del key, state, X
        arms = jnp.argmax(jnp.where(eligible, mean_reward, -jnp.inf), -1)
        return arms.astype(jnp.int32), jnp.ones((mean_reward.shape[0],))

    return Policy("oracle", _counts_init, select, _counts_update)
