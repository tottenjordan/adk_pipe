"""The synthetic-traffic loop: drive the endpoint, replay baselines, log rows.

For each episode ``e`` (key = ``bandit.simulate.episode_keys(seed, scenario)[e]``):

1. send ``reset {episode, seed, policy_key, batch_size}`` (plus ``discount`` when
   the run forgets); ``policy_key`` is the episode's simulator policy stream, so
   the endpoint's LinTS draws exactly what ``simulate`` would (contracts §2);
2. for each batch ``b``: draw the batch with ``bandit.simulate.batch_draws``
   (the simulator's own context / reward streams, i.e. common random numbers),
   send the decision instances (with their ``batch`` and ``row``, split under the
   request limits), read each chosen arm's outcome from the pre-drawn coin flips,
   send the reward instances;
3. replay the baselines (``ucb1``, ``epsilon_greedy``, ``beta_bernoulli_ts``,
   ``uniform``, ``oracle``) locally with ``bandit.simulate.run_episodes`` on the
   same episode key, so they see identical users and coin flips;
4. with shifts, replay the **ghost** ``linear_ts_unshifted``: a local LinTS
   (the experiment's policy params, plus the run's discount when it forgets) on
   the *unshifted* environment with the same episode key, i.e. the same users
   and coin flips without the shifts;
5. build one ``bandit_episode_metrics`` row per policy with
   ``bandit.metrics.episode_metrics`` (shared checkpoints), write them, and
   update ``bandit_experiments.progress``.

``bandit_events`` rows (endpoint policy only) are flushed in batches as the
episode runs. ``round`` is the 0-based round index within the episode (the
environment's ``t``); ``request_id`` is
``{experiment_id}-r{traffic_run}-e{episode}-r{round}`` (the run is always
included, run 1 too, so a rerun never collides with an earlier run's ids).

**Traffic runs and shifts** (contracts §10): every event and metrics row carries
``traffic_run``. With scripted ``shifts`` the endpoint and every baseline share
the shifted environment; checkpoints are linear and merged around each shift
round (``merge_checkpoints``), and every metrics row also carries
``shift_response`` (against the shift rounds, the ghost's too; each entry also
carries its shift's resolved record, ``resolved_shift_fields``, so readers
without ``bandit`` see the concrete ``"leader"``) and ``regimes``
(``regime_stats`` split at the shift and shock-end rounds). ``forget`` (default:
on iff there are shifts) sends ``discount = default_shift_discount(...)``,
floored at ``RESET_DISCOUNT_BOUNDS[0]``, in every ``reset``.

A decision that comes back as a per-instance ``error`` gets no event row and no
reward; for the metrics the round falls back to a uniformly random arm (what an
ad server would serve by default). If the episode's decision error rate exceeds
``error_threshold`` the run aborts with ``EndpointErrorRate``.

The ground truth is ``bandit.config.resolve_scenario(cfg)``: the scenario preset
with ``experiment.json``'s optional ``scenario_overrides`` applied (contracts §9).
The episode keys still derive from the scenario *name*, so a tuned experiment
sees the same random draws as its preset.

Engaged-mode rewards are sent unscaled (click × dwell seconds, plus ``dwell_s``);
the predictor scales them like the simulator (contracts §2).

**Continuous learning** (``learning="continuous"``, contracts §11): the run is
one long episode of H = E·T rounds cut into E segments of T rounds. The world
is built with horizon H (``continuous_world_config``: drift and shifts resolve
over the whole run), the endpoint gets **one** reset (episode 0's seed and
policy stream; the forgetting discount computed with H), decisions carry the
*global* batch index and events the global ``round``. After each segment the
baselines and the ghost are replayed with ``simulate.run_segment`` from their
carried state, so the parity above holds round for round over the whole run
(endpoint == ``run_segment`` chained == ``run_episodes`` at horizon H). Each
segment writes one metrics row per policy (``episode`` = segment, linear
segment-local checkpoints, ``curve.segment_start``); ``shift_response`` /
``regimes`` are computed once over the whole run and ride on the last
segment's rows (``"continuous": true``).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import jax
import numpy as np

from bandit import environment as envm
from bandit import simulate
from bandit.config import (
    LEARNING_MODES,
    RESET_DISCOUNT_BOUNDS,
    ExperimentConfig,
    ShiftSpec,
    continuous_world_config,
    default_shift_discount,
    resolve_scenario,
)
from bandit.metrics import (
    episode_metrics,
    log_checkpoints,
    make_checkpoints,
    merge_checkpoints,
    regime_stats,
    shift_response,
)
from bandit.policies import make_policy
from bandit_traffic import bq
from bandit_traffic.endpoint_client import (
    MAX_REQUEST_BYTES,
    MAX_REQUEST_INSTANCES,
    EndpointClient,
    split_instances,
)

log = logging.getLogger(__name__)

ENDPOINT_POLICY = "linear_ts"
GHOST_POLICY = "linear_ts_unshifted"
BASELINES: tuple[str, ...] = (
    "ucb1",
    "epsilon_greedy",
    "beta_bernoulli_ts",
    "uniform",
    "oracle",
)
_draws = jax.jit(simulate.batch_draws, static_argnames=("batch_size", "reward_mode"))


class TrafficError(RuntimeError):
    """The run cannot continue (exit code 1)."""


class EndpointErrorRate(TrafficError):
    pass


@dataclass(frozen=True)
class TrafficSettings:
    """Run knobs. Episodes / horizon / batch size / reward mode come from the
    ``ExperimentConfig`` (``main`` applies the env/flag overrides to it first, so
    the ground truth, e.g. the drift point, matches the horizon)."""

    error_threshold: float = 0.05
    event_flush_rows: int = 2000
    max_request_instances: int = MAX_REQUEST_INSTANCES
    max_request_bytes: int = MAX_REQUEST_BYTES
    baselines: tuple[str, ...] = BASELINES
    parameters: dict | None = None
    num_checkpoints: int = 50
    #: keep each episode's per-round arrays in ``TrafficRunner.outputs`` (tests)
    keep_outputs: bool = False


@dataclass
class TrafficSummary:
    experiment_id: str
    episodes_done: int = 0
    events_written: int = 0
    decision_errors: int = 0
    reward_errors: int = 0
    rewards_rejected: int = 0
    requests: int = 0
    traffic_run: int = 1
    resolved_shifts: list[dict] = field(default_factory=list)
    discount: float | None = None
    learning: str = "per_episode"
    metrics: list[dict] = field(default_factory=list)

    def regret_by_policy(self) -> dict[str, float]:
        out: dict[str, list[float]] = {}
        for row in self.metrics:
            out.setdefault(row["policy"], []).append(float(row["cumulative_regret"]))
        return {p: float(np.mean(v)) for p, v in out.items()}


def reset_seed(seed: int, episode: int) -> int:
    """The endpoint's PRNG seed for an episode (31-bit, distinct per episode)."""
    return (int(seed) * 1_000_003 + int(episode)) % (2**31)


def run_discount(cfg: ExperimentConfig) -> float:
    """The endpoint's discount for a forgetting run (contracts §10):
    ``default_shift_discount`` floored at the reset bound (short runs would
    otherwise forget faster than the predictor accepts). A continuous run
    passes its world config (horizon E·T, §11)."""
    gamma = default_shift_discount(cfg.ctr_mode, cfg.batch_size, cfg.horizon)
    return max(RESET_DISCOUNT_BOUNDS[0], gamma)


def policy_stream_fields(k_pol: Any, batch_size: int) -> dict[str, Any]:
    """The ``reset`` fields that key the endpoint like the simulator (contracts
    §2): ``policy_key`` = the uint32 ``key_data`` of the episode's policy stream
    (``simulate.episode_streams(key)[2]``) and the simulator ``batch_size``. With
    each decision's ``batch``/``row``, the endpoint draws batch ``b`` from
    ``fold_in(k_pol, b)`` at the simulator's batch shape, so its LinTS picks the
    same arms as ``simulate.run_episodes`` (and as the ghost before a shift)."""
    words = np.asarray(jax.random.key_data(k_pol)).ravel()
    return {"policy_key": [int(w) for w in words], "batch_size": int(batch_size)}


def resolved_shift_fields(rec: dict) -> dict:
    """The part of a resolved shift record (``environment.resolved_shifts``)
    that rides on each ``shift_response`` entry (contracts §3/§10): ``index``,
    ``kind``, ``round``, ``end_round``, ``segment``, ``creative_id`` (concrete,
    ``"leader"`` resolved), ``requested_creative_id`` and ``targets`` (per
    segment ``ctr_before`` / ``ctr_after``). A mix shift has null creative /
    segment fields and no targets."""
    return {
        "index": int(rec["index"]),
        "kind": rec["kind"],
        "round": int(rec["round"]),
        "end_round": rec.get("end_round"),
        "segment": rec.get("segment"),
        "creative_id": rec.get("creative_id"),
        "requested_creative_id": rec.get("requested_creative_id"),
        "targets": [
            {k: t[k] for k in ("segment", "ctr_before", "ctr_after")}
            for t in rec.get("targets") or ()
        ],
    }


#: Per-round endpoint columns the metrics need (``bandit.metrics.episode_metrics``).
_COLUMNS = (
    "segment",
    "arm",
    "reward",
    "clicked",
    "opt_arm",
    "mean_opt",
    "mean_chosen",
    "reward_opt",
    "p_all",
)
#: What a continuous run keeps per round for the whole-run ``shift_response`` /
#: ``regime_stats`` (§11): each policy's own columns, and each world's columns
#: (identical for every policy in one world, so stored once per world).
_POLICY_TRACE = ("arm", "reward", "mean_chosen")
_WORLD_TRACE = ("segment", "opt_arm", "mean_opt", "p_all")
_TRACE_DTYPES = {
    "arm": np.int8,
    "segment": np.int8,
    "opt_arm": np.int8,
    "reward": np.float32,
    "mean_chosen": np.float32,
    "mean_opt": np.float32,
    "p_all": np.float32,
}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _is_error(pred: Any) -> bool:
    return not isinstance(pred, dict) or pred.get("type") == "error"


class TrafficRunner:
    def __init__(
        self,
        cfg: ExperimentConfig,
        client: EndpointClient,
        writer: bq.RowWriter,
        settings: TrafficSettings,
        *,
        experiment_id: str | None = None,
        now: Callable[[], dt.datetime] = _utcnow,
        shifts: Sequence[ShiftSpec] = (),
        traffic_run: int = 1,
        forget: bool | None = None,
        learning: str = "per_episode",
    ):
        if traffic_run < 1:
            raise ValueError(f"traffic_run must be >= 1, got {traffic_run}")
        if learning not in LEARNING_MODES:
            raise ValueError(
                f"learning must be one of {LEARNING_MODES}, got {learning!r}"
            )
        self.learning = learning
        self.continuous = learning == "continuous"
        # a continuous run's world spans the whole run (horizon E·T, §11)
        world_cfg = continuous_world_config(cfg) if self.continuous else cfg
        self.cfg = cfg
        self.client = client
        self.writer = writer
        self.s = settings
        self.experiment_id = experiment_id or cfg.experiment_id
        self.now = now
        self.shifts = tuple(shifts)
        self.traffic_run = int(traffic_run)
        self.forget = bool(self.shifts) if forget is None else bool(forget)
        self.discount = run_discount(world_cfg) if self.forget else None
        # the preset + experiment.json's scenario_overrides (contracts §9) + the
        # run's shifts (§10); the baselines share self.env, so they see the same
        # ground truth
        scenario = resolve_scenario(cfg)
        self.env = simulate.build_environment(
            world_cfg, scenario=scenario, shifts=self.shifts
        )
        self.resolved = envm.resolved_shifts(self.env)
        self.shift_rounds = [int(rec["round"]) for rec in self.resolved]
        # regime boundaries: every shift round plus every shock's end round
        self.regime_bounds = sorted(
            {*self.shift_rounds}
            | {int(r["end_round"]) for r in self.resolved if r["end_round"] is not None}
        )
        # the ghost: same scenario and overrides, no shifts
        self.ghost_env = (
            simulate.build_environment(world_cfg, scenario=scenario)
            if self.shifts
            else None
        )
        self.arm_ids = list(self.env.arm_ids)
        self.arm_index = {cid: i for i, cid in enumerate(self.arm_ids)}
        if self.continuous:
            # segment-local; merged per segment in _segment_checkpoints
            self.checkpoints = make_checkpoints(
                cfg.horizon, settings.num_checkpoints, "linear"
            )
        elif self.shifts:
            self.checkpoints = merge_checkpoints(
                make_checkpoints(cfg.horizon, settings.num_checkpoints, "linear"),
                self.shift_rounds,
                cfg.horizon,
            )
        else:
            self.checkpoints = log_checkpoints(cfg.horizon, settings.num_checkpoints)
        self.keys = simulate.episode_keys(cfg.seed, cfg.scenario, cfg.episodes)
        self.summary = TrafficSummary(
            self.experiment_id,
            traffic_run=self.traffic_run,
            resolved_shifts=self.resolved,
            discount=self.discount,
            learning=self.learning,
        )
        self.outputs: dict[tuple[int, str], dict[str, np.ndarray]] = {}
        self._events: list[dict] = []

    # ------------------------------------------------------------- transport
    def _send(self, instances: list[dict]) -> list[dict]:
        preds: list[dict] = []
        for part in split_instances(
            instances,
            max_instances=self.s.max_request_instances,
            max_bytes=self.s.max_request_bytes,
        ):
            self.summary.requests += 1
            preds.extend(self.client.predict(part, self.s.parameters))
        return preds

    def _flush_events(self, force: bool = False) -> None:
        if self._events and (force or len(self._events) >= self.s.event_flush_rows):
            self.writer.write_events(self._events)
            self.summary.events_written += len(self._events)
            self._events = []

    # --------------------------------------------------------------- episode
    def run(self) -> TrafficSummary:
        total = self.cfg.episodes
        log.info(
            "traffic run %d: learning=%s, %d shift(s), forget=%s, discount=%s, "
            "resolved shifts %s",
            self.traffic_run,
            self.learning,
            len(self.shifts),
            self.forget,
            self.discount,
            json.dumps(self.resolved, separators=(",", ":")),
        )
        if self.continuous:
            return self._run_continuous()
        for e in range(total):
            rows = self.run_episode(e)
            self.writer.write_metrics(rows)
            self.summary.metrics.extend(rows)
            self.summary.episodes_done = e + 1
            self.writer.update_progress(self.experiment_id, e + 1, total)
            log.info(
                "episode %d/%d done: %s",
                e + 1,
                total,
                {r["policy"]: r["cumulative_regret"] for r in rows},
            )
        return self.summary

    def _reset(self, episode: int, k_pol: Any) -> None:
        reset_inst: dict[str, Any] = {
            "type": "reset",
            "episode": episode,
            "seed": reset_seed(self.cfg.seed, episode),
            **policy_stream_fields(k_pol, self.cfg.batch_size),
        }
        if self.discount is not None:
            reset_inst["discount"] = self.discount
        reset = self._send([reset_inst])[0]
        if _is_error(reset):
            raise TrafficError(f"reset failed for episode {episode}: {reset}")

    def _drive(
        self,
        episode: int,
        start_batch: int,
        rounds: int,
        k_ctx: Any,
        k_rew: Any,
        fallback: np.random.Generator,
    ) -> dict[str, np.ndarray]:
        """Drive the endpoint over ``rounds`` rounds from global batch
        ``start_batch`` (decisions, rewards, event rows) and return the
        per-round columns. ``episode`` labels the events and request ids (the
        segment in a continuous run); ``round`` is the environment's ``t``."""
        s, env = self.s, self.env
        bs, K = self.cfg.batch_size, len(self.arm_ids)
        elig = jax.numpy.ones((K,), bool)
        cols: dict[str, list[np.ndarray]] = {k: [] for k in _COLUMNS}
        errors = 0
        for i in range(-(-rounds // bs)):
            b = start_batch + i
            n = min(bs, rounds - i * bs)
            d = {
                k: np.asarray(v)[:n]
                for k, v in _draws(
                    env.model, k_ctx, k_rew, b, bs, env.reward_mode, elig
                ).items()
            }
            ts = bq.iso_ts(self.now())
            contexts = envm.decode_contexts(d["levels"])
            rounds_t = d["t"].astype(int)
            prefix = f"{self.experiment_id}-r{self.traffic_run}-e{episode}"
            rids = [f"{prefix}-r{t}" for t in rounds_t]
            preds = self._send(
                [
                    {
                        "type": "decision",
                        "request_id": rid,
                        "ts": ts,
                        "context": ctx,
                        "batch": b,
                        "row": j,
                    }
                    for j, (rid, ctx) in enumerate(zip(rids, contexts, strict=True))
                ]
            )
            arms = np.empty(n, np.int64)
            ok = np.zeros(n, bool)
            for j, pred in enumerate(preds):
                a = (
                    None
                    if _is_error(pred)
                    else self.arm_index.get(pred.get("chosen_arm"))
                )
                if a is None:
                    errors += 1
                    arms[j] = int(fallback.integers(K))
                    log.debug("decision error for %s: %s", rids[j], pred)
                else:
                    arms[j], ok[j] = a, True
            rows_idx = np.arange(n)
            reward = d["reward_all"][rows_idx, arms].astype(np.float64)
            clicked = d["clicked_all"][rows_idx, arms].astype(np.int64)
            opt = d["opt"].astype(np.int64)
            mean = d["mean"].astype(np.float64)

            reward_instances = []
            for j in np.flatnonzero(ok):
                inst = {
                    "type": "reward",
                    "request_id": rids[j],
                    "arm": self.arm_ids[arms[j]],
                    "reward": float(reward[j]),
                    "clicked": int(clicked[j]),
                }
                if env.reward_mode == "engaged":
                    inst["dwell_s"] = float(reward[j])
                reward_instances.append(inst)
            if reward_instances:
                for pred in self._send(reward_instances):
                    if _is_error(pred):
                        self.summary.reward_errors += 1
                    elif not pred.get("accepted", False):
                        self.summary.rewards_rejected += 1

            for j in np.flatnonzero(ok):
                pred = preds[j]
                a, o = int(arms[j]), int(opt[j])
                self._events.append(
                    bq.build_event_row(
                        experiment_id=self.experiment_id,
                        episode=episode,
                        round=int(rounds_t[j]),
                        batch=b,
                        request_id=rids[j],
                        ts=ts,
                        segment=env.segment_names[int(d["segment"][j])],
                        context=contexts[j],
                        arm=self.arm_ids[a],
                        propensity=pred.get("propensity"),
                        reward=float(reward[j]),
                        clicked=int(clicked[j]),
                        dwell_s=float(reward[j])
                        if env.reward_mode == "engaged"
                        else None,
                        p_chosen=float(d["p"][j, a]),
                        p_optimal=float(d["p"][j, o]),
                        optimal_arm=self.arm_ids[o],
                        regret=float(mean[j, o] - mean[j, a]),
                        model_version=pred.get("model_version"),
                        latency_ms=pred.get("latency_ms"),
                        traffic_run=self.traffic_run,
                    )
                )
            self._flush_events()

            cols["segment"].append(d["segment"])
            cols["arm"].append(arms)
            cols["reward"].append(reward)
            cols["clicked"].append(clicked)
            cols["opt_arm"].append(opt)
            cols["mean_opt"].append(mean[rows_idx, opt])
            cols["mean_chosen"].append(mean[rows_idx, arms])
            cols["reward_opt"].append(d["reward_all"][rows_idx, opt])
            cols["p_all"].append(d["p"])

            seen = i * bs + n
            if (
                errors
                and errors / seen > s.error_threshold
                and seen >= min(rounds, 500)
            ):
                self._flush_events(force=True)
                self.summary.decision_errors += errors
                raise EndpointErrorRate(
                    f"episode {episode}: {errors}/{seen} decision errors "
                    f"(> {s.error_threshold:.0%})"
                )
        self._flush_events(force=True)
        self.summary.decision_errors += errors
        if errors:
            log.warning("episode %d: %d/%d decision errors", episode, errors, rounds)
        return {k: np.concatenate(v) for k, v in cols.items()}

    def _policy(self, name: str) -> Any:
        return make_policy(
            name,
            lints_params=self.cfg.policy,
            reward_mode=self.cfg.reward_mode,
            log_propensity=False,
        )

    def _ghost_policy(self) -> Any:
        """LinTS with the experiment's params, plus the run's discount when it
        forgets: the endpoint's twin."""
        params = self.cfg.policy
        if self.discount is not None:
            params = dataclasses.replace(params, discount=self.discount)
        return make_policy(
            "linear_ts",
            lints_params=params,
            reward_mode=self.cfg.reward_mode,
            log_propensity=False,
        )

    def run_episode(self, episode: int) -> list[dict]:
        cfg, s, env = self.cfg, self.s, self.env
        T, bs = cfg.horizon, cfg.batch_size
        key = self.keys[episode]
        k_ctx, k_rew, k_pol = simulate.episode_streams(key)
        fallback = np.random.default_rng([cfg.seed, episode])
        self._reset(episode, k_pol)
        ours = self._drive(episode, 0, T, k_ctx, k_rew, fallback)
        rows = [self._metrics(ENDPOINT_POLICY, episode, ours)]
        for name in s.baselines:
            out = simulate.run_episodes(self._policy(name), env, key[None], T, bs)
            ep = {k: v[0] for k, v in out.items()}
            if not np.array_equal(ep["segment"], ours["segment"]):
                raise TrafficError("baseline replay diverged from the endpoint's users")
            rows.append(self._metrics(name, episode, ep))
        if self.ghost_env is not None:
            rows.append(self._metrics(GHOST_POLICY, episode, self._ghost(key)))
        return rows

    # ------------------------------------------------- continuous (§11)
    def _segment_checkpoints(self, segment: int) -> list[int]:
        """Linear segment-local checkpoints, merged around the shift rounds
        that fall inside the segment."""
        T = self.cfg.horizon
        start = segment * T
        local = [r - start for r in self.shift_rounds if start <= r < start + T]
        if not local:
            return list(self.checkpoints)
        return merge_checkpoints(self.checkpoints, local, T)

    def _run_continuous(self) -> TrafficSummary:
        cfg, s = self.cfg, self.s
        E, T, bs = cfg.episodes, cfg.horizon, cfg.batch_size
        nb = T // bs
        key = self.keys[0]
        k_ctx, k_rew, k_pol = simulate.episode_streams(key)
        self._reset(0, k_pol)
        replays: dict[str, tuple[Any, envm.Environment]] = {
            name: (self._policy(name), self.env) for name in s.baselines
        }
        if self.ghost_env is not None:
            replays[GHOST_POLICY] = (self._ghost_policy(), self.ghost_env)
        states: dict[str, Any] = {}
        trace: dict[str, dict[str, list[np.ndarray]]] = {}
        for seg in range(E):
            fallback = np.random.default_rng([cfg.seed, seg])
            ours = self._drive(seg, seg * nb, T, k_ctx, k_rew, fallback)
            outs = {ENDPOINT_POLICY: ours}
            for name, (pol, env) in replays.items():
                out, states[name] = simulate.run_segment(
                    pol, env, key, seg * nb, nb, bs, states.get(name)
                )
                if name != GHOST_POLICY and not np.array_equal(
                    out["segment"], ours["segment"]
                ):
                    raise TrafficError(
                        "baseline replay diverged from the endpoint's users"
                    )
                outs[name] = out
            if self.shifts:
                self._trace(trace, outs)
            metrics = {
                name: self._segment_metrics(name, seg, out)
                for name, out in outs.items()
            }
            if self.shifts and seg == E - 1:
                self._attach_run_shift_payloads(metrics, trace)
            rows = [self._row(m) for m in metrics.values()]
            self.writer.write_metrics(rows)
            self.summary.metrics.extend(rows)
            self.summary.episodes_done = seg + 1
            self.writer.update_progress(self.experiment_id, seg + 1, E)
            log.info(
                "segment %d/%d done (rounds %d-%d): %s",
                seg + 1,
                E,
                seg * T,
                (seg + 1) * T - 1,
                {r["policy"]: r["cumulative_regret"] for r in rows},
            )
        return self.summary

    @staticmethod
    def _trace(
        trace: dict[str, dict[str, list[np.ndarray]]],
        outs: dict[str, dict[str, np.ndarray]],
    ) -> None:
        """Keep the compact per-round columns the whole-run shift payloads
        need: each policy's own, plus each world's once (the endpoint's for
        the shifted world, the ghost's for the unshifted one)."""
        for name, out in outs.items():
            cols = [(k, k) for k in _POLICY_TRACE]
            if name in (ENDPOINT_POLICY, GHOST_POLICY):
                cols += [(f"world:{k}", k) for k in _WORLD_TRACE]
            dst = trace.setdefault(name, {})
            for slot, col in cols:
                dst.setdefault(slot, []).append(
                    np.asarray(out[col]).astype(_TRACE_DTYPES[col])
                )

    def _attach_run_shift_payloads(
        self,
        metrics: dict[str, dict],
        trace: dict[str, dict[str, list[np.ndarray]]],
    ) -> None:
        """``shift_response`` / ``regimes`` over the whole run (global rounds)
        onto the last segment's metrics, each entry marked ``continuous``."""
        cat = {
            name: {k: np.concatenate(v) for k, v in cols.items()}
            for name, cols in trace.items()
        }
        trace.clear()
        for name, m in metrics.items():
            world = cat[GHOST_POLICY if name == GHOST_POLICY else ENDPOINT_POLICY]
            out = {k: world[f"world:{k}"] for k in _WORLD_TRACE}
            out.update({k: cat[name][k] for k in _POLICY_TRACE})
            m["shift_response"] = [
                {**sr, **resolved_shift_fields(rec), "continuous": True}
                for sr, rec in zip(
                    shift_response(out, self.shift_rounds), self.resolved, strict=True
                )
            ]
            m["regimes"] = [
                {**g, "continuous": True}
                for g in regime_stats(
                    out, self.regime_bounds, self.arm_ids, self.env.segment_names
                )
            ]

    def _segment_metrics(
        self, policy: str, segment: int, out: dict[str, np.ndarray]
    ) -> dict:
        if self.s.keep_outputs:
            self.outputs[(segment, policy)] = out
        m = episode_metrics(
            out,
            policy=policy,
            episode=segment,
            arm_ids=self.arm_ids,
            segment_names=self.env.segment_names,
            checkpoints=self._segment_checkpoints(segment),
        )
        m["curve"]["segment_start"] = segment * self.cfg.horizon
        return m

    def _ghost(self, key: Any) -> dict[str, np.ndarray]:
        """The ghost episode: LinTS (with the run's discount when it forgets)
        on the unshifted environment, same episode key."""
        assert self.ghost_env is not None
        out = simulate.run_episodes(
            self._ghost_policy(),
            self.ghost_env,
            key[None],
            self.cfg.horizon,
            self.cfg.batch_size,
        )
        return {k: v[0] for k, v in out.items()}

    def _metrics(self, policy: str, episode: int, out: dict[str, np.ndarray]) -> dict:
        if self.s.keep_outputs:
            self.outputs[(episode, policy)] = out
        m = episode_metrics(
            out,
            policy=policy,
            episode=episode,
            arm_ids=self.arm_ids,
            segment_names=self.env.segment_names,
            checkpoints=self.checkpoints,
        )
        if self.shifts:
            m["shift_response"] = [
                {**sr, **resolved_shift_fields(rec)}
                for sr, rec in zip(
                    shift_response(out, self.shift_rounds), self.resolved, strict=True
                )
            ]
            m["regimes"] = regime_stats(
                out, self.regime_bounds, self.arm_ids, self.env.segment_names
            )
        return self._row(m)

    def _row(self, m: dict) -> dict:
        return bq.build_episode_metrics_row(
            m,
            experiment_id=self.experiment_id,
            created_at=self.now(),
            traffic_run=self.traffic_run,
        )


def run_traffic(
    cfg: ExperimentConfig,
    client: EndpointClient,
    writer: bq.RowWriter,
    settings: TrafficSettings,
    *,
    experiment_id: str | None = None,
    now: Callable[[], dt.datetime] = _utcnow,
    shifts: Sequence[ShiftSpec] = (),
    traffic_run: int = 1,
    forget: bool | None = None,
    learning: str = "per_episode",
) -> TrafficSummary:
    return TrafficRunner(
        cfg,
        client,
        writer,
        settings,
        experiment_id=experiment_id,
        now=now,
        shifts=shifts,
        traffic_run=traffic_run,
        forget=forget,
        learning=learning,
    ).run()
