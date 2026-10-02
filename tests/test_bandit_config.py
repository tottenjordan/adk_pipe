"""Experiment config loading/validation (contracts §1 ``bandit/config.py``)."""

import json

import pytest

from bandit.config import (
    ArmSpec,
    ExperimentConfig,
    LinTSParams,
    default_arms,
    experiment_config_to_dict,
    load_experiment_config,
    load_scenario,
)

ARMS = [
    {"creative_id": "c1", "label": "One", "scores": {"overall": 0.8}},
    {"creative_id": "c2", "label": "Two", "scores": {"overall": 0.6}},
]


def _cfg(**over):
    d = {"experiment_id": "exp-1", "arms": ARMS, "scenario": "clear_winner"}
    d.update(over)
    return d


def test_load_from_dict_defaults():
    cfg = load_experiment_config(_cfg())
    assert isinstance(cfg, ExperimentConfig)
    assert cfg.arms[0] == ArmSpec("c1", "One", {"overall": 0.8})
    assert cfg.ctr_mode == "demo" and cfg.reward_mode == "click"
    assert cfg.policy == LinTSParams()


def test_roundtrip_dict_and_file(tmp_path):
    cfg = load_experiment_config(
        _cfg(ctr_mode="realistic", policy={"discount": 0.99}, seed=3)
    )
    d = experiment_config_to_dict(cfg)
    assert load_experiment_config(d) == cfg
    json.dumps(d)  # JSON-serialisable
    path = tmp_path / "experiment.json"
    path.write_text(json.dumps(d))
    assert load_experiment_config(path) == cfg
    assert load_experiment_config(str(path)) == cfg


@pytest.mark.parametrize(
    "over",
    [
        {"arms": ARMS[:1]},
        {"arms": ARMS * 3},
        {"arms": [ARMS[0], ARMS[0]]},
        {"scenario": "nope"},
        {"ctr_mode": "huge"},
        {"reward_mode": "likes"},
        {"horizon": 0},
        {"batch_size": 0},
        {"episodes": 0},
        {"policy": {"min_propensity": 0.9}},
        {"policy": {"unknown": 1}},
        {"surprise": True},
    ],
)
def test_validation_errors(over):
    with pytest.raises(ValueError):
        load_experiment_config(_cfg(**over))


def test_default_arms_and_scenarios():
    arms = default_arms(3)
    assert len(arms) == 3 and len({a.creative_id for a in arms}) == 3
    assert all(0.0 <= v <= 1.0 for a in arms for v in a.scores.values())
    for name in ("clear_winner", "segment_winners", "drift"):
        sc = load_scenario(name)
        assert sc.name == name
        assert abs(sum(s.weight for s in sc.segments) - 1.0) < 1e-9
    assert len(load_scenario("segment_winners").segments) == 4
    assert load_scenario("drift").drift.kind == "abrupt"
