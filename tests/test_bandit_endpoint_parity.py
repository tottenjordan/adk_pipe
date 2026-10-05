"""Endpoint LinTS == simulator LinTS, round for round (contracts §7 "Serving parity").

The live ``linear_ts`` curves come from the traffic job driving the CPR
``BanditPredictor``, while every simulator sweep (exploration, discount, notebook
parity) runs ``bandit.simulate.run_episodes`` with the ``linear_ts`` policy. The two
paths only differ in their PRNG stream: the predictor folds a per-call counter into
``key(reset seed)`` and pads each decision batch to a power of two, the simulator
folds the batch index into the episode's policy stream. With the simulator's key
injected and the padding switched off, the whole serving path (traffic loop ->
request JSON -> feature encoding -> ``select`` -> pending map -> reward scaling ->
batched ``update``) must reproduce the simulator's arm choices exactly, so any
real algorithmic gap (params, reward scale, update timing, discount, dropped
rewards) would show up as a divergence here.
"""

import json

import jax
import numpy as np
import pytest

import bandit_serving.predictor as pm
from bandit import simulate
from bandit.config import (
    LinTSParams,
    build_sim_config,
    default_noise_var,
    experiment_config_to_dict,
    load_scenario,
    resolve_scenario,
)
from bandit.policies import make_policy
from bandit_traffic.traffic import TrafficRunner, TrafficSettings


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

    def predict(self, instances, parameters=None):
        body = {"instances": list(instances)}
        if parameters:
            body["parameters"] = parameters
        return self.p.postprocess(self.p.predict(self.p.preprocess(body)))[
            "predictions"
        ]


def _cfg(scenario, discount):
    sc = load_scenario(scenario)
    return build_sim_config(
        scenario,
        horizon=2000,
        episodes=2,
        batch_size=100,
        policy=LinTSParams(
            noise_var=default_noise_var(sc.target_ctr["demo"], "click"),
            discount=discount,
        ),
    )


@pytest.mark.parametrize(
    ("scenario", "discount"), [("segment_winners", 1.0), ("drift", 0.98)]
)
def test_endpoint_lints_matches_simulator_round_for_round(
    tmp_path, monkeypatch, scenario, discount
):
    cfg = _cfg(scenario, discount)
    keys = simulate.episode_keys(cfg.seed, cfg.scenario, cfg.episodes)
    monkeypatch.setenv("BANDIT_WARMUP", "0")
    # unpadded decision batches, so the normal draws have the simulator's shape
    monkeypatch.setattr(pm, "_bucket", lambda n: n)

    class Lockstep(pm.BanditPredictor):
        def _next_key(self):  # decision call c of an episode = batch c
            batch, self._calls = self._calls, self._calls + 1
            policy_stream = simulate.episode_streams(keys[self._episode])[2]
            return jax.random.fold_in(policy_stream, batch)

    (tmp_path / "experiment.json").write_text(
        json.dumps(experiment_config_to_dict(cfg))
    )
    predictor = Lockstep(checkpoint_every=10**9, checkpoint_seconds=1e9)
    predictor.load(str(tmp_path))
    assert predictor.params == cfg.policy

    writer = _Writer()
    summary = TrafficRunner(
        cfg, _Client(predictor), writer, TrafficSettings(baselines=())
    ).run()
    assert summary.decision_errors == summary.rewards_rejected == 0

    env = simulate.build_environment(cfg, scenario=resolve_scenario(cfg))
    pol = make_policy(
        "linear_ts",
        lints_params=cfg.policy,
        reward_mode=cfg.reward_mode,
        log_propensity=False,
    )
    local = simulate.run_episodes(pol, env, keys, cfg.horizon, cfg.batch_size)
    arm_index = {cid: i for i, cid in enumerate(env.arm_ids)}
    for e in range(cfg.episodes):
        rows = sorted(
            (r for r in writer.events if r["episode"] == e), key=lambda r: r["round"]
        )
        served = np.array([arm_index[r["arm"]] for r in rows])
        np.testing.assert_array_equal(served, local["arm"][e])
        regret = sum(r["regret"] for r in rows)
        local_regret = float(np.sum(local["mean_opt"][e] - local["mean_chosen"][e]))
        assert regret == pytest.approx(local_regret, rel=1e-4)
    # the posterior the endpoint ends on saw every round's reward
    assert int(predictor._state.step) == cfg.horizon
