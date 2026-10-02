"""``BanditPredictor``: the CPR predictor behind a bandit endpoint (contracts §2).

One process holds one linear-TS posterior (``bandit.linear_ts``) for one
experiment, so the container must run a single web worker
(``VERTEX_CPR_WEB_CONCURRENCY=1``, set by ``deployment/bandit/endpoint.py``).

Request flow (CPR's ``PredictionHandler`` calls
``postprocess(predict(preprocess(body)))`` and JSON-serialises the result as the
response body, so ``postprocess`` returns the full ``{"predictions": [...]}``):

- ``preprocess`` validates every instance on its own; a bad instance becomes a
  per-instance ``{"type": "error", ...}`` and never fails the batch. Only a
  malformed request envelope is a 400.
- ``predict`` walks the instances in order, batching each maximal run of
  consecutive ``decision`` (or ``reward``) instances: decisions are encoded with
  ``bandit.features``, Thompson-sampled with ``linear_ts.select`` and given Monte
  Carlo ``propensities``; rewards are de-duplicated against the bounded
  ``pending`` map (request_id -> (arm, x)) / seen-id set and applied as one
  batched ``linear_ts.update``, which bumps ``model_version``.
- Checkpoints (``checkpoints/<model_version>.npz`` + ``checkpoints/latest.json``
  under ``AIP_STORAGE_URI``) are written by a background thread every
  ``BANDIT_CHECKPOINT_EVERY`` update batches or ``BANDIT_CHECKPOINT_SECONDS``
  seconds, and on reset. A failed write is logged and never blocks serving.

All state reads/mutations happen under one ``threading.Lock``.
"""

from __future__ import annotations

import dataclasses
import functools
import io
import json
import logging
import math
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import jax
import jax.numpy as jnp
import numpy as np
from fastapi import HTTPException
from google.cloud.aiplatform.prediction.predictor import Predictor

from bandit import linear_ts as lts
from bandit.config import (
    ExperimentConfig,
    LinTSParams,
    load_experiment_config,
    reward_scale,
    scenario_noise_var,
)
from bandit.features import DIM, FEATURE_SPEC_VERSION, context_levels, levels_to_matrix

log = logging.getLogger(__name__)

POLICY_NAME = "linear_ts"
EXPLORATION_SCALE_BOUNDS = (0.1, 5.0)
PROPENSITY_SAMPLES_BOUNDS = (100, 5000)
MAX_REQUEST_ID_LEN = 256
MIN_BUCKET = 16  # pad batches to powers of two >= this (bounded jit recompiles)
MAX_CHUNK = 1024  # rows per decision kernel call (bounds the MC propensity tensor)


# ----------------------------------------------------------------------- storage


class Storage(Protocol):
    def read_bytes(self, rel: str) -> bytes | None: ...
    def write_bytes(self, rel: str, data: bytes) -> None: ...


class LocalStorage:
    """A local directory (tests, the CPR local endpoint's mounted model dir)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def read_bytes(self, rel: str) -> bytes | None:
        path = self.root / rel
        return path.read_bytes() if path.exists() else None

    def write_bytes(self, rel: str, data: bytes) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)


class GcsStorage:
    """A ``gs://bucket/prefix`` (Vertex sets ``AIP_STORAGE_URI`` to this)."""

    def __init__(self, uri: str) -> None:
        from google.cloud import storage  # lazy: only on Vertex

        bucket, _, prefix = uri.removeprefix("gs://").partition("/")
        self._bucket = storage.Client().bucket(bucket)
        self._prefix = prefix.strip("/")

    def _blob(self, rel: str) -> Any:
        return self._bucket.blob(f"{self._prefix}/{rel}" if self._prefix else rel)

    def read_bytes(self, rel: str) -> bytes | None:
        blob = self._blob(rel)
        return blob.download_as_bytes() if blob.exists() else None

    def write_bytes(self, rel: str, data: bytes) -> None:
        self._blob(rel).upload_from_string(data)


def open_storage(artifacts_uri: str) -> Storage:
    if artifacts_uri.startswith("gs://"):
        return GcsStorage(artifacts_uri)
    return LocalStorage(artifacts_uri.removeprefix("file://"))


# --------------------------------------------------------------------- instances


@dataclass(frozen=True)
class _Error:
    request_id: str | None
    error: str


@dataclass(frozen=True)
class _Decision:
    request_id: str
    levels: np.ndarray  # (G,) context level indices
    eligible: tuple[bool, ...]


@dataclass(frozen=True)
class _Reward:
    request_id: str
    arm: int
    reward: float


@dataclass(frozen=True)
class _Reset:
    episode: int
    seed: int


@dataclass(frozen=True)
class _StateReq:
    pass


_Item = _Error | _Decision | _Reward | _Reset | _StateReq


@dataclass(frozen=True)
class Batch:
    items: list[_Item]
    params: LinTSParams


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)


def resolve_params(
    base: LinTSParams, parameters: Mapping[str, Any] | None
) -> LinTSParams:
    """Apply the request ``parameters`` (contracts §2), clamped to their bounds;
    non-numeric values are ignored."""
    if not parameters:
        return base
    changes: dict[str, Any] = {}
    scale = _finite(parameters.get("exploration_scale"))
    if scale is not None:
        changes["exploration_scale"] = _clamp(scale, *EXPLORATION_SCALE_BOUNDS)
    samples = _finite(parameters.get("propensity_samples"))
    if samples is not None:
        changes["propensity_samples"] = int(
            _clamp(int(samples), *PROPENSITY_SAMPLES_BOUNDS)
        )
    return dataclasses.replace(base, **changes) if changes else base


def _finite(v: Any) -> float | None:
    """``v`` as a float if it is a finite (non-bool) number, else None."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v) if math.isfinite(v) else None


def _request_id(inst: Mapping[str, Any]) -> str:
    rid = inst.get("request_id")
    if not isinstance(rid, str) or not rid or len(rid) > MAX_REQUEST_ID_LEN:
        raise ValueError(
            f"request_id must be a non-empty string <= {MAX_REQUEST_ID_LEN}"
        )
    return rid


def _int_field(inst: Mapping[str, Any], name: str, minimum: int | None = None) -> int:
    v = inst.get(name)
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and v < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return v


# ------------------------------------------------------------------- jit kernels


def _bucket(n: int) -> int:
    return max(MIN_BUCKET, 1 << max(n - 1, 0).bit_length())


def _pad_rows(a: np.ndarray, rows: int) -> np.ndarray:
    pad = [(0, rows - a.shape[0])] + [(0, 0)] * (a.ndim - 1)
    return np.pad(a, pad)


@functools.partial(jax.jit, static_argnames=("params",))
def _decide_kernel(key, state, X, eligible, params):
    k_sel, k_prop = jax.random.split(key)
    arms, _ = lts.select(k_sel, state, X, params, eligible)
    probs = lts.propensities_batch(k_prop, state, X, params, eligible)
    greedy_scores = jnp.where(eligible, X @ lts.posterior_mean(state).T, -jnp.inf)
    return arms, probs, jnp.argmax(greedy_scores, axis=-1)


@functools.partial(jax.jit, static_argnames=("params",))
def _update_kernel(state, arms, X, rewards, valid, params):
    """``lts.update`` on zero-padded rows; padding adds nothing to Λ/b and is
    excluded from the pull counts and step."""
    new = lts.update(state, arms, X, rewards, params)
    counts = jnp.zeros_like(state.n).at[arms].add(valid.astype(state.n.dtype))
    return new._replace(n=state.n + counts, step=state.step + jnp.sum(valid))


# --------------------------------------------------------------------- predictor


def _env_number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def load_serving_config(raw: Mapping[str, Any]) -> ExperimentConfig:
    """``experiment.json`` -> ``ExperimentConfig``, filling an absent/None
    ``policy.noise_var`` with the calibrated ``scenario_noise_var`` (contracts §7)."""
    data = dict(raw)
    policy = dict(data.get("policy") or {})
    if policy.get("noise_var") is None:
        policy["noise_var"] = scenario_noise_var(
            str(data.get("scenario")),
            str(data.get("ctr_mode", "demo")),
            str(data.get("reward_mode", "click")),
        )
    data["policy"] = policy
    return load_experiment_config(data)


class BanditPredictor(Predictor):
    """Contracts §2 decision/reward/reset/state endpoint over one LinTS posterior."""

    def __init__(
        self,
        *,
        checkpoint_every: int | None = None,
        checkpoint_seconds: float | None = None,
        max_pending: int | None = None,
        max_seen: int | None = None,
    ) -> None:
        super().__init__()
        self.checkpoint_every = int(
            checkpoint_every
            if checkpoint_every is not None
            else _env_number("BANDIT_CHECKPOINT_EVERY", 50)
        )
        self.checkpoint_seconds = float(
            checkpoint_seconds
            if checkpoint_seconds is not None
            else _env_number("BANDIT_CHECKPOINT_SECONDS", 120.0)
        )
        self.max_pending = int(
            max_pending
            if max_pending is not None
            else _env_number("BANDIT_MAX_PENDING", 200_000)
        )
        self.max_seen = int(
            max_seen
            if max_seen is not None
            else _env_number("BANDIT_MAX_SEEN", 400_000)
        )
        self._lock = threading.Lock()
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ckpt")
        self._last_write: Future[None] | None = None

    # -------------------------------------------------------------- lifecycle

    def load(self, artifacts_uri: str, **kwargs: Any) -> None:
        workers = os.environ.get("WEB_CONCURRENCY")
        if workers not in (None, "1"):
            log.warning(
                "WEB_CONCURRENCY=%s: each worker holds its own posterior; deploy "
                "with VERTEX_CPR_WEB_CONCURRENCY=1",
                workers,
            )
        self._storage = open_storage(artifacts_uri)
        raw = self._storage.read_bytes("experiment.json")
        if raw is None:
            raise FileNotFoundError(f"no experiment.json under {artifacts_uri}")
        cfg = load_serving_config(json.loads(raw))
        self.config = cfg
        self.params = cfg.policy
        self.arm_ids = [a.creative_id for a in cfg.arms]
        self._arm_index = {cid: i for i, cid in enumerate(self.arm_ids)}
        self._reward_scale = reward_scale(cfg.scenario, cfg.reward_mode)
        self._dim = DIM
        with self._lock:
            self._fresh(episode=0, seed=cfg.seed)
            self._restore()
        log.info(
            "bandit predictor loaded: experiment=%s arms=%s version=%s noise_var=%s",
            cfg.experiment_id,
            self.arm_ids,
            self._version(),
            self.params.noise_var,
        )

    def _fresh(self, episode: int, seed: int) -> None:
        self._state = lts.init_state(
            len(self.arm_ids), self._dim, self.params.prior_var
        )
        self._episode = episode
        self._seed = seed
        self._base_key = jax.random.key(seed)
        self._calls = 0  # PRNG fold-in counter
        self._n_updates = 0
        self._pending: OrderedDict[str, tuple[int, np.ndarray]] = OrderedDict()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._updates_since_ckpt = 0
        self._last_ckpt = time.monotonic()

    def _version(self) -> str:
        return f"{self.config.experiment_id}-e{self._episode}-v{self._n_updates}"

    def _next_key(self) -> jax.Array:
        self._calls += 1
        return jax.random.fold_in(self._base_key, self._calls)

    # ------------------------------------------------------------ checkpoints

    def _restore(self) -> None:
        try:
            meta_raw = self._storage.read_bytes("checkpoints/latest.json")
            if meta_raw is None:
                return
            meta = json.loads(meta_raw)
            if meta.get("experiment_id") != self.config.experiment_id:
                log.warning(
                    "ignoring checkpoint for experiment %s", meta.get("experiment_id")
                )
                return
            npz_raw = self._storage.read_bytes(f"checkpoints/{meta['npz']}")
            if npz_raw is None:
                log.warning("checkpoint %s missing; starting fresh", meta["npz"])
                return
            arrays = np.load(io.BytesIO(npz_raw))
            state = lts.LinTSState(
                precision=jnp.asarray(arrays["precision"], jnp.float32),
                b=jnp.asarray(arrays["b"], jnp.float32),
                n=jnp.asarray(arrays["n"], jnp.int32),
                step=jnp.asarray(arrays["step"], jnp.int32),
            )
            if state.b.shape != (len(self.arm_ids), self._dim):
                log.warning(
                    "checkpoint shape %s mismatches config; ignoring", state.b.shape
                )
                return
            self._fresh(episode=int(meta["episode"]), seed=int(meta["seed"]))
            self._state = state
            self._n_updates = int(meta["n_updates"])
            self._calls = int(meta["calls"])
            log.info("restored checkpoint %s", meta["model_version"])
        except Exception:
            log.exception("checkpoint restore failed; starting fresh")

    def _schedule_checkpoint(self) -> None:
        """Snapshot under the lock; write in the background (never raises)."""
        version = self._version()
        buf = io.BytesIO()
        np.savez(
            buf,
            precision=np.asarray(self._state.precision),
            b=np.asarray(self._state.b),
            n=np.asarray(self._state.n),
            step=np.asarray(self._state.step),
        )
        npz_name = f"{version}.npz"
        meta = {
            "experiment_id": self.config.experiment_id,
            "model_version": version,
            "npz": npz_name,
            "episode": self._episode,
            "seed": self._seed,
            "n_updates": self._n_updates,
            "calls": self._calls,
            "feature_spec_version": FEATURE_SPEC_VERSION,
            "saved_at": time.time(),
        }
        self._updates_since_ckpt = 0
        self._last_ckpt = time.monotonic()
        storage = self._storage

        def write() -> None:
            try:
                storage.write_bytes(f"checkpoints/{npz_name}", buf.getvalue())
                storage.write_bytes(
                    "checkpoints/latest.json", json.dumps(meta).encode("utf-8")
                )
            except Exception:
                log.exception(
                    "checkpoint write failed for %s (serving continues)", version
                )

        try:
            self._last_write = self._writer.submit(write)
        except RuntimeError:
            log.exception("checkpoint writer unavailable")

    def flush(self, timeout: float | None = 30.0) -> None:
        """Wait for queued checkpoint writes (tests, shutdown)."""
        fut = self._last_write
        if fut is not None:
            fut.result(timeout=timeout)

    def _maybe_checkpoint(self) -> None:
        due_count = self._updates_since_ckpt >= self.checkpoint_every
        due_time = time.monotonic() - self._last_ckpt >= self.checkpoint_seconds
        if self._updates_since_ckpt and (due_count or due_time):
            self._schedule_checkpoint()

    # ------------------------------------------------------------ CPR methods

    def preprocess(self, prediction_input: Any) -> Batch:
        if not isinstance(prediction_input, Mapping):
            raise HTTPException(
                status_code=400, detail="request body must be an object"
            )
        instances = prediction_input.get("instances")
        parameters = prediction_input.get("parameters")
        if not isinstance(instances, list):
            raise HTTPException(status_code=400, detail="'instances' must be a list")
        if parameters is not None and not isinstance(parameters, Mapping):
            raise HTTPException(
                status_code=400, detail="'parameters' must be an object"
            )
        return Batch(
            items=[self._parse(inst) for inst in instances],
            params=resolve_params(self.params, parameters),
        )

    def _parse(self, inst: Any) -> _Item:
        if not isinstance(inst, Mapping):
            return _Error(None, "instance must be an object")
        rid = inst.get("request_id")
        rid = rid if isinstance(rid, str) else None
        try:
            kind = inst.get("type")
            if kind == "decision":
                return self._parse_decision(inst)
            if kind == "reward":
                return self._parse_reward(inst)
            if kind == "reset":
                return _Reset(_int_field(inst, "episode", 0), _int_field(inst, "seed"))
            if kind == "state":
                return _StateReq()
            raise ValueError(f"unknown instance type {kind!r}")
        except ValueError as exc:
            return _Error(rid, str(exc))

    def _parse_decision(self, inst: Mapping[str, Any]) -> _Decision:
        rid = _request_id(inst)
        ctx = inst.get("context")
        if not isinstance(ctx, Mapping):
            raise ValueError("context must be an object")
        levels = context_levels(ctx)
        eligible_ids = inst.get("eligible_arms")
        if eligible_ids is None:
            eligible = (True,) * len(self.arm_ids)
        else:
            if not isinstance(eligible_ids, list) or not eligible_ids:
                raise ValueError(
                    "eligible_arms must be a non-empty list of creative ids"
                )
            unknown = [a for a in eligible_ids if a not in self._arm_index]
            if unknown:
                raise ValueError(f"unknown eligible_arms: {unknown}")
            eligible = tuple(cid in eligible_ids for cid in self.arm_ids)
        return _Decision(rid, levels, eligible)

    def _parse_reward(self, inst: Mapping[str, Any]) -> _Reward:
        rid = _request_id(inst)
        arm = inst.get("arm")
        if arm not in self._arm_index:
            raise ValueError(f"unknown arm {arm!r}")
        reward = _finite(inst.get("reward"))
        if reward is None:
            raise ValueError("reward must be a finite number")
        clicked = inst.get("clicked")
        if clicked not in (0, 1) or not isinstance(clicked, int | bool):
            raise ValueError("clicked must be 0 or 1")
        dwell = inst.get("dwell_s")
        if dwell is not None and _finite(dwell) is None:
            raise ValueError("dwell_s must be a finite number")
        return _Reward(rid, self._arm_index[str(arm)], reward)

    def predict(self, instances: Batch) -> list[dict[str, Any]]:
        items = instances.items
        out: list[dict[str, Any] | None] = [None] * len(items)
        with self._lock:
            i = 0
            while i < len(items):
                item = items[i]
                if isinstance(item, _Decision | _Reward):
                    j = i
                    while j < len(items) and type(items[j]) is type(item):
                        j += 1
                    idx = list(range(i, j))
                    run = [items[k] for k in idx]
                    if isinstance(item, _Decision):
                        decisions = [d for d in run if isinstance(d, _Decision)]
                        results = self._decide(decisions, instances.params)
                    else:
                        rewards = [r for r in run if isinstance(r, _Reward)]
                        results = self._apply_rewards(rewards)
                    for k, res in zip(idx, results, strict=True):
                        out[k] = res
                    i = j
                    continue
                if isinstance(item, _Reset):
                    self._fresh(item.episode, item.seed)
                    self._schedule_checkpoint()
                    out[i] = {
                        "type": "reset",
                        "episode": item.episode,
                        "model_version": self._version(),
                    }
                elif isinstance(item, _StateReq):
                    out[i] = self._summary()
                else:
                    assert isinstance(item, _Error)
                    out[i] = {
                        "type": "error",
                        "request_id": item.request_id,
                        "error": item.error,
                    }
                i += 1
        return [o for o in out if o is not None]

    def postprocess(self, prediction_results: list[dict[str, Any]]) -> dict[str, Any]:
        return {"predictions": prediction_results}

    # ------------------------------------------------------------- handlers

    def _decide(
        self, decisions: Sequence[_Decision], params: LinTSParams
    ) -> list[dict]:
        t0 = time.perf_counter()
        results: list[dict | None] = [None] * len(decisions)
        groups: dict[tuple[bool, ...], list[int]] = {}
        for k, d in enumerate(decisions):
            groups.setdefault(d.eligible, []).append(k)
        version, step = self._version(), int(self._state.step)
        rows: list[tuple[int, int, np.ndarray, np.ndarray, int]] = []
        chunks = [
            (eligible, idx[c : c + MAX_CHUNK])
            for eligible, idx in groups.items()
            for c in range(0, len(idx), MAX_CHUNK)
        ]
        for eligible, idx in chunks:
            X = levels_to_matrix(np.stack([decisions[k].levels for k in idx]))
            n = X.shape[0]
            arms, probs, greedy = _decide_kernel(
                self._next_key(),
                self._state,
                jnp.asarray(_pad_rows(X, _bucket(n))),
                jnp.asarray(eligible),
                params,
            )
            arms_np = np.asarray(arms)[:n]
            probs_np = np.asarray(probs)[:n]
            greedy_np = np.asarray(greedy)[:n]
            for r, k in enumerate(idx):
                rows.append((k, int(arms_np[r]), probs_np[r], X[r], int(greedy_np[r])))
        latency_ms = (time.perf_counter() - t0) * 1000.0
        for k, arm, prob, x, greedy in rows:
            d = decisions[k]
            self._remember(d.request_id, arm, x)
            results[k] = {
                "request_id": d.request_id,
                "type": "decision",
                "chosen_arm": self.arm_ids[arm],
                "arm_index": arm,
                "propensity": float(prob[arm]),
                "arm_probabilities": {
                    cid: float(prob[a]) for a, cid in enumerate(self.arm_ids)
                },
                "explored": arm != greedy,
                "model_version": version,
                "policy": POLICY_NAME,
                "episode": self._episode,
                "step": step,
                "latency_ms": round(latency_ms, 3),
            }
        return [r for r in results if r is not None]

    def _remember(self, request_id: str, arm: int, x: np.ndarray) -> None:
        self._pending[request_id] = (arm, x)
        self._pending.move_to_end(request_id)
        while len(self._pending) > self.max_pending:
            self._pending.popitem(last=False)

    def _apply_rewards(self, rewards: Sequence[_Reward]) -> list[dict]:
        verdicts: list[str | None] = []
        arms: list[int] = []
        xs: list[np.ndarray] = []
        values: list[float] = []
        for r in rewards:
            reason = None
            if r.request_id in self._seen:
                reason = "duplicate"
            elif r.request_id not in self._pending:
                reason = "unknown request_id"
            elif self._pending[r.request_id][0] != r.arm:
                reason = "arm does not match the decision"
            if reason is None:
                arm, x = self._pending.pop(r.request_id)
                self._seen[r.request_id] = None
                while len(self._seen) > self.max_seen:
                    self._seen.popitem(last=False)
                arms.append(arm)
                xs.append(x)
                values.append(r.reward / self._reward_scale)
            verdicts.append(reason)
        if arms:
            n = len(arms)
            size = _bucket(n)
            self._state = _update_kernel(
                self._state,
                jnp.asarray(_pad_rows(np.asarray(arms, np.int32), size)),
                jnp.asarray(_pad_rows(np.stack(xs), size)),
                jnp.asarray(_pad_rows(np.asarray(values, np.float32), size)),
                jnp.asarray(np.arange(size) < n),
                self.params,
            )
            self._n_updates += 1
            self._updates_since_ckpt += 1
            self._maybe_checkpoint()
        version = self._version()
        out = []
        for r, reason in zip(rewards, verdicts, strict=True):
            ack: dict[str, Any] = {
                "type": "reward",
                "request_id": r.request_id,
                "accepted": reason is None,
                "model_version": version,
            }
            if reason is not None:
                ack["reason"] = reason
            out.append(ack)
        return out

    def _summary(self) -> dict[str, Any]:
        mu = np.asarray(lts.posterior_mean(self._state))
        pulls = np.asarray(self._state.n)
        return {
            "type": "state",
            "episode": self._episode,
            "step": int(self._state.step),
            "model_version": self._version(),
            "pulls": {cid: int(pulls[a]) for a, cid in enumerate(self.arm_ids)},
            "posterior_mean": {
                cid: [float(v) for v in mu[a]] for a, cid in enumerate(self.arm_ids)
            },
            "feature_spec_version": FEATURE_SPEC_VERSION,
        }
