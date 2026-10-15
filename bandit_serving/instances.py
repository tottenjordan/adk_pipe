"""Endpoint instances (contracts §2): typed parse results + pure validation.

``parse_instance`` turns one raw instance into a ``Decision`` / ``Reward`` /
``Reset`` / ``StateRequest``, or an ``InstanceError`` for anything invalid, so a
bad instance never fails the batch. All instance content (incl. the reward
bounds per reward mode) is validated here, before the predictor changes any
state; it needs only the arm index and the experiment config.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import jax
import jax.numpy as jnp
import numpy as np

from bandit.config import (
    MAX_DWELL_S,
    RESET_DISCOUNT_BOUNDS,
    ExperimentConfig,
    LinTSParams,
)
from bandit.features import context_levels

EXPLORATION_SCALE_BOUNDS = (0.1, 5.0)
PROPENSITY_SAMPLES_BOUNDS = (100, 5000)
MAX_REQUEST_ID_LEN = 256
MAX_SEED = 2**31  # reset seeds are 31-bit (``bandit_traffic.traffic.reset_seed``)
MAX_BATCH_INDEX = 2**31  # decision batch/row bound (``fold_in`` takes a uint32)
#: length of ``jax.random.key_data`` for the default PRNG implementation
KEY_DATA_LEN = int(np.prod(jax.random.key_data(jax.random.key(0)).shape))
MAX_POLICY_BATCH = 10_000  # bound on a reset's batch_size (a static jit argument)


@dataclass(frozen=True)
class InstanceError:
    request_id: str | None
    error: str


@dataclass(frozen=True)
class Decision:
    request_id: str
    levels: np.ndarray  # (G,) context level indices
    eligible: tuple[bool, ...]
    batch: int | None = None  # simulator batch index (with ``row``), contracts §2
    row: int | None = None  # row within that batch


@dataclass(frozen=True)
class Reward:
    request_id: str
    arm: int
    reward: float


@dataclass(frozen=True)
class PolicyStream:
    """The simulator's policy stream for an episode (contracts §2 ``reset``):
    ``key_data`` of ``bandit.simulate.episode_streams(episode_key)[2]`` and the
    batch size the simulator's draws are shaped by."""

    key_data: tuple[int, ...]
    batch_size: int

    def key(self) -> jax.Array:
        return jax.random.wrap_key_data(jnp.asarray(self.key_data, jnp.uint32))

    def to_json(self) -> dict[str, Any]:
        return {"key_data": list(self.key_data), "batch_size": self.batch_size}


@dataclass(frozen=True)
class Reset:
    episode: int
    seed: int
    discount: float | None = None
    policy_stream: PolicyStream | None = None


@dataclass(frozen=True)
class StateRequest:
    pass


Item = InstanceError | Decision | Reward | Reset | StateRequest


@dataclass(frozen=True)
class Batch:
    items: list[Item]
    params: LinTSParams


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)


def resolve_params(
    base: LinTSParams, parameters: Mapping[str, Any] | None
) -> LinTSParams:
    """Apply the request ``parameters`` (contracts §2), clamped to their bounds;
    non-numeric values are ignored.

    ``params`` is a static jit argument, so each distinct value compiles the
    decision kernel once; values are quantised (exploration_scale to 0.1,
    propensity_samples to 100) to keep that cache small and bounded."""
    if not parameters:
        return base
    changes: dict[str, Any] = {}
    scale = _finite(parameters.get("exploration_scale"))
    if scale is not None:
        changes["exploration_scale"] = round(
            _clamp(round(scale, 1), *EXPLORATION_SCALE_BOUNDS), 1
        )
    samples = _finite(parameters.get("propensity_samples"))
    if samples is not None:
        changes["propensity_samples"] = int(
            _clamp(round(samples / 100) * 100, *PROPENSITY_SAMPLES_BOUNDS)
        )
    return dataclasses.replace(base, **changes) if changes else base


def _finite(v: Any) -> float | None:
    """``v`` as a float if it is a finite (non-bool) number, else None."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v) if math.isfinite(v) else None


def _reset_discount(inst: Mapping[str, Any]) -> float | None:
    """A reset's optional per-run ``discount`` (contracts §2 / §10): absent or
    null = the config's ``policy.discount``; else a finite number in
    ``RESET_DISCOUNT_BOUNDS``, quantised to 0.001 (it is a static jit argument of
    the update kernel, so the compile cache stays bounded)."""
    raw = inst.get("discount")
    if raw is None:
        return None
    value = _finite(raw)
    lo, hi = RESET_DISCOUNT_BOUNDS
    if value is None or not lo <= value <= hi:
        raise ValueError(f"discount must be a number in [{lo}, {hi}]")
    return round(value, 3)


def _integral(v: Any) -> int | None:
    """``v`` as an int if it is an integer or an integral float (a JSON number
    may come back as a float after a protobuf round trip), else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and math.isfinite(v) and v.is_integer():
        return int(v)
    return None


def _policy_stream(inst: Mapping[str, Any]) -> PolicyStream | None:
    """A reset's optional ``policy_key`` + ``batch_size`` (contracts §2). Both
    absent: None (the legacy per-call key). Otherwise ``policy_key`` is the
    ``jax.random.key_data`` (uint32 words) of the simulator's policy stream and
    ``batch_size`` the simulator batch size."""
    raw_key, raw_bs = inst.get("policy_key"), inst.get("batch_size")
    if raw_key is None and raw_bs is None:
        return None
    msg = f"policy_key must be a list of {KEY_DATA_LEN} uint32 words"
    if not isinstance(raw_key, list) or len(raw_key) != KEY_DATA_LEN:
        raise ValueError(msg)
    words: list[int] = []
    for w in raw_key:
        v = _integral(w)
        if v is None or not 0 <= v < 2**32:
            raise ValueError(msg)
        words.append(v)
    bs = _integral(raw_bs)
    if bs is None or not 1 <= bs <= MAX_POLICY_BATCH:
        raise ValueError(f"batch_size must be an integer in [1, {MAX_POLICY_BATCH}]")
    return PolicyStream(tuple(words), bs)


def _batch_row(inst: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """A decision's optional ``batch`` + ``row`` (both or neither, ints in
    [0, 2**31): ``batch`` is folded into a uint32 PRNG key and ``row`` indexes an
    int32 array, so a larger value would raise ``OverflowError`` in ``predict``)."""
    raw_b, raw_r = inst.get("batch"), inst.get("row")
    if raw_b is None and raw_r is None:
        return None, None
    b, r = _integral(raw_b), _integral(raw_r)
    if (
        b is None
        or r is None
        or not (0 <= b < MAX_BATCH_INDEX and 0 <= r < MAX_BATCH_INDEX)
    ):
        raise ValueError("batch and row must both be integers in [0, 2**31)")
    return b, r


def _reward_value(inst: Mapping[str, Any], reward_mode: str) -> float:
    """A reward's ``reward`` checked against ``clicked`` and the reward mode
    (contracts §2): ``click`` mode 0 or 1 and equal to ``clicked``; ``engaged``
    mode dwell seconds in [0, ``MAX_DWELL_S``], 0 without a click. A click may
    carry a zero dwell: the simulator's Exponential(1) draw is exactly 0 when its
    uniform is 0. Unbounded values would overflow the float32 posterior."""
    reward = _finite(inst.get("reward"))
    if reward is None:
        raise ValueError("reward must be a finite number")
    clicked = inst.get("clicked")
    if clicked not in (0, 1) or not isinstance(clicked, int | bool):
        raise ValueError("clicked must be 0 or 1")
    if reward_mode == "engaged":
        if not 0.0 <= reward <= MAX_DWELL_S:
            raise ValueError(
                f"reward must be dwell seconds in [0, {MAX_DWELL_S:g}] in engaged mode"
            )
        if reward > 0 and not clicked:
            raise ValueError("reward must be 0 when clicked is 0")
    elif reward != clicked:
        raise ValueError("reward must equal clicked (0 or 1) in click mode")
    return reward


def _request_id(inst: Mapping[str, Any]) -> str:
    rid = inst.get("request_id")
    if not isinstance(rid, str) or not rid or len(rid) > MAX_REQUEST_ID_LEN:
        raise ValueError(
            f"request_id must be a non-empty string <= {MAX_REQUEST_ID_LEN}"
        )
    return rid


def _int_field(
    inst: Mapping[str, Any],
    name: str,
    minimum: int | None = None,
    below: int | None = None,
) -> int:
    v = inst.get(name)
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and v < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if below is not None and v >= below:
        raise ValueError(f"{name} must be < {below}")
    return v


def parse_instance(
    inst: Any, arm_index: Mapping[str, int], cfg: ExperimentConfig
) -> Item:
    """One raw instance -> its typed item; any content error -> ``InstanceError``.

    ``arm_index`` maps each creative id to its arm index, in arm order."""
    if not isinstance(inst, Mapping):
        return InstanceError(None, "instance must be an object")
    rid = inst.get("request_id")
    rid = rid if isinstance(rid, str) else None
    try:
        kind = inst.get("type")
        if kind == "decision":
            return _parse_decision(inst, arm_index)
        if kind == "reward":
            return _parse_reward(inst, arm_index, cfg.reward_mode)
        if kind == "reset":
            return _parse_reset(inst, cfg.batch_size)
        if kind == "state":
            return StateRequest()
        raise ValueError(f"unknown instance type {kind!r}")
    except (ValueError, TypeError, OverflowError) as exc:
        # whatever the content, a bad instance never fails the request
        return InstanceError(rid, str(exc))


def _parse_reset(inst: Mapping[str, Any], batch_size: int) -> Reset:
    episode = _int_field(inst, "episode", 0)
    seed = _int_field(inst, "seed", 0, MAX_SEED)
    discount = _reset_discount(inst)
    stream = _policy_stream(inst)
    if stream is not None and stream.batch_size != batch_size:
        # a static jit argument: only the config's value (warmed up) compiles
        raise ValueError(
            f"batch_size must equal the experiment's batch_size {batch_size}"
        )
    return Reset(episode, seed, discount, stream)


def _parse_decision(inst: Mapping[str, Any], arm_index: Mapping[str, int]) -> Decision:
    rid = _request_id(inst)
    ctx = inst.get("context")
    if not isinstance(ctx, Mapping):
        raise ValueError("context must be an object")
    levels = context_levels(ctx)
    eligible_ids = inst.get("eligible_arms")
    if eligible_ids is None:
        eligible = (True,) * len(arm_index)
    else:
        if not isinstance(eligible_ids, list) or not eligible_ids:
            raise ValueError("eligible_arms must be a non-empty list of creative ids")
        if not all(isinstance(a, str) for a in eligible_ids):
            raise ValueError("eligible_arms must be creative id strings")
        unknown = [a for a in eligible_ids if a not in arm_index]
        if unknown:
            raise ValueError(f"unknown eligible_arms: {unknown}")
        eligible = tuple(cid in eligible_ids for cid in arm_index)
    batch, row = _batch_row(inst)
    return Decision(rid, levels, eligible, batch, row)


def _parse_reward(
    inst: Mapping[str, Any], arm_index: Mapping[str, int], reward_mode: str
) -> Reward:
    rid = _request_id(inst)
    arm = inst.get("arm")
    if not isinstance(arm, str):
        raise ValueError("arm must be a creative id string")
    if arm not in arm_index:
        raise ValueError(f"unknown arm {arm!r}")
    reward = _reward_value(inst, reward_mode)
    dwell = inst.get("dwell_s")
    if dwell is not None and _finite(dwell) is None:
        raise ValueError("dwell_s must be a finite number")
    return Reward(rid, arm_index[arm], reward)
