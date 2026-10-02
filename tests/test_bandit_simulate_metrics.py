"""Simulator, metrics and cross-episode aggregation (``bandit.simulate`` /
``bandit.metrics`` / ``bandit.aggregate``)."""

import json
import re

import jax
import numpy as np
import pytest

from bandit import aggregate, metrics, simulate
from bandit import environment as envm
from bandit.config import build_sim_config
from bandit.policies import make_policy

POLICIES = ["linear_ts", "beta_bernoulli_ts", "uniform", "oracle"]
T, E = 6000, 3


@pytest.fixture(scope="module")
def seg_result():
    cfg = build_sim_config("segment_winners", horizon=T, episodes=E, seed=0)
    return simulate.run_experiment(cfg, POLICIES, log_propensity=False)


@pytest.fixture(scope="module")
def seg_rows(seg_result):
    return metrics.experiment_rows(seg_result)


def _regret(out):
    return np.cumsum(out["mean_opt"] - out["mean_chosen"], axis=-1)


def test_output_shapes(seg_result):
    out = seg_result.results["linear_ts"]
    for key in simulate.OUTPUT_KEYS:
        assert out[key].shape[:2] == (E, T), key
    assert out["p_all"].shape == (E, T, 4)
    assert np.all(np.isnan(out["propensity"]))  # log_propensity=False
    np.testing.assert_allclose(seg_result.results["uniform"]["propensity"], 0.25)


def test_linear_ts_logs_mc_propensities():
    cfg = build_sim_config("clear_winner", horizon=400, episodes=1, batch_size=50)
    out = simulate.run_experiment(cfg, ["linear_ts"]).results["linear_ts"]
    prop = out["propensity"]
    assert np.all((prop >= cfg.policy.min_propensity - 1e-6) & (prop <= 1))
    assert prop[0, :50].mean() == pytest.approx(1 / 3, abs=0.1)  # prior: ~uniform


def test_regret_nonnegative_and_nondecreasing(seg_result):
    for name, out in seg_result.results.items():
        reg = _regret(out)
        assert np.all(reg >= -1e-6), name
        assert np.all(np.diff(reg, axis=-1) >= -1e-6), name


def test_oracle_regret_zero(seg_result):
    out = seg_result.results["oracle"]
    assert np.allclose(_regret(out), 0.0)
    assert np.all(out["arm"] == out["opt_arm"])


def test_uniform_regret_roughly_linear(seg_result):
    reg = _regret(seg_result.results["uniform"]).mean(0)
    ratio = reg[-1] / reg[T // 2 - 1]
    assert 1.8 < ratio < 2.2


def test_linear_ts_beats_uniform_and_noncontextual_ts(seg_result):
    final = {p: _regret(o)[:, -1].mean() for p, o in seg_result.results.items()}
    assert final["linear_ts"] < final["uniform"]
    assert final["linear_ts"] < final["beta_bernoulli_ts"]


def test_common_random_numbers_across_policies(seg_result):
    a, b = seg_result.results["linear_ts"], seg_result.results["uniform"]
    np.testing.assert_array_equal(a["segment"], b["segment"])
    np.testing.assert_array_equal(a["p_all"], b["p_all"])
    same = a["arm"] == b["arm"]
    assert same.any()
    np.testing.assert_array_equal(a["clicked"][same], b["clicked"][same])
    np.testing.assert_array_equal(a["reward_opt"], b["reward_opt"])


def test_episode_reproducible_and_episodes_differ():
    cfg = build_sim_config("clear_winner", horizon=500, episodes=2, batch_size=50)
    env = envm.build_true_model(cfg, jax.random.key(0))
    pol = make_policy("ucb1", lints_params=cfg.policy)
    k = simulate.episode_keys(0, "clear_winner", 2)
    one = simulate.run_episode(pol, env, k[0], batch_size=50)
    again = simulate.run_episode(pol, env, k[0], batch_size=50)
    other = simulate.run_episode(pol, env, k[1], batch_size=50)
    for key in simulate.OUTPUT_KEYS:
        np.testing.assert_array_equal(one[key], again[key])
    assert not np.array_equal(one["segment"], other["segment"])


def test_horizon_not_multiple_of_batch():
    cfg = build_sim_config("clear_winner", horizon=530, episodes=1, batch_size=100)
    res = simulate.run_experiment(cfg, ["uniform"])
    assert res.results["uniform"]["arm"].shape == (1, 530)


def test_arm_schedule_expiry_and_injection():
    cfg = build_sim_config(
        "clear_winner", num_arms=4, horizon=2000, episodes=2, batch_size=100
    )
    schedule = [(0, [1, 2, 3]), (1000, [0, 1, 2])]
    res = simulate.run_experiment(cfg, ["ucb1", "oracle"], arm_schedule=schedule)
    for out in res.results.values():
        arms = out["arm"]
        assert not np.any(arms[:, :1000] == 0)
        assert not np.any(arms[:, 1000:] == 3)
        assert np.all(out["opt_arm"][:, :1000] != 0)
    best = int(np.argmax(res.results["oracle"]["p_all"][0].mean(0)))
    assert best == 0  # the injected arm is the best one
    assert np.mean(res.results["ucb1"]["arm"][:, 1500:] == 0) > 0.4
    with pytest.raises(ValueError):
        simulate.schedule_masks([(5, [0])], 2, 3, 10)


def test_log_checkpoints():
    cps = metrics.log_checkpoints(20000)
    assert 45 <= len(cps) <= 55
    assert cps[0] == 1 and cps[-1] == 20000
    assert cps == sorted(set(cps))
    assert metrics.log_checkpoints(10) == list(range(1, 11))
    lin = metrics.linear_checkpoints(1000, 200)
    assert len(lin) == 200 and lin[0] == 5 and lin[-1] == 1000
    assert metrics.make_checkpoints(1000, 200, "linear") == lin
    with pytest.raises(ValueError):
        metrics.make_checkpoints(1000, 50, "cubic")


def test_steps_to_converge_synthetic():
    rewards = np.r_[np.zeros(100), np.ones(200)]
    assert metrics.steps_to_converge(rewards, 1.0, window=10) == 110
    assert metrics.steps_to_converge(np.zeros(50), 1.0, window=10) is None
    assert metrics.steps_to_converge(np.ones(5), 1.0, window=10) is None
    # per-round optimum works too
    assert metrics.steps_to_converge(np.full(40, 0.5), np.full(40, 0.5), 10) == 10


def test_episode_row_matches_contract_payloads(seg_rows):
    row = next(r for r in seg_rows if r["policy"] == "linear_ts")
    cps = row["curve"]["checkpoints"]
    assert set(row["curve"]) == {
        "checkpoints",
        "cum_avg_reward",
        "cum_regret",
        "pct_optimal",
    }
    assert all(len(row["curve"][k]) == len(cps) for k in row["curve"])
    assert cps[-1] == T
    assert set(row["arm_share"]) == {f"synthetic-{c}" for c in "abcd"}
    shares = np.array(list(row["arm_share"].values()))
    np.testing.assert_allclose(shares.sum(0), 1.0, atol=1e-4)
    seg = row["per_segment"]["mobile_scrollers"]
    assert set(seg) == {"optimal_arm", "pct_optimal", "avg_reward", "rounds"}
    assert sum(s["rounds"] for s in row["per_segment"].values()) == T
    stats = row["arm_stats"]["synthetic-a"]
    assert set(stats) == {"impressions", "clicks", "estimated_ctr", "true_ctr"}
    assert sum(s["impressions"] for s in row["arm_stats"].values()) == T
    for key in (
        "total_reward",
        "total_clicks",
        "cumulative_regret",
        "pct_optimal",
        "steps_to_converge",
        "horizon",
        "episode",
    ):
        assert key in row
    assert row["cumulative_regret"] == pytest.approx(
        row["curve"]["cum_regret"][-1], rel=1e-4
    )
    assert sum(row["regret_by_arm"].values()) == pytest.approx(
        row["cumulative_regret"], rel=1e-3
    )
    json.dumps(row)


def test_segment_oracle_arms_differ_in_rows(seg_rows):
    row = next(r for r in seg_rows if r["policy"] == "oracle")
    opts = {s["optimal_arm"] for s in row["per_segment"].values()}
    assert len(opts) == 4
    assert all(s["pct_optimal"] == 1.0 for s in row["per_segment"].values())


def test_aggregate_matches_section5_structure(seg_rows):
    agg = aggregate.aggregate_episode_metrics(seg_rows, experiment_id="exp-x")
    assert set(agg) == {
        "experiment_id",
        "episodes",
        "horizon",
        "checkpoints",
        "policies",
        "curves",
        "totals",
        "arm_share",
        "per_segment",
        "arms",
    }
    assert agg["experiment_id"] == "exp-x"
    assert agg["episodes"] == E and agg["horizon"] == T
    assert agg["policies"][0] == "linear_ts" and agg["policies"][-1] == "oracle"
    m = len(agg["checkpoints"])
    for p in POLICIES:
        assert set(agg["curves"][p]) == {"cum_avg_reward", "cum_regret", "pct_optimal"}
        for band in agg["curves"][p].values():
            assert set(band) == {"mean", "lo", "hi"}
            assert all(len(v) == m for v in band.values())
            assert all(
                lo <= mu + 1e-9 <= hi + 2e-9
                for lo, mu, hi in zip(band["lo"], band["mean"], band["hi"], strict=True)
            )
        assert set(agg["totals"][p]) == {"mean", "std"}
    assert all(len(v) == m for v in agg["arm_share"].values())
    seg = agg["per_segment"]["trend_followers"]
    assert set(seg) == {"optimal_arm", "policies"}
    assert set(seg["policies"]["uniform"]) == {"pct_optimal", "avg_reward"}
    assert [a["creative_id"] for a in agg["arms"]] == [f"synthetic-{c}" for c in "abcd"]
    assert set(agg["arms"][0]) == {
        "creative_id",
        "impressions",
        "estimated_ctr",
        "true_ctr",
    }
    # BigQuery rows carry JSON strings: same result
    as_bq = [
        {
            k: json.dumps(v)
            if k in ("curve", "arm_share", "per_segment", "arm_stats")
            else v
            for k, v in r.items()
        }
        for r in seg_rows
    ]
    assert aggregate.aggregate_episode_metrics(as_bq, experiment_id="exp-x") == agg


def test_aggregate_ci_math_and_empty():
    mean, lo, hi = aggregate.mean_ci([1.0, 2.0, 3.0])
    assert mean == 2.0
    assert hi - mean == pytest.approx(4.303 * 1.0 / np.sqrt(3), rel=1e-3)
    assert aggregate.mean_ci([5.0]) == (5.0, 5.0, 5.0)
    empty = aggregate.aggregate_episode_metrics([], experiment_id="e")
    assert (
        empty["episodes"] == 0 and empty["horizon"] is None and empty["policies"] == []
    )


def test_aggregate_is_jax_free():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(aggregate.__file__).read_text())
    imported = {
        (n.module or "").split(".")[0]
        if isinstance(n, ast.ImportFrom)
        else a.name.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    assert not imported & {"jax", "numpy", "bandit"}


def test_bandit_not_imported_by_runserver_or_agents():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    pkgs = [
        "runserver",
        "creative_agent",
        "trend_scout",
        "interactive_creative",
        "creative_eval",
        "agent_common",
        "deployment",
    ]
    offenders = [
        str(p)
        for pkg in pkgs
        for p in (root / pkg).rglob("*.py")
        if re.search(r"^\s*(from|import)\s+bandit\b", p.read_text(), re.MULTILINE)
    ]
    assert offenders == []
