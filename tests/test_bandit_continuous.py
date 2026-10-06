"""Continuous learning mode in the simulator (contracts §11).

A continuous traffic run is one long episode cut into segments:
``simulate.run_segment`` replays a segment from a carried policy state with the
global batch index, so chaining it over the segments must reproduce
``run_episodes`` at the whole run's horizon round for round.
"""

import jax
import numpy as np
import pytest

from bandit import simulate
from bandit.config import build_sim_config, resolve_scenario
from bandit.policies import make_policy
from tests._bandit_sizes import BATCH, HORIZON_S

SEGMENTS = 4
T, BS = HORIZON_S, BATCH
NB = T // BS


def _setup(scenario="segment_winners", horizon=SEGMENTS * T):
    cfg = build_sim_config(scenario, horizon=horizon, batch_size=BS, episodes=1)
    env = simulate.build_environment(cfg, scenario=resolve_scenario(cfg))
    key = simulate.episode_keys(cfg.seed, cfg.scenario, 1)[0]
    return cfg, env, key


def _policy(cfg, name):
    return make_policy(
        name,
        lints_params=cfg.policy,
        reward_mode=cfg.reward_mode,
        log_propensity=False,
    )


@pytest.mark.parametrize("name", ["linear_ts", "ucb1"])
def test_segments_equal_one_long_episode(name):
    cfg, env, key = _setup()
    pol = _policy(cfg, name)
    long = simulate.run_episodes(pol, env, key[None], SEGMENTS * T, BS)
    long = {k: v[0] for k, v in long.items()}
    segs, state = [], None
    for s in range(SEGMENTS):
        out, state = simulate.run_segment(
            pol, env, key, s * NB, NB, BS, init_state=state
        )
        assert out["arm"].shape == (T,)
        segs.append(out)
    for k in simulate.OUTPUT_KEYS:
        np.testing.assert_array_equal(
            long[k], np.concatenate([s[k] for s in segs]), err_msg=k
        )


def test_state_carries_across_segments():
    cfg, env, key = _setup()
    pol = _policy(cfg, "linear_ts")
    _, state = simulate.run_segment(pol, env, key, 0, NB, BS)
    carried, _ = simulate.run_segment(pol, env, key, NB, NB, BS, init_state=state)
    fresh, _ = simulate.run_segment(pol, env, key, NB, NB, BS)
    # same users and coin flips (policy-independent draws) ...
    np.testing.assert_array_equal(carried["segment"], fresh["segment"])
    np.testing.assert_array_equal(carried["p_all"], fresh["p_all"])
    # ... but a different posterior, so different choices
    assert not np.array_equal(carried["arm"], fresh["arm"])
    # the carried posterior has seen the first segment's rounds
    assert int(state.step) == T


def test_run_segment_final_state_counts_every_round():
    cfg, env, key = _setup()
    pol = _policy(cfg, "ucb1")
    state = None
    for s in range(2):
        _, state = simulate.run_segment(pol, env, key, s * NB, NB, BS, state)
    assert int(state.step) == 2 * T  # decisions so far (ucb1 uses it)
    assert int(np.asarray(state.n).sum()) == 2 * T
    assert jax.tree_util.tree_structure(state) == jax.tree_util.tree_structure(
        pol.init(env.num_arms, int(env.model.theta.shape[1]))
    )
