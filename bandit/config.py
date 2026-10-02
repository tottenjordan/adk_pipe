"""Experiment, policy and scenario configuration (contracts §1 ``bandit/config.py``).

``ExperimentConfig`` is the JSON document the api writes to GCS as
``experiment.json`` (``experiment_config_to_dict``) and the serving container /
traffic job read back (``load_experiment_config``). ``ScenarioConfig`` is the
simulator-only ground-truth recipe loaded from ``bandit/scenarios/<name>.yaml``.

Pure Python (no JAX) so the api could import it later.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bandit.features import BOOL_KEYS, CONTEXT_SPEC

SCENARIOS: tuple[str, ...] = ("clear_winner", "segment_winners", "drift")
CTR_MODES: tuple[str, ...] = ("demo", "realistic")
REWARD_MODES: tuple[str, ...] = ("click", "engaged")
MIN_ARMS, MAX_ARMS = 2, 4
SCENARIO_DIR = Path(__file__).parent / "scenarios"
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
    exploration_scale: float = 1.0
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
    validate_lints_params(cfg.policy, len(cfg.arms))
    return cfg


def _strict_kwargs(cls: Any, data: Mapping[str, Any], what: str) -> dict[str, Any]:
    allowed = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"unknown {what} fields: {unknown}")
    return dict(data)


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
                kw[name] = int(kw[name])
        cfg = ExperimentConfig(**kw)
    except TypeError as exc:  # missing required fields
        raise ValueError(str(exc)) from exc
    return validate_experiment_config(cfg)


def experiment_config_to_dict(cfg: ExperimentConfig) -> dict[str, Any]:
    """JSON-ready dict; ``load_experiment_config(experiment_config_to_dict(c)) == c``."""
    d = dataclasses.asdict(cfg)
    d["arms"] = [dict(a) for a in d["arms"]]
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


# ---------------------------------------------------------------------- scenarios

#: Population-level context marginals (bool groups: P(True)); segments override.
BASE_MARGINALS: dict[str, dict[str, float] | float] = {
    "devicetype": {"mobile": 0.6, "desktop": 0.32, "tablet": 0.08},
    "os": {"ios": 0.45, "android": 0.4, "other": 0.15},
    "connectiontype": {"wifi": 0.65, "cellular": 0.35},
    "region": {"northeast": 0.17, "midwest": 0.21, "south": 0.38, "west": 0.24},
    "age_bucket": {"21-34": 0.35, "35-54": 0.38, "55+": 0.27},
    "daypart": {"morning": 0.22, "afternoon": 0.3, "evening": 0.33, "night": 0.15},
    "weekend": 2 / 7,
    "topic_matches_trend": 0.5,
    "interest_matches_product": 0.3,
    "freq_24h": {"0": 0.55, "1": 0.28, "2+": 0.17},
}


@dataclass(frozen=True)
class SegmentSpec:
    """A latent user segment: mix ``weight``, context-marginal overrides,
    ``winner_key`` (score dimension that picks its oracle arm in the
    ``segment_winners`` arm effect) and an engaged-dwell multiplier."""

    name: str
    weight: float
    marginals: dict[str, Any] = field(default_factory=dict)
    winner_key: str = "overall"
    dwell_factor: float = 1.0

    def __hash__(self) -> int:
        return hash((self.name, self.weight, self.winner_key, self.dwell_factor))


@dataclass(frozen=True)
class DriftSpec:
    """``none`` | ``abrupt`` (best and worst arm bases swap at ``at_frac``·T) |
    ``gradual`` (linear ramp over ``width_frac``·T centred on ``at_frac``·T)."""

    kind: str = "none"
    at_frac: float = 0.5
    width_frac: float = 0.2


@dataclass(frozen=True)
class ScenarioConfig:
    """Ground-truth recipe (see ``bandit.environment`` for the model)."""

    name: str
    segments: tuple[SegmentSpec, ...]
    target_ctr: dict[str, float]
    horizon: dict[str, int]
    description: str = ""
    batch_size: int = 100
    episodes: int = 50
    default_num_arms: int = 3
    arm_effect: str = "rank_ctrs"  # rank_ctrs | segment_winners
    rank_ctrs: tuple[float, ...] = (0.06, 0.045, 0.035)  # demo, best -> worst
    lift_pp: float = 0.015  # demo; segment_winners winner lift
    kappa: float = 1.0  # arm base per unit of (overall - mean)
    lam: float = 0.5  # segment-affinity scale
    eta: float = 0.5  # context x arm-score interaction scale
    noise_sd: float = 0.05  # seeded logit noise on arm/segment effects
    theta_sd: float = 0.05  # seeded random context coefficients
    judge_wrong: float = 0.0  # 0 = judge scores order arms, 1 = reversed
    dwell_base_s: float = 30.0
    dwell_score_scale: float = 1.0
    drift: DriftSpec = field(default_factory=DriftSpec)

    def __hash__(self) -> int:
        return hash((self.name, self.segments, self.arm_effect, self.drift))

    def ctr_scale(self, ctr_mode: str) -> float:
        """Multiplier from demo-unit CTR gaps to ``ctr_mode`` units."""
        return self.target_ctr[ctr_mode] / self.target_ctr["demo"]


def _validate_marginals(name: str, marginals: Mapping[str, Any]) -> None:
    spec = dict(CONTEXT_SPEC)
    for key, value in marginals.items():
        if key not in spec:
            raise ValueError(f"segment {name}: unknown marginal group {key!r}")
        if key in BOOL_KEYS:
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"segment {name}: P({key}) must be in [0, 1]")
        else:
            bad = set(value) - set(spec[key])
            if bad or any(float(v) < 0 for v in value.values()):
                raise ValueError(f"segment {name}: bad levels for {key}: {bad}")


def scenario_from_dict(data: Mapping[str, Any]) -> ScenarioConfig:
    kw = _strict_kwargs(ScenarioConfig, data, "scenario")
    segs = []
    for s in kw.pop("segments"):
        seg = SegmentSpec(**_strict_kwargs(SegmentSpec, s, "segment"))
        _validate_marginals(seg.name, seg.marginals)
        segs.append(seg)
    total = sum(s.weight for s in segs)
    if not segs or total <= 0:
        raise ValueError("scenario needs >= 1 segment with positive weight")
    segs = [dataclasses.replace(s, weight=s.weight / total) for s in segs]
    if "drift" in kw:
        kw["drift"] = DriftSpec(**_strict_kwargs(DriftSpec, kw["drift"], "drift"))
        if kw["drift"].kind not in ("none", "abrupt", "gradual"):
            raise ValueError(f"unknown drift kind {kw['drift'].kind!r}")
    if "rank_ctrs" in kw:
        kw["rank_ctrs"] = tuple(float(v) for v in kw["rank_ctrs"])
    kw["target_ctr"] = {m: float(v) for m, v in kw["target_ctr"].items()}
    kw["horizon"] = {m: int(v) for m, v in kw["horizon"].items()}
    if set(kw["target_ctr"]) != set(CTR_MODES) or set(kw["horizon"]) != set(CTR_MODES):
        raise ValueError(f"target_ctr and horizon need keys {CTR_MODES}")
    sc = ScenarioConfig(segments=tuple(segs), **kw)
    if sc.arm_effect not in ("rank_ctrs", "segment_winners"):
        raise ValueError(f"unknown arm_effect {sc.arm_effect!r}")
    return sc


def load_scenario(name: str) -> ScenarioConfig:
    """Load a scenario preset by name (``bandit/scenarios/<name>.yaml``)."""
    if name not in SCENARIOS:
        raise ValueError(f"unknown scenario {name!r}; one of {SCENARIOS}")
    data = yaml.safe_load((SCENARIO_DIR / f"{name}.yaml").read_text())
    return scenario_from_dict(data)


def with_segment_mix(sc: ScenarioConfig, weights: list[float]) -> ScenarioConfig:
    """Override the segment mix (e.g. the notebook's (1,0) / (0,1) / (.5,.5) users)."""
    if len(weights) != len(sc.segments) or sum(weights) <= 0 or min(weights) < 0:
        raise ValueError(f"need {len(sc.segments)} non-negative weights")
    total = sum(weights)
    segs = tuple(
        dataclasses.replace(s, weight=w / total)
        for s, w in zip(sc.segments, weights, strict=True)
    )
    return dataclasses.replace(sc, segments=segs)


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
    )
    return validate_experiment_config(cfg)


def default_noise_var(ctr: float, reward_mode: str) -> float:
    """Reward variance the linear-Gaussian model should assume at mean CTR ``ctr``.

    click: Bernoulli variance p(1-p). engaged (rewards scaled by the base dwell,
    so dwell ~ Exponential(≈1)): E[r²] - E[r]² = 2p - p². ``LinTSParams``'s 0.25
    default is ~6x the Bernoulli variance at a 4% CTR and over-explores.
    """
    if reward_mode == "engaged":
        return round(2 * ctr - ctr * ctr, 6)
    return round(ctr * (1 - ctr), 6)
