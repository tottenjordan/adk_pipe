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
import math
from collections.abc import Mapping, Sequence
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


#: Inclusive bounds per ``ScenarioOverrides`` field (contracts §9). ``segment_mix``
#: bounds apply to each raw weight (before renormalisation). ``runserver``
#: duplicates this table (parity-tested); keep the two in sync.
OVERRIDE_BOUNDS: dict[str, tuple[float, float]] = {
    "segment_mix": (0.05, 1.0),
    "gap_scale": (0.25, 2.0),
    "judge_wrong": (0.0, 1.0),
    "noise_scale": (0.0, 2.0),
    "drift_at_frac": (0.2, 0.8),
}


@dataclass(frozen=True)
class ScenarioOverrides:
    """User-tuned tweaks to a scenario preset (contracts §9); ``None`` = keep the
    preset value. Applied by ``apply_scenario_overrides``.

    - ``segment_mix``: one weight per scenario segment (renormalised to sum 1).
    - ``gap_scale``: ``segment_winners`` scales ``lift_pp``; ``rank_ctrs``
      scenarios spread the preset CTRs around their logit mean.
    - ``judge_wrong``: replaces the preset's (0 right, 0.5 uninformative, 1 reversed).
    - ``noise_scale``: multiplies ``noise_sd`` and ``theta_sd``.
    - ``drift_at_frac``: the drift change point (``drift`` scenario only).
    """

    segment_mix: tuple[float, ...] | None = None
    gap_scale: float | None = None
    judge_wrong: float | None = None
    noise_scale: float | None = None
    drift_at_frac: float | None = None

    def is_empty(self) -> bool:
        return all(getattr(self, f.name) is None for f in dataclasses.fields(self))


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
    validate_lints_params(cfg.policy, len(cfg.arms))
    if cfg.scenario_overrides is not None:
        validate_scenario_overrides(cfg.scenario_overrides, load_scenario(cfg.scenario))
    return cfg


def _check_bound(name: str, value: float) -> None:
    lo, hi = OVERRIDE_BOUNDS[name]
    if not (math.isfinite(value) and lo <= value <= hi):
        raise ValueError(
            f"scenario_overrides.{name} must be in [{lo}, {hi}], got {value}"
        )


def validate_scenario_overrides(
    ov: ScenarioOverrides, scenario: ScenarioConfig
) -> ScenarioOverrides:
    """Check ``ov`` against ``OVERRIDE_BOUNDS`` and ``scenario`` (segment count,
    drift-only fields). Raises ``ValueError`` naming the offending field."""
    if ov.segment_mix is not None:
        n = len(scenario.segments)
        if len(ov.segment_mix) != n:
            raise ValueError(
                f"scenario_overrides.segment_mix needs {n} weights for "
                f"{scenario.name}, got {len(ov.segment_mix)}"
            )
        for w in ov.segment_mix:
            _check_bound("segment_mix", w)
    for name in ("gap_scale", "judge_wrong", "noise_scale", "drift_at_frac"):
        value = getattr(ov, name)
        if value is not None:
            _check_bound(name, value)
    if ov.drift_at_frac is not None and scenario.name != "drift":
        raise ValueError(
            "scenario_overrides.drift_at_frac is only valid for the drift scenario"
        )
    return ov


def _override_number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"scenario_overrides.{name} must be a number, got {value!r}")
    return float(value)


def scenario_overrides_from_dict(data: Any) -> ScenarioOverrides | None:
    """Strictly parse ``experiment.json``'s ``scenario_overrides`` (types and
    unknown keys only; bounds are checked by ``validate_scenario_overrides``).
    ``None`` or an empty mapping -> ``None``."""
    if data is None:
        return None
    if not isinstance(data, Mapping):
        raise ValueError("scenario_overrides must be an object")
    kw = _strict_kwargs(ScenarioOverrides, data, "scenario_overrides")
    for name, value in list(kw.items()):
        if value is None:
            continue
        if name == "segment_mix":
            if not isinstance(value, list | tuple):
                raise ValueError("scenario_overrides.segment_mix must be a list")
            kw[name] = tuple(_override_number(name, v) for v in value)
        else:
            kw[name] = _override_number(name, value)
    ov = ScenarioOverrides(**kw)
    return None if ov.is_empty() else ov


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
        if "scenario_overrides" in kw:
            kw["scenario_overrides"] = scenario_overrides_from_dict(
                kw["scenario_overrides"]
            )
        cfg = ExperimentConfig(**kw)
    except TypeError as exc:  # missing required fields
        raise ValueError(str(exc)) from exc
    return validate_experiment_config(cfg)


def scenario_overrides_to_dict(ov: ScenarioOverrides | None) -> dict[str, Any] | None:
    """JSON-ready dict of the *set* override fields, or ``None`` when none are set."""
    if ov is None or ov.is_empty():
        return None
    return {
        f.name: list(v) if isinstance(v, tuple) else v
        for f in dataclasses.fields(ov)
        if (v := getattr(ov, f.name)) is not None
    }


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


def _logit(p: float) -> float:
    return math.log(p) - math.log1p(-p)


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


def apply_scenario_overrides(
    sc: ScenarioConfig, ov: ScenarioOverrides | None
) -> ScenarioConfig:
    """``sc`` with ``ov`` applied (contracts §9). Does not validate; see
    ``validate_scenario_overrides``.

    ``gap_scale`` g: ``segment_winners`` -> ``lift_pp · g``; ``rank_ctrs``
    scenarios -> each preset CTR becomes ``sigmoid(mid + g·(logit(ctr) - mid))``
    with ``mid`` the mean logit, so CTRs stay in (0, 1).
    """
    if ov is None:
        return sc
    if ov.segment_mix is not None:
        sc = with_segment_mix(sc, list(ov.segment_mix))
    changes: dict[str, Any] = {}
    if ov.gap_scale is not None:
        g = ov.gap_scale
        if sc.arm_effect == "segment_winners":
            changes["lift_pp"] = sc.lift_pp * g
        else:
            logits = [_logit(c) for c in sc.rank_ctrs]
            mid = sum(logits) / len(logits)
            changes["rank_ctrs"] = tuple(_sigmoid(mid + g * (z - mid)) for z in logits)
    if ov.judge_wrong is not None:
        changes["judge_wrong"] = ov.judge_wrong
    if ov.noise_scale is not None:
        changes["noise_sd"] = sc.noise_sd * ov.noise_scale
        changes["theta_sd"] = sc.theta_sd * ov.noise_scale
    if ov.drift_at_frac is not None:
        changes["drift"] = dataclasses.replace(sc.drift, at_frac=ov.drift_at_frac)
    return dataclasses.replace(sc, **changes) if changes else sc


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


def default_noise_var(ctr: float, reward_mode: str) -> float:
    """Reward variance the linear-Gaussian model should assume at mean CTR ``ctr``.

    click: Bernoulli variance p(1-p). engaged (rewards scaled by the base dwell,
    so dwell ~ Exponential(≈1)): E[r²] - E[r]² = 2p - p². ``LinTSParams``'s 0.25
    default is ~6x the Bernoulli variance at a 4% CTR and over-explores.
    """
    if reward_mode == "engaged":
        return round(2 * ctr - ctr * ctr, 6)
    return round(ctr * (1 - ctr), 6)


def scenario_noise_var(scenario: str, ctr_mode: str, reward_mode: str) -> float:
    """Calibrated σ² for an experiment: ``default_noise_var`` at the scenario's
    target CTR for ``ctr_mode`` (what ``build_sim_config`` uses). The serving
    container applies it when ``experiment.json`` has no ``policy.noise_var``;
    ``runserver/experiments.py`` duplicates it (parity-tested)."""
    return default_noise_var(load_scenario(scenario).target_ctr[ctr_mode], reward_mode)


#: Effective memory, in rounds, of the endpoint's discounted posterior per scenario
#: and ctr mode (contracts §7). Only ``drift`` forgets: a discount costs the
#: stationary scenarios regret (docs/experiments/bandit-simulation.md, "Discount
#: sweep"). The window is 1/8 of the preset horizon (5k of 40k demo rounds, 50k of
#: 400k realistic ones), the flat optimum of that sweep. ``runserver`` duplicates
#: this table and ``discount_for_memory`` (parity-tested).
DISCOUNT_MEMORY_ROUNDS: dict[str, dict[str, int]] = {
    "drift": {"demo": 5000, "realistic": 50000},
}


def discount_for_memory(memory_rounds: float, batch_size: int = 100) -> float:
    """Per-update discount γ for an effective memory of ``memory_rounds`` rounds.

    ``update`` applies γ once per batch, so evidence from ``b`` batches ago keeps
    weight γᵇ; with γ = exp(-batch_size / N) a round ``t`` rounds old keeps
    exp(-t / N), i.e. an exponential window of N rounds (equivalently
    N ≈ batch_size / (1 - γ) for γ near 1). Rounded to 3 decimals."""
    if not memory_rounds > 0 or not batch_size > 0:
        raise ValueError("memory_rounds and batch_size must be > 0")
    return round(math.exp(-batch_size / memory_rounds), 3)


def default_discount(scenario: str, ctr_mode: str, batch_size: int = 100) -> float:
    """The endpoint's ``policy.discount`` for an experiment: forgetting with the
    ``DISCOUNT_MEMORY_ROUNDS`` window where one is set (``drift``), else 1.0."""
    memory = DISCOUNT_MEMORY_ROUNDS.get(scenario, {}).get(ctr_mode)
    return discount_for_memory(memory, batch_size) if memory else 1.0


def reward_scale(scenario: str, reward_mode: str) -> float:
    """Divisor applied to rewards before a policy update: the scenario's base
    dwell for ``engaged`` (so rewards are ~Exponential(1)-scaled, matching
    ``default_noise_var``), 1 for clicks. Mirrors ``TrueModel.reward_scale``."""
    return load_scenario(scenario).dwell_base_s if reward_mode == "engaged" else 1.0


# ------------------------------------------------------------- scripted shifts

#: Behaviour-shift kinds (contracts §10). ``runserver`` duplicates this tuple and
#: ``SHIFT_BOUNDS`` (parity-tested); keep them in sync.
SHIFT_KINDS: tuple[str, ...] = ("promote", "demote", "mix", "shock")
MAX_SHIFTS = 4
#: Shortest shock window, as a fraction of the run (``until_frac - at_frac``).
SHIFT_MIN_WINDOW = 0.02
#: ``demote`` / ``shock`` ``creative_id`` may name the creative leading at that
#: moment (``LEADER_KINDS``).
LEADER = "leader"
LEADER_KINDS: tuple[str, ...] = ("demote", "shock")

#: Inclusive bounds per shift field (contracts §10). ``lift_pp`` / ``drop_pp`` are
#: demo-mode CTR points; ``validate_shifts`` scales them by the scenario's
#: ``ctr_scale(ctr_mode)`` (×0.2 realistic). ``segment_mix`` bounds each raw weight
#: (as §9). ``until_frac`` must also be ≥ ``at_frac + SHIFT_MIN_WINDOW``.
SHIFT_BOUNDS: dict[str, tuple[float, float]] = {
    "at_frac": (0.05, 0.95),
    "until_frac": (0.07, 1.0),
    "lift_pp": (0.005, 0.03),
    "drop_pp": (0.005, 0.03),
    "segment_mix": (0.05, 1.0),
    "ctr_multiplier": (0.3, 2.0),
}

#: Optional fields each kind accepts (``kind`` and ``at_frac`` are always
#: required) and the ones it requires.
_SHIFT_FIELDS: dict[str, tuple[str, ...]] = {
    "promote": ("segment", "creative_id", "lift_pp"),
    "demote": ("segment", "creative_id", "drop_pp"),
    "mix": ("segment_mix",),
    "shock": ("segment", "creative_id", "until_frac", "ctr_multiplier"),
}
_SHIFT_REQUIRED: dict[str, tuple[str, ...]] = {
    "promote": ("creative_id", "lift_pp"),
    "demote": ("creative_id", "drop_pp"),
    "mix": ("segment_mix",),
    "shock": ("creative_id", "until_frac", "ctr_multiplier"),
}
_REL_TOL = 1e-9  # float slack on the ctr-mode-scaled magnitude bounds


@dataclass(frozen=True)
class ShiftSpec:
    """One scripted behaviour shift (contracts §10), resolved by
    ``bandit.environment.build_true_model`` in ``at_frac`` order.

    - ``promote``: from ``at_frac``·T, ``creative_id`` beats the best other
      creative by ``lift_pp`` (in ``segment``, or in every segment when ``None``).
    - ``demote``: ``creative_id`` (or ``"leader"``, the creative leading at that
      moment) falls ``drop_pp`` below the best other creative.
    - ``mix``: the segment mix becomes ``segment_mix`` (renormalised).
    - ``shock``: click probabilities of ``creative_id`` (or ``"leader"``) are
      multiplied by ``ctr_multiplier`` over ``[at_frac, until_frac)``.
    """

    kind: str
    at_frac: float
    segment: str | None = None
    creative_id: str | None = None
    lift_pp: float | None = None
    drop_pp: float | None = None
    segment_mix: tuple[float, ...] | None = None
    until_frac: float | None = None
    ctr_multiplier: float | None = None


def _shift_number(i: int, name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"shifts[{i}].{name} must be a number, got {value!r}")
    return float(value)


def _shift_bound(i: int, name: str, value: float, scale: float = 1.0) -> None:
    lo, hi = SHIFT_BOUNDS[name]
    lo, hi = lo * scale, hi * scale
    if not (
        math.isfinite(value) and lo * (1 - _REL_TOL) <= value <= hi * (1 + _REL_TOL)
    ):
        raise ValueError(
            f"shifts[{i}].{name} must be in [{lo:.6g}, {hi:.6g}], got {value}"
        )


def _check_shift(i: int, s: ShiftSpec) -> None:
    """Scenario-independent checks: kind, fields per kind, static bounds."""
    if s.kind not in SHIFT_KINDS:
        raise ValueError(f"shifts[{i}].kind must be one of {SHIFT_KINDS}")
    _shift_bound(i, "at_frac", s.at_frac)
    allowed = _SHIFT_FIELDS[s.kind]
    for f in dataclasses.fields(ShiftSpec):
        if f.name in ("kind", "at_frac"):
            continue
        value = getattr(s, f.name)
        if value is not None and f.name not in allowed:
            raise ValueError(f"shifts[{i}].{f.name} is not valid for a {s.kind} shift")
        if value is None and f.name in _SHIFT_REQUIRED[s.kind]:
            raise ValueError(f"shifts[{i}].{f.name} is required for a {s.kind} shift")
    if s.segment is not None and not (isinstance(s.segment, str) and s.segment):
        raise ValueError(f"shifts[{i}].segment must be a segment name or null")
    if s.creative_id is not None:
        if not (isinstance(s.creative_id, str) and s.creative_id):
            raise ValueError(f"shifts[{i}].creative_id must be a creative id")
        if s.creative_id == LEADER and s.kind not in LEADER_KINDS:
            raise ValueError(
                f'shifts[{i}].creative_id "{LEADER}" is only valid for '
                f"{' / '.join(LEADER_KINDS)}"
            )
    for name in ("lift_pp", "drop_pp"):  # ctr-mode bounds: validate_shifts
        value = getattr(s, name)
        if value is not None and not (
            math.isfinite(value) and 0 < value <= SHIFT_BOUNDS[name][1]
        ):
            raise ValueError(
                f"shifts[{i}].{name} must be in (0, {SHIFT_BOUNDS[name][1]}]"
            )
    if s.segment_mix is not None:
        for w in s.segment_mix:
            _shift_bound(i, "segment_mix", w)
    if s.ctr_multiplier is not None:
        _shift_bound(i, "ctr_multiplier", s.ctr_multiplier)
    if s.until_frac is not None:
        _shift_bound(i, "until_frac", s.until_frac)
        if s.until_frac - s.at_frac < SHIFT_MIN_WINDOW - 1e-9:
            raise ValueError(
                f"shifts[{i}].until_frac must be at least {SHIFT_MIN_WINDOW} "
                f"after at_frac ({s.at_frac}), got {s.until_frac}"
            )


def shifts_from_dict(data: Any) -> tuple[ShiftSpec, ...]:
    """Strictly parse a snake_case shift list (``SHIFTS_JSON``, contracts §10):
    types, unknown keys, fields per kind and the scenario-independent bounds.
    ``validate_shifts`` adds the scenario/arm/ctr-mode checks. ``None`` -> ()."""
    if data is None:
        return ()
    if not isinstance(data, list | tuple):
        raise ValueError("shifts must be a list")
    if len(data) > MAX_SHIFTS:
        raise ValueError(f"at most {MAX_SHIFTS} shifts, got {len(data)}")
    allowed = {f.name for f in dataclasses.fields(ShiftSpec)}
    out = []
    for i, doc in enumerate(data):
        if not isinstance(doc, Mapping):
            raise ValueError(f"shifts[{i}] must be an object")
        for key in doc:
            if key not in allowed:
                raise ValueError(f"shifts[{i}].{key} is not a shift field")
        kw: dict[str, Any] = {}
        for key, value in doc.items():
            if value is None:
                continue
            if key in ("kind", "segment", "creative_id"):
                if not isinstance(value, str):
                    raise ValueError(f"shifts[{i}].{key} must be a string")
                kw[key] = value
            elif key == "segment_mix":
                if not isinstance(value, list | tuple):
                    raise ValueError(f"shifts[{i}].segment_mix must be a list")
                kw[key] = tuple(_shift_number(i, key, v) for v in value)
            else:
                kw[key] = _shift_number(i, key, value)
        if "kind" not in kw:
            raise ValueError(f"shifts[{i}].kind is required")
        if "at_frac" not in kw:
            raise ValueError(f"shifts[{i}].at_frac is required")
        spec = ShiftSpec(**kw)
        _check_shift(i, spec)
        out.append(spec)
    return tuple(out)


def shifts_to_dict(shifts: Sequence[ShiftSpec]) -> list[dict[str, Any]]:
    """JSON-ready snake_case list; ``shifts_from_dict(shifts_to_dict(s)) == s``.
    Unset fields are omitted, except ``segment`` (``null`` = everyone) on the
    kinds that take one."""
    out = []
    for s in shifts:
        d: dict[str, Any] = {"kind": s.kind, "at_frac": s.at_frac}
        if "segment" in _SHIFT_FIELDS[s.kind]:
            d["segment"] = s.segment
        for f in dataclasses.fields(ShiftSpec):
            value = getattr(s, f.name)
            if f.name in d or value is None:
                continue
            d[f.name] = list(value) if isinstance(value, tuple) else value
        out.append(d)
    return out


def validate_shifts(
    shifts: Sequence[ShiftSpec],
    scenario: ScenarioConfig,
    arms: Sequence[ArmSpec] | Sequence[str],
    ctr_mode: str,
) -> tuple[ShiftSpec, ...]:
    """Check ``shifts`` against the scenario (segment names and count), the
    experiment's arms (creative ids, or ``"leader"`` for demote / shock) and the
    ``ctr_mode``-scaled ``lift_pp`` / ``drop_pp`` bounds. Raises ``ValueError``
    naming the field (``shifts[i].<field>``)."""
    if ctr_mode not in CTR_MODES:
        raise ValueError(f"ctr_mode must be one of {CTR_MODES}")
    if len(shifts) > MAX_SHIFTS:
        raise ValueError(f"at most {MAX_SHIFTS} shifts, got {len(shifts)}")
    ids = {a if isinstance(a, str) else a.creative_id for a in arms}
    names = [seg.name for seg in scenario.segments]
    scale = scenario.ctr_scale(ctr_mode)
    for i, s in enumerate(shifts):
        _check_shift(i, s)
        if s.segment is not None and s.segment not in names:
            raise ValueError(f"shifts[{i}].segment {s.segment!r} not in {names}")
        if s.creative_id is not None and s.creative_id not in ids:
            if not (s.kind in LEADER_KINDS and s.creative_id == LEADER):
                raise ValueError(
                    f"shifts[{i}].creative_id {s.creative_id!r} is not an arm"
                )
        if s.segment_mix is not None and len(s.segment_mix) != len(names):
            raise ValueError(
                f"shifts[{i}].segment_mix needs {len(names)} weights for "
                f"{scenario.name}, got {len(s.segment_mix)}"
            )
        for name in ("lift_pp", "drop_pp"):
            value = getattr(s, name)
            if value is not None:
                _shift_bound(i, name, value, scale)
    return tuple(shifts)


def default_shift_discount(ctr_mode: str, batch_size: int, horizon: int) -> float:
    """The endpoint's discount for a traffic run with shifts when forgetting is
    on (contracts §10): the drift memory rule (``DISCOUNT_MEMORY_ROUNDS``, 1/8
    of the horizon) applied to this run's ``horizon`` via
    ``discount_for_memory``. ``ctr_mode`` is checked for symmetry with
    ``default_discount``; the run's horizon already carries the mode's scale."""
    if ctr_mode not in CTR_MODES:
        raise ValueError(f"ctr_mode must be one of {CTR_MODES}")
    return discount_for_memory(horizon / 8, batch_size)


#: Bounds on a ``reset`` instance's optional ``discount`` (contracts §2 / §10): the
#: per-run forgetting γ the traffic job sends when ``forget`` is on. Short runs
#: (``default_shift_discount`` below 0.95, i.e. horizon < ~15.6k at batch 100)
#: are clamped up to the floor by the job. ``bandit_serving`` and the fake
#: endpoint both validate against this.
RESET_DISCOUNT_BOUNDS: tuple[float, float] = (0.95, 1.0)
