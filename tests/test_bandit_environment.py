"""Synthetic logistic ground truth + context/reward samplers (``bandit/environment.py``)."""

import dataclasses

import jax
import jax.numpy as jnp
import numpy as np
import pytest

from bandit import environment as envm
from bandit import features
from bandit.config import (
    ScenarioOverrides,
    build_sim_config,
    load_scenario,
    resolve_scenario,
)

KEY = jax.random.key(0)


def _env(scenario, ctr_mode="demo", reward_mode="click", **kw):
    cfg = build_sim_config(scenario, ctr_mode=ctr_mode, reward_mode=reward_mode, **kw)
    return envm.build_true_model(cfg, jax.random.key(cfg.seed))


@pytest.mark.parametrize("ctr_mode", ["demo", "realistic"])
@pytest.mark.parametrize("scenario", ["clear_winner", "segment_winners", "drift"])
def test_calibration_hits_target_mean_ctr(scenario, ctr_mode):
    env = _env(scenario, ctr_mode)
    target = load_scenario(scenario).target_ctr[ctr_mode]
    seg, _, X = envm.sample_contexts(jax.random.key(123), env.model, 40000)
    p = envm.click_probs(env.model, X, seg, jnp.zeros(seg.shape[0]))
    mean_ctr = float(jnp.mean(p))
    assert abs(mean_ctr - target) / target < 0.10, (mean_ctr, target)
    assert env.target_ctr == target


def test_clear_winner_arm_ctrs_follow_preset():
    env = _env("clear_winner")
    ctrs = envm.marginal_ctrs(env, jax.random.key(1))["overall"]
    order = np.argsort(-ctrs)
    np.testing.assert_allclose(ctrs[order], [0.060, 0.045, 0.035], rtol=0.15)
    # ranked by judge score: synthetic-a (overall .82) is the best arm
    assert env.arm_ids[order[0]] == "synthetic-a"


def test_judge_wrong_reverses_score_ranking():
    sc = dataclasses.replace(load_scenario("clear_winner"), judge_wrong=1.0)
    cfg = build_sim_config("clear_winner")
    env = envm.build_true_model(cfg, KEY, scenario=sc)
    ctrs = envm.marginal_ctrs(env, jax.random.key(1))["overall"]
    assert env.arm_ids[int(np.argmax(ctrs))] == "synthetic-c"  # lowest judge score


def test_segment_winners_have_distinct_oracle_arms():
    env = _env("segment_winners")
    assert len(env.arm_ids) == 4
    by_seg = envm.marginal_ctrs(env, jax.random.key(2))["by_segment"]  # (S, K)
    winners = np.argmax(by_seg, axis=1)
    assert len(set(winners.tolist())) == 4
    # winner lift is about +1.5 pp over the best other arm in each segment
    for s, w in enumerate(winners):
        others = np.delete(by_seg[s], w)
        assert 0.008 < by_seg[s, w] - others.max() < 0.03


def test_drift_flips_best_arm_at_change_point():
    env = _env("drift", horizon=1000)
    before = envm.marginal_ctrs(env, jax.random.key(3), t=0)["overall"]
    after = envm.marginal_ctrs(env, jax.random.key(3), t=999)["overall"]
    assert int(np.argmax(before)) == int(np.argmin(after))
    assert int(np.argmax(before)) != int(np.argmax(after))
    mid = envm.marginal_ctrs(env, jax.random.key(3), t=499)["overall"]
    np.testing.assert_allclose(mid, before, rtol=1e-6)  # abrupt at T/2 = 500


def test_gradual_drift_ramps():
    sc = load_scenario("drift")
    sc = dataclasses.replace(
        sc, drift=dataclasses.replace(sc.drift, kind="gradual", width_frac=0.4)
    )
    cfg = build_sim_config("drift", horizon=1000)
    env = envm.build_true_model(cfg, KEY, scenario=sc)
    best = int(np.argmax(envm.marginal_ctrs(env, jax.random.key(4), t=0)["overall"]))
    traj = [
        envm.marginal_ctrs(env, jax.random.key(4), t=t)["overall"][best]
        for t in (0, 300, 450, 550, 700, 999)
    ]
    assert traj[0] == pytest.approx(traj[1])
    assert traj[1] > traj[2] > traj[3] > traj[4]
    assert traj[4] == pytest.approx(traj[5])


def test_engaged_reward_zero_without_click():
    p = jnp.full((5000, 2), 0.3)
    dwell = jnp.full((5000, 2), 40.0)
    clicked, reward = envm.sample_rewards(jax.random.key(5), p, "engaged", dwell)
    clicked, reward = np.asarray(clicked), np.asarray(reward)
    assert np.all(reward[clicked == 0] == 0)
    assert np.all(reward[clicked == 1] > 0)
    assert abs(reward[clicked == 1].mean() - 40.0) / 40.0 < 0.1
    clicked_c, reward_c = envm.sample_rewards(jax.random.key(5), p, "click", dwell)
    np.testing.assert_array_equal(np.asarray(clicked_c), clicked)  # same coin flips
    np.testing.assert_array_equal(np.asarray(reward_c), clicked)


def test_contexts_and_rewards_reproducible_from_keys():
    env = _env("segment_winners")
    a = envm.sample_contexts(jax.random.key(9), env.model, 100)
    b = envm.sample_contexts(jax.random.key(9), env.model, 100)
    c = envm.sample_contexts(jax.random.key(10), env.model, 100)
    for x, y in zip(a, b, strict=True):
        np.testing.assert_array_equal(np.asarray(x), np.asarray(y))
    assert not np.array_equal(np.asarray(a[2]), np.asarray(c[2]))
    p = jnp.full((100, 4), 0.5)
    r1 = envm.sample_rewards(jax.random.key(1), p, "click", p)
    r2 = envm.sample_rewards(jax.random.key(1), p, "click", p)
    np.testing.assert_array_equal(np.asarray(r1[0]), np.asarray(r2[0]))
    # same seed -> identical ground truth
    env2 = _env("segment_winners")
    np.testing.assert_array_equal(
        np.asarray(env.model.seg_aff), np.asarray(env2.model.seg_aff)
    )


def test_context_matrix_matches_features_encoding_and_no_sensitive_fields():
    env = _env("segment_winners")
    _, levels, X = envm.sample_contexts(jax.random.key(11), env.model, 300)
    ctxs = envm.decode_contexts(levels)
    for ctx in ctxs:
        assert set(ctx) == set(features.CONTEXT_KEYS)
        assert not set(ctx) & features.SENSITIVE_KEYS
        assert ctx["age_bucket"] in ("21-34", "35-54", "55+")
    np.testing.assert_array_equal(features.encode_batch(ctxs), np.asarray(X))


def test_segment_marginals_are_respected():
    env = _env("segment_winners")
    seg, _, X = envm.sample_contexts(jax.random.key(12), env.model, 40000)
    seg, X = np.asarray(seg), np.asarray(X)
    names = features.feature_names()
    desktop = names.index("devicetype=desktop")
    intender = env.segment_names.index("product_intenders")
    assert abs(X[seg == intender, desktop].mean() - 0.85) < 0.03
    np.testing.assert_allclose(np.bincount(seg) / len(seg), 0.25, atol=0.02)


def test_optimal_arms_respects_eligibility():
    v = jnp.array([[0.1, 0.3, 0.2], [0.5, 0.1, 0.4]])
    np.testing.assert_array_equal(np.asarray(envm.optimal_arms(v)), [1, 0])
    elig = jnp.array([True, False, True])
    np.testing.assert_array_equal(np.asarray(envm.optimal_arms(v, elig)), [2, 0])


def test_dwell_means_positive_and_engaged_mode():
    env = _env("clear_winner", reward_mode="engaged")
    dm = np.asarray(env.model.dwell_means)
    assert dm.shape == (3, 3) and np.all(dm > 0)
    assert env.reward_scale == pytest.approx(30.0)
    assert _env("clear_winner").reward_scale == 1.0


# ------------------------------------------------- scenario overrides (contracts §9)


def _env_ov(scenario, key=None, **ov):
    """Ground truth for ``scenario`` tuned by ``ScenarioOverrides(**ov)``, built the
    way the traffic job does it (``resolve_scenario(cfg)``)."""
    horizon = ov.pop("horizon", None)
    cfg = build_sim_config(
        scenario, horizon=horizon, scenario_overrides=ScenarioOverrides(**ov)
    )
    return envm.build_true_model(
        cfg, key if key is not None else KEY, scenario=resolve_scenario(cfg)
    )


def _rank_gap(env):
    ctrs = envm.marginal_ctrs(env, jax.random.key(21))["overall"]
    return float(ctrs.max() - ctrs.min())


def _segment_lift(env):
    by_seg = envm.marginal_ctrs(env, jax.random.key(22))["by_segment"]
    lifts = [
        by_seg[s, w] - np.delete(by_seg[s], w).max()
        for s, w in enumerate(np.argmax(by_seg, axis=1))
    ]
    return float(np.mean(lifts))


def test_segment_mix_override_shifts_sampled_segments():
    env = _env_ov("segment_winners", segment_mix=(0.7, 0.1, 0.1, 0.1))
    seg, _, _ = envm.sample_contexts(jax.random.key(13), env.model, 40000)
    freq = np.bincount(np.asarray(seg), minlength=4) / seg.shape[0]
    np.testing.assert_allclose(freq, [0.7, 0.1, 0.1, 0.1], atol=0.02)


@pytest.mark.parametrize("scenario", ["clear_winner", "drift"])
def test_gap_scale_widens_and_narrows_rank_ctr_gap(scenario):
    base = _rank_gap(_env_ov(scenario))
    wide = _rank_gap(_env_ov(scenario, gap_scale=2.0))
    narrow = _rank_gap(_env_ov(scenario, gap_scale=0.5))
    assert narrow < 0.7 * base and wide > 1.5 * base, (narrow, base, wide)


def test_gap_scale_widens_and_narrows_segment_winner_lift():
    base = _segment_lift(_env_ov("segment_winners"))
    wide = _segment_lift(_env_ov("segment_winners", gap_scale=2.0))
    narrow = _segment_lift(_env_ov("segment_winners", gap_scale=0.5))
    assert narrow < 0.7 * base and wide > 1.5 * base, (narrow, base, wide)


def test_judge_wrong_override_reverses_ranking():
    env = _env_ov("clear_winner", judge_wrong=1.0)
    ctrs = envm.marginal_ctrs(env, jax.random.key(1))["overall"]
    assert env.arm_ids[int(np.argmax(ctrs))] == "synthetic-c"  # lowest judge score
    assert env.arm_ids[int(np.argmin(ctrs))] == "synthetic-a"  # highest judge score


@pytest.mark.parametrize("scenario", ["clear_winner", "segment_winners"])
def test_noise_scale_zero_removes_noise(scenario):
    a = _env_ov(scenario, jax.random.key(1), noise_scale=0.0)
    b = _env_ov(scenario, jax.random.key(2), noise_scale=0.0)
    for field in ("arm_base", "seg_aff", "theta"):
        np.testing.assert_allclose(
            np.asarray(getattr(a.model, field)), np.asarray(getattr(b.model, field))
        )
    # only the structured interaction columns carry weight in theta
    names = features.feature_names()
    structured = {
        names.index(n)
        for n in (
            "topic_matches_trend",
            "interest_matches_product",
            "devicetype=desktop",
            "devicetype=tablet",
        )
    }
    theta = np.asarray(a.model.theta)
    others = [c for c in range(theta.shape[1]) if c not in structured]
    np.testing.assert_array_equal(theta[:, others], 0.0)
    # with the default noise, different keys give different truths
    c = _env_ov(scenario, jax.random.key(1))
    d = _env_ov(scenario, jax.random.key(2))
    assert not np.allclose(np.asarray(c.model.theta), np.asarray(d.model.theta))


def test_drift_at_frac_moves_the_flip_point():
    env = _env_ov("drift", horizon=1000, drift_at_frac=0.3)
    before = envm.marginal_ctrs(env, jax.random.key(3), t=0)["overall"]
    at_299 = envm.marginal_ctrs(env, jax.random.key(3), t=299)["overall"]
    at_300 = envm.marginal_ctrs(env, jax.random.key(3), t=300)["overall"]
    np.testing.assert_allclose(at_299, before, rtol=1e-6)
    assert int(np.argmax(before)) == int(np.argmin(at_300))


@pytest.mark.parametrize(
    ("scenario", "ov"),
    [
        ("clear_winner", {"gap_scale": 2.0, "noise_scale": 2.0}),
        ("clear_winner", {"segment_mix": (1.0, 0.05, 0.05), "judge_wrong": 0.5}),
        ("segment_winners", {"gap_scale": 2.0, "segment_mix": (0.05, 0.05, 0.05, 1)}),
        ("drift", {"gap_scale": 0.25, "drift_at_frac": 0.8, "noise_scale": 0.0}),
    ],
)
def test_calibration_hits_target_with_overrides(scenario, ov):
    env = _env_ov(scenario, **ov)
    target = load_scenario(scenario).target_ctr["demo"]
    seg, _, X = envm.sample_contexts(jax.random.key(123), env.model, 40000)
    p = envm.click_probs(env.model, X, seg, jnp.zeros(seg.shape[0]))
    mean_ctr = float(jnp.mean(p))
    assert abs(mean_ctr - target) / target < 0.10, (mean_ctr, target)


def _calibrate_alpha_reference(logits_wo_alpha, target):
    """The original 80-step bisection, kept as the oracle for the fast version."""
    lo, hi = -20.0, 10.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if np.mean(1.0 / (1.0 + np.exp(-(mid + logits_wo_alpha)))) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


@pytest.mark.parametrize("seed", range(6))
def test_fast_calibrate_alpha_matches_reference_bisection(seed):
    rng = np.random.default_rng(seed)
    logits = rng.normal(rng.uniform(-5.0, 0.0), rng.uniform(0.2, 2.0), (5000, 3))
    target = float(rng.uniform(0.005, 0.3))
    fast = envm._calibrate_alpha(logits, target)
    ref = _calibrate_alpha_reference(logits, target)
    assert abs(fast - ref) < 1e-9, (fast, ref)
