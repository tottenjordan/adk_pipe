"""The traffic loop end to end (``bandit_traffic.traffic`` / ``.main``), with the
in-process fake endpoint and a fake BigQuery client (no GCP)."""

import json
import re

import jax
import numpy as np
import pytest

from bandit import simulate
from bandit.config import (
    ScenarioOverrides,
    build_sim_config,
    experiment_config_to_dict,
    load_scenario,
    resolve_scenario,
)
from bandit.policies import make_policy
from bandit_traffic import bq, main, traffic
from bandit_traffic.endpoint_client import InProcessClient
from bandit_traffic.fake_endpoint import FakeBanditEndpoint

EID = "0123456789abcdef"
E, T, BS = 2, 2000, 100
POLICIES = {"linear_ts", *traffic.BASELINES}


class FakeBQ:
    def __init__(self):
        self.inserts: list[tuple[str, list[dict], list[str]]] = []
        self.queries: list[tuple[str, list]] = []

    def insert_rows_json(self, table, rows, row_ids=None):
        self.inserts.append((table, list(rows), list(row_ids or [])))
        return []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        return self

    def result(self):
        return []

    def rows(self, table_suffix):
        return [
            r for t, rows, _ in self.inserts if t.endswith(table_suffix) for r in rows
        ]


TABLES = {
    "experiments": "p.d.bandit_experiments",
    "events": "p.d.bandit_events",
    "metrics": "p.d.bandit_episode_metrics",
}


def _writer(fake: FakeBQ) -> bq.BigQueryWriter:
    return bq.BigQueryWriter(
        TABLES, client_factory=lambda: fake, job_config_factory=lambda p: p
    )


def _cfg(**kw):
    base = dict(horizon=T, episodes=E, batch_size=BS, seed=0, experiment_id=EID)
    base.update(kw)
    return build_sim_config("segment_winners", **base)


@pytest.fixture(scope="module")
def run():
    cfg = _cfg()
    fake_bq = FakeBQ()
    endpoint = FakeBanditEndpoint(cfg)
    summary = traffic.run_traffic(
        cfg, InProcessClient(endpoint), _writer(fake_bq), traffic.TrafficSettings()
    )
    return cfg, fake_bq, endpoint, summary


def test_event_rows(run):
    cfg, fake, _, summary = run
    events = fake.rows("bandit_events")
    assert len(events) == E * T == summary.events_written
    assert all(list(r) == list(bq.EVENT_COLUMN_TYPES) for r in events)
    assert {r["policy"] for r in events} == {"linear_ts"}
    rids = [r["request_id"] for r in events]
    assert len(set(rids)) == len(rids)
    assert rids[0] == f"{EID}-e0-r0" and rids[-1] == f"{EID}-e1-r{T - 1}"
    # streaming insertId = request_id
    for table, rows, row_ids in fake.inserts:
        if table.endswith("bandit_events"):
            assert row_ids == [r["request_id"] for r in rows]
            assert len(rows) <= bq.INSERT_BATCH_ROWS
    arm_ids = {a.creative_id for a in cfg.arms}
    r = events[123]
    assert r["arm"] in arm_ids and r["optimal_arm"] in arm_ids
    assert isinstance(json.loads(r["context"]), dict)
    assert r["batch"] == r["round"] // BS
    assert r["regret"] >= -1e-9 and 0 < r["propensity"] <= 1
    assert r["dwell_s"] is None  # click mode
    assert re.match(r"^\d{4}-\d\d-\d\dT.*Z$", r["ts"])


def test_one_metrics_row_per_episode_and_policy(run):
    _, fake, _, _ = run
    rows = fake.rows("bandit_episode_metrics")
    assert sorted((r["episode"], r["policy"]) for r in rows) == sorted(
        (e, p) for e in range(E) for p in POLICIES
    )
    assert all(list(r) == list(bq.METRICS_COLUMN_TYPES) for r in rows)
    cps = {tuple(json.loads(r["curve"])["checkpoints"]) for r in rows}
    assert len(cps) == 1 and next(iter(cps))[-1] == T  # shared checkpoints
    assert all(r["horizon"] == T for r in rows)


def test_oracle_zero_regret_and_linear_ts_beats_uniform(run):
    _, _, _, summary = run
    regret = summary.regret_by_policy()
    assert regret["oracle"] == 0.0
    assert regret["linear_ts"] < regret["uniform"]


def test_linear_ts_metrics_match_events(run):
    _, fake, _, _ = run
    events = [r for r in fake.rows("bandit_events") if r["episode"] == 0]
    row = next(
        r
        for r in fake.rows("bandit_episode_metrics")
        if r["episode"] == 0 and r["policy"] == "linear_ts"
    )
    assert row["total_clicks"] == sum(e["clicked"] for e in events)
    assert row["cumulative_regret"] == pytest.approx(
        sum(e["regret"] for e in events), rel=1e-4
    )
    rounds = {k: v["rounds"] for k, v in json.loads(row["per_segment"]).items()}
    seg_counts: dict[str, int] = {}
    for e in events:
        seg_counts[e["segment"]] = seg_counts.get(e["segment"], 0) + 1
    assert rounds == seg_counts


def test_a_reward_request_per_batch(run):
    _, _, endpoint, summary = run
    kinds = [set(types) for types, _ in endpoint.calls]
    assert kinds.count({"reset"}) == E
    assert kinds.count({"decision"}) == E * (T // BS)
    assert kinds.count({"reward"}) == E * (T // BS)
    assert summary.rewards_rejected == 0 and summary.reward_errors == 0


def test_progress_updates_are_column_level(run):
    _, fake, _, _ = run
    assert len(fake.queries) == E
    for i, (sql, params) in enumerate(fake.queries, start=1):
        assert sql.startswith("UPDATE `p.d.bandit_experiments` SET ")
        set_clause = sql.split(" SET ")[1].split(" WHERE ")[0]
        assert set_clause == "progress = @progress, updated_at = @updated_at"
        values = {name: value for name, _, value in params}
        assert json.loads(values["progress"]) == {
            "episodes_done": i,
            "episodes_total": E,
        }
        assert values["experiment_id"] == EID


def test_batch_draws_reproduce_simulator_streams():
    cfg = _cfg(horizon=300, episodes=1)
    env = simulate.build_environment(cfg)
    key = simulate.episode_keys(cfg.seed, cfg.scenario, 1)[0]
    k_ctx, k_rew, _ = simulate.episode_streams(key)
    elig = jax.numpy.ones((env.num_arms,), bool)
    segs = np.concatenate(
        [
            np.asarray(
                simulate.batch_draws(env.model, k_ctx, k_rew, b, BS, "click", elig)[
                    "segment"
                ]
            )
            for b in range(3)
        ]
    )
    pol = make_policy("uniform", lints_params=cfg.policy)
    out = simulate.run_episode(pol, env, key, 300, BS)
    np.testing.assert_array_equal(segs, out["segment"])


class RecordingClient:
    """Wraps a client; records request sizes; optionally turns decisions into errors."""

    def __init__(self, inner, error_every: int = 0):
        self.inner = inner
        self.error_every = error_every
        self.requests: list[list[dict]] = []
        self.n_decisions = 0

    def predict(self, instances, parameters=None):
        self.requests.append(list(instances))
        preds = self.inner.predict(instances, parameters)
        if self.error_every:
            for i, inst in enumerate(instances):
                if inst["type"] == "decision":
                    self.n_decisions += 1
                    if self.n_decisions % self.error_every == 0:
                        preds[i] = {
                            "type": "error",
                            "request_id": inst["request_id"],
                            "error": "boom",
                        }
        return preds


def _small_run(client_wrapper, settings, horizon=400):
    cfg = _cfg(horizon=horizon, episodes=1)
    client = client_wrapper(InProcessClient(FakeBanditEndpoint(cfg)))
    fake_bq = FakeBQ()
    summary = traffic.run_traffic(cfg, client, _writer(fake_bq), settings)
    return client, fake_bq, summary


def test_requests_split_under_size_limits():
    max_bytes = 8_000
    client, fake, _ = _small_run(
        RecordingClient,
        traffic.TrafficSettings(
            max_request_instances=30, max_request_bytes=max_bytes, baselines=("oracle",)
        ),
    )
    decisions = [r for r in client.requests if r[0]["type"] == "decision"]
    assert len(decisions) > 400 // 30
    for req in client.requests:
        assert len(req) <= 30
        assert len(json.dumps({"instances": req}, separators=(",", ":"))) <= max_bytes
    assert sum(len(r) for r in decisions) == 400
    assert len(fake.rows("bandit_events")) == 400


def test_per_instance_errors_tolerated():
    client, fake, summary = _small_run(
        lambda inner: RecordingClient(inner, error_every=50),
        traffic.TrafficSettings(baselines=("oracle",)),
    )
    assert summary.decision_errors == 8
    events = fake.rows("bandit_events")
    assert len(events) == 400 - 8
    metrics = fake.rows("bandit_episode_metrics")
    assert {r["policy"] for r in metrics} == {"linear_ts", "oracle"}
    assert next(r for r in metrics if r["policy"] == "linear_ts")["horizon"] == 400
    # errored decisions get no reward
    rewarded = [
        i["request_id"] for r in client.requests for i in r if i["type"] == "reward"
    ]
    assert len(rewarded) == 400 - 8
    assert set(rewarded) == {e["request_id"] for e in events}


def test_error_rate_above_threshold_aborts():
    with pytest.raises(traffic.EndpointErrorRate):
        _small_run(
            lambda inner: RecordingClient(inner, error_every=5),
            traffic.TrafficSettings(error_threshold=0.1, baselines=("oracle",)),
            horizon=1000,
        )


def _write_config(tmp_path, **kw):
    path = tmp_path / "experiment.json"
    path.write_text(json.dumps(experiment_config_to_dict(_cfg(**kw))))
    return path


def test_main_dry_run_writes_jsonl(tmp_path):
    cfg_path = _write_config(tmp_path)
    out = tmp_path / "out"
    code = main.main(
        [
            "--in-process",
            "--config",
            str(cfg_path),
            "--episodes",
            "2",
            "--horizon",
            "200",
            "--dry-run",
            "--out",
            str(out),
        ],
        env={},
    )
    assert code == 0
    events = [json.loads(line) for line in (out / "events.jsonl").read_text().split()]
    metrics = [
        json.loads(line) for line in (out / "episode_metrics.jsonl").read_text().split()
    ]
    progress = [
        json.loads(line) for line in (out / "progress.jsonl").read_text().splitlines()
    ]
    assert len(events) == 2 * 200
    assert len(metrics) == 2 * len(POLICIES)
    assert [json.loads(p["params"]["progress"]) for p in progress] == [
        {"episodes_done": 1, "episodes_total": 2},
        {"episodes_done": 2, "episodes_total": 2},
    ]
    assert all(set(e) == set(bq.EVENT_COLUMN_TYPES) for e in events)


def test_main_env_and_injected_writer(tmp_path):
    cfg_path = _write_config(tmp_path, horizon=200)
    fake_bq = FakeBQ()
    env = {"CONFIG_URI": str(cfg_path), "EPISODES": "1", "EXPERIMENT_ID": EID}
    code = main.main(["--in-process"], env=env, writer=_writer(fake_bq))
    assert code == 0
    assert len(fake_bq.rows("bandit_events")) == 200
    assert len(fake_bq.queries) == 1


@pytest.mark.parametrize(
    ("argv", "env"),
    [
        (["--in-process"], {}),  # no CONFIG_URI
        ([], {"CONFIG_URI": "CFG"}),  # no target
        ([], {"CONFIG_URI": "CFG", "ENDPOINT_ID": "123"}),  # not a resource name
        (["--in-process"], {"CONFIG_URI": "CFG", "EPISODES": "x"}),
        (["--in-process"], {"CONFIG_URI": "CFG", "HORIZON": "5"}),
        (["--in-process"], {"CONFIG_URI": "CFG"}),  # not dry-run, no BQ env
        (["--in-process", "--dry-run"], {"CONFIG_URI": "/nonexistent.json"}),
    ],
)
def test_main_config_errors_exit_2(tmp_path, argv, env):
    cfg_path = _write_config(tmp_path, horizon=200)
    env = {k: (str(cfg_path) if v == "CFG" else v) for k, v in env.items()}
    assert main.main([*argv, "--out", str(tmp_path / "o")], env=env) == 2


def test_main_endpoint_failure_exits_1(tmp_path):
    cfg_path = _write_config(tmp_path, horizon=200)

    class Down:
        def predict(self, instances, parameters=None):
            from bandit_traffic.endpoint_client import EndpointError

            raise EndpointError("HTTP 404")

    code = main.main(
        ["--dry-run", "--out", str(tmp_path / "o")],
        env={"CONFIG_URI": str(cfg_path)},
        client=Down(),
    )
    assert code == 1


# ------------------------------------------------- scenario overrides (contracts §9)

MIX = (0.7, 0.1, 0.1, 0.1)


def _segment_freqs(events, names):
    counts = {n: 0 for n in names}
    for e in events:
        counts[e["segment"]] += 1
    return np.array([counts[n] / len(events) for n in names])


def test_traffic_with_overrides_uses_tuned_environment():
    cfg = _cfg(
        horizon=2000, episodes=1, scenario_overrides=ScenarioOverrides(segment_mix=MIX)
    )
    tr = traffic.TrafficRunner(
        cfg,
        InProcessClient(FakeBanditEndpoint(cfg)),
        _writer(FakeBQ()),
        traffic.TrafficSettings(),
    )
    assert tr.env.scenario == resolve_scenario(cfg)
    assert [s.weight for s in tr.env.scenario.segments] == pytest.approx(MIX)

    fake_bq = FakeBQ()
    traffic.run_traffic(
        cfg,
        InProcessClient(FakeBanditEndpoint(cfg)),
        _writer(fake_bq),
        traffic.TrafficSettings(baselines=("oracle",)),
    )
    events = fake_bq.rows("bandit_events")
    assert len(events) == 2000
    names = [s.name for s in load_scenario("segment_winners").segments]
    np.testing.assert_allclose(_segment_freqs(events, names), MIX, atol=0.04)


def test_main_dry_run_reads_overrides_from_config(tmp_path):
    cfg_path = _write_config(
        tmp_path, scenario_overrides=ScenarioOverrides(segment_mix=MIX, gap_scale=1.5)
    )
    written = json.loads(cfg_path.read_text())
    assert written["scenario_overrides"] == {"segment_mix": list(MIX), "gap_scale": 1.5}
    out = tmp_path / "out"
    code = main.main(
        [
            "--in-process",
            "--config",
            str(cfg_path),
            "--episodes",
            "1",
            "--horizon",
            "2000",
            "--dry-run",
            "--out",
            str(out),
        ],
        env={},
    )
    assert code == 0
    events = [json.loads(line) for line in (out / "events.jsonl").read_text().split()]
    names = [s.name for s in load_scenario("segment_winners").segments]
    np.testing.assert_allclose(_segment_freqs(events, names), MIX, atol=0.04)
