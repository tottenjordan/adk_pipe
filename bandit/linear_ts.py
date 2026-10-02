"""Disjoint linear Thompson sampling in JAX (contracts §1 ``bandit/linear_ts.py``).

Each arm ``k`` has an independent Bayesian linear regression of reward on the
context features ``x`` (``bandit.features``):

    θ_k ~ N(0, τ² I),   r | x, θ_k ~ N(xᵀθ_k, σ²)

with τ² = ``prior_var`` and σ² = ``noise_var``. The state stores the natural
parameters of the Gaussian posterior,

    Λ_k = I/τ² + Σ_{i: a_i=k} x_i x_iᵀ / σ²      (``precision``)
    b_k = Σ_{i: a_i=k} r_i x_i / σ²              (``b``)

so μ_k = Λ_k⁻¹ b_k equals the ridge solution (XᵀX + (σ²/τ²) I)⁻¹ Xᵀr, and a
posterior draw is θ̃_k ~ N(μ_k, s² Λ_k⁻¹) with s = ``exploration_scale``. All
linear algebra goes through ``jax.scipy.linalg.cho_factor/cho_solve``.

Clicks are Bernoulli and the simulator's ground truth is logistic, so this is a
deliberately misspecified linear-Gaussian fit (fast, conjugate, jit-able).

Every function is pure and jit-able; ``LinTSParams`` is a frozen (hashable)
dataclass, so pass it as a static argument: ``jax.jit(select,
static_argnames=("params",))``. Randomness only comes from the ``key`` argument
(``jax.random.key`` + ``fold_in``/``split``), never from global seeds.
"""

from __future__ import annotations

from typing import NamedTuple

import jax
import jax.numpy as jnp
from jax import Array
from jax.scipy.linalg import cho_factor, cho_solve, solve_triangular

from bandit.config import LinTSParams


class LinTSState(NamedTuple):
    precision: Array  # (K, d, d) posterior precision Λ_k
    b: Array  # (K, d) Σ r x / σ²
    n: Array  # (K,) pulls
    step: Array  # () rounds seen


def init_state(num_arms: int, dim: int, prior_var: float) -> LinTSState:
    """Prior state: Λ_k = I/τ², b_k = 0."""
    eye = jnp.eye(dim, dtype=jnp.float32) / prior_var
    return LinTSState(
        precision=jnp.tile(eye, (num_arms, 1, 1)),
        b=jnp.zeros((num_arms, dim), jnp.float32),
        n=jnp.zeros((num_arms,), jnp.int32),
        step=jnp.zeros((), jnp.int32),
    )


def _chol(precision: Array) -> Array:
    """Lower Cholesky factors of each arm's precision, (K, d, d)."""
    return jax.vmap(lambda p: cho_factor(p, lower=True)[0])(precision)


def posterior_mean(state: LinTSState) -> Array:
    """μ_k = Λ_k⁻¹ b_k for every arm, shape (K, d)."""
    chol = _chol(state.precision)
    return jax.vmap(lambda c, b: cho_solve((c, True), b))(chol, state.b)


def _score_moments(state: LinTSState, X: Array) -> tuple[Array, Array]:
    """Posterior mean and variance of xᵀθ_k for each row/arm: two (n, K) arrays.

    Uses the explicit posterior covariance Λ_k⁻¹ = cho_solve(L_k, I) (d×d per
    arm): far cheaper on CPU than a triangular solve against all n rows.
    """
    chol = _chol(state.precision)
    eye = jnp.eye(state.b.shape[1], dtype=state.b.dtype)
    mu = jax.vmap(lambda c, b: cho_solve((c, True), b))(chol, state.b)
    cov = jax.vmap(lambda c: cho_solve((c, True), eye))(chol)  # (K, d, d)
    means = X @ mu.T
    var = jnp.einsum("nd,kde,ne->nk", X, cov, X)
    return means, jnp.maximum(var, 0.0)


def _mask(scores: Array, eligible: Array | None) -> Array:
    if eligible is None:
        return scores
    return jnp.where(eligible, scores, -jnp.inf)


def sample_thetas(
    key: Array, state: LinTSState, scale: float, num_samples: int | None = None
) -> Array:
    """Draw θ̃_k ~ N(μ_k, scale² Λ_k⁻¹): shape (K, d), or (M, K, d) if ``num_samples``.

    With Λ = L Lᵀ, θ̃ = μ + scale · L⁻ᵀ z, z ~ N(0, I).
    """
    chol = _chol(state.precision)
    mu = jax.vmap(lambda c, b: cho_solve((c, True), b))(chol, state.b)
    shape = (num_samples or 1, *mu.shape)
    z = jax.random.normal(key, shape, mu.dtype)
    # solve Lᵀ y = z per arm -> y ~ N(0, Λ⁻¹)
    y = jax.vmap(
        lambda c, zk: solve_triangular(c.T, zk.T, lower=False).T,
        in_axes=(0, 1),
        out_axes=1,
    )(chol, z)
    thetas = mu[None] + scale * y
    return thetas if num_samples is not None else thetas[0]


def select(
    key: Array,
    state: LinTSState,
    X: Array,
    params: LinTSParams,
    eligible: Array | None = None,
) -> tuple[Array, Array]:
    """Thompson-sample an arm for each row of ``X`` (n, d).

    Each row gets an independent posterior draw. Only the scalar score xᵀθ̃_k
    matters for the decision, and for a fixed row it is exactly
    N(xᵀμ_k, s²·xᵀΛ_k⁻¹x), so we sample that directly (identical in
    distribution to drawing θ̃ per row, at O(nK) instead of O(nKd²)).

    Returns ``(arms (n,) int32, sampled_scores (n, K))``; ineligible arms score
    ``-inf`` and are never chosen. ``eligible`` is a (K,) bool mask.
    """
    means, var = _score_moments(state, X)
    z = jax.random.normal(key, means.shape, means.dtype)
    scores = _mask(means + params.exploration_scale * jnp.sqrt(var) * z, eligible)
    return jnp.argmax(scores, axis=-1).astype(jnp.int32), scores


def _apply_floor(freq: Array, floor: float, eligible: Array) -> Array:
    """Clip eligible arms to ``floor`` and renormalise the rest proportionally.

    Water-filling: p_k = max(floor, c·freq_k) on eligible arms, with c chosen so
    Σp = 1 (iterated K times, enough for K arms). Ineligible arms get 0.
    """
    k = freq.shape[0]
    freq = jnp.where(eligible, freq, 0.0)
    clipped = jnp.zeros_like(eligible)
    for _ in range(k):
        free_mass = jnp.sum(jnp.where(clipped, 0.0, freq))
        budget = 1.0 - floor * jnp.sum(clipped & eligible)
        c = jnp.where(free_mass > 0, budget / jnp.maximum(free_mass, 1e-12), 0.0)
        clipped = clipped | (eligible & (c * freq < floor))
    free_mass = jnp.sum(jnp.where(clipped, 0.0, freq))
    budget = 1.0 - floor * jnp.sum(clipped & eligible)
    c = jnp.where(free_mass > 0, budget / jnp.maximum(free_mass, 1e-12), 0.0)
    p = jnp.where(clipped, floor, c * freq)
    p = jnp.where(eligible, p, 0.0)
    return p / jnp.sum(p)


def propensities_batch(
    key: Array,
    state: LinTSState,
    X: Array,
    params: LinTSParams,
    eligible: Array | None = None,
) -> Array:
    """``propensities`` for every row of ``X`` (n, d) at once: shape (n, K)."""
    means, var = _score_moments(state, X)
    n, k = means.shape
    m = params.propensity_samples
    z = jax.random.normal(key, (n, m, k), means.dtype)
    sd = params.exploration_scale * jnp.sqrt(var)
    scores = _mask(means[:, None, :] + sd[:, None, :] * z, eligible)
    wins = jnp.argmax(scores, axis=-1)  # (n, m)
    freq = jax.nn.one_hot(wins, k, dtype=jnp.float32).mean(axis=1)  # (n, k)
    elig = jnp.ones((k,), bool) if eligible is None else eligible
    return jax.vmap(lambda f: _apply_floor(f, params.min_propensity, elig))(freq)


def propensities(
    key: Array,
    state: LinTSState,
    x: Array,
    params: LinTSParams,
    eligible: Array | None = None,
) -> Array:
    """P(select = k | x), shape (K,), by Monte Carlo over ``propensity_samples``
    posterior draws (win frequencies), then floor-clipped to ``min_propensity``
    on eligible arms and renormalised (sums to 1; ineligible arms are 0)."""
    return propensities_batch(key, state, x[None, :], params, eligible)[0]


def update(
    state: LinTSState,
    arms: Array,
    X: Array,
    rewards: Array,
    params: LinTSParams,
) -> LinTSState:
    """Batched conjugate update via scatter-add of xxᵀ/σ² and r·x/σ².

    If ``params.discount`` (γ) < 1, the posterior first decays toward the prior
    (once per call, i.e. per batch): Λ ← γΛ + (1-γ)I/τ², b ← γb. γ = 1 is an
    exact no-op. ``n`` (pull counts) and ``step`` are never discounted.
    """
    gamma = params.discount
    dim = state.b.shape[1]
    prior = jnp.eye(dim, dtype=state.precision.dtype) / params.prior_var
    precision = gamma * state.precision + (1.0 - gamma) * prior[None]
    b = gamma * state.b
    inv_noise = 1.0 / params.noise_var
    outer = jnp.einsum("nd,ne->nde", X, X) * inv_noise
    precision = precision.at[arms].add(outer)
    b = b.at[arms].add(X * (rewards * inv_noise)[:, None])
    n = state.n.at[arms].add(1)
    return LinTSState(precision, b, n, state.step + arms.shape[0])
