"""Scripted behaviour shifts (contracts §10) and continuous runs (§11).

``ShiftSpec`` lists are parsed strictly (``shifts_from_dict``), checked against a
scenario / the arms / the ctr mode (``validate_shifts``) and resolved by
``bandit.environment.build_true_model``. A continuous run (``LEARNING_MODES``)
is validated by ``validate_continuous_run`` and simulated on
``continuous_world_config``. ``runserver`` duplicates the kinds, bounds and
caps (parity-tested).

Pure Python (no JAX).
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bandit.scenario import CTR_MODES, ScenarioConfig

if TYPE_CHECKING:
    from bandit.config import ArmSpec, ExperimentConfig

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


#: A traffic run's learning mode (contracts §11): ``per_episode`` resets the
#: endpoint every episode; ``continuous`` sends one reset and keeps learning
#: across the run's episodes (then called segments).
LEARNING_MODES: tuple[str, ...] = ("per_episode", "continuous")
#: Cap on a continuous run's total rounds E·T (job runtime and memory: the
#: traffic job's 1 h task timeout at ~650 rounds/s; the api applies the same cap
#: to per-episode runs, runserver ``MAX_TRAFFIC_ROUNDS``). It replaces the
#: per-episode ``horizon <= 1_000_000`` cap for the world's total.
MAX_CONTINUOUS_ROUNDS = 1_500_000


def validate_continuous_run(episodes: int, horizon: int, batch_size: int) -> int:
    """A continuous run's total rounds E·T (contracts §11). ``ValueError``
    naming ``episodes`` when E·T > ``MAX_CONTINUOUS_ROUNDS``, or ``horizon``
    when T is not a multiple of ``batch_size`` (a policy batch must not straddle
    two segments)."""
    total = int(episodes) * int(horizon)
    if total > MAX_CONTINUOUS_ROUNDS:
        raise ValueError(
            f"episodes x horizon must be <= {MAX_CONTINUOUS_ROUNDS:,} in a "
            f"continuous run, got {episodes} x {horizon} = {total:,}"
        )
    if int(horizon) % int(batch_size):
        raise ValueError(
            f"horizon must be a multiple of batch_size ({batch_size}) in a "
            f"continuous run, got {horizon}"
        )
    return total


def continuous_world_config(cfg: ExperimentConfig) -> ExperimentConfig:
    """The config a continuous run's world is built from (contracts §11):
    ``cfg`` with ``horizon`` = E·T and one episode, so drift points and shift
    rounds resolve over the whole run. Seed, scenario and arms are unchanged, so
    the model key is too. Deliberately not passed through
    ``validate_experiment_config`` (its per-episode horizon cap)."""
    total = validate_continuous_run(cfg.episodes, cfg.horizon, cfg.batch_size)
    return dataclasses.replace(cfg, horizon=total, episodes=1)
