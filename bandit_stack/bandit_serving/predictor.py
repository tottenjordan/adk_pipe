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

Modules: ``instances`` (instance parsing + validation, pure), ``kernels`` (the
jit kernels, padding buckets, warm-up), ``checkpoint`` (``latest.json`` /
``.npz`` snapshot, restore and the writer thread) and ``storage`` (local / GCS).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any

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
from bandit.features import DIM, FEATURE_SPEC_VERSION, levels_to_matrix
from bandit_serving.checkpoint import (
    CheckpointMeta,
    CheckpointWriter,
    restore_checkpoint,
    snapshot_npz,
)
from bandit_serving.instances import (
    Batch,
    Decision,
    InstanceError,
    PolicyStream,
    Reset,
    Reward,
    StateRequest,
    parse_instance,
    resolve_params,
)
from bandit_serving.kernels import (
    MAX_CHUNK,
    bucket,
    decide_kernel,
    decide_rows_kernel,
    pad_rows,
    posterior_mean,
    state_finite,
    update_kernel,
    warmup,
)
from bandit_serving.storage import Storage, open_storage

log = logging.getLogger(__name__)

POLICY_NAME = "linear_ts"
MAX_INSTANCES = 1000  # per request (contracts §2; the traffic client splits at 500)
CHECKPOINT_ENV = "BANDIT_CHECKPOINT_URI"
MAX_DISTINCT_PARAMS = 16  # distinct request params per process (each one compiles)


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
        self._writer = CheckpointWriter()

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
        warmup(
            len(self.arm_ids),
            self._dim,
            self.params,
            self.config.batch_size,
            self._reward_scale,
        )
        log.info(
            "bandit predictor loaded: experiment=%s arms=%s version=%s noise_var=%s",
            cfg.experiment_id,
            self.arm_ids,
            self._version(),
            self.params.noise_var,
        )

    def _fresh(
        self,
        episode: int,
        seed: int,
        discount: float | None = None,
        policy_stream: PolicyStream | None = None,
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
        restored = restore_checkpoint(
            storage,
            experiment_id=self.config.experiment_id,
            n_arms=len(self.arm_ids),
            dim=self._dim,
            batch_size=self.config.batch_size,
        )
        if restored is None:
            return
        meta, state = restored
        self._fresh(
            episode=meta.episode,
            seed=meta.seed,
            discount=meta.discount,
            policy_stream=meta.policy_stream,
        )
        self._state = state
        self._n_updates = meta.n_updates
        self._calls = meta.calls
        log.info("restored checkpoint %s", meta.model_version)

    def _schedule_checkpoint(self) -> None:
        """Snapshot under the lock; write in the background (never raises)."""
        self._updates_since_ckpt = 0
        self._last_ckpt = time.monotonic()
        storage = self._ckpt_storage
        if storage is None:
            return
        version = self._version()
        if not bool(state_finite(self._state)):
            log.warning("not checkpointing %s: non-finite posterior", version)
            return
        meta = CheckpointMeta(
            experiment_id=self.config.experiment_id,
            model_version=version,
            npz=f"{version}.npz",
            episode=self._episode,
            seed=self._seed,
            discount=self._discount,
            policy_stream=self._policy_stream,
            n_updates=self._n_updates,
            calls=self._calls,
            feature_spec_version=FEATURE_SPEC_VERSION,
            saved_at=time.time(),
        )
        self._writer.submit(storage, meta, snapshot_npz(self._state))

    def flush(self, timeout: float | None = 30.0) -> None:
        """Wait for queued checkpoint writes (tests, shutdown)."""
        self._writer.flush(timeout)

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
            items=[
                parse_instance(inst, self._arm_index, self.config) for inst in instances
            ],
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

    def predict(self, instances: Batch) -> list[dict[str, Any]]:
        items = instances.items
        out: list[dict[str, Any] | None] = [None] * len(items)
        with self._lock:
            i = 0
            while i < len(items):
                item = items[i]
                if isinstance(item, Decision | Reward):
                    j = i
                    while j < len(items) and type(items[j]) is type(item):
                        j += 1
                    idx = list(range(i, j))
                    run = [items[k] for k in idx]
                    if isinstance(item, Decision):
                        decisions = [d for d in run if isinstance(d, Decision)]
                        results = self._decide(decisions, instances.params)
                    else:
                        rewards = [r for r in run if isinstance(r, Reward)]
                        results = self._apply_rewards(rewards)
                    for k, res in zip(idx, results, strict=True):
                        out[k] = res
                    i = j
                    continue
                if isinstance(item, Reset):
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
                elif isinstance(item, StateRequest):
                    out[i] = self._summary()
                else:
                    assert isinstance(item, InstanceError)
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

    def _decide(self, decisions: Sequence[Decision], params: LinTSParams) -> list[dict]:
        """Thompson-sample each decision. Rows are grouped by eligibility and, when
        the episode's reset carried a policy stream, by simulator ``batch``: such
        a group is keyed ``fold_in(k_pol, batch)`` with noise drawn at the
        simulator's batch shape (``decide_rows_kernel``), exactly like
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
            size = bucket(n)
            X_pad = jnp.asarray(pad_rows(X, size))
            if batch is None or stream is None or self._policy_key is None:
                arms, probs, greedy = decide_kernel(
                    self._next_key(), self._state, X_pad, jnp.asarray(eligible), params
                )
            else:
                served = np.asarray([decisions[k].row for k in idx], np.int32)
                arms, probs, greedy = decide_rows_kernel(
                    jax.random.fold_in(self._policy_key, batch),
                    self._state,
                    X_pad,
                    jnp.asarray(pad_rows(served, size)),
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

    def _apply_rewards(self, rewards: Sequence[Reward]) -> list[dict]:
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
            size = bucket(n)
            new_state, finite = update_kernel(
                self._state,
                jnp.asarray(pad_rows(np.asarray(arms, np.int32), size)),
                jnp.asarray(pad_rows(np.stack(xs), size)),
                jnp.asarray(pad_rows(np.asarray(values, np.float32), size)),
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
        mu = np.asarray(posterior_mean(self._state))
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
