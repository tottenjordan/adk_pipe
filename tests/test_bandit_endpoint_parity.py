"""Endpoint LinTS == simulator LinTS, round for round (contracts §2 / §7 / §10).

The live ``linear_ts`` curves come from the traffic job driving the CPR
``BanditPredictor``; every simulator sweep (and the §10 ghost) runs
``bandit.simulate.run_episodes`` with the ``linear_ts`` policy. The traffic job
sends each episode's simulator policy stream in its ``reset`` (``policy_key`` +
``batch_size``) and each decision's ``batch``/``row``, so the predictor draws
batch ``b`` from ``fold_in(k_pol, b)`` at the simulator's batch shape and scales
rewards with the simulator's float32 op. The whole serving path (traffic loop ->
request JSON -> feature encoding -> ``select`` -> pending map -> reward scaling
-> batched ``update``) must then reproduce the simulator's arm choices exactly,
whatever the padding or request splitting, so any algorithmic gap (params,
reward scale, update timing, discount, dropped rewards) shows up here.
"""

import dataclasses
import json

import numpy as np
import pytest

from bandit import simulate
from bandit.config import (
    LinTSParams,
    build_sim_config,
    continuous_world_config,
    default_noise_var,
    experiment_config_to_dict,
    load_scenario,
    resolve_scenario,
    shifts_from_dict,
    validate_shifts,
)
from bandit.policies import make_policy
from bandit_serving.predictor import BanditPredictor
from bandit_traffic import traffic
from bandit_traffic.endpoint_client import InProcessClient
from bandit_traffic.fake_endpoint import FakeBanditEndpoint
from bandit_traffic.traffic import TrafficRunner, TrafficSettings
from tests._bandit_sizes import BATCH, HORIZON_M, HORIZON_S


class _Writer:
    def __init__(self):
        self.events: list[dict] = []
        self.metrics: list[dict] = []

    def write_events(self, rows):
        self.events.extend(rows)

    def write_metrics(self, rows):
        self.metrics.extend(rows)

    def update_progress(self, *args, **kwargs):
        pass


class _Client:
    """The CPR request path in-process: preprocess -> predict -> postprocess."""

    def __init__(self, predictor):
        self.p = predictor
        self.requests: list[list[dict]] = []

    def predict(self, instances, parameters=None):
        self.requests.append(list(instances))
        body = {"instances": list(instances)}
        if parameters:
            body["parameters"] = parameters
        return self.p.postprocess(self.p.predict(self.p.preprocess(body)))[
            "predictions"
        ]


# One episode of HORIZON_S rounds per case: parity is exact round for round, so
# it shows from the first diverging batch; more rounds or episodes add no power.
def _cfg(scenario, reward_mode="click", discount=1.0, horizon=HORIZON_S, episodes=1):
    sc = load_scenario(scenario)
    return build_sim_config(
        scenario,
        reward_mode=reward_mode,
        horizon=horizon,
        episodes=episodes,
        batch_size=BATCH,
        policy=LinTSParams(
            noise_var=default_noise_var(sc.target_ctr["demo"], reward_mode),
            discount=discount,
        ),
    )


def _predictor(tmp_path, monkeypatch, cfg):
    monkeypatch.setenv("BANDIT_WARMUP", "0")
    (tmp_path / "experiment.json").write_text(
        json.dumps(experiment_config_to_dict(cfg))
    )
    predictor = BanditPredictor(checkpoint_every=10**9, checkpoint_seconds=1e9)
    predictor.load(str(tmp_path))
    assert predictor.params == cfg.policy
    return predictor


def _local(cfg, params=None):
    env = simulate.build_environment(cfg, scenario=resolve_scenario(cfg))
    pol = make_policy(
        "linear_ts",
        lints_params=params or cfg.policy,
        reward_mode=cfg.reward_mode,
        log_propensity=False,
    )
    keys = simulate.episode_keys(cfg.seed, cfg.scenario, cfg.episodes)
    return env, simulate.run_episodes(pol, env, keys, cfg.horizon, cfg.batch_size)


def _served_arms(writer, env, episode):
    arm_index = {cid: i for i, cid in enumerate(env.arm_ids)}
    rows = sorted(
        (r for r in writer.events if r["episode"] == episode), key=lambda r: r["round"]
    )
    return np.array([arm_index[r["arm"]] for r in rows]), rows


@pytest.mark.parametrize(
    ("scenario", "reward_mode", "discount", "max_instances"),
    [
        ("segment_winners", "click", 1.0, None),
        ("clear_winner", "engaged", 1.0, None),
        ("drift", "click", 0.98, None),
        # 37-instance requests: every decision batch and every reward batch is
        # split over several requests (and padded to different buckets)
        ("drift", "engaged", 0.98, 37),
    ],
)
def test_endpoint_lints_matches_simulator_round_for_round(
    tmp_path, monkeypatch, scenario, reward_mode, discount, max_instances
):
    cfg = _cfg(scenario, reward_mode, discount)
    predictor = _predictor(tmp_path, monkeypatch, cfg)
    settings = TrafficSettings(baselines=())
    if max_instances:
        settings = dataclasses.replace(settings, max_request_instances=max_instances)
    writer, client = _Writer(), _Client(predictor)
    summary = TrafficRunner(cfg, client, writer, settings).run()
    assert summary.decision_errors == summary.rewards_rejected == 0
    if max_instances:
        sizes = {len(r) for r in client.requests if r[0]["type"] == "decision"}
        assert max(sizes) == max_instances and len(sizes) > 1

    env, local = _local(cfg)
    for e in range(cfg.episodes):
        served, rows = _served_arms(writer, env, e)
        np.testing.assert_array_equal(served, local["arm"][e])
        regret = sum(r["regret"] for r in rows)
        local_regret = float(np.sum(local["mean_opt"][e] - local["mean_chosen"][e]))
        assert regret == pytest.approx(local_regret, rel=1e-4)
    # the posterior the endpoint ends on saw every round's reward
    assert int(predictor._state.step) == cfg.horizon


def test_fake_endpoint_matches_simulator_round_for_round():
    """``--in-process`` runs (the fake endpoint) follow the simulator too."""
    cfg = _cfg("segment_winners", "engaged")
    writer = _Writer()
    client = InProcessClient(FakeBanditEndpoint(cfg))
    TrafficRunner(cfg, client, writer, TrafficSettings(baselines=())).run()
    env, local = _local(cfg)
    for e in range(cfg.episodes):
        served, _ = _served_arms(writer, env, e)
        np.testing.assert_array_equal(served, local["arm"][e])


# ------------------------------------------- continuous learning (contracts §11)

SEGMENTS = 3


def _continuous_local(cfg):
    """The continuous run replayed locally: ``run_segment`` chained over the
    segments on the whole-run world, plus ``run_episodes`` at horizon E·T."""
    world = continuous_world_config(cfg)
    env = simulate.build_environment(world, scenario=resolve_scenario(cfg))
    pol = make_policy(
        "linear_ts",
        lints_params=cfg.policy,
        reward_mode=cfg.reward_mode,
        log_propensity=False,
    )
    key = simulate.episode_keys(cfg.seed, cfg.scenario, 1)[0]
    nb = cfg.horizon // cfg.batch_size
    segs, state = [], None
    for s in range(cfg.episodes):
        out, state = simulate.run_segment(
            pol, env, key, s * nb, nb, cfg.batch_size, state
        )
        segs.append(out["arm"])
    long = simulate.run_episodes(pol, env, key[None], world.horizon, cfg.batch_size)
    return env, np.concatenate(segs), long["arm"][0]


def _global_served(writer, env):
    arm_index = {cid: i for i, cid in enumerate(env.arm_ids)}
    rows = sorted(writer.events, key=lambda r: r["round"])
    assert [r["round"] for r in rows] == list(range(len(rows)))
    return np.array([arm_index[r["arm"]] for r in rows])


@pytest.mark.parametrize(
    ("scenario", "reward_mode", "discount", "max_instances"),
    [
        ("segment_winners", "click", 1.0, None),
        # drift flips once at half the *run* (inside segment 1), forgetting on,
        # and every batch split over several requests
        ("drift", "engaged", 0.98, 37),
    ],
)
def test_endpoint_lints_matches_simulator_in_continuous_mode(
    tmp_path, monkeypatch, scenario, reward_mode, discount, max_instances
):
    """One reset per run + the global batch index: the real predictor over a
    3-segment continuous run equals ``run_segment`` chained (and one long
    ``run_episodes``) round for round, with no predictor change."""
    cfg = _cfg(scenario, reward_mode, discount, episodes=SEGMENTS)
    predictor = _predictor(tmp_path, monkeypatch, cfg)
    settings = TrafficSettings(baselines=())
    if max_instances:
        settings = dataclasses.replace(settings, max_request_instances=max_instances)
    writer, client = _Writer(), _Client(predictor)
    tr = TrafficRunner(cfg, client, writer, settings, learning="continuous")
    summary = tr.run()
    assert summary.decision_errors == summary.rewards_rejected == 0
    resets = [i for req in client.requests for i in req if i["type"] == "reset"]
    assert len(resets) == 1

    env, chained, long = _continuous_local(cfg)
    np.testing.assert_array_equal(chained, long)
    np.testing.assert_array_equal(_global_served(writer, env), chained)
    # the posterior accumulated over the whole run, in one episode
    total = SEGMENTS * cfg.horizon
    assert int(predictor._state.step) == total
    # one episode's version counter over the whole run (split reward requests
    # each count as an applied batch)
    episode, _, n = predictor._version().rpartition("-v")
    assert episode.endswith("-e0") and int(n) >= total // cfg.batch_size


def test_fake_endpoint_matches_simulator_in_continuous_mode():
    """The fake endpoint (``--in-process``) behaves identically under one reset
    plus global batch indices."""
    cfg = _cfg("segment_winners", "engaged", episodes=SEGMENTS)
    writer = _Writer()
    endpoint = FakeBanditEndpoint(cfg)
    TrafficRunner(
        cfg,
        InProcessClient(endpoint),
        writer,
        TrafficSettings(baselines=()),
        learning="continuous",
    ).run()
    env, chained, _ = _continuous_local(cfg)
    np.testing.assert_array_equal(_global_served(writer, env), chained)
    assert endpoint.model_version == f"e0-v{SEGMENTS * cfg.horizon // cfg.batch_size}"


SHIFTS = [
    {"kind": "demote", "at_frac": 0.5, "creative_id": "leader", "drop_pp": 0.015},
]


def test_endpoint_and_ghost_choose_identical_arms_until_the_first_shift(
    tmp_path, monkeypatch
):
    """With shifts, the ghost (``linear_ts_unshifted``: local LinTS on the
    unshifted world, same episode key, same discount) is the endpoint's twin up
    to the first shift, so the post-shift gap is purely the shift's effect."""
    # HORIZON_M, not HORIZON_S: the post-shift check below needs enough rounds for
    # the demoted leader's lost clicks to move the posterior; with only 500
    # rounds after the shift some seeds leave the arms identical
    cfg = _cfg("segment_winners", horizon=HORIZON_M)
    shifts = validate_shifts(
        shifts_from_dict(SHIFTS), resolve_scenario(cfg), cfg.arms, cfg.ctr_mode
    )
    predictor = _predictor(tmp_path, monkeypatch, cfg)
    tr = TrafficRunner(
        cfg,
        _Client(predictor),
        _Writer(),
        TrafficSettings(baselines=(), keep_outputs=True),
        shifts=shifts,
    )
    tr.run()
    assert tr.discount is not None and tr.discount < 1.0
    r0 = tr.shift_rounds[0]
    assert r0 == HORIZON_M // 2
    for e in range(cfg.episodes):
        ours = tr.outputs[(e, traffic.ENDPOINT_POLICY)]["arm"]
        ghost = tr.outputs[(e, traffic.GHOST_POLICY)]["arm"]
        np.testing.assert_array_equal(ours[:r0], ghost[:r0])
        assert not np.array_equal(ours[r0:], ghost[r0:])
