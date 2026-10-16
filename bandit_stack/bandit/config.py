"""Experiment, policy and scenario configuration (contracts §1 ``bandit/config.py``).

``ExperimentConfig`` is the JSON document the api writes to GCS as
``experiment.json`` (``experiment_config_to_dict``) and the serving container /
traffic job read back (``load_experiment_config``).

The scenario presets (``bandit.scenario``), calibration rules
(``bandit.calibration``) and scripted shifts / continuous runs
(``bandit.shifts``) live in their own modules; their public names are
re-exported here because contracts §1 publishes them as ``bandit.config.*``.

Pure Python (no JAX) so the api could import it later.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bandit.calibration import (
    DISCOUNT_MEMORY_ROUNDS,
    MAX_DWELL_S,
    RESET_DISCOUNT_BOUNDS,
    default_discount,
    default_noise_var,
    default_shift_discount,
    discount_for_memory,
    reward_scale,
    scenario_noise_var,
)
from bandit.scenario import (
    BASE_MARGINALS,
    CTR_MODES,
    OVERRIDE_BOUNDS,
    REWARD_MODES,
    SCENARIO_DIR,
    SCENARIOS,
    DriftSpec,
    ScenarioConfig,
    ScenarioOverrides,
    SegmentSpec,
    _strict_kwargs,
    apply_scenario_overrides,
    load_scenario,
    scenario_from_dict,
    scenario_overrides_from_dict,
    scenario_overrides_to_dict,
    validate_scenario_overrides,
    with_segment_mix,
)
from bandit.shifts import (
    LEADER,
    LEADER_KINDS,
    LEARNING_MODES,
    MAX_CONTINUOUS_ROUNDS,
    MAX_SHIFTS,
    SHIFT_BOUNDS,
    SHIFT_KINDS,
    SHIFT_MIN_WINDOW,
    ShiftSpec,
    continuous_world_config,
    shifts_from_dict,
    shifts_to_dict,
    validate_continuous_run,
    validate_shifts,
)

__all__ = [
    # defined here
    "DEFAULT_EXPLORATION_SCALE",
    "MAX_ARMS",
    "MIN_ARMS",
    "ArmSpec",
    "ExperimentConfig",
    "LinTSParams",
    "arm_from_dict",
    "build_sim_config",
    "default_arms",
    "experiment_config_to_dict",
    "load_experiment_config",
    "resolve_scenario",
    "validate_experiment_config",
    "validate_lints_params",
    # re-exported (contracts §1 paths): bandit.calibration
    "DISCOUNT_MEMORY_ROUNDS",
    "MAX_DWELL_S",
    "RESET_DISCOUNT_BOUNDS",
    "default_discount",
    "default_noise_var",
    "default_shift_discount",
    "discount_for_memory",
    "reward_scale",
    "scenario_noise_var",
    # re-exported: bandit.scenario
    "BASE_MARGINALS",
    "CTR_MODES",
    "OVERRIDE_BOUNDS",
    "REWARD_MODES",
    "SCENARIOS",
    "SCENARIO_DIR",
    "DriftSpec",
    "ScenarioConfig",
    "ScenarioOverrides",
    "SegmentSpec",
    "apply_scenario_overrides",
    "load_scenario",
    "scenario_from_dict",
    "scenario_overrides_from_dict",
    "scenario_overrides_to_dict",
    "validate_scenario_overrides",
    "with_segment_mix",
    # re-exported: bandit.shifts
    "LEADER",
    "LEADER_KINDS",
    "LEARNING_MODES",
    "MAX_CONTINUOUS_ROUNDS",
    "MAX_SHIFTS",
    "SHIFT_BOUNDS",
    "SHIFT_KINDS",
    "SHIFT_MIN_WINDOW",
    "ShiftSpec",
    "continuous_world_config",
    "shifts_from_dict",
    "shifts_to_dict",
    "validate_continuous_run",
    "validate_shifts",
]

MIN_ARMS, MAX_ARMS = 2, 4
_OVERALL_PARTS = ("ad_copy_overall", "visual_overall")


@dataclass(frozen=True)
class ArmSpec:
    """One creative under test. ``scores`` are creative_eval dimension scores in
    [0, 1] (optionally including ``overall``); missing dimensions fall back to
    ``overall`` (or the mean of the given scores) in the simulator."""

    creative_id: str
    label: str
    scores: dict[str, float]
    visual_style: str = ""

    def __hash__(self) -> int:  # dict field: hash on identity-defining fields
        return hash((self.creative_id, self.label, self.visual_style))

    @property
    def overall(self) -> float:
        if "overall" in self.scores:
            return float(self.scores["overall"])
        # api arms (contracts §6): mean of the ad-copy and visual overall scores
        parts = [self.scores[k] for k in _OVERALL_PARTS if k in self.scores]
        if parts:
            return float(sum(parts) / len(parts))
        if not self.scores:
            return 0.5
        return float(sum(self.scores.values()) / len(self.scores))

    def score(self, key: str) -> float:
        return float(self.scores.get(key, self.overall))


#: Posterior-draw scale s for every scenario and ctr mode (contracts §7). Tuned in
#: the 2026-10-05 exploration sweep (docs/experiments/bandit-simulation.md,
#: "Exploration sweep"): s = 0.5 lowers LinTS regret in all 12 scenario × ctr mode
#: × arm-count cells (4–26 %) without lock-in, while s ≤ 0.35 buys a little more
#: mean at 2–3× the across-episode spread. ``runserver`` duplicates it in
#: ``DEFAULT_POLICY`` (parity-tested).
DEFAULT_EXPLORATION_SCALE = 0.5


@dataclass(frozen=True)
class LinTSParams:
    """Linear TS hyper-parameters.

    - ``prior_var``: τ², prior θ ~ N(0, τ² I) per arm.
    - ``noise_var``: σ², Gaussian reward noise of the (misspecified) linear model.
    - ``exploration_scale``: s, posterior samples use covariance s²·Λ⁻¹.
    - ``propensity_samples``: Monte Carlo draws M for ``propensities``.
    - ``min_propensity``: floor applied to eligible arms' propensities.
    - ``discount``: γ ∈ (0, 1]; each ``update`` first decays the posterior toward
      the prior (Λ ← γΛ + (1-γ)Λ₀, b ← γb). 1.0 = stationary (no-op).
    """

    prior_var: float = 1.0
    noise_var: float = 0.25
    exploration_scale: float = DEFAULT_EXPLORATION_SCALE
    propensity_samples: int = 1000
    min_propensity: float = 0.02
    discount: float = 1.0


@dataclass(frozen=True)
class ExperimentConfig:
    experiment_id: str
    arms: tuple[ArmSpec, ...]
    scenario: str
    ctr_mode: str = "demo"
    reward_mode: str = "click"
    horizon: int = 20000
    batch_size: int = 100
    episodes: int = 20
    seed: int = 0
    policy: LinTSParams = field(default_factory=LinTSParams)
    scenario_overrides: ScenarioOverrides | None = None


def validate_lints_params(p: LinTSParams, num_arms: int = MIN_ARMS) -> LinTSParams:
    if not p.prior_var > 0 or not p.noise_var > 0:
        raise ValueError("prior_var and noise_var must be > 0")
    if not 0.1 <= p.exploration_scale <= 5.0:
        raise ValueError("exploration_scale must be in [0.1, 5]")
    if not 100 <= p.propensity_samples <= 5000:
        raise ValueError("propensity_samples must be in [100, 5000]")
    if not 0.0 <= p.min_propensity <= 1.0 / num_arms:
        raise ValueError(f"min_propensity must be in [0, 1/{num_arms}]")
    if not 0.0 < p.discount <= 1.0:
        raise ValueError("discount must be in (0, 1]")
    return p


def validate_experiment_config(cfg: ExperimentConfig) -> ExperimentConfig:
    if not cfg.experiment_id:
        raise ValueError("experiment_id is required")
    if not MIN_ARMS <= len(cfg.arms) <= MAX_ARMS:
        raise ValueError(f"need {MIN_ARMS}-{MAX_ARMS} arms, got {len(cfg.arms)}")
    ids = [a.creative_id for a in cfg.arms]
    if len(set(ids)) != len(ids) or not all(ids):
        raise ValueError("arm creative_ids must be non-empty and unique")
    for arm in cfg.arms:
        for k, v in arm.scores.items():
            if not 0.0 <= float(v) <= 1.0:
                raise ValueError(f"arm {arm.creative_id} score {k}={v} not in [0,1]")
    if cfg.scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {cfg.scenario!r}; one of {SCENARIOS}")
    if cfg.ctr_mode not in CTR_MODES:
        raise ValueError(f"ctr_mode must be one of {CTR_MODES}")
    if cfg.reward_mode not in REWARD_MODES:
        raise ValueError(f"reward_mode must be one of {REWARD_MODES}")
    if not 10 <= cfg.horizon <= 1_000_000:
        raise ValueError("horizon must be in [10, 1_000_000]")
    if not 1 <= cfg.batch_size <= min(10_000, cfg.horizon):
        raise ValueError("batch_size must be in [1, min(10000, horizon)]")
    if not 1 <= cfg.episodes <= 1000:
        raise ValueError("episodes must be in [1, 1000]")
    # JAX keys with x64 off keep only the low 32 bits of a seed (distinct seeds
    # would collide) and key(2**64) raises OverflowError; 31 bits also matches
    # the api's default seed and the reset seed's mod 2**31.
    if not 0 <= cfg.seed < 2**31:
        raise ValueError(f"seed must be in [0, 2**31), got {cfg.seed}")
    validate_lints_params(cfg.policy, len(cfg.arms))
    if cfg.scenario_overrides is not None:
        validate_scenario_overrides(cfg.scenario_overrides, load_scenario(cfg.scenario))
    return cfg


def _integral_field(name: str, value: Any) -> int:
    """``value`` as an int if it is an int or an integral float (a JSON number can
    come back as a float), else ``ValueError``: ``int()`` would truncate 1.7 to 1
    and parse ``"12"``."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise ValueError(f"{name} must be an integer, got {value!r}")


def arm_from_dict(d: Mapping[str, Any]) -> ArmSpec:
    kw = _strict_kwargs(ArmSpec, d, "arm")
    kw["scores"] = {str(k): float(v) for k, v in dict(kw.get("scores", {})).items()}
    return ArmSpec(**kw)


def load_experiment_config(src: str | Path | Mapping[str, Any]) -> ExperimentConfig:
    """Load + validate an ``ExperimentConfig`` from a dict or a JSON/YAML file path."""
    if isinstance(src, str | Path):
        text = Path(src).read_text()
        data = json.loads(text) if str(src).endswith(".json") else yaml.safe_load(text)
    else:
        data = src
    if not isinstance(data, Mapping):
        raise ValueError("experiment config must be a mapping")
    kw = _strict_kwargs(ExperimentConfig, data, "experiment config")
    try:
        kw["arms"] = tuple(arm_from_dict(a) for a in kw.get("arms", ()))
        kw["policy"] = LinTSParams(
            **_strict_kwargs(LinTSParams, kw.get("policy") or {}, "policy")
        )
        for name in ("horizon", "batch_size", "episodes", "seed"):
            if name in kw:
                kw[name] = _integral_field(name, kw[name])
        if "scenario_overrides" in kw:
            kw["scenario_overrides"] = scenario_overrides_from_dict(
                kw["scenario_overrides"]
            )
        cfg = ExperimentConfig(**kw)
    except TypeError as exc:  # missing required fields
        raise ValueError(str(exc)) from exc
    return validate_experiment_config(cfg)


def experiment_config_to_dict(cfg: ExperimentConfig) -> dict[str, Any]:
    """JSON-ready dict; ``load_experiment_config(experiment_config_to_dict(c)) == c``.

    ``scenario_overrides`` is written only when at least one override is set
    (contracts §9), so default configs serialise exactly as before."""
    d = dataclasses.asdict(cfg)
    d["arms"] = [dict(a) for a in d["arms"]]
    d.pop("scenario_overrides")
    ov = scenario_overrides_to_dict(cfg.scenario_overrides)
    if ov is not None:
        d["scenario_overrides"] = ov
    return d


# --------------------------------------------------------------------------- arms

_SYNTHETIC_ARMS: tuple[ArmSpec, ...] = (
    ArmSpec(
        "synthetic-a",
        "Bold trend hook",
        {
            "overall": 0.82,
            "trend_authenticity": 0.9,
            "audience_fit": 0.72,
            "stopping_power": 0.86,
        },
        "graphic_pop",
    ),
    ArmSpec(
        "synthetic-b",
        "Product-first benefit",
        {
            "overall": 0.74,
            "trend_authenticity": 0.6,
            "audience_fit": 0.88,
            "stopping_power": 0.64,
        },
        "studio_product",
    ),
    ArmSpec(
        "synthetic-c",
        "Lifestyle mood",
        {
            "overall": 0.66,
            "trend_authenticity": 0.58,
            "audience_fit": 0.62,
            "stopping_power": 0.78,
        },
        "lifestyle_photo",
    ),
    ArmSpec(
        "synthetic-d",
        "Minimal typographic",
        {
            "overall": 0.7,
            "trend_authenticity": 0.74,
            "audience_fit": 0.66,
            "stopping_power": 0.55,
        },
        "typographic",
    ),
)


def default_arms(k: int = 3) -> tuple[ArmSpec, ...]:
    """``k`` (2-4) synthetic arms with plausible creative_eval scores."""
    if not MIN_ARMS <= k <= MAX_ARMS:
        raise ValueError(f"k must be in [{MIN_ARMS}, {MAX_ARMS}]")
    return _SYNTHETIC_ARMS[:k]


def resolve_scenario(cfg: ExperimentConfig) -> ScenarioConfig:
    """The experiment's effective scenario: the preset named by ``cfg.scenario``
    with ``cfg.scenario_overrides`` applied (what the traffic job simulates)."""
    return apply_scenario_overrides(load_scenario(cfg.scenario), cfg.scenario_overrides)


def build_sim_config(
    scenario: str,
    *,
    ctr_mode: str = "demo",
    reward_mode: str = "click",
    arms: tuple[ArmSpec, ...] | None = None,
    num_arms: int | None = None,
    horizon: int | None = None,
    batch_size: int | None = None,
    episodes: int | None = None,
    seed: int = 0,
    policy: LinTSParams | None = None,
    experiment_id: str = "sim",
    scenario_overrides: ScenarioOverrides | None = None,
) -> ExperimentConfig:
    """An ``ExperimentConfig`` for offline simulation, defaulting the arms to
    ``default_arms(scenario.default_num_arms)`` and horizon/batch/episodes to the
    scenario preset for ``ctr_mode``. The default policy uses
    ``default_noise_var`` for the scenario's target CTR."""
    sc = load_scenario(scenario)
    cfg = ExperimentConfig(
        experiment_id=experiment_id,
        arms=arms
        if arms is not None
        else default_arms(num_arms or sc.default_num_arms),
        scenario=scenario,
        ctr_mode=ctr_mode,
        reward_mode=reward_mode,
        horizon=horizon if horizon is not None else sc.horizon.get(ctr_mode, 20000),
        batch_size=batch_size if batch_size is not None else sc.batch_size,
        episodes=episodes if episodes is not None else sc.episodes,
        seed=seed,
        policy=policy
        if policy is not None
        else LinTSParams(
            noise_var=default_noise_var(sc.target_ctr[ctr_mode], reward_mode)
        ),
        scenario_overrides=None
        if scenario_overrides is None or scenario_overrides.is_empty()
        else scenario_overrides,
    )
    return validate_experiment_config(cfg)
