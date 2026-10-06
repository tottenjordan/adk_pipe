"""Continuous learning mode in the simulator (contracts §11).

A continuous traffic run is one long episode cut into segments:
``simulate.run_segment`` replays a segment from a carried policy state with the
global batch index, so chaining it over the segments must reproduce
``run_episodes`` at the whole run's horizon round for round.
"""

import jax
import numpy as np
import pytest

from bandit import environment as envm
from bandit import simulate
from bandit.config import (
    LEARNING_MODES,
    MAX_CONTINUOUS_ROUNDS,
    build_sim_config,
    continuous_world_config,
    resolve_scenario,
    shifts_from_dict,
    validate_continuous_run,
    validate_shifts,
)
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


# ------------------------------------------- the whole-run world (contracts §11)


def test_continuous_world_config_spans_the_whole_run():
    cfg = build_sim_config("drift", horizon=T, batch_size=BS, episodes=SEGMENTS)
    world = continuous_world_config(cfg)
    assert world.horizon == SEGMENTS * T and world.episodes == 1
    assert (world.seed, world.scenario, world.arms) == (
        cfg.seed,
        cfg.scenario,
        cfg.arms,
    )
    # the total may exceed the per-episode horizon cap (1e6), up to 2e6
    big = build_sim_config("drift", horizon=400_000, batch_size=BS, episodes=5)
    assert continuous_world_config(big).horizon == MAX_CONTINUOUS_ROUNDS == 2_000_000


@pytest.mark.parametrize(
    ("episodes", "horizon", "batch_size", "field"),
    [
        (6, 400_000, 100, "episodes"),  # 2.4M rounds > 2M
        (2, 1_050, 100, "horizon"),  # a batch would straddle segments
    ],
)
def test_validate_continuous_run_rejects(episodes, horizon, batch_size, field):
    with pytest.raises(ValueError, match=field):
        validate_continuous_run(episodes, horizon, batch_size)
    assert validate_continuous_run(5, 400_000, 100) == 2_000_000
    assert LEARNING_MODES == ("per_episode", "continuous")


def test_drift_flips_once_over_the_whole_run():
    cfg = build_sim_config("drift", horizon=T, batch_size=BS, episodes=SEGMENTS)
    sc = resolve_scenario(cfg)
    env = simulate.build_environment(continuous_world_config(cfg), scenario=sc)
    H = SEGMENTS * T
    flip = sc.drift.at_frac * H
    assert float(env.model.drift_start) == flip  # abrupt drift
    t = np.arange(H)
    w = np.asarray(envm.drift_weight(env.model, t))
    np.testing.assert_array_equal(w, (t >= flip).astype(np.float32))
    # once per run, not once per segment
    assert int(np.sum(np.diff(w) != 0)) == 1


def test_shift_resolves_against_the_whole_run():
    cfg = build_sim_config(
        "segment_winners", horizon=T, batch_size=BS, episodes=SEGMENTS
    )
    sc = resolve_scenario(cfg)
    demote = {"kind": "demote", "at_frac": 0.5, "creative_id": "leader"}
    shifts = validate_shifts(
        shifts_from_dict([{**demote, "drop_pp": 0.015}]), sc, cfg.arms, cfg.ctr_mode
    )
    env = simulate.build_environment(
        continuous_world_config(cfg), scenario=sc, shifts=shifts
    )
    assert [r["round"] for r in envm.resolved_shifts(env)] == [SEGMENTS * T // 2]
