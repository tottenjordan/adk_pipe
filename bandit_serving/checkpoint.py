"""Posterior checkpoints under ``BANDIT_CHECKPOINT_URI`` (contracts §2 / §7).

A checkpoint is ``<model_version>.npz`` (the LinTS state) plus ``latest.json``
(``CheckpointMeta``, pointing at that ``.npz``). ``CheckpointWriter`` writes
them on one background thread, the ``.npz`` before ``latest.json``, then keeps
only the newest ``CHECKPOINT_KEEP`` ``.npz`` files; a failed write logs one
concise warning and never blocks serving. ``restore_checkpoint`` reads them back
with full shape and finiteness checks; anything wrong means "start fresh".

The ``latest.json`` layout is an on-disk format: older checkpoints (incl. ones
without ``discount`` / ``policy_stream``) must keep restoring.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import jax.numpy as jnp
import numpy as np

from bandit import linear_ts as lts
from bandit_serving.instances import KEY_DATA_LEN, PolicyStream
from bandit_serving.kernels import state_finite
from bandit_serving.storage import Storage, prune_checkpoints, short_error

log = logging.getLogger(__name__)

CHECKPOINT_KEEP = 5  # newest checkpoint .npz files kept under BANDIT_CHECKPOINT_URI
LATEST = "latest.json"


@dataclass(frozen=True)
class CheckpointMeta:
    """``latest.json``: which ``.npz`` holds the posterior, plus the episode
    state the predictor needs to resume (seed, discount, policy stream and the
    update / PRNG call counters)."""

    experiment_id: str
    model_version: str
    npz: str
    episode: int
    seed: int
    discount: float | None
    policy_stream: PolicyStream | None
    n_updates: int
    calls: int
    feature_spec_version: str | None = None
    saved_at: float | None = None

    def to_json(self) -> bytes:
        """The ``latest.json`` bytes (key order fixed: an on-disk format)."""
        return json.dumps(
            {
                "experiment_id": self.experiment_id,
                "model_version": self.model_version,
                "npz": self.npz,
                "episode": self.episode,
                "seed": self.seed,
                "discount": self.discount,
                "policy_stream": None
                if self.policy_stream is None
                else self.policy_stream.to_json(),
                "n_updates": self.n_updates,
                "calls": self.calls,
                "feature_spec_version": self.feature_spec_version,
                "saved_at": self.saved_at,
            }
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, meta: Mapping[str, Any]) -> CheckpointMeta:
        """Parse a ``latest.json`` object; a missing required key raises
        ``KeyError``. ``discount`` / ``policy_stream`` may be absent (older
        checkpoints)."""
        discount = meta.get("discount")
        raw_stream = meta.get("policy_stream")
        return cls(
            experiment_id=str(meta["experiment_id"]),
            model_version=str(meta["model_version"]),
            npz=str(meta["npz"]),
            episode=int(meta["episode"]),
            seed=int(meta["seed"]),
            discount=None if discount is None else float(discount),
            policy_stream=None
            if raw_stream is None
            else PolicyStream(
                tuple(int(w) for w in raw_stream["key_data"]),
                int(raw_stream["batch_size"]),
            ),
            n_updates=int(meta["n_updates"]),
            calls=int(meta["calls"]),
            feature_spec_version=meta.get("feature_spec_version"),
            saved_at=meta.get("saved_at"),
        )


def snapshot_npz(state: lts.LinTSState) -> bytes:
    """The ``.npz`` bytes of a LinTS state."""
    buf = io.BytesIO()
    np.savez(
        buf,
        precision=np.asarray(state.precision),
        b=np.asarray(state.b),
        n=np.asarray(state.n),
        step=np.asarray(state.step),
    )
    return buf.getvalue()


def restore_checkpoint(
    storage: Storage,
    *,
    experiment_id: str,
    n_arms: int,
    dim: int,
    batch_size: int,
) -> tuple[CheckpointMeta, lts.LinTSState] | None:
    """The newest checkpoint for this experiment, or None (logged) when there
    is none or it doesn't fit the config: another experiment, a missing
    ``.npz``, wrong shapes, a non-finite posterior or a policy stream of the
    wrong key length / batch size. Never raises."""
    try:
        meta_raw = storage.read_bytes(LATEST)
        if meta_raw is None:
            return None
        raw = json.loads(meta_raw)
        if raw.get("experiment_id") != experiment_id:
            log.warning(
                "ignoring checkpoint for experiment %s", raw.get("experiment_id")
            )
            return None
        npz_raw = storage.read_bytes(str(raw["npz"]))
        if npz_raw is None:
            log.warning("checkpoint %s missing; starting fresh", raw["npz"])
            return None
        arrays = np.load(io.BytesIO(npz_raw))
        state = lts.LinTSState(
            precision=jnp.asarray(arrays["precision"], jnp.float32),
            b=jnp.asarray(arrays["b"], jnp.float32),
            n=jnp.asarray(arrays["n"], jnp.int32),
            step=jnp.asarray(arrays["step"], jnp.int32),
        )
        k, d = n_arms, dim
        expected = {"precision": (k, d, d), "b": (k, d), "n": (k,), "step": ()}
        shapes = {name: getattr(state, name).shape for name in expected}
        if shapes != expected:
            log.warning(
                "checkpoint shapes %s mismatch the config %s; starting fresh",
                shapes,
                expected,
            )
            return None
        if not bool(state_finite(state)):
            log.warning(
                "checkpoint %s holds a non-finite posterior; starting fresh",
                raw.get("model_version"),
            )
            return None
        meta = CheckpointMeta.from_dict(raw)
        stream = meta.policy_stream
        if stream is not None and (
            len(stream.key_data) != KEY_DATA_LEN or stream.batch_size != batch_size
        ):
            log.warning(
                "checkpoint policy stream mismatches the config "
                "(%d key words, batch_size %d); starting fresh",
                len(stream.key_data),
                stream.batch_size,
            )
            return None
        return meta, state
    except Exception:
        log.exception("checkpoint restore failed; starting fresh")
        return None


class CheckpointWriter:
    """One background thread writing checkpoints in submission order."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ckpt")
        self._last: Future[None] | None = None

    def submit(self, storage: Storage, meta: CheckpointMeta, npz: bytes) -> None:
        """Queue ``npz`` then ``latest.json``, then prune; never raises."""

        def write() -> None:
            try:
                storage.write_bytes(meta.npz, npz)
                storage.write_bytes(LATEST, meta.to_json())
            except Exception as exc:
                log.warning(
                    "checkpoint write failed for %s (serving continues): %s",
                    meta.model_version,
                    short_error(exc),
                )
                return
            prune_checkpoints(storage, meta.npz, CHECKPOINT_KEEP)

        try:
            self._last = self._executor.submit(write)
        except RuntimeError:
            log.exception("checkpoint writer unavailable")

    def flush(self, timeout: float | None = 30.0) -> None:
        """Wait for the queued writes (tests, shutdown)."""
        fut = self._last
        if fut is not None:
            fut.result(timeout=timeout)
