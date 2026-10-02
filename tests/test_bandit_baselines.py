"""Non-contextual baselines + the shared policy interface (``bandit/baselines.py``)."""

import jax
import jax.numpy as jnp
import numpy as np
import pytest

from bandit import baselines
from bandit.config import LinTSParams
from bandit.policies import POLICY_NAMES, make_policy

K, D, N = 3, 4, 2000
X = jnp.ones((N, D), jnp.float32)
ALL = jnp.ones((K,), bool)
MEAN = jnp.zeros((N, K), jnp.float32)


def _state(n, sum_r, step=None):
    n = jnp.asarray(n, jnp.int32)
    sum_r = jnp.asarray(sum_r, jnp.float32)
    return baselines.CountsState(
        n=n,
        sum_r=sum_r,
        clicks=sum_r.astype(jnp.int32),
        step=jnp.asarray(int(np.sum(n)) if step is None else step, jnp.int32),
    )


def test_policy_names_match_contract():
    assert POLICY_NAMES == (
        "linear_ts",
        "ucb1",
        "epsilon_greedy",
        "beta_bernoulli_ts",
        "uniform",
        "oracle",
    )


def test_make_policy_specs():
    p = make_policy("ucb1:c=0.5", lints_params=LinTSParams(), reward_mode="click")
    assert p.name == "ucb1:c=0.5"
    p = make_policy(
        "linear_ts:discount=0.9", lints_params=LinTSParams(), reward_mode="click"
    )
    assert p.name == "linear_ts:discount=0.9"
    with pytest.raises(ValueError):
        make_policy("greedy_magic", lints_params=LinTSParams(), reward_mode="click")
    with pytest.raises(ValueError):
        make_policy("ucb1:bogus=1", lints_params=LinTSParams(), reward_mode="click")


def test_uniform_is_uniform_over_eligible():
    pol = baselines.uniform()
    s = pol.init(K, D)
    elig = jnp.array([True, False, True])
    arms, prop = pol.select(jax.random.key(0), s, X, elig, MEAN)
    counts = np.bincount(np.asarray(arms), minlength=K) / N
    assert counts[1] == 0
    np.testing.assert_allclose(counts[[0, 2]], 0.5, atol=0.05)
    np.testing.assert_allclose(np.asarray(prop), 0.5)


def test_ucb_tries_unseen_arms_first():
    pol = baselines.ucb1(c=0.1)
    s = _state([50, 0, 0], [40, 0, 0])  # arm 0 looks great but 1, 2 unseen
    arms, prop = pol.select(jax.random.key(0), s, X[:200], ALL, MEAN[:200])
    arms = np.asarray(arms)
    assert set(arms) == {1, 2}  # random tie-break among unseen arms
    np.testing.assert_allclose(np.asarray(prop), 0.5)


def test_ucb_eq_2_10_bonus():
    """UCB_k = mean_k + c * sqrt(ln t / n_k) (Sutton & Barto eq. 2.10)."""
    c = 0.5
    pol = baselines.ucb1(c=c)
    n = np.array([100, 10, 400])
    sum_r = np.array([10.0, 0.5, 44.0])  # means 0.1, 0.05, 0.11
    s = _state(n, sum_r, step=509)
    arms, _ = pol.select(jax.random.key(0), s, X[:1], ALL, MEAN[:1])
    t = 510
    ucb = sum_r / n + c * np.sqrt(np.log(t) / n)
    assert int(arms[0]) == int(np.argmax(ucb)) == 1
    # with a tiny multiplier the greedy arm wins (the notebook's small-epsilon trap)
    arms, _ = baselines.ucb1(c=0.001).select(jax.random.key(0), s, X[:1], ALL, MEAN[:1])
    assert int(arms[0]) == 2


def test_epsilon_greedy_explores_at_rate_epsilon():
    eps = 0.2
    pol = baselines.epsilon_greedy(epsilon=eps)
    s = _state([100, 100, 100], [30, 5, 5])  # arm 0 greedy
    arms, prop = pol.select(jax.random.key(1), s, X, ALL, MEAN)
    non_greedy = np.mean(np.asarray(arms) != 0)
    assert abs(non_greedy - eps * (K - 1) / K) < 0.03
    prop = np.asarray(prop)
    arms = np.asarray(arms)
    np.testing.assert_allclose(prop[arms == 0], 1 - eps + eps / K, rtol=1e-6)
    np.testing.assert_allclose(prop[arms != 0], eps / K, rtol=1e-6)


def test_epsilon_greedy_all_zero_estimates_random():
    pol = baselines.epsilon_greedy(epsilon=0.0)
    arms, _ = pol.select(jax.random.key(2), pol.init(K, D), X, ALL, MEAN)
    counts = np.bincount(np.asarray(arms), minlength=K) / N
    np.testing.assert_allclose(counts, 1 / K, atol=0.05)


def test_counts_update_and_bbts_posterior():
    pol = baselines.beta_bernoulli_ts()
    s = pol.init(K, D)
    arms = jnp.array([0, 0, 1, 2, 2, 2], jnp.int32)
    rewards = jnp.array([1.0, 0.0, 1.0, 0.0, 0.0, 1.0])
    s = pol.update(s, arms, X[:6], rewards)
    np.testing.assert_array_equal(np.asarray(s.n), [2, 1, 3])
    np.testing.assert_array_equal(np.asarray(s.clicks), [1, 1, 1])
    alpha, beta = baselines.beta_posterior(s)
    np.testing.assert_array_equal(np.asarray(alpha), [2, 2, 2])
    np.testing.assert_array_equal(np.asarray(beta), [2, 1, 3])
    assert int(s.step) == 6


def test_bbts_engaged_counts_clicks_from_positive_reward():
    pol = baselines.beta_bernoulli_ts(engaged=True)
    s = pol.update(
        pol.init(2, D),
        jnp.array([0, 0, 1], jnp.int32),
        X[:3],
        jnp.array([12.5, 0.0, 3.0]),
    )
    np.testing.assert_array_equal(np.asarray(s.clicks), [1, 1])
    np.testing.assert_allclose(np.asarray(s.sum_r), [12.5, 3.0])


def test_bbts_prefers_better_arm_and_propensity():
    pol = baselines.beta_bernoulli_ts()
    s = _state([1000, 1000, 1000], [80, 40, 40])
    arms, prop = pol.select(jax.random.key(3), s, X, ALL, MEAN)
    assert np.mean(np.asarray(arms) == 0) > 0.95
    prop, arms = np.asarray(prop), np.asarray(arms)
    assert np.all((prop >= 0) & (prop <= 1))
    # MC propensity of the favoured arm matches its empirical selection share
    assert abs(prop[arms == 0].mean() - np.mean(arms == 0)) < 0.02


def test_oracle_picks_argmax_of_true_mean_over_eligible():
    pol = baselines.oracle()
    mean = jax.random.uniform(jax.random.key(4), (N, K))
    arms, prop = pol.select(jax.random.key(5), pol.init(K, D), X, ALL, mean)
    np.testing.assert_array_equal(np.asarray(arms), np.argmax(np.asarray(mean), 1))
    np.testing.assert_allclose(np.asarray(prop), 1.0)
    elig = jnp.array([True, True, False])
    arms, _ = pol.select(jax.random.key(5), pol.init(K, D), X, elig, mean)
    np.testing.assert_array_equal(
        np.asarray(arms), np.argmax(np.asarray(mean)[:, :2], 1)
    )


def test_linear_ts_policy_wrapper_runs_under_jit():
    pol = make_policy("linear_ts", lints_params=LinTSParams(), reward_mode="click")
    s = pol.init(K, D)
    sel = jax.jit(pol.select)
    arms, prop = sel(jax.random.key(0), s, X[:10], ALL, MEAN[:10])
    assert arms.shape == (10,) and prop.shape == (10,)
    assert np.all((np.asarray(prop) > 0) & (np.asarray(prop) <= 1))
    s2 = jax.jit(pol.update)(s, arms, X[:10], jnp.ones(10))
    assert int(s2.step) == 10
