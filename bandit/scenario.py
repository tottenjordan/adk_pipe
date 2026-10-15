"""Scenario presets: the simulator's ground-truth recipe (contracts §1 / §9).

``ScenarioConfig`` (segments, drift, arm effect, CTR targets) is loaded from
``bandit/scenarios/<name>.yaml`` by ``load_scenario``; ``BASE_MARGINALS`` is the
population-level context mix the segments override; ``ScenarioOverrides`` are
the user's tweaks (§9), parsed, validated and applied here.

Pure Python (no JAX).
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bandit.features import BOOL_KEYS, CONTEXT_SPEC

SCENARIOS: tuple[str, ...] = ("clear_winner", "segment_winners", "drift")
CTR_MODES: tuple[str, ...] = ("demo", "realistic")
REWARD_MODES: tuple[str, ...] = ("click", "engaged")
SCENARIO_DIR = Path(__file__).parent / "scenarios"


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


def scenario_overrides_to_dict(ov: ScenarioOverrides | None) -> dict[str, Any] | None:
    """JSON-ready dict of the *set* override fields, or ``None`` when none are set."""
    if ov is None or ov.is_empty():
        return None
    return {
        f.name: list(v) if isinstance(v, tuple) else v
        for f in dataclasses.fields(ov)
        if (v := getattr(ov, f.name)) is not None
    }


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
            # lazy: ``bandit.calibration`` imports this module (load_scenario)
            from bandit.calibration import logit, sigmoid

            logits = [float(logit(c)) for c in sc.rank_ctrs]
            mid = sum(logits) / len(logits)
            changes["rank_ctrs"] = tuple(
                float(sigmoid(mid + g * (z - mid))) for z in logits
            )
    if ov.judge_wrong is not None:
        changes["judge_wrong"] = ov.judge_wrong
    if ov.noise_scale is not None:
        changes["noise_sd"] = sc.noise_sd * ov.noise_scale
        changes["theta_sd"] = sc.theta_sd * ov.noise_scale
    if ov.drift_at_frac is not None:
        changes["drift"] = dataclasses.replace(sc.drift, at_frac=ov.drift_at_frac)
    return dataclasses.replace(sc, **changes) if changes else sc
