"""JAX linear Thompson sampling (contracts §1 ``bandit/linear_ts.py``)."""

import dataclasses

import jax
import jax.numpy as jnp
import numpy as np
import pytest

from bandit import linear_ts as lts
from bandit.config import LinTSParams

K, D = 3, 5
PARAMS = LinTSParams(prior_var=2.0, noise_var=0.5)


def _data(seed=0, n=200):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, D)).astype(np.float32)
    X[:, 0] = 1.0
    arms = rng.integers(0, K, size=n).astype(np.int32)
    rewards = rng.normal(size=n).astype(np.float32)
    return jnp.asarray(arms), jnp.asarray(X), jnp.asarray(rewards)


def test_init_state_shapes():
    s = lts.init_state(K, D, prior_var=2.0)
    assert s.precision.shape == (K, D, D)
    assert s.b.shape == (K, D)
    assert s.n.shape == (K,)
    assert int(s.step) == 0
    np.testing.assert_allclose(s.precision[1], np.eye(D) / 2.0)


def test_posterior_mean_matches_closed_form_ridge():
    arms, X, r = _data()
    s = lts.update(lts.init_state(K, D, PARAMS.prior_var), arms, X, r, PARAMS)
    mu = np.asarray(lts.posterior_mean(s))
    Xn, rn, an = np.asarray(X, np.float64), np.asarray(r, np.float64), np.asarray(arms)
    lam = PARAMS.noise_var / PARAMS.prior_var
    for k in range(K):
        Xk, rk = Xn[an == k], rn[an == k]
        ridge = np.linalg.solve(Xk.T @ Xk + lam * np.eye(D), Xk.T @ rk)
        np.testing.assert_allclose(mu[k], ridge, rtol=1e-4, atol=1e-5)
    np.testing.assert_array_equal(np.asarray(s.n), np.bincount(an, minlength=K))
    assert int(s.step) == len(an)


def test_batched_update_equals_sequential():
    arms, X, r = _data(seed=1, n=60)
    s0 = lts.init_state(K, D, PARAMS.prior_var)
    batched = lts.update(s0, arms, X, r, PARAMS)
    seq = s0
    for i in range(len(arms)):
        seq = lts.update(seq, arms[i : i + 1], X[i : i + 1], r[i : i + 1], PARAMS)
    for a, b in zip(batched, seq, strict=True):
        np.testing.assert_allclose(np.asarray(a), np.asarray(b), rtol=1e-5, atol=1e-5)


def test_discount_one_is_noop_and_lt_one_decays_toward_prior():
    arms, X, r = _data(seed=2, n=80)
    s0 = lts.init_state(K, D, PARAMS.prior_var)
    s1 = lts.update(s0, arms, X, r, PARAMS)
    same = lts.update(s1, arms[:0], X[:0], r[:0], PARAMS)  # discount=1, empty batch
    for a, b in zip(s1, same, strict=True):
        np.testing.assert_array_equal(np.asarray(a), np.asarray(b))
    p_half = dataclasses.replace(PARAMS, discount=0.5)
    decayed = lts.update(s1, arms[:0], X[:0], r[:0], p_half)
    expected = 0.5 * np.asarray(s1.precision) + 0.5 * np.asarray(s0.precision)
    np.testing.assert_allclose(np.asarray(decayed.precision), expected, rtol=1e-6)
    np.testing.assert_allclose(np.asarray(decayed.b), 0.5 * np.asarray(s1.b))


def test_select_shapes_and_eligibility():
    arms, X, r = _data(seed=3)
    s = lts.update(lts.init_state(K, D, PARAMS.prior_var), arms, X, r, PARAMS)
    chosen, scores = lts.select(jax.random.key(0), s, X[:7], PARAMS)
    assert chosen.shape == (7,) and scores.shape == (7, K)
    eligible = jnp.array([False, True, True])
    chosen, scores = lts.select(jax.random.key(0), s, X, PARAMS, eligible)
    assert not np.any(np.asarray(chosen) == 0)
    assert np.all(np.isneginf(np.asarray(scores[:, 0])))


def test_select_is_deterministic_under_jit():
    arms, X, r = _data(seed=4)
    s = lts.update(lts.init_state(K, D, PARAMS.prior_var), arms, X, r, PARAMS)
    jsel = jax.jit(lts.select, static_argnames=("params",))
    a1, s1 = jsel(jax.random.key(7), s, X, params=PARAMS)
    a2, s2 = jsel(jax.random.key(7), s, X, params=PARAMS)
    a3, _ = lts.select(jax.random.key(7), s, X, PARAMS)
    np.testing.assert_array_equal(np.asarray(a1), np.asarray(a2))
    np.testing.assert_array_equal(np.asarray(s1), np.asarray(s2))
    np.testing.assert_array_equal(np.asarray(a1), np.asarray(a3))
    a4, _ = jsel(jax.random.key(8), s, X, params=PARAMS)
    assert not np.array_equal(np.asarray(a1), np.asarray(a4))


def _two_arm_state(n_obs=40):
    """Two arms with overlapping posteriors on a single context x."""
    x = jnp.zeros(D).at[0].set(1.0)
    X = jnp.tile(x, (2 * n_obs, 1))
    arms = jnp.array([0] * n_obs + [1] * n_obs, dtype=jnp.int32)
    rewards = jnp.array([0.1] * n_obs + [0.0] * n_obs, dtype=jnp.float32)
    params = LinTSParams(noise_var=0.25, propensity_samples=4000, min_propensity=0.0)
    s = lts.update(lts.init_state(2, D, params.prior_var), arms, X, rewards, params)
    return s, x, params


def test_propensities_sum_to_one_and_match_empirical_selection():
    s, x, params = _two_arm_state()
    p = np.asarray(lts.propensities(jax.random.key(1), s, x, params))
    assert p.shape == (2,)
    assert abs(p.sum() - 1.0) < 1e-6
    assert 0.6 < p[0] < 0.99  # overlapping posteriors, arm 0 favoured
    chosen, _ = lts.select(jax.random.key(2), s, jnp.tile(x, (20000, 1)), params)
    empirical = np.bincount(np.asarray(chosen), minlength=2) / 20000
    np.testing.assert_allclose(p, empirical, atol=0.03)


def test_propensities_respect_floor_and_eligibility():
    s, x, params = _two_arm_state(n_obs=2000)  # arm 0 wins ~always
    floored = dataclasses.replace(params, min_propensity=0.05)
    p = np.asarray(lts.propensities(jax.random.key(3), s, x, floored))
    assert abs(p.sum() - 1.0) < 1e-6
    assert p.min() >= 0.05 - 1e-6
    np.testing.assert_allclose(p[1], 0.05, atol=1e-6)
    only1 = np.asarray(
        lts.propensities(jax.random.key(3), s, x, floored, jnp.array([False, True]))
    )
    np.testing.assert_allclose(only1, [0.0, 1.0])


def test_propensities_jit():
    s, x, params = _two_arm_state()
    jp = jax.jit(lts.propensities, static_argnames=("params",))
    np.testing.assert_array_equal(
        np.asarray(jp(jax.random.key(5), s, x, params=params)),
        np.asarray(jp(jax.random.key(5), s, x, params=params)),
    )


def test_learns_clearly_better_arm():
    """After enough updates the clearly better arm is chosen > 90% of the time."""
    rng = np.random.default_rng(0)
    params = LinTSParams()
    true_theta = np.zeros((3, D), np.float32)
    true_theta[:, 0] = [0.2, 0.05, 0.0]
    s = lts.init_state(3, D, params.prior_var)
    key = jax.random.key(0)
    for t in range(40):
        X = rng.integers(0, 2, size=(50, D)).astype(np.float32)
        X[:, 0] = 1.0
        arms, _ = lts.select(jax.random.fold_in(key, t), s, jnp.asarray(X), params)
        arms_np = np.asarray(arms)
        mean = (X * true_theta[arms_np]).sum(1)
        rewards = (rng.random(50) < mean).astype(np.float32)
        s = lts.update(s, arms, jnp.asarray(X), jnp.asarray(rewards), params)
    X = rng.integers(0, 2, size=(1000, D)).astype(np.float32)
    X[:, 0] = 1.0
    arms, _ = lts.select(jax.random.key(99), s, jnp.asarray(X), params)
    assert np.mean(np.asarray(arms) == 0) > 0.9


def test_sample_thetas_covariance():
    s, _, params = _two_arm_state()
    thetas = lts.sample_thetas(jax.random.key(0), s, 1.0, num_samples=20000)
    assert thetas.shape == (20000, 2, D)
    cov = np.cov(np.asarray(thetas[:, 0, :2]).T)
    expected = np.linalg.inv(np.asarray(s.precision[0], np.float64))[:2, :2]
    np.testing.assert_allclose(cov, expected, rtol=0.1, atol=3e-3)


@pytest.mark.parametrize("bad", [{"prior_var": 0.0}, {"discount": 1.5}])
def test_params_validation(bad):
    from bandit.config import validate_lints_params

    with pytest.raises(ValueError):
        validate_lints_params(dataclasses.replace(LinTSParams(), **bad))
