"""Scripted behaviour shifts (contracts §10): config parsing/validation, the
shifted ground truth, shift metrics and the CLI.

Config tests are pure Python; the rest import JAX (dev group).
"""

import dataclasses
import json
import math

import jax
import jax.numpy as jnp
import numpy as np
import pytest

from bandit import cli, metrics, simulate
from bandit import environment as envm
from bandit.config import (
    MAX_SHIFTS,
    SHIFT_BOUNDS,
    SHIFT_KINDS,
    SHIFT_MIN_WINDOW,
    ShiftSpec,
    build_sim_config,
    default_arms,
    default_shift_discount,
    discount_for_memory,
    load_scenario,
    shifts_from_dict,
    shifts_to_dict,
    validate_shifts,
)

SEG_ARMS = default_arms(4)  # synthetic-a..d
SEG_SC = load_scenario("segment_winners")

SHIFT_DOCS = [
    {
        "kind": "promote",
        "at_frac": 0.4,
        "segment": "mobile_scrollers",
        "creative_id": "synthetic-c",
        "lift_pp": 0.015,
    },
    {
        "kind": "demote",
        "at_frac": 0.5,
        "segment": None,
        "creative_id": "leader",
        "drop_pp": 0.015,
    },
    {"kind": "mix", "at_frac": 0.3, "segment_mix": [0.6, 0.2, 0.1, 0.1]},
    {
        "kind": "shock",
        "at_frac": 0.6,
        "until_frac": 0.7,
        "segment": None,
        "creative_id": "synthetic-a",
        "ctr_multiplier": 0.6,
    },
]


# ------------------------------------------------------------------- config


def test_shift_constants():
    assert SHIFT_KINDS == ("promote", "demote", "mix", "shock")
    assert MAX_SHIFTS == 4 and SHIFT_MIN_WINDOW == 0.02
    assert SHIFT_BOUNDS == {
        "at_frac": (0.05, 0.95),
        "until_frac": (0.07, 1.0),
        "lift_pp": (0.005, 0.03),
        "drop_pp": (0.005, 0.03),
        "segment_mix": (0.05, 1.0),
        "ctr_multiplier": (0.3, 2.0),
    }


def test_shifts_round_trip_and_validate():
    shifts = shifts_from_dict(SHIFT_DOCS)
    assert all(isinstance(s, ShiftSpec) for s in shifts)
    assert shifts[2].segment_mix == (0.6, 0.2, 0.1, 0.1)
    assert shifts_from_dict(shifts_to_dict(shifts)) == shifts
    assert validate_shifts(shifts, SEG_SC, SEG_ARMS, "demo") == shifts
    ids = [a.creative_id for a in SEG_ARMS]  # plain creative ids work too
    assert validate_shifts(shifts, SEG_SC, ids, "demo") == shifts
    json.dumps(shifts_to_dict(shifts))  # JSON-ready
    assert shifts_to_dict(shifts)[1]["segment"] is None  # "everyone" is explicit
    assert shifts_from_dict(None) == () and shifts_from_dict([]) == ()


def test_shift_spec_is_frozen():
    s = shifts_from_dict(SHIFT_DOCS[:1])[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.at_frac = 0.5  # type: ignore[misc]


@pytest.mark.parametrize(
    ("doc", "field"),
    [
        ({"kind": "teleport", "at_frac": 0.5}, "kind"),
        ({**SHIFT_DOCS[0], "at_frac": True}, "at_frac"),
        ({**SHIFT_DOCS[0], "at_frac": "0.5"}, "at_frac"),
        ({**SHIFT_DOCS[0], "at_frac": 0.01}, "at_frac"),
        ({**SHIFT_DOCS[0], "at_frac": 0.96}, "at_frac"),
        ({**SHIFT_DOCS[0], "at_frac": math.nan}, "at_frac"),
        ({**SHIFT_DOCS[0], "lift_pp": None}, "lift_pp"),
        ({**SHIFT_DOCS[0], "lift_pp": 0.5}, "lift_pp"),  # above every ctr mode
        ({**SHIFT_DOCS[0], "drop_pp": 0.01}, "drop_pp"),  # another kind's field
        ({**SHIFT_DOCS[0], "color": "red"}, "color"),  # unknown key
        ({**SHIFT_DOCS[0], "creative_id": 3}, "creative_id"),
        ({**SHIFT_DOCS[0], "creative_id": "leader"}, "creative_id"),  # demote only
        ({**SHIFT_DOCS[0], "segment": 2}, "segment"),
        ({**SHIFT_DOCS[2], "segment": "mobile_scrollers"}, "segment"),
        ({**SHIFT_DOCS[2], "segment_mix": "0.5,0.5"}, "segment_mix"),
        ({**SHIFT_DOCS[2], "segment_mix": [0.5, 0.01]}, "segment_mix"),
        ({**SHIFT_DOCS[3], "until_frac": None}, "until_frac"),
        ({**SHIFT_DOCS[3], "until_frac": 0.61}, "until_frac"),  # window < 2%
        ({**SHIFT_DOCS[3], "until_frac": 0.5}, "until_frac"),  # before at_frac
        ({**SHIFT_DOCS[3], "until_frac": 1.2}, "until_frac"),
        ({**SHIFT_DOCS[0], "until_frac": 0.8}, "until_frac"),  # shock only
        ({**SHIFT_DOCS[3], "ctr_multiplier": 2.5}, "ctr_multiplier"),
        ({**SHIFT_DOCS[3], "ctr_multiplier": False}, "ctr_multiplier"),
    ],
)
def test_shifts_from_dict_rejects(doc, field):
    with pytest.raises(ValueError, match=rf"shifts\[0\]\.{field}"):
        shifts_from_dict([doc])


def test_shifts_from_dict_rejects_shape():
    with pytest.raises(ValueError, match="shifts must be a list"):
        shifts_from_dict({"kind": "mix"})
    with pytest.raises(ValueError, match=r"shifts\[0\] must be an object"):
        shifts_from_dict(["promote"])
    with pytest.raises(ValueError, match="at most 4"):
        shifts_from_dict([SHIFT_DOCS[2]] * 5)


def test_shock_window_boundary_is_inclusive():
    ok = {**SHIFT_DOCS[3], "at_frac": 0.6, "until_frac": 0.62}
    assert shifts_from_dict([ok])[0].until_frac == 0.62


@pytest.mark.parametrize(
    ("doc", "field"),
    [
        ({**SHIFT_DOCS[0], "segment": "night_owls"}, "segment"),
        ({**SHIFT_DOCS[0], "creative_id": "synthetic-z"}, "creative_id"),
        ({**SHIFT_DOCS[1], "creative_id": "nope"}, "creative_id"),
        ({**SHIFT_DOCS[3], "creative_id": "nope"}, "creative_id"),
        ({**SHIFT_DOCS[2], "segment_mix": [0.5, 0.5, 0.5]}, "segment_mix"),
        ({**SHIFT_DOCS[0], "lift_pp": 0.004}, "lift_pp"),
        ({**SHIFT_DOCS[1], "drop_pp": 0.004}, "drop_pp"),
    ],
)
def test_validate_shifts_rejects(doc, field):
    shifts = shifts_from_dict([doc])
    with pytest.raises(ValueError, match=rf"shifts\[0\]\.{field}"):
        validate_shifts(shifts, SEG_SC, SEG_ARMS, "demo")


def test_validate_shifts_magnitude_bounds_scale_with_ctr_mode():
    # realistic CTRs are 0.2x demo for segment_winners: bounds [0.001, 0.006]
    scale = SEG_SC.ctr_scale("realistic")
    assert scale == pytest.approx(0.2)
    lo, hi = SHIFT_BOUNDS["lift_pp"]
    ok = shifts_from_dict([{**SHIFT_DOCS[0], "lift_pp": lo * scale}])
    validate_shifts(ok, SEG_SC, SEG_ARMS, "realistic")
    ok = shifts_from_dict([{**SHIFT_DOCS[1], "drop_pp": hi * scale}])
    validate_shifts(ok, SEG_SC, SEG_ARMS, "realistic")
    demo_value = shifts_from_dict([SHIFT_DOCS[0]])  # 0.015 > 0.006
    with pytest.raises(ValueError, match=r"shifts\[0\]\.lift_pp"):
        validate_shifts(demo_value, SEG_SC, SEG_ARMS, "realistic")
    with pytest.raises(ValueError, match="ctr_mode"):
        validate_shifts(demo_value, SEG_SC, SEG_ARMS, "huge")


def test_validate_shifts_names_the_index_and_caps_count():
    docs = [SHIFT_DOCS[2], {**SHIFT_DOCS[0], "segment": "night_owls"}]
    with pytest.raises(ValueError, match=r"shifts\[1\]\.segment"):
        validate_shifts(shifts_from_dict(docs), SEG_SC, SEG_ARMS, "demo")
    five = shifts_from_dict([SHIFT_DOCS[2]]) * 5
    with pytest.raises(ValueError, match="at most 4"):
        validate_shifts(five, SEG_SC, SEG_ARMS, "demo")


def test_default_shift_discount_is_the_drift_memory_rule():
    # 1/8 of the horizon, like DISCOUNT_MEMORY_ROUNDS (5k of 40k demo rounds)
    assert default_shift_discount("demo", 100, 40_000) == 0.98
    assert default_shift_discount("realistic", 100, 400_000) == 0.998
    assert default_shift_discount("demo", 100, 20_000) == discount_for_memory(2500)
    with pytest.raises(ValueError, match="ctr_mode"):
        default_shift_discount("huge", 100, 40_000)


# -------------------------------------------------------------- environment

H = 40_000  # segment_winners / drift demo horizon


def _cfg(scenario="segment_winners", **kw):
    return build_sim_config(scenario, horizon=H, **kw)


def _env(docs=(), scenario="segment_winners", **kw):
    cfg = _cfg(scenario, **kw)
    return simulate.build_environment(cfg, shifts=shifts_from_dict(list(docs)))


def _by_seg(env, t, n=60_000):
    return envm.marginal_ctrs(env, jax.random.key(5), n=n, t=t)["by_segment"]


def test_no_shifts_matches_the_unshifted_model_exactly():
    base, empty = _env(), _env([])
    assert float(base.model.alpha) == float(empty.model.alpha)
    for t in (0, H - 1):
        np.testing.assert_array_equal(_by_seg(base, t), _by_seg(empty, t))
    assert envm.resolved_shifts(base) == []


def test_alpha_is_not_recalibrated():
    base, shifted = _env(), _env(SHIFT_DOCS)
    assert float(shifted.model.alpha) == float(base.model.alpha)
    np.testing.assert_array_equal(shifted.model.seg_aff, base.model.seg_aff)
    np.testing.assert_array_equal(shifted.model.seg_logits, base.model.seg_logits)


def test_crn_alignment_shifted_vs_unshifted():
    base, shifted = _env(), _env(SHIFT_DOCS)
    k_ctx, k_rew, _ = simulate.episode_streams(jax.random.key(42))
    elig = jnp.ones(4, bool)
    flipped_any = False
    for b in (0, 100, 119, 120, 200, 260, 399):  # mix starts at round 12000
        d0 = simulate.batch_draws(base.model, k_ctx, k_rew, b, 100, "click", elig)
        d1 = simulate.batch_draws(shifted.model, k_ctx, k_rew, b, 100, "click", elig)
        np.testing.assert_array_equal(d0["t"], d1["t"])
        s0, s1 = np.asarray(d0["segment"]), np.asarray(d1["segment"])
        same = s0 == s1
        if b * 100 < 0.3 * H:
            assert same.all()
        else:
            flipped_any |= not same.all()
        # rows with the same segment get identical levels / contexts
        np.testing.assert_array_equal(
            np.asarray(d0["levels"])[same], np.asarray(d1["levels"])[same]
        )
        # clicks are U < p with one shared U per (row, arm)
        p0, p1 = np.asarray(d0["p"]), np.asarray(d1["p"])
        c0, c1 = np.asarray(d0["clicked_all"]), np.asarray(d1["clicked_all"])
        eq = (p0 == p1) & same[:, None]
        np.testing.assert_array_equal(c0[eq], c1[eq])
        up = (p1 >= p0) & same[:, None]
        assert (c1[up] >= c0[up]).all()
        down = (p1 <= p0) & same[:, None]
        assert (c1[down] <= c0[down]).all()
        if b * 100 < 0.3 * H:
            np.testing.assert_array_equal(p0, p1)
    assert flipped_any  # the mix shift moved some readers


def test_promote_changes_the_segment_oracle_at_the_round():
    base = _env()
    s = base.segment_names.index("mobile_scrollers")
    before = _by_seg(base, 0)
    loser = int(np.argmin(before[s]))
    doc = {**SHIFT_DOCS[0], "creative_id": base.arm_ids[loser]}
    env = _env([doc])
    r = round(0.4 * H)
    pre, post = _by_seg(env, r - 1), _by_seg(env, r)
    np.testing.assert_array_equal(pre, _by_seg(base, r - 1))
    assert int(np.argmax(pre[s])) != loser
    assert int(np.argmax(post[s])) == loser
    gap = post[s, loser] - np.max(np.delete(post[s], loser))
    assert gap == pytest.approx(0.015, abs=0.004)
    others = [i for i in range(4) if i != s]
    np.testing.assert_allclose(post[others], pre[others], rtol=1e-6)


def test_demote_leader_for_everyone():
    base = _env()
    pooled = envm.marginal_ctrs(base, jax.random.key(5), n=60_000, t=0)["overall"]
    leader = int(np.argmax(pooled))
    env = _env([SHIFT_DOCS[1]])
    (rec,) = envm.resolved_shifts(env)
    assert rec["creative_id"] == base.arm_ids[leader]
    assert rec["requested_creative_id"] == "leader"
    r = round(0.5 * H)
    assert rec["round"] == r
    pre, post = _by_seg(env, r - 1), _by_seg(env, r)
    for s in range(4):
        assert int(np.argmax(post[s])) != leader
        assert post[s, leader] <= pre[s, leader] + 1e-9  # never raised
        if int(np.argmax(pre[s])) == leader:
            gap = np.max(np.delete(post[s], leader)) - post[s, leader]
            assert gap == pytest.approx(0.015, abs=0.004)


def test_leader_resolves_in_time_order():
    docs = [
        {**SHIFT_DOCS[1], "at_frac": 0.6},  # listed first, resolved second
        {**SHIFT_DOCS[1], "at_frac": 0.3},
    ]
    env = _env(docs)
    first, second = envm.resolved_shifts(env)
    assert (first["index"], second["index"]) == (1, 0)
    assert first["round"] < second["round"]
    assert first["creative_id"] != second["creative_id"]
    pooled = envm.marginal_ctrs(env, jax.random.key(5), n=60_000, t=first["round"])
    assert env.arm_ids[int(np.argmax(pooled["overall"]))] == second["creative_id"]
    # a promoted creative is that segment's leader afterwards
    seg = "trend_followers"
    promote = {**SHIFT_DOCS[0], "segment": seg, "creative_id": "synthetic-d"}
    demote = {**SHIFT_DOCS[1], "segment": seg, "at_frac": 0.7}
    _, dem = envm.resolved_shifts(_env([promote, demote]))
    assert dem["creative_id"] == "synthetic-d" and dem["segment"] == seg


def test_shock_multiplies_only_inside_its_window():
    env, base = _env([SHIFT_DOCS[3]]), _env()
    seg, _, X = envm.sample_contexts(jax.random.key(3), base.model, 500)
    a = env.arm_ids.index("synthetic-a")
    start, end = round(0.6 * H), round(0.7 * H)
    for t, inside in ((start - 1, False), (start, True), (end - 1, True), (end, False)):
        tt = jnp.full(seg.shape, t)
        p1 = np.asarray(envm.click_probs(env.model, X, seg, tt))
        p0 = np.asarray(envm.click_probs(base.model, X, seg, tt))
        others = [k for k in range(4) if k != a]
        np.testing.assert_array_equal(p1[:, others], p0[:, others])
        expect = p0[:, a] * (0.6 if inside else 1.0)
        np.testing.assert_allclose(p1[:, a], expect, rtol=1e-6)
    (rec,) = envm.resolved_shifts(env)
    assert (rec["round"], rec["end_round"]) == (start, end)


def test_shock_clips_probabilities_below_one():
    doc = {**SHIFT_DOCS[3], "ctr_multiplier": 2.0}
    env = _env([doc])
    seg = jnp.zeros(4, jnp.int32)
    X = jnp.zeros((4, env.model.theta.shape[1]))
    big = env.model._replace(alpha=jnp.asarray(10.0))  # p ~ 1 before the shock
    p = np.asarray(envm.click_probs(big, X, seg, jnp.full(4, round(0.65 * H))))
    assert (p < 1.0).all() and (p > 0.99).all()


def test_mix_changes_segment_frequencies_from_the_round():
    env = _env([SHIFT_DOCS[2]])
    r, n = round(0.3 * H), 100_000
    key = jax.random.key(8)
    freq = []
    for t in (r - 1, r):
        seg, _, _ = envm.sample_contexts(key, env.model, n, jnp.full(n, t))
        freq.append(np.bincount(np.asarray(seg), minlength=4) / n)
    base_w = [s.weight for s in SEG_SC.segments]
    np.testing.assert_allclose(freq[0], base_w, atol=0.01)
    np.testing.assert_allclose(freq[1], [0.6, 0.2, 0.1, 0.1], atol=0.01)
    # without t (or before every mix) the preset mix applies
    seg, _, _ = envm.sample_contexts(key, env.model, n)
    np.testing.assert_allclose(np.bincount(np.asarray(seg)) / n, base_w, atol=0.01)


def test_mix_changes_the_pooled_leader_for_a_later_demote():
    env = _env([SHIFT_DOCS[2], {**SHIFT_DOCS[1], "at_frac": 0.5}])
    mix, dem = envm.resolved_shifts(env)
    assert mix["kind"] == "mix" and mix["segment_weights"] == pytest.approx(
        [0.6, 0.2, 0.1, 0.1]
    )
    pooled = envm.marginal_ctrs(env, jax.random.key(5), n=80_000, t=dem["round"] - 1)
    assert env.arm_ids[int(np.argmax(pooled["overall"]))] == dem["creative_id"]


def test_drift_and_shifts_compose():
    doc = {**SHIFT_DOCS[1], "at_frac": 0.7}  # demote the post-drift leader
    base, env = _env(scenario="drift"), _env([doc], scenario="drift")
    k = env.num_arms
    pre_drift = _by_seg(base, 0)
    post_drift = _by_seg(base, round(0.6 * H))
    assert not np.allclose(pre_drift, post_drift)  # drift happened at 0.5
    np.testing.assert_array_equal(_by_seg(env, round(0.6 * H)), post_drift)
    (rec,) = envm.resolved_shifts(env)
    weights = np.array([s.weight for s in env.scenario.segments])
    post_leader = int(np.argmax(weights @ post_drift))
    assert rec["creative_id"] == env.arm_ids[post_leader]
    after = _by_seg(env, round(0.7 * H))
    for s in range(len(env.segment_names)):
        assert int(np.argmax(after[s])) != post_leader
        others = [i for i in range(k) if i != post_leader]
        np.testing.assert_allclose(after[s, others], post_drift[s, others], rtol=1e-6)


def test_resolved_shift_records():
    env = _env(SHIFT_DOCS)
    recs = envm.resolved_shifts(env)
    assert [r["kind"] for r in recs] == ["mix", "promote", "demote", "shock"]
    assert [r["index"] for r in recs] == [2, 0, 1, 3]
    assert [r["round"] for r in recs] == [12_000, 16_000, 20_000, 24_000]
    promote = recs[1]
    assert promote["creative_id"] == "synthetic-c"
    assert promote["lift_pp"] == 0.015
    (tgt,) = promote["targets"]
    assert tgt["segment"] == "mobile_scrollers"
    assert tgt["ctr_after"] - tgt["best_other_ctr"] == pytest.approx(0.015, abs=1e-6)
    demote = recs[2]
    assert demote["segment"] is None and len(demote["targets"]) == 4
    assert recs[3]["end_round"] == 28_000 and recs[3]["ctr_multiplier"] == 0.6
    assert all(r.get("end_round") is None for r in recs[:3])
    json.dumps(recs)  # the per-run record is JSON-ready
    recs[0]["kind"] = "changed"  # a copy: the environment is unaffected
    assert envm.resolved_shifts(env)[0]["kind"] == "mix"


def test_build_environment_validates_shifts():
    with pytest.raises(ValueError, match=r"shifts\[0\]\.creative_id"):
        _env([{**SHIFT_DOCS[0], "creative_id": "synthetic-z"}])


# ------------------------------------------------------------------ metrics


def _synthetic_episode(T=10_000, r=5_000, dip=1_500):
    is_opt = np.ones(T, bool)
    is_opt[r : r + dip] = False
    opt_arm = np.zeros(T, int)
    arm = np.where(is_opt, 0, 1)
    mean_opt = np.full(T, 0.05)
    mean_chosen = np.where(is_opt, 0.05, 0.03)
    return {
        "arm": arm,
        "opt_arm": opt_arm,
        "mean_opt": mean_opt,
        "mean_chosen": mean_chosen,
    }


def test_shift_response_on_a_synthetic_series():
    (resp,) = metrics.shift_response(
        _synthetic_episode(), [5_000], window=2_000, recovery_window=1_000
    )
    assert resp["round"] == 5_000
    assert resp["pct_optimal_before"] == 1.0
    assert resp["pct_optimal_after"] == pytest.approx(0.25)
    assert resp["regret_rate_before"] == 0.0
    assert resp["regret_rate_after"] == pytest.approx(1_500 * 0.02 / 2_000)
    # trailing-1k rate reaches 80% of 1.0 once 800 of the last 1000 are optimal:
    # round 6500 + 800 = 7300, i.e. 2300 rounds after the shift
    assert resp["recovery_rounds"] == 2_300


def test_shift_response_never_recovers_and_window_clipping():
    ep = _synthetic_episode(dip=5_000)
    (resp,) = metrics.shift_response(ep, [5_000], window=2_000, recovery_window=1_000)
    assert resp["recovery_rounds"] is None and resp["pct_optimal_after"] == 0.0
    # windows clip to the episode; defaults derive from the horizon
    resp = metrics.shift_response(_synthetic_episode(), [9_500, 300])
    assert [x["round"] for x in resp] == [9_500, 300]
    assert resp[0]["pct_optimal_after"] == 1.0  # 500 rounds left, all optimal
    assert resp[0]["recovery_rounds"] == 500  # default trailing window: 1000 // 2
    assert resp[1]["pct_optimal_before"] == 1.0  # clipped to the first 300 rounds
    no_room = metrics.shift_response(_synthetic_episode(), [9_800])[0]
    assert no_room["recovery_rounds"] is None  # fewer rounds left than the window


def test_merge_checkpoints():
    cps = metrics.make_checkpoints(40_000, 50, "linear")
    merged = metrics.merge_checkpoints(cps, [20_000, 39_900], 40_000)
    assert {19_999, 20_000, 20_200, 20_800, 22_000} <= set(merged)
    assert 39_899 in merged and 39_900 in merged
    assert merged == sorted(set(merged)) and merged[-1] == 40_000
    assert all(1 <= c <= 40_000 for c in merged)
    assert set(cps) <= set(merged)
    assert metrics.merge_checkpoints(cps, [], 40_000) == cps


def test_regime_stats_splits_per_segment_and_true_ctr():
    T = 1_000
    seg = np.tile([0, 1], T // 2)
    opt_arm = np.where(np.arange(T) < 400, 0, 1)
    p_all = np.where(
        (np.arange(T) < 400)[:, None], [[0.06, 0.04]], [[0.03, 0.05]]
    ).astype(float)
    arrays = {
        "segment": seg,
        "arm": np.zeros(T, int),
        "opt_arm": opt_arm,
        "reward": np.ones(T),
        "p_all": p_all,
    }
    regimes = metrics.regime_stats(
        arrays, [400], arm_ids=("a", "b"), segment_names=("s0", "s1")
    )
    assert [(g["start"], g["end"]) for g in regimes] == [(0, 400), (400, 1_000)]
    first, second = regimes
    assert first["per_segment"]["s0"]["optimal_arm"] == "a"
    assert first["per_segment"]["s0"]["pct_optimal"] == 1.0
    assert second["per_segment"]["s1"]["optimal_arm"] == "b"
    assert second["per_segment"]["s1"]["pct_optimal"] == 0.0
    assert second["per_segment"]["s1"]["rounds"] == 300
    assert first["true_ctr"] == {"a": 0.06, "b": 0.04}
    assert second["true_ctr"] == {"a": 0.03, "b": 0.05}
    assert len(metrics.regime_stats(arrays, [], ("a", "b"), ("s0", "s1"))) == 1


def test_simulated_demote_shows_in_shift_response():
    cfg = _cfg(episodes=2)
    shifts = shifts_from_dict([SHIFT_DOCS[1]])
    res = simulate.run_experiment(cfg, ["oracle", "uniform"], shifts=shifts)
    rows = metrics.experiment_rows(res, spacing="linear", shift_rounds=[20_000])
    assert all(len(r["shift_response"]) == 1 for r in rows)
    oracle = next(r for r in rows if r["policy"] == "oracle")
    assert oracle["shift_response"][0]["pct_optimal_after"] == 1.0
    assert 20_000 in oracle["curve"]["checkpoints"]
    assert 19_999 in oracle["curve"]["checkpoints"]


# ---------------------------------------------------------------------- CLI


def test_cli_shifts_and_forget():
    doc = cli.simulate(
        scenario="segment_winners",
        policies=["linear_ts", "uniform"],
        episodes=1,
        horizon=4_000,
        log_propensity=False,
        shifts=[SHIFT_DOCS[1]],
        forget=True,
    )
    assert doc["config"]["policy"]["discount"] == default_shift_discount(
        "demo", 100, 4_000
    )
    block = doc["shifts"]
    assert block["requested"] == shifts_to_dict(shifts_from_dict([SHIFT_DOCS[1]]))
    (rec,) = block["resolved"]
    assert rec["creative_id"] in doc["env"]["arm_ids"] and rec["round"] == 2_000
    assert block["forget"] is True
    assert block["discount"] == doc["config"]["policy"]["discount"] == 0.819
    assert 2_000 in doc["checkpoints"] and 1_999 in doc["checkpoints"]
    assert all(len(r["shift_response"]) == 1 for r in doc["rows"])
    summary = doc["shift_response"]["linear_ts"][0]
    assert summary["round"] == 2_000 and summary["episodes"] == 1
    assert set(summary) >= {
        "pct_optimal_before",
        "pct_optimal_after",
        "regret_rate_before",
        "regret_rate_after",
        "recovery_rounds",
        "recovered_episodes",
    }


def test_cli_without_shifts_has_no_shift_block():
    doc = cli.simulate(
        scenario="clear_winner",
        policies=["uniform"],
        episodes=1,
        horizon=200,
        log_propensity=False,
    )
    assert doc["shifts"] is None and doc["shift_response"] is None
    assert "shift_response" not in doc["rows"][0]


def test_cli_parser_shift_flags(tmp_path):
    text = json.dumps([SHIFT_DOCS[2]])
    args = cli.build_parser().parse_args(
        ["simulate", "--scenario", "segment_winners", "--out", "x.json"]
        + ["--shifts", text, "--forget"]
    )
    assert args.shifts == [SHIFT_DOCS[2]] and args.forget is True
    path = tmp_path / "shifts.json"
    path.write_text(text)
    args = cli.build_parser().parse_args(
        ["simulate", "--scenario", "segment_winners", "--out", "x.json"]
        + ["--shifts", str(path)]
    )
    assert args.shifts == [SHIFT_DOCS[2]] and args.forget is False
    with pytest.raises(ValueError, match=r"shifts\[0\]\.creative_id"):
        cli.simulate(
            scenario="segment_winners",
            policies=["uniform"],
            episodes=1,
            horizon=200,
            shifts=[{**SHIFT_DOCS[0], "creative_id": "nope"}],
        )
