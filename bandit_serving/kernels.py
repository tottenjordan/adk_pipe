"""The predictor's jit kernels over ``bandit.linear_ts`` (contracts §2).

Batches are zero-padded to power-of-two buckets (``bucket``) so the kernels
recompile only for a bounded set of shapes; ``warmup_buckets`` names the sizes
compiled at load.
"""

from __future__ import annotations

import functools
import logging
import os
import time

import jax
import jax.numpy as jnp
import numpy as np

from bandit import linear_ts as lts
from bandit.config import LinTSParams

log = logging.getLogger(__name__)

MIN_BUCKET = 16  # pad batches to powers of two >= this (bounded jit recompiles)
MAX_CHUNK = 1024  # rows per decision kernel call (bounds the MC propensity tensor)


def bucket(n: int) -> int:
    return max(MIN_BUCKET, 1 << max(n - 1, 0).bit_length())


def pad_rows(a: np.ndarray, rows: int) -> np.ndarray:
    pad = [(0, rows - a.shape[0])] + [(0, 0)] * (a.ndim - 1)
    return np.pad(a, pad)


@functools.partial(jax.jit, static_argnames=("params",))
def decide_kernel(key, state, X, eligible, params):
    k_sel, k_prop = jax.random.split(key)
    arms, _ = lts.select(k_sel, state, X, params, eligible)
    probs = lts.propensities_batch(k_prop, state, X, params, eligible)
    greedy_scores = jnp.where(eligible, X @ lts.posterior_mean(state).T, -jnp.inf)
    return arms, probs, jnp.argmax(greedy_scores, axis=-1)


@functools.partial(jax.jit, static_argnames=("params", "batch_size"))
def decide_rows_kernel(key, state, X, rows, eligible, params, batch_size):
    """``decide_kernel`` keyed like ``bandit.simulate``: ``key`` is the batch's
    ``fold_in(k_pol, batch)``, and the selection noise is drawn at the simulator's
    ``(batch_size, K)`` shape and indexed by each served row's ``rows`` (padding
    rows read row 0 and are discarded). A row's arm therefore doesn't depend on
    padding, request splitting or retries."""
    k_sel, k_prop = jax.random.split(key)
    z = jax.random.normal(k_sel, (batch_size, state.b.shape[0]), state.b.dtype)
    arms, _ = lts.select_with_noise(state, X, z[rows], params, eligible)
    probs = lts.propensities_batch(k_prop, state, X, params, eligible)
    greedy_scores = jnp.where(eligible, X @ lts.posterior_mean(state).T, -jnp.inf)
    return arms, probs, jnp.argmax(greedy_scores, axis=-1)


@functools.partial(jax.jit, static_argnames=("params", "reward_scale"))
def update_kernel(state, arms, X, rewards, valid, params, reward_scale=1.0):
    """``lts.update`` on zero-padded rows; padding adds nothing to Λ/b and is
    excluded from the pull counts and step. The raw float32 rewards are divided
    by ``reward_scale`` in here: the same float32 op as ``simulate._episode``.
    Returns ``(new_state, finite)``: ``finite`` is False when the update left Λ
    or b non-finite, and the caller then keeps the previous state."""
    new = lts.update(state, arms, X, rewards / reward_scale, params)
    counts = jnp.zeros_like(state.n).at[arms].add(valid.astype(state.n.dtype))
    new = new._replace(n=state.n + counts, step=state.step + jnp.sum(valid))
    return new, state_finite(new)


def state_finite(state: lts.LinTSState) -> jax.Array:
    return jnp.all(jnp.isfinite(state.precision)) & jnp.all(jnp.isfinite(state.b))


posterior_mean = jax.jit(lts.posterior_mean)


def warmup_buckets() -> list[int]:
    """Padded batch sizes to pre-compile (``BANDIT_WARMUP_BUCKETS``; default
    16..128 covers the traffic job's 100-round batches); ``BANDIT_WARMUP=0``
    disables warm-up."""
    if os.environ.get("BANDIT_WARMUP", "1") == "0":
        return []
    raw = os.environ.get("BANDIT_WARMUP_BUCKETS", "16,32,64,128")
    sizes = {bucket(int(v)) for v in raw.split(",") if v.strip().isdigit()}
    return sorted(min(size, MAX_CHUNK) for size in sizes)


def warmup(
    n_arms: int,
    dim: int,
    params: LinTSParams,
    batch_size: int,
    reward_scale: float,
) -> None:
    """Compile the default-params kernels for the ``BANDIT_WARMUP_BUCKETS``
    batch sizes before the model server reports healthy (a cold jit costs
    ~0.3-1 s per kernel and shape). ``BANDIT_WARMUP=0`` skips it. Runs on a
    throwaway prior state, never the live posterior."""
    sizes = warmup_buckets()
    if not sizes:
        return
    t0 = time.perf_counter()
    state = lts.init_state(n_arms, dim, params.prior_var)
    eligible = jnp.ones((n_arms,), bool)
    for size in sizes:
        X = jnp.zeros((size, dim), jnp.float32)
        zeros = jnp.zeros((size,), jnp.int32)
        jax.block_until_ready(
            decide_kernel(jax.random.key(0), state, X, eligible, params)
        )
        jax.block_until_ready(
            decide_rows_kernel(
                jax.random.key(0), state, X, zeros, eligible, params, batch_size
            )
        )
        jax.block_until_ready(
            update_kernel(
                state,
                zeros,
                X,
                zeros.astype(jnp.float32),
                zeros > 0,
                params,
                reward_scale,
            )
        )
    jax.block_until_ready(posterior_mean(state))
    log.info("jit warm-up: %.0f ms", (time.perf_counter() - t0) * 1000.0)
