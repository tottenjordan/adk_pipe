"""Experiment config loading/validation (contracts §1 ``bandit/config.py``)."""

import dataclasses
import json
import math

import pytest

from bandit.config import (
    OVERRIDE_BOUNDS,
    ArmSpec,
    ExperimentConfig,
    LinTSParams,
    ScenarioOverrides,
    apply_scenario_overrides,
    build_sim_config,
    default_arms,
    experiment_config_to_dict,
    load_experiment_config,
    load_scenario,
    resolve_scenario,
    validate_scenario_overrides,
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


def test_arm_overall_resolution():
    # contracts §6: api arms carry ad_copy_overall + visual_overall, no "overall"
    api_arm = ArmSpec(
        "c", "C", {"ad_copy_overall": 0.8, "visual_overall": 0.6, "audience_fit": 0.1}
    )
    assert api_arm.overall == pytest.approx(0.7)
    assert api_arm.score("stopping_power") == pytest.approx(0.7)
    assert api_arm.score("audience_fit") == pytest.approx(0.1)
    assert ArmSpec("x", "X", {"overall": 0.4, "a": 1.0}).overall == 0.4
    assert ArmSpec("y", "Y", {"a": 0.2, "b": 0.4}).overall == pytest.approx(0.3)
    assert ArmSpec("z", "Z", {}).overall == 0.5


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


@pytest.mark.parametrize("scenario", ["clear_winner", "segment_winners", "drift"])
@pytest.mark.parametrize("ctr_mode", ["demo", "realistic"])
@pytest.mark.parametrize("reward_mode", ["click", "engaged"])
def test_scenario_noise_var_matches_sim_default(scenario, ctr_mode, reward_mode):
    from bandit.config import build_sim_config, default_noise_var, scenario_noise_var

    p = load_scenario(scenario).target_ctr[ctr_mode]
    nv = scenario_noise_var(scenario, ctr_mode, reward_mode)
    assert nv == default_noise_var(p, reward_mode)
    sim = build_sim_config(scenario, ctr_mode=ctr_mode, reward_mode=reward_mode)
    assert sim.policy.noise_var == nv
    assert nv < LinTSParams().noise_var  # the 0.25 default over-explores


def test_reward_scale_is_base_dwell_for_engaged_only():
    from bandit.config import reward_scale

    assert reward_scale("drift", "click") == 1.0
    assert reward_scale("drift", "engaged") == load_scenario("drift").dwell_base_s


# ------------------------------------------------- scenario overrides (contracts §9)

OVERRIDES = {
    "segment_mix": [0.6, 0.2, 0.2],
    "gap_scale": 1.5,
    "judge_wrong": 0.8,
    "noise_scale": 0.5,
}


def test_roundtrip_without_overrides_omits_key():
    cfg = load_experiment_config(_cfg())
    assert cfg.scenario_overrides is None
    d = experiment_config_to_dict(cfg)
    assert "scenario_overrides" not in d
    assert load_experiment_config(d) == cfg


@pytest.mark.parametrize("empty", [None, {}])
def test_empty_overrides_normalise_to_none(empty):
    cfg = load_experiment_config(_cfg(scenario_overrides=empty))
    assert cfg.scenario_overrides is None
    assert "scenario_overrides" not in experiment_config_to_dict(cfg)


def test_roundtrip_with_overrides(tmp_path):
    cfg = load_experiment_config(_cfg(scenario_overrides=OVERRIDES))
    assert cfg.scenario_overrides == ScenarioOverrides(
        segment_mix=(0.6, 0.2, 0.2), gap_scale=1.5, judge_wrong=0.8, noise_scale=0.5
    )
    d = experiment_config_to_dict(cfg)
    # only the set fields are written, as plain JSON types
    assert d["scenario_overrides"] == OVERRIDES
    path = tmp_path / "experiment.json"
    path.write_text(json.dumps(d))
    assert load_experiment_config(path) == cfg


def test_drift_at_frac_roundtrip_on_drift():
    cfg = load_experiment_config(
        _cfg(scenario="drift", scenario_overrides={"drift_at_frac": 0.3})
    )
    assert cfg.scenario_overrides is not None
    assert cfg.scenario_overrides.drift_at_frac == 0.3
    d = experiment_config_to_dict(cfg)
    assert d["scenario_overrides"] == {"drift_at_frac": 0.3}


@pytest.mark.parametrize(
    ("over", "field"),
    [
        ({"segment_mix": [0.04, 0.5, 0.5]}, "segment_mix"),
        ({"segment_mix": [1.1, 0.5, 0.5]}, "segment_mix"),
        ({"segment_mix": [0.5, 0.5]}, "segment_mix"),  # clear_winner has 3
        ({"segment_mix": [0.25, 0.25, 0.25, 0.25]}, "segment_mix"),
        ({"segment_mix": "0.5,0.5,0.5"}, "segment_mix"),
        ({"segment_mix": [0.5, None, 0.5]}, "segment_mix"),
        ({"gap_scale": 0.2}, "gap_scale"),
        ({"gap_scale": 2.1}, "gap_scale"),
        ({"judge_wrong": -0.1}, "judge_wrong"),
        ({"judge_wrong": 1.01}, "judge_wrong"),
        ({"noise_scale": -0.5}, "noise_scale"),
        ({"noise_scale": 2.5}, "noise_scale"),
        ({"noise_scale": float("nan")}, "noise_scale"),
        ({"gap_scale": True}, "gap_scale"),
        ({"gap_scale": "big"}, "gap_scale"),
        ({"drift_at_frac": 0.5}, "drift_at_frac"),  # clear_winner is not drift
        ({"surprise": 1}, "surprise"),
    ],
)
def test_override_validation_errors_name_the_field(over, field):
    with pytest.raises(ValueError, match=field):
        load_experiment_config(_cfg(scenario_overrides=over))


@pytest.mark.parametrize("frac", [0.19, 0.81])
def test_drift_at_frac_bounds(frac):
    with pytest.raises(ValueError, match="drift_at_frac"):
        load_experiment_config(
            _cfg(scenario="drift", scenario_overrides={"drift_at_frac": frac})
        )


@pytest.mark.parametrize(
    ("over", "scenario"),
    [
        ({"segment_mix": [0.05, 1.0, 1.0]}, "clear_winner"),  # bounds inclusive
        ({"gap_scale": 0.25, "judge_wrong": 0.0, "noise_scale": 0.0}, "drift"),
        ({"gap_scale": 2.0, "judge_wrong": 1.0, "noise_scale": 2.0}, "drift"),
        ({"drift_at_frac": 0.2}, "drift"),
        ({"drift_at_frac": 0.8}, "drift"),
    ],
)
def test_override_bounds_are_inclusive(over, scenario):
    load_experiment_config(_cfg(scenario=scenario, scenario_overrides=over))


def test_segment_mix_length_follows_scenario():
    cfg = load_experiment_config(
        _cfg(
            scenario="segment_winners",
            scenario_overrides={"segment_mix": [0.4, 0.2, 0.2, 0.2]},
        )
    )
    assert cfg.scenario_overrides is not None
    assert len(cfg.scenario_overrides.segment_mix or ()) == 4


def test_override_bounds_constant():
    assert OVERRIDE_BOUNDS == {
        "segment_mix": (0.05, 1.0),
        "gap_scale": (0.25, 2.0),
        "judge_wrong": (0.0, 1.0),
        "noise_scale": (0.0, 2.0),
        "drift_at_frac": (0.2, 0.8),
    }
    assert set(OVERRIDE_BOUNDS) == {
        f.name for f in dataclasses.fields(ScenarioOverrides)
    }


def test_overrides_must_be_a_mapping():
    with pytest.raises(ValueError, match="scenario_overrides"):
        load_experiment_config(_cfg(scenario_overrides=[1, 2]))


def test_validate_scenario_overrides_direct():
    sc = load_scenario("segment_winners")
    ov = ScenarioOverrides(segment_mix=(0.1, 0.2, 0.3, 0.4))
    assert validate_scenario_overrides(ov, sc) is ov
    with pytest.raises(ValueError, match="segment_mix"):
        validate_scenario_overrides(ScenarioOverrides(segment_mix=(0.5,) * 3), sc)


def test_apply_scenario_overrides_fields():
    base = load_scenario("clear_winner")
    sc = apply_scenario_overrides(
        base,
        ScenarioOverrides(
            segment_mix=(0.8, 0.2, 0.2),
            gap_scale=2.0,
            judge_wrong=0.5,
            noise_scale=0.0,
        ),
    )
    assert [s.weight for s in sc.segments] == pytest.approx([2 / 3, 1 / 6, 1 / 6])
    assert sc.judge_wrong == 0.5
    assert sc.noise_sd == 0.0 and sc.theta_sd == 0.0

    def logit(p):
        return math.log(p / (1 - p))

    lb = [logit(c) for c in base.rank_ctrs]
    mid = sum(lb) / len(lb)
    for new, old in zip(sc.rank_ctrs, lb, strict=True):
        assert 0.0 < new < 1.0
        assert logit(new) == pytest.approx(mid + 2.0 * (old - mid))
    assert sc.rank_ctrs[0] > base.rank_ctrs[0] > base.rank_ctrs[-1] > sc.rank_ctrs[-1]
    assert sc.lift_pp == base.lift_pp  # rank_ctrs scenario: lift untouched

    half = apply_scenario_overrides(base, ScenarioOverrides(noise_scale=0.5))
    assert half.noise_sd == pytest.approx(base.noise_sd * 0.5)
    assert half.theta_sd == pytest.approx(base.theta_sd * 0.5)

    sw = load_scenario("segment_winners")
    sw2 = apply_scenario_overrides(sw, ScenarioOverrides(gap_scale=0.5))
    assert sw2.lift_pp == pytest.approx(sw.lift_pp * 0.5)
    assert sw2.rank_ctrs == sw.rank_ctrs

    dr = load_scenario("drift")
    dr2 = apply_scenario_overrides(dr, ScenarioOverrides(drift_at_frac=0.3))
    assert dr2.drift.at_frac == 0.3 and dr2.drift.kind == dr.drift.kind

    assert apply_scenario_overrides(base, None) == base
    assert apply_scenario_overrides(base, ScenarioOverrides()) == base


def test_resolve_scenario_applies_overrides():
    cfg = load_experiment_config(_cfg(scenario_overrides={"judge_wrong": 1.0}))
    assert resolve_scenario(cfg).judge_wrong == 1.0
    plain = load_experiment_config(_cfg())
    assert resolve_scenario(plain) == load_scenario("clear_winner")


def test_build_sim_config_accepts_overrides():
    ov = ScenarioOverrides(segment_mix=(0.7, 0.1, 0.1, 0.1))
    cfg = build_sim_config("segment_winners", scenario_overrides=ov)
    assert cfg.scenario_overrides == ov
    with pytest.raises(ValueError, match="segment_mix"):
        build_sim_config(
            "clear_winner", scenario_overrides=ScenarioOverrides(segment_mix=(1.0,))
        )
