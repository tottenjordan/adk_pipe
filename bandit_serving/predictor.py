"""``BanditPredictor``: the CPR predictor behind a bandit endpoint (contracts §2).

One process holds one linear-TS posterior (``bandit.linear_ts``) for one
experiment, so the container must run a single web worker
(``VERTEX_CPR_WEB_CONCURRENCY=1``, set by ``deployment/bandit/endpoint.py``).

Request flow (CPR's ``PredictionHandler`` calls
``postprocess(predict(preprocess(body)))`` and JSON-serialises the result as the
response body, so ``postprocess`` returns the full ``{"predictions": [...]}``):

- ``preprocess`` validates every instance on its own; a bad instance becomes a
  per-instance ``{"type": "error", ...}`` and never fails the batch. Only a
  malformed request envelope is a 400. Instance content (incl. the reward bounds
  per reward mode) is validated here, before any state change, so ``predict``
  can't raise on it (only ``row < batch_size`` waits for the reset in force).
- ``predict`` walks the instances in order, batching each maximal run of
  consecutive ``decision`` (or ``reward``) instances: decisions are encoded with
  ``bandit.features``, Thompson-sampled with ``linear_ts.select`` and given Monte
  Carlo ``propensities``; rewards are de-duplicated against the bounded
  ``pending`` map (request_id -> (arm, x)) / seen-id set and applied as one
  batched ``linear_ts.update``, which bumps ``model_version``. An update that
  would leave the posterior non-finite is refused and the old state kept.
- ``reset`` starts a fresh posterior; its optional ``discount`` (the traffic
  run's forgetting γ, contracts §10) replaces ``policy.discount`` for every
  update until the next reset, and a reset without one restores the config value.
  Its optional ``policy_key`` + ``batch_size`` (the simulator's policy stream)
  key every decision carrying ``batch``/``row`` exactly like ``bandit.simulate``
  (``fold_in(k_pol, batch)``, noise at the simulator batch shape), so the
  endpoint's arms match a local LinTS replay round for round (contracts §2).
- ``experiment.json`` is read from ``AIP_STORAGE_URI``, which on Vertex is a
  read-only copy of the model artifacts in a Vertex-managed bucket. Checkpoints
  (``<model_version>.npz`` + ``latest.json``) therefore go to the separate,
  writable ``BANDIT_CHECKPOINT_URI`` (set by ``deployment/bandit/endpoint.py`` to
  ``<artifact_uri>/checkpoints/``) and are restored from there on load. They are
  written by a background thread every ``BANDIT_CHECKPOINT_EVERY`` update batches
  or ``BANDIT_CHECKPOINT_SECONDS`` seconds, and on reset (the ``.npz`` before
  ``latest.json``); only the newest ``CHECKPOINT_KEEP`` ``.npz`` files are kept.
  Without ``BANDIT_CHECKPOINT_URI`` checkpointing is off (one startup warning). A
  failed write logs one concise warning and never blocks serving.

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
import re
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
    MAX_DWELL_S,
    RESET_DISCOUNT_BOUNDS,
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
MAX_INSTANCES = 1000  # per request (contracts §2; the traffic client splits at 500)
MIN_BUCKET = 16  # pad batches to powers of two >= this (bounded jit recompiles)
MAX_CHUNK = 1024  # rows per decision kernel call (bounds the MC propensity tensor)
MAX_SEED = 2**31  # reset seeds are 31-bit (``bandit_traffic.traffic.reset_seed``)
MAX_BATCH_INDEX = 2**31  # decision batch/row bound (``fold_in`` takes a uint32)
CHECKPOINT_KEEP = 5  # newest checkpoint .npz files kept under BANDIT_CHECKPOINT_URI
CHECKPOINT_ENV = "BANDIT_CHECKPOINT_URI"
MAX_LOGGED_ERROR = 300  # chars of a storage error kept in a warning
MAX_DISTINCT_PARAMS = 16  # distinct request params per process (each one compiles)


# ----------------------------------------------------------------------- storage


class Storage(Protocol):
    def read_bytes(self, rel: str) -> bytes | None: ...
    def write_bytes(self, rel: str, data: bytes) -> None: ...
    def list_files(self, suffix: str) -> list[tuple[str, float]]:
        """``(name, modified time)`` of the files directly under the root whose
        name ends with ``suffix``."""
        ...

    def delete(self, rel: str) -> None:
        """Delete ``rel``; a missing file is fine."""
        ...


class LocalStorage:
    """A local directory (tests, the CPR local endpoint's mounted model dir)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def read_bytes(self, rel: str) -> bytes | None:
        try:  # no exists() check first: a concurrent prune may delete the file
            return (self.root / rel).read_bytes()
        except FileNotFoundError:
            return None

    def write_bytes(self, rel: str, data: bytes) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def list_files(self, suffix: str) -> list[tuple[str, float]]:
        if not self.root.is_dir():
            return []
        return [
            (f.name, f.stat().st_mtime)
            for f in self.root.iterdir()
            if f.is_file() and f.name.endswith(suffix)
        ]

    def delete(self, rel: str) -> None:
        (self.root / rel).unlink(missing_ok=True)


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
        from google.api_core.exceptions import NotFound

        try:
            return self._blob(rel).download_as_bytes()
        except NotFound:
            return None

    def write_bytes(self, rel: str, data: bytes) -> None:
        self._blob(rel).upload_from_string(data)

    def list_files(self, suffix: str) -> list[tuple[str, float]]:
        prefix = f"{self._prefix}/" if self._prefix else ""
        out = []
        for blob in self._bucket.list_blobs(prefix=prefix):
            name = blob.name[len(prefix) :]
            if name.endswith(suffix) and "/" not in name:
                updated = getattr(blob, "updated", None)
                out.append((name, updated.timestamp() if updated else 0.0))
        return out

    def delete(self, rel: str) -> None:
        from google.api_core.exceptions import NotFound

        try:
            self._blob(rel).delete()
        except NotFound:
            pass


def open_storage(artifacts_uri: str) -> Storage:
    if artifacts_uri.startswith("gs://"):
        return GcsStorage(artifacts_uri)
    return LocalStorage(artifacts_uri.removeprefix("file://"))


def _short_error(exc: BaseException) -> str:
    """``Type: message`` on one line, capped (GCS errors carry long bodies)."""
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    if len(text) > MAX_LOGGED_ERROR:
        return text[: MAX_LOGGED_ERROR - 1] + "…"
    return text


def _natural_key(name: str) -> tuple[Any, ...]:
    """``exp-e1-v10`` sorts after ``exp-e1-v9`` (digit runs compare as numbers)."""
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name))


def prune_checkpoints(storage: Storage, keep_name: str, keep: int) -> None:
    """Delete all but the newest ``keep`` ``.npz`` files (``keep_name``, the one
    ``latest.json`` points at, always stays). Failures only log."""
    try:
        files = storage.list_files(".npz")
    except Exception as exc:
        log.warning("checkpoint prune: listing failed: %s", _short_error(exc))
        return
    files.sort(key=lambda f: (f[1], _natural_key(f[0])), reverse=True)
    kept = {keep_name}
    for name, _ in files:
        if len(kept) >= max(keep, 1):
            break
        kept.add(name)
    for name in (n for n, _ in files if n not in kept):
        try:
            storage.delete(name)
        except Exception as exc:
            log.warning(
                "checkpoint prune: delete %s failed: %s", name, _short_error(exc)
            )


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
    batch: int | None = None  # simulator batch index (with ``row``), contracts §2
    row: int | None = None  # row within that batch


@dataclass(frozen=True)
class _Reward:
    request_id: str
    arm: int
    reward: float


@dataclass(frozen=True)
class _PolicyStream:
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
class _Reset:
    episode: int
    seed: int
    discount: float | None = None
    policy_stream: _PolicyStream | None = None


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


#: length of ``jax.random.key_data`` for the default PRNG implementation
KEY_DATA_LEN = int(np.prod(jax.random.key_data(jax.random.key(0)).shape))
MAX_POLICY_BATCH = 10_000  # bound on a reset's batch_size (a static jit argument)


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


def _policy_stream(inst: Mapping[str, Any]) -> _PolicyStream | None:
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
    return _PolicyStream(tuple(words), bs)


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


@functools.partial(jax.jit, static_argnames=("params", "batch_size"))
def _decide_rows_kernel(key, state, X, rows, eligible, params, batch_size):
    """``_decide_kernel`` keyed like ``bandit.simulate``: ``key`` is the batch's
    ``fold_in(k_pol, batch)``, and the selection noise is drawn at the simulator's
    ``(batch_size, K)`` shape and indexed by each served row's ``rows`` (padding
    rows read row 0 and are discarded). A row's arm therefore doesn't depend on
    padding, request splitting or retries."""
    k_sel, k_prop = jax.random.split(key)
    z = jax.random.normal(k_sel, (batch_size, state.b.shape[0]), state.b.dtype)
    arms, _ = lts.select_with_noise(state, X, z[rows], params, eligible)
    probs = lts.propensities_batch(k_prop, state, X, params, eligible)
    greedy_scores = jnp.where(eligible, X @ lts.posterior_mean(state).T, -jnp.inf)
    return arms, probs, jnp.argmax(greedy_scores, axis=-1)


@functools.partial(jax.jit, static_argnames=("params", "reward_scale"))
def _update_kernel(state, arms, X, rewards, valid, params, reward_scale=1.0):
    """``lts.update`` on zero-padded rows; padding adds nothing to Λ/b and is
    excluded from the pull counts and step. The raw float32 rewards are divided
    by ``reward_scale`` in here: the same float32 op as ``simulate._episode``.
    Returns ``(new_state, finite)``: ``finite`` is False when the update left Λ
    or b non-finite, and the caller then keeps the previous state."""
    new = lts.update(state, arms, X, rewards / reward_scale, params)
    counts = jnp.zeros_like(state.n).at[arms].add(valid.astype(state.n.dtype))
    new = new._replace(n=state.n + counts, step=state.step + jnp.sum(valid))
    return new, _state_finite(new)


def _state_finite(state: lts.LinTSState) -> jax.Array:
    return jnp.all(jnp.isfinite(state.precision)) & jnp.all(jnp.isfinite(state.b))


_posterior_mean = jax.jit(lts.posterior_mean)


# --------------------------------------------------------------------- predictor


def warmup_buckets() -> list[int]:
    """Padded batch sizes to pre-compile (``BANDIT_WARMUP_BUCKETS``; default
    16..128 covers the traffic job's 100-round batches); ``BANDIT_WARMUP=0``
    disables warm-up."""
    if os.environ.get("BANDIT_WARMUP", "1") == "0":
        return []
    raw = os.environ.get("BANDIT_WARMUP_BUCKETS", "16,32,64,128")
    sizes = {_bucket(int(v)) for v in raw.split(",") if v.strip().isdigit()}
    return sorted(min(size, MAX_CHUNK) for size in sizes)


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
        checkpoint_uri: str | None = None,
        checkpoint_every: int | None = None,
        checkpoint_seconds: float | None = None,
        max_pending: int | None = None,
        max_seen: int | None = None,
    ) -> None:
        super().__init__()
        # ``None`` -> the env var; ``""`` -> checkpointing off.
        self.checkpoint_uri = (
            checkpoint_uri
            if checkpoint_uri is not None
            else os.environ.get(CHECKPOINT_ENV, "")
        ).strip()
        self._ckpt_storage: Storage | None = None
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
        if self.checkpoint_uri:
            self._ckpt_storage = open_storage(self.checkpoint_uri)
        else:
            log.warning(
                "%s is not set: posterior checkpoints are disabled, so a restart "
                "starts from the prior (AIP_STORAGE_URI is read-only on Vertex)",
                CHECKPOINT_ENV,
            )
        cfg = load_serving_config(json.loads(raw))
        self.config = cfg
        self.params = cfg.policy
        self.arm_ids = [a.creative_id for a in cfg.arms]
        self._arm_index = {cid: i for i, cid in enumerate(self.arm_ids)}
        self._known_params: set[LinTSParams] = {cfg.policy}
        self._params_cap_warned = False
        self._reward_scale = reward_scale(cfg.scenario, cfg.reward_mode)
        self._dim = DIM
        with self._lock:
            self._fresh(episode=0, seed=cfg.seed)
            self._restore()
        if warmup_buckets():
            self._warmup()
        log.info(
            "bandit predictor loaded: experiment=%s arms=%s version=%s noise_var=%s",
            cfg.experiment_id,
            self.arm_ids,
            self._version(),
            self.params.noise_var,
        )

    def _warmup(self) -> None:
        """Compile the default-params kernels for the ``BANDIT_WARMUP_BUCKETS``
        batch sizes before the model server reports healthy (a cold jit costs
        ~0.3-1 s per kernel and shape). ``BANDIT_WARMUP=0`` skips it."""
        t0 = time.perf_counter()
        k = len(self.arm_ids)
        state = lts.init_state(k, self._dim, self.params.prior_var)
        eligible = jnp.ones((k,), bool)
        for size in warmup_buckets():
            X = jnp.zeros((size, self._dim), jnp.float32)
            zeros = jnp.zeros((size,), jnp.int32)
            jax.block_until_ready(
                _decide_kernel(jax.random.key(0), state, X, eligible, self.params)
            )
            jax.block_until_ready(
                _decide_rows_kernel(
                    jax.random.key(0),
                    state,
                    X,
                    zeros,
                    eligible,
                    self.params,
                    self.config.batch_size,
                )
            )
            jax.block_until_ready(
                _update_kernel(
                    state,
                    zeros,
                    X,
                    zeros.astype(jnp.float32),
                    zeros > 0,
                    self.params,
                    self._reward_scale,
                )
            )
        jax.block_until_ready(_posterior_mean(state))
        log.info("jit warm-up: %.0f ms", (time.perf_counter() - t0) * 1000.0)

    def _fresh(
        self,
        episode: int,
        seed: int,
        discount: float | None = None,
        policy_stream: _PolicyStream | None = None,
    ) -> None:
        self._state = lts.init_state(
            len(self.arm_ids), self._dim, self.params.prior_var
        )
        self._episode = episode
        self._seed = seed
        self._discount: float | None = discount
        self._base_key = jax.random.key(seed)
        self._calls = 0  # PRNG fold-in counter (legacy, unkeyed decisions)
        self._policy_stream = policy_stream
        self._policy_key = None if policy_stream is None else policy_stream.key()
        self._n_updates = 0
        self._pending: OrderedDict[str, tuple[int, np.ndarray, int | None]] = (
            OrderedDict()
        )
        self._last_update_batch: int | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._updates_since_ckpt = 0
        self._last_ckpt = time.monotonic()

    def _update_params(self) -> LinTSParams:
        """``self.params`` with the episode's reset ``discount``, if any."""
        if self._discount is None:
            return self.params
        return dataclasses.replace(self.params, discount=self._discount)

    def _version(self) -> str:
        return f"{self.config.experiment_id}-e{self._episode}-v{self._n_updates}"

    def _next_key(self) -> jax.Array:
        self._calls += 1
        return jax.random.fold_in(self._base_key, self._calls)

    # ------------------------------------------------------------ checkpoints

    def _restore(self) -> None:
        storage = self._ckpt_storage
        if storage is None:
            return
        try:
            meta_raw = storage.read_bytes("latest.json")
            if meta_raw is None:
                return
            meta = json.loads(meta_raw)
            if meta.get("experiment_id") != self.config.experiment_id:
                log.warning(
                    "ignoring checkpoint for experiment %s", meta.get("experiment_id")
                )
                return
            npz_raw = storage.read_bytes(str(meta["npz"]))
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
            k, d = len(self.arm_ids), self._dim
            expected = {"precision": (k, d, d), "b": (k, d), "n": (k,), "step": ()}
            shapes = {name: getattr(state, name).shape for name in expected}
            if shapes != expected:
                log.warning(
                    "checkpoint shapes %s mismatch the config %s; starting fresh",
                    shapes,
                    expected,
                )
                return
            if not bool(_state_finite(state)):
                log.warning(
                    "checkpoint %s holds a non-finite posterior; starting fresh",
                    meta.get("model_version"),
                )
                return
            discount = meta.get("discount")
            raw_stream = meta.get("policy_stream")
            stream = (
                None
                if raw_stream is None
                else _PolicyStream(
                    tuple(int(w) for w in raw_stream["key_data"]),
                    int(raw_stream["batch_size"]),
                )
            )
            if stream is not None and (
                len(stream.key_data) != KEY_DATA_LEN
                or stream.batch_size != self.config.batch_size
            ):
                log.warning(
                    "checkpoint policy stream mismatches the config "
                    "(%d key words, batch_size %d); starting fresh",
                    len(stream.key_data),
                    stream.batch_size,
                )
                return
            self._fresh(
                episode=int(meta["episode"]),
                seed=int(meta["seed"]),
                discount=None if discount is None else float(discount),
                policy_stream=stream,
            )
            self._state = state
            self._n_updates = int(meta["n_updates"])
            self._calls = int(meta["calls"])
            log.info("restored checkpoint %s", meta["model_version"])
        except Exception:
            log.exception("checkpoint restore failed; starting fresh")

    def _schedule_checkpoint(self) -> None:
        """Snapshot under the lock; write in the background (never raises)."""
        self._updates_since_ckpt = 0
        self._last_ckpt = time.monotonic()
        storage = self._ckpt_storage
        if storage is None:
            return
        version = self._version()
        if not bool(_state_finite(self._state)):
            log.warning("not checkpointing %s: non-finite posterior", version)
            return
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
            "discount": self._discount,
            "policy_stream": None
            if self._policy_stream is None
            else self._policy_stream.to_json(),
            "n_updates": self._n_updates,
            "calls": self._calls,
            "feature_spec_version": FEATURE_SPEC_VERSION,
            "saved_at": time.time(),
        }

        def write() -> None:
            try:
                storage.write_bytes(npz_name, buf.getvalue())
                storage.write_bytes("latest.json", json.dumps(meta).encode("utf-8"))
            except Exception as exc:
                log.warning(
                    "checkpoint write failed for %s (serving continues): %s",
                    version,
                    _short_error(exc),
                )
                return
            prune_checkpoints(storage, npz_name, CHECKPOINT_KEEP)

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
        if len(instances) > MAX_INSTANCES:
            raise HTTPException(
                status_code=400,
                detail=f"at most {MAX_INSTANCES} instances per request; split the batch",
            )
        if parameters is not None and not isinstance(parameters, Mapping):
            raise HTTPException(
                status_code=400, detail="'parameters' must be an object"
            )
        return Batch(
            items=[self._parse(inst) for inst in instances],
            params=self._capped_params(resolve_params(self.params, parameters)),
        )

    def _capped_params(self, params: LinTSParams) -> LinTSParams:
        """``params`` unless it would be the process's ``MAX_DISTINCT_PARAMS``+1th
        distinct value (each compiles the decision kernels), then the config's
        params, with one warning per process."""
        with self._lock:
            if params in self._known_params:
                return params
            if len(self._known_params) < MAX_DISTINCT_PARAMS:
                self._known_params.add(params)
                return params
            if not self._params_cap_warned:
                self._params_cap_warned = True
                log.warning(
                    "more than %d distinct request parameters; serving the "
                    "config's parameters for new values",
                    MAX_DISTINCT_PARAMS,
                )
        return self.params

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
                return self._parse_reset(inst)
            if kind == "state":
                return _StateReq()
            raise ValueError(f"unknown instance type {kind!r}")
        except (ValueError, TypeError, OverflowError) as exc:
            # whatever the content, a bad instance never fails the request
            return _Error(rid, str(exc))

    def _parse_reset(self, inst: Mapping[str, Any]) -> _Reset:
        episode = _int_field(inst, "episode", 0)
        seed = _int_field(inst, "seed", 0, MAX_SEED)
        discount = _reset_discount(inst)
        stream = _policy_stream(inst)
        if stream is not None and stream.batch_size != self.config.batch_size:
            # a static jit argument: only the config's value (warmed up) compiles
            raise ValueError(
                f"batch_size must equal the experiment's batch_size "
                f"{self.config.batch_size}"
            )
        return _Reset(episode, seed, discount, stream)

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
            if not all(isinstance(a, str) for a in eligible_ids):
                raise ValueError("eligible_arms must be creative id strings")
            unknown = [a for a in eligible_ids if a not in self._arm_index]
            if unknown:
                raise ValueError(f"unknown eligible_arms: {unknown}")
            eligible = tuple(cid in eligible_ids for cid in self.arm_ids)
        batch, row = _batch_row(inst)
        return _Decision(rid, levels, eligible, batch, row)

    def _parse_reward(self, inst: Mapping[str, Any]) -> _Reward:
        rid = _request_id(inst)
        arm = inst.get("arm")
        if not isinstance(arm, str):
            raise ValueError("arm must be a creative id string")
        if arm not in self._arm_index:
            raise ValueError(f"unknown arm {arm!r}")
        reward = _reward_value(inst, self.config.reward_mode)
        dwell = inst.get("dwell_s")
        if dwell is not None and _finite(dwell) is None:
            raise ValueError("dwell_s must be a finite number")
        return _Reward(rid, self._arm_index[arm], reward)

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
                    self._fresh(
                        item.episode, item.seed, item.discount, item.policy_stream
                    )
                    self._schedule_checkpoint()
                    out[i] = {
                        "type": "reset",
                        "episode": item.episode,
                        "model_version": self._version(),
                        "discount": self._update_params().discount,
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
        """Thompson-sample each decision. Rows are grouped by eligibility and, when
        the episode's reset carried a policy stream, by simulator ``batch``: such
        a group is keyed ``fold_in(k_pol, batch)`` with noise drawn at the
        simulator's batch shape (``_decide_rows_kernel``), exactly like
        ``bandit.simulate._episode``. Otherwise (no stream, or a decision without
        ``batch``/``row``) the legacy per-call key is used."""
        t0 = time.perf_counter()
        results: list[dict | None] = [None] * len(decisions)
        stream = self._policy_stream
        groups: dict[tuple[tuple[bool, ...], int | None], list[int]] = {}
        for k, d in enumerate(decisions):
            batch = d.batch if stream is not None else None
            if stream is not None and d.row is not None and d.row >= stream.batch_size:
                results[k] = {
                    "type": "error",
                    "request_id": d.request_id,
                    "error": f"row must be < the reset batch_size {stream.batch_size}",
                }
                continue
            groups.setdefault((d.eligible, batch), []).append(k)
        version, step = self._version(), int(self._state.step)
        rows: list[tuple[int, int, np.ndarray, np.ndarray, int, int | None]] = []
        chunks = [
            (eligible, batch, idx[c : c + MAX_CHUNK])
            for (eligible, batch), idx in groups.items()
            for c in range(0, len(idx), MAX_CHUNK)
        ]
        for eligible, batch, idx in chunks:
            X = levels_to_matrix(np.stack([decisions[k].levels for k in idx]))
            n = X.shape[0]
            size = _bucket(n)
            X_pad = jnp.asarray(_pad_rows(X, size))
            if batch is None or stream is None or self._policy_key is None:
                arms, probs, greedy = _decide_kernel(
                    self._next_key(), self._state, X_pad, jnp.asarray(eligible), params
                )
            else:
                served = np.asarray([decisions[k].row for k in idx], np.int32)
                arms, probs, greedy = _decide_rows_kernel(
                    jax.random.fold_in(self._policy_key, batch),
                    self._state,
                    X_pad,
                    jnp.asarray(_pad_rows(served, size)),
                    jnp.asarray(eligible),
                    params,
                    stream.batch_size,
                )
            arms_np = np.asarray(arms)[:n]
            probs_np = np.asarray(probs)[:n]
            greedy_np = np.asarray(greedy)[:n]
            for r, k in enumerate(idx):
                rows.append(
                    (k, int(arms_np[r]), probs_np[r], X[r], int(greedy_np[r]), batch)
                )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        for k, arm, prob, x, greedy, batch in rows:
            d = decisions[k]
            self._remember(d.request_id, arm, x, batch)
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

    def _remember(
        self, request_id: str, arm: int, x: np.ndarray, batch: int | None = None
    ) -> None:
        self._pending[request_id] = (arm, x, batch)
        self._pending.move_to_end(request_id)
        while len(self._pending) > self.max_pending:
            self._pending.popitem(last=False)

    def _apply_rewards(self, rewards: Sequence[_Reward]) -> list[dict]:
        """Accept/reject each reward, then apply the accepted ones as one batched
        ``update`` per run of consecutive rewards from the same decision batch
        (one update for the whole request when decisions carried no ``batch``).
        A run that continues the previous update's batch (a batch whose rewards
        were split over requests) is applied without decaying again, so γ is
        applied once per simulator batch as in ``bandit.simulate``."""
        verdicts: list[str | None] = []
        runs: list[
            tuple[int | None, list[int], list[np.ndarray], list[float], list[int]]
        ] = []
        for i, r in enumerate(rewards):
            reason = None
            if r.request_id in self._seen:
                reason = "duplicate"
            elif r.request_id not in self._pending:
                reason = "unknown request_id"
            elif self._pending[r.request_id][0] != r.arm:
                reason = "arm does not match the decision"
            if reason is None:
                arm, x, batch = self._pending.pop(r.request_id)
                self._seen[r.request_id] = None
                while len(self._seen) > self.max_seen:
                    self._seen.popitem(last=False)
                if not runs or (batch is not None and runs[-1][0] != batch):
                    runs.append((batch, [], [], [], []))
                runs[-1][1].append(arm)
                runs[-1][2].append(x)
                runs[-1][3].append(r.reward)
                runs[-1][4].append(i)
            verdicts.append(reason)
        for batch, arms, xs, values, members in runs:
            params = self._update_params()
            if batch is not None and batch == self._last_update_batch:
                params = dataclasses.replace(params, discount=1.0)
            n = len(arms)
            size = _bucket(n)
            new_state, finite = _update_kernel(
                self._state,
                jnp.asarray(_pad_rows(np.asarray(arms, np.int32), size)),
                jnp.asarray(_pad_rows(np.stack(xs), size)),
                jnp.asarray(_pad_rows(np.asarray(values, np.float32), size)),
                jnp.asarray(np.arange(size) < n),
                params,
                self._reward_scale,
            )
            if not bool(finite):
                # keep the previous posterior; the run's rewards stay consumed
                log.warning("refused a non-finite posterior update (%d rewards)", n)
                for k in members:
                    verdicts[k] = "non-finite update"
                continue
            self._state = new_state
            self._last_update_batch = batch
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
        mu = np.asarray(_posterior_mean(self._state))
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
