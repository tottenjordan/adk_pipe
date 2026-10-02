"""The synthetic-traffic loop: drive the endpoint, replay baselines, log rows.

For each episode ``e`` (key = ``bandit.simulate.episode_keys(seed, scenario)[e]``):

1. send ``reset {episode, seed}``;
2. for each batch ``b``: draw the batch with ``bandit.simulate.batch_draws``
   (the simulator's own context / reward streams, i.e. common random numbers),
   send the decision instances (split under the request limits), read each chosen
   arm's outcome from the pre-drawn coin flips, send the reward instances;
3. replay the baselines (``ucb1``, ``epsilon_greedy``, ``beta_bernoulli_ts``,
   ``uniform``, ``oracle``) locally with ``bandit.simulate.run_episodes`` on the
   same episode key, so they see identical users and coin flips;
4. build one ``bandit_episode_metrics`` row per policy with
   ``bandit.metrics.episode_metrics`` (shared log-spaced checkpoints), write them,
   and update ``bandit_experiments.progress``.

``bandit_events`` rows (endpoint policy only) are flushed in batches as the
episode runs. ``round`` is the 0-based round index within the episode (the
environment's ``t``); ``request_id`` is ``{experiment_id}-e{episode}-r{round}``.

A decision that comes back as a per-instance ``error`` gets no event row and no
reward; for the metrics the round falls back to a uniformly random arm (what an
ad server would serve by default). If the episode's decision error rate exceeds
``error_threshold`` the run aborts with ``EndpointErrorRate``.

Engaged-mode rewards are sent unscaled (click × dwell seconds, plus ``dwell_s``);
the predictor scales them like the simulator (contracts §2).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import jax
import numpy as np

from bandit import environment as envm
from bandit import simulate
from bandit.config import ExperimentConfig
from bandit.metrics import episode_metrics, log_checkpoints
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


@dataclass
class TrafficSummary:
    experiment_id: str
    episodes_done: int = 0
    events_written: int = 0
    decision_errors: int = 0
    reward_errors: int = 0
    rewards_rejected: int = 0
    requests: int = 0
    metrics: list[dict] = field(default_factory=list)

    def regret_by_policy(self) -> dict[str, float]:
        out: dict[str, list[float]] = {}
        for row in self.metrics:
            out.setdefault(row["policy"], []).append(float(row["cumulative_regret"]))
        return {p: float(np.mean(v)) for p, v in out.items()}


def reset_seed(seed: int, episode: int) -> int:
    """The endpoint's PRNG seed for an episode (31-bit, distinct per episode)."""
    return (int(seed) * 1_000_003 + int(episode)) % (2**31)


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
    ):
        self.cfg = cfg
        self.client = client
        self.writer = writer
        self.s = settings
        self.experiment_id = experiment_id or cfg.experiment_id
        self.now = now
        self.env = simulate.build_environment(cfg)
        self.arm_ids = list(self.env.arm_ids)
        self.arm_index = {cid: i for i, cid in enumerate(self.arm_ids)}
        self.checkpoints = log_checkpoints(cfg.horizon, settings.num_checkpoints)
        self.keys = simulate.episode_keys(cfg.seed, cfg.scenario, cfg.episodes)
        self.summary = TrafficSummary(self.experiment_id)
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

    def run_episode(self, episode: int) -> list[dict]:
        cfg, s, env = self.cfg, self.s, self.env
        T, bs, K = cfg.horizon, cfg.batch_size, len(self.arm_ids)
        key = self.keys[episode]
        k_ctx, k_rew, _ = simulate.episode_streams(key)
        elig = jax.numpy.ones((K,), bool)
        fallback = np.random.default_rng([cfg.seed, episode])

        reset = self._send(
            [
                {
                    "type": "reset",
                    "episode": episode,
                    "seed": reset_seed(cfg.seed, episode),
                }
            ]
        )[0]
        if _is_error(reset):
            raise TrafficError(f"reset failed for episode {episode}: {reset}")

        cols: dict[str, list[np.ndarray]] = {
            k: []
            for k in (
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
        }
        errors = 0
        num_batches = -(-T // bs)
        for b in range(num_batches):
            n = min(bs, T - b * bs)
            d = {
                k: np.asarray(v)[:n]
                for k, v in _draws(
                    env.model, k_ctx, k_rew, b, bs, env.reward_mode, elig
                ).items()
            }
            ts = bq.iso_ts(self.now())
            contexts = envm.decode_contexts(d["levels"])
            rounds = d["t"].astype(int)
            rids = [f"{self.experiment_id}-e{episode}-r{t}" for t in rounds]
            preds = self._send(
                [
                    {"type": "decision", "request_id": rid, "ts": ts, "context": ctx}
                    for rid, ctx in zip(rids, contexts, strict=True)
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
                        round=int(rounds[j]),
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

            seen = b * bs + n
            if errors and errors / seen > s.error_threshold and seen >= min(T, 500):
                self._flush_events(force=True)
                self.summary.decision_errors += errors
                raise EndpointErrorRate(
                    f"episode {episode}: {errors}/{seen} decision errors "
                    f"(> {s.error_threshold:.0%})"
                )
        self._flush_events(force=True)
        self.summary.decision_errors += errors
        if errors:
            log.warning("episode %d: %d/%d decision errors", episode, errors, T)

        ours = {k: np.concatenate(v) for k, v in cols.items()}
        rows = [self._metrics(ENDPOINT_POLICY, episode, ours)]
        for name in s.baselines:
            pol = make_policy(
                name,
                lints_params=cfg.policy,
                reward_mode=cfg.reward_mode,
                log_propensity=False,
            )
            out = simulate.run_episodes(pol, env, key[None], T, bs)
            ep = {k: v[0] for k, v in out.items()}
            if not np.array_equal(ep["segment"], ours["segment"]):
                raise TrafficError("baseline replay diverged from the endpoint's users")
            rows.append(self._metrics(name, episode, ep))
        return rows

    def _metrics(self, policy: str, episode: int, out: dict[str, np.ndarray]) -> dict:
        m = episode_metrics(
            out,
            policy=policy,
            episode=episode,
            arm_ids=self.arm_ids,
            segment_names=self.env.segment_names,
            checkpoints=self.checkpoints,
        )
        return bq.build_episode_metrics_row(
            m, experiment_id=self.experiment_id, created_at=self.now()
        )


def run_traffic(
    cfg: ExperimentConfig,
    client: EndpointClient,
    writer: bq.RowWriter,
    settings: TrafficSettings,
    *,
    experiment_id: str | None = None,
    now: Callable[[], dt.datetime] = _utcnow,
) -> TrafficSummary:
    return TrafficRunner(
        cfg, client, writer, settings, experiment_id=experiment_id, now=now
    ).run()
