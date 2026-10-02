"""An in-process stand-in for the CPR ``BanditPredictor`` (contracts §2).

Implements the four instance types with ``bandit.linear_ts`` so the traffic loop
can run end to end (tests, ``--in-process``) before ``bandit_serving`` exists. Same
interface as a predictor: ``predict(instances, parameters) -> list[dict]``, one
prediction per instance, in order; a bad instance yields an ``error`` prediction
and never fails the rest of the batch.

Semantics:
- ``decision``: batch-encode the contexts, Thompson-sample an arm per row, compute
  the Monte Carlo propensities, record ``request_id -> (arm, x)`` in a bounded
  ``pending`` map.
- ``reward``: accepted only for a pending, not-yet-rewarded ``request_id`` whose
  ``arm`` matches the decision; all accepted rewards of a request are applied as
  one batched ``update`` (bumping ``model_version``). Duplicates / unknown ids
  return ``accepted: false``. Engaged rewards (seconds) are divided by the
  scenario's ``dwell_base_s`` before the update, as in the simulator.
- ``reset``: fresh prior state and PRNG key for a new episode.
- ``state``: pulls, posterior means, version and step.
"""

from __future__ import annotations

import dataclasses
import time
from collections import OrderedDict
from typing import Any

import jax
import jax.numpy as jnp
import numpy as np

from bandit import features
from bandit import linear_ts as lts
from bandit.config import ExperimentConfig, load_scenario, validate_lints_params

_select = jax.jit(lts.select, static_argnames=("params",))
_propensities = jax.jit(lts.propensities_batch, static_argnames=("params",))
_update = jax.jit(lts.update, static_argnames=("params",))
_posterior_mean = jax.jit(lts.posterior_mean)

PENDING_LIMIT = 200_000


def _error(inst: Any, msg: str) -> dict:
    rid = inst.get("request_id") if isinstance(inst, dict) else None
    return {"type": "error", "request_id": rid, "error": msg}


class FakeBanditEndpoint:
    def __init__(self, cfg: ExperimentConfig, pending_limit: int = PENDING_LIMIT):
        self.cfg = cfg
        self.arm_ids = [a.creative_id for a in cfg.arms]
        self.arm_index = {cid: i for i, cid in enumerate(self.arm_ids)}
        self.reward_scale = (
            load_scenario(cfg.scenario).dwell_base_s
            if cfg.reward_mode == "engaged"
            else 1.0
        )
        self.pending_limit = pending_limit
        self.calls: list[tuple[list[str], dict | None]] = []
        self._reset(0, cfg.seed)

    # ------------------------------------------------------------------ state
    def _reset(self, episode: int, seed: int) -> None:
        self.episode = int(episode)
        self.key = jax.random.key(int(seed))
        self.state = lts.init_state(
            len(self.arm_ids), features.DIM, self.cfg.policy.prior_var
        )
        self.version = 0
        self.decisions = 0
        self.pending: OrderedDict[str, tuple[int, np.ndarray]] = OrderedDict()
        self.rewarded: OrderedDict[str, None] = OrderedDict()

    @property
    def model_version(self) -> str:
        return f"e{self.episode}-v{self.version}"

    def _params(self, parameters: dict | None):
        params = self.cfg.policy
        if parameters:
            upd: dict[str, Any] = {}
            if "exploration_scale" in parameters:
                upd["exploration_scale"] = float(parameters["exploration_scale"])
            if "propensity_samples" in parameters:
                upd["propensity_samples"] = int(parameters["propensity_samples"])
            params = validate_lints_params(
                dataclasses.replace(params, **upd), len(self.arm_ids)
            )
        return params

    def _remember(self, rid: str, value: tuple[int, np.ndarray]) -> None:
        self.pending[rid] = value
        while len(self.pending) > self.pending_limit:
            self.pending.popitem(last=False)

    # ---------------------------------------------------------------- predict
    def predict(self, instances: list[dict], parameters: dict | None = None) -> list:
        t0 = time.perf_counter()
        self.calls.append(
            ([str(i.get("type")) if isinstance(i, dict) else "?" for i in instances],
             parameters)
        )  # fmt: skip
        out: list[dict | None] = [None] * len(instances)
        try:
            params = self._params(parameters)
        except (TypeError, ValueError) as exc:
            return [_error(i, f"bad parameters: {exc}") for i in instances]
        decisions: list[tuple[int, dict, np.ndarray, np.ndarray | None]] = []
        rewards: list[tuple[int, dict]] = []
        for i, inst in enumerate(instances):
            kind = inst.get("type") if isinstance(inst, dict) else None
            try:
                if kind == "reset":
                    self._reset(int(inst["episode"]), int(inst["seed"]))
                    out[i] = {
                        "type": "reset",
                        "episode": self.episode,
                        "model_version": self.model_version,
                    }
                elif kind == "state":
                    out[i] = self._state_prediction()
                elif kind == "decision":
                    if not inst.get("request_id"):
                        raise ValueError("request_id is required")
                    x = features.encode_context(inst["context"])
                    elig = None
                    if inst.get("eligible_arms"):
                        elig = np.zeros(len(self.arm_ids), bool)
                        for cid in inst["eligible_arms"]:
                            elig[self.arm_index[cid]] = True
                    decisions.append((i, inst, x, elig))
                elif kind == "reward":
                    rewards.append((i, inst))
                else:
                    raise ValueError(f"unknown instance type {kind!r}")
            except (KeyError, TypeError, ValueError) as exc:
                out[i] = _error(inst, f"{type(exc).__name__}: {exc}")
        if decisions:
            self._decide(decisions, params, out)
        if rewards:
            self._reward(rewards, params, out)
        latency = (time.perf_counter() - t0) * 1000.0
        for p in out:
            if p is not None and p.get("type") == "decision":
                p["latency_ms"] = latency
        return out

    def _decide(self, decisions, params, out) -> None:
        # Group by eligibility mask (one jit call per distinct mask).
        groups: dict[bytes | None, list] = {}
        for d in decisions:
            groups.setdefault(None if d[3] is None else d[3].tobytes(), []).append(d)
        means = np.asarray(_posterior_mean(self.state))  # (K, d)
        for group in groups.values():
            X = jnp.asarray(np.stack([d[2] for d in group]))
            elig = None if group[0][3] is None else jnp.asarray(group[0][3])
            self.key, k_sel, k_prop = jax.random.split(self.key, 3)
            arms, _ = _select(k_sel, self.state, X, params, elig)
            probs = np.asarray(_propensities(k_prop, self.state, X, params, elig))
            arms = np.asarray(arms)
            greedy_scores = np.asarray(X) @ means.T
            if elig is not None:
                greedy_scores = np.where(np.asarray(elig), greedy_scores, -np.inf)
            greedy = greedy_scores.argmax(axis=1)
            for j, (i, inst, x, _) in enumerate(group):
                a = int(arms[j])
                self.decisions += 1
                self._remember(str(inst["request_id"]), (a, x))
                out[i] = {
                    "request_id": inst["request_id"],
                    "type": "decision",
                    "chosen_arm": self.arm_ids[a],
                    "arm_index": a,
                    "propensity": float(probs[j, a]),
                    "arm_probabilities": {
                        cid: float(probs[j, k]) for k, cid in enumerate(self.arm_ids)
                    },
                    "explored": bool(a != int(greedy[j])),
                    "model_version": self.model_version,
                    "policy": "linear_ts",
                    "episode": self.episode,
                    "step": self.decisions,
                }

    def _reward(self, rewards, params, out) -> None:
        arms, xs, rs = [], [], []
        accepted_idx = []
        for i, inst in rewards:
            rid = inst.get("request_id")
            try:
                reward = float(inst["reward"])
                int(inst.get("clicked", 0))
            except (KeyError, TypeError, ValueError) as exc:
                out[i] = _error(inst, f"{type(exc).__name__}: {exc}")
                continue
            entry = self.pending.get(str(rid)) if rid else None
            ok = (
                entry is not None
                and str(rid) not in self.rewarded
                and self.arm_index.get(inst.get("arm", "")) == entry[0]
            )
            if ok and entry is not None:
                self.rewarded[str(rid)] = None
                self.pending.pop(str(rid), None)
                while len(self.rewarded) > self.pending_limit:
                    self.rewarded.popitem(last=False)
                arms.append(entry[0])
                xs.append(entry[1])
                rs.append(reward / self.reward_scale)
                accepted_idx.append(i)
            out[i] = {"request_id": rid, "type": "reward", "accepted": bool(ok)}
        if arms:
            self.state = _update(
                self.state,
                jnp.asarray(arms, jnp.int32),
                jnp.asarray(np.stack(xs)),
                jnp.asarray(rs, jnp.float32),
                params,
            )
            self.version += 1
        for i, _ in rewards:
            if out[i] is not None and out[i].get("type") == "reward":
                out[i]["model_version"] = self.model_version

    def _state_prediction(self) -> dict:
        means = np.asarray(_posterior_mean(self.state))
        pulls = np.asarray(self.state.n)
        return {
            "type": "state",
            "episode": self.episode,
            "step": int(self.state.step),
            "model_version": self.model_version,
            "pulls": {cid: int(pulls[k]) for k, cid in enumerate(self.arm_ids)},
            "posterior_mean": {
                cid: [float(v) for v in means[k]] for k, cid in enumerate(self.arm_ids)
            },
            "feature_spec_version": features.FEATURE_SPEC_VERSION,
        }
