"""The traffic loop end to end (``bandit_traffic.traffic`` / ``.main``), with the
in-process fake endpoint and a fake BigQuery client (no GCP)."""

import json
import re

import jax
import numpy as np
import pytest

from bandit import simulate
from bandit.config import (
    RESET_DISCOUNT_BOUNDS,
    ScenarioOverrides,
    build_sim_config,
    default_shift_discount,
    experiment_config_to_dict,
    load_scenario,
    resolve_scenario,
    shifts_from_dict,
    validate_shifts,
)
from bandit.metrics import log_checkpoints, make_checkpoints, merge_checkpoints
from bandit_traffic import bq, main, traffic
from bandit_traffic.endpoint_client import InProcessClient
from bandit_traffic.fake_endpoint import FakeBanditEndpoint
from tests._bandit_sizes import BATCH, HORIZON_M, HORIZON_S
from tests._fake_bq import FakeBigQueryClient as FakeBQ

EID = "0123456789abcdef"
# Plumbing checks only, on the shared test sizes (compiled programs are reused
# across tests and files). Whether LinTS actually beats uniform is a statistical
# question for the simulator tests (test_bandit_simulate_metrics.py).
E, T, BS = 2, HORIZON_S, BATCH
POLICIES = {"linear_ts", *traffic.BASELINES}


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
    assert rids[0] == f"{EID}-r1-e0-r0" and rids[-1] == f"{EID}-r1-e1-r{T - 1}"
    assert {r["traffic_run"] for r in events} == {1}
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


def test_oracle_has_zero_regret(run):
    _, _, _, summary = run
    regret = summary.regret_by_policy()
    assert regret["oracle"] == 0.0
    assert set(regret) == POLICIES


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


def _small_run(client_wrapper, settings, horizon=T):
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
    assert len(decisions) > T // 30
    for req in client.requests:
        assert len(req) <= 30
        assert len(json.dumps({"instances": req}, separators=(",", ":"))) <= max_bytes
    assert sum(len(r) for r in decisions) == T
    assert len(fake.rows("bandit_events")) == T


def test_per_instance_errors_tolerated():
    client, fake, summary = _small_run(
        lambda inner: RecordingClient(inner, error_every=50),
        traffic.TrafficSettings(baselines=("oracle",)),
    )
    n_errors = T // 50
    assert summary.decision_errors == n_errors
    events = fake.rows("bandit_events")
    assert len(events) == T - n_errors
    metrics = fake.rows("bandit_episode_metrics")
    assert {r["policy"] for r in metrics} == {"linear_ts", "oracle"}
    assert next(r for r in metrics if r["policy"] == "linear_ts")["horizon"] == T
    # errored decisions get no reward
    rewarded = [
        i["request_id"] for r in client.requests for i in r if i["type"] == "reward"
    ]
    assert len(rewarded) == T - n_errors
    assert set(rewarded) == {e["request_id"] for e in events}


def test_error_rate_above_threshold_aborts():
    with pytest.raises(traffic.EndpointErrorRate):
        _small_run(
            lambda inner: RecordingClient(inner, error_every=5),
            traffic.TrafficSettings(error_threshold=0.1, baselines=("oracle",)),
        )


def _write_config(tmp_path, **kw):
    path = tmp_path / "experiment.json"
    path.write_text(json.dumps(experiment_config_to_dict(_cfg(**kw))))
    return path


def test_main_dry_run_writes_jsonl(tmp_path):
    # --horizon overrides the config's horizon
    cfg_path = _write_config(tmp_path, horizon=HORIZON_M)
    out = tmp_path / "out"
    code = main.main(
        [
            "--in-process",
            "--config",
            str(cfg_path),
            "--episodes",
            "2",
            "--horizon",
            str(T),
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
    assert len(events) == 2 * T
    assert len(metrics) == 2 * len(POLICIES)
    assert [json.loads(p["params"]["progress"]) for p in progress] == [
        {"episodes_done": 1, "episodes_total": 2},
        {"episodes_done": 2, "episodes_total": 2},
    ]
    assert all(set(e) == set(bq.EVENT_COLUMN_TYPES) for e in events)


def test_main_env_and_injected_writer(tmp_path):
    cfg_path = _write_config(tmp_path)
    fake_bq = FakeBQ()
    env = {"CONFIG_URI": str(cfg_path), "EPISODES": "1", "EXPERIMENT_ID": EID}
    code = main.main(["--in-process"], env=env, writer=_writer(fake_bq))
    assert code == 0
    assert len(fake_bq.rows("bandit_events")) == T
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
    cfg_path = _write_config(tmp_path)
    env = {k: (str(cfg_path) if v == "CFG" else v) for k, v in env.items()}
    assert main.main([*argv, "--out", str(tmp_path / "o")], env=env) == 2


def test_main_endpoint_failure_exits_1(tmp_path):
    cfg_path = _write_config(tmp_path)

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
# Segment frequencies over T = 1000 users: the tuned mix is 0.45 away from the
# default (uniform 0.25) on the first segment, so a 0.1 tolerance (~7 sd of the
# p = 0.7 frequency, ~10 sd of the p = 0.1 ones) proves the override reached the
# environment without depending on the seed (a 0.04 tolerance fails for some).
MIX_ATOL = 0.1


def _segment_freqs(events, names):
    counts = {n: 0 for n in names}
    for e in events:
        counts[e["segment"]] += 1
    return np.array([counts[n] / len(events) for n in names])


def test_traffic_with_overrides_uses_tuned_environment():
    cfg = _cfg(episodes=1, scenario_overrides=ScenarioOverrides(segment_mix=MIX))
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
    assert len(events) == T
    names = [s.name for s in load_scenario("segment_winners").segments]
    np.testing.assert_allclose(_segment_freqs(events, names), MIX, atol=MIX_ATOL)


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
            str(T),
            "--dry-run",
            "--out",
            str(out),
        ],
        env={},
    )
    assert code == 0
    events = [json.loads(line) for line in (out / "events.jsonl").read_text().split()]
    names = [s.name for s in load_scenario("segment_winners").segments]
    np.testing.assert_allclose(_segment_freqs(events, names), MIX, atol=MIX_ATOL)


def test_no_shift_run_is_unchanged(run):
    """Without shifts: log checkpoints, no ghost row, NULL shift payloads,
    run 1 everywhere and no reset discount."""
    _, fake, endpoint, summary = run
    rows = fake.rows("bandit_episode_metrics")
    assert traffic.GHOST_POLICY not in {r["policy"] for r in rows}
    assert all(r["traffic_run"] == 1 for r in rows)
    assert all(r["shift_response"] is None and r["regimes"] is None for r in rows)
    cps = json.loads(rows[0]["curve"])["checkpoints"]
    assert cps == log_checkpoints(T, 50)
    assert summary.discount is None and summary.resolved_shifts == []
    assert endpoint.discount is None
    assert f"{EID}-r1-e0-linear_ts" in {bq.metrics_row_id(r) for r in rows}


# ----------------------------------------------- scripted shifts (contracts §10)

SHIFTS = [
    {"kind": "demote", "at_frac": 0.5, "creative_id": "leader", "drop_pp": 0.015},
    {
        "kind": "shock",
        "at_frac": 0.7,
        "until_frac": 0.8,
        "creative_id": "leader",
        "ctr_multiplier": 0.5,
    },
]
RUN = 3


@pytest.fixture(scope="module")
def shifted():
    cfg = _cfg()
    shifts = validate_shifts(
        shifts_from_dict(SHIFTS), resolve_scenario(cfg), cfg.arms, cfg.ctr_mode
    )
    fake_bq = FakeBQ()
    client = RecordingClient(InProcessClient(FakeBanditEndpoint(cfg)))
    tr = traffic.TrafficRunner(
        cfg,
        client,
        _writer(fake_bq),
        traffic.TrafficSettings(keep_outputs=True),
        shifts=shifts,
        traffic_run=RUN,
        forget=True,
    )
    summary = tr.run()
    return cfg, fake_bq, client, tr, summary


def test_shifted_run_numbers_every_row(shifted):
    _, fake, _, _, _ = shifted
    events = fake.rows("bandit_events")
    assert len(events) == E * T
    assert {r["traffic_run"] for r in events} == {RUN}
    assert events[0]["request_id"] == f"{EID}-r{RUN}-e0-r0"
    rows = fake.rows("bandit_episode_metrics")
    assert sorted((r["episode"], r["policy"]) for r in rows) == sorted(
        (e, p) for e in range(E) for p in POLICIES | {traffic.GHOST_POLICY}
    )
    assert {r["traffic_run"] for r in rows} == {RUN}
    for table, inserted, row_ids in fake.inserts:
        if table.endswith("bandit_episode_metrics"):
            assert row_ids == [
                f"{EID}-r{RUN}-e{r['episode']}-{r['policy']}" for r in inserted
            ]


def test_shifted_run_sends_the_forget_discount(shifted):
    cfg, _, client, tr, summary = shifted
    resets = [i for req in client.requests for i in req if i["type"] == "reset"]
    assert len(resets) == E
    # T = 1000 -> default_shift_discount = exp(-0.8) ~ 0.449, floored at 0.95
    assert default_shift_discount(cfg.ctr_mode, BS, T) < RESET_DISCOUNT_BOUNDS[0]
    assert {r["discount"] for r in resets} == {RESET_DISCOUNT_BOUNDS[0]}
    # the simulator's policy stream per episode (contracts §2)
    for e, r in enumerate(resets):
        k_pol = simulate.episode_streams(tr.keys[e])[2]
        assert r["policy_key"] == [int(w) for w in jax.random.key_data(k_pol)]
        assert r["batch_size"] == BS
    decisions = [i for req in client.requests for i in req if i["type"] == "decision"]
    assert [(d["batch"], d["row"]) for d in decisions[: BS + 1]] == [
        (0, j) for j in range(BS)
    ] + [(1, 0)]
    assert summary.discount == tr.discount == RESET_DISCOUNT_BOUNDS[0]
    assert client.inner.target.discount == RESET_DISCOUNT_BOUNDS[0]
    assert traffic.run_discount(_cfg(horizon=40_000)) == 0.98


def test_shifted_run_resolves_and_records_shifts(shifted):
    _, _, _, tr, summary = shifted
    resolved = summary.resolved_shifts
    assert [r["kind"] for r in resolved] == ["demote", "shock"]
    assert [r["round"] for r in resolved] == [T // 2, int(0.7 * T)]
    assert resolved[1]["end_round"] == int(0.8 * T)
    assert all(r["creative_id"] in tr.arm_ids for r in resolved)
    assert tr.regime_bounds == [T // 2, int(0.7 * T), int(0.8 * T)]


def test_shifted_rows_carry_shift_response_and_regimes(shifted):
    _, fake, _, tr, _ = shifted
    rows = fake.rows("bandit_episode_metrics")
    cps = {tuple(json.loads(r["curve"])["checkpoints"]) for r in rows}
    assert len(cps) == 1
    expected = merge_checkpoints(make_checkpoints(T, 50, "linear"), tr.shift_rounds, T)
    assert list(next(iter(cps))) == expected
    for r in rows:
        sr = json.loads(r["shift_response"])
        assert [s["round"] for s in sr] == tr.shift_rounds
        regimes = json.loads(r["regimes"])
        assert [(g["start"], g["end"]) for g in regimes] == [
            (0, T // 2),
            (T // 2, int(0.7 * T)),
            (int(0.7 * T), int(0.8 * T)),
            (int(0.8 * T), T),
        ]
        for g in regimes:
            assert set(g["true_ctr"]) == set(tr.arm_ids)
            assert sum(s["rounds"] for s in g["per_segment"].values()) == (
                g["end"] - g["start"]
            )
    # the demoted leader's true CTR drops after the shift (endpoint world)
    leader = tr.resolved[0]["creative_id"]
    lt = json.loads(next(r for r in rows if r["policy"] == "linear_ts")["regimes"])
    assert lt[1]["true_ctr"][leader] < lt[0]["true_ctr"][leader]


def test_shift_response_entries_carry_the_resolved_shift(shifted):
    """Each metrics row's shift_response entry carries the authoritative
    resolution of its shift (contracts §3/§10), "leader" made concrete, while
    keeping the shift_response numbers."""
    _, fake, _, tr, _ = shifted
    rows = fake.rows("bandit_episode_metrics")
    assert rows
    for r in rows:
        sr = json.loads(r["shift_response"])
        assert len(sr) == len(tr.resolved)
        for entry, rec in zip(sr, tr.resolved, strict=True):
            for key in (
                "pct_optimal_before",
                "pct_optimal_after",
                "regret_rate_before",
                "regret_rate_after",
                "recovery_rounds",
            ):
                assert key in entry
            assert entry["round"] == rec["round"]
            assert entry["index"] == rec["index"]
            assert entry["kind"] == rec["kind"]
            assert entry["end_round"] == rec["end_round"]
            assert entry["segment"] == rec["segment"]
            assert entry["requested_creative_id"] == "leader"
            assert entry["creative_id"] == rec["creative_id"]
            assert entry["creative_id"] in tr.arm_ids
            assert entry["targets"]
            assert entry["targets"] == [
                {k: t[k] for k in ("segment", "ctr_before", "ctr_after")}
                for t in rec["targets"]
            ]
    first = json.loads(rows[0]["shift_response"])
    assert first[1]["end_round"] == int(0.8 * T)


def test_resolved_shift_fields_for_a_mix_shift():
    rec = {
        "index": 0,
        "kind": "mix",
        "at_frac": 0.5,
        "round": 10,
        "end_round": None,
        "segment_mix": [1, 1],
        "segment_weights": [0.5, 0.5],
    }
    assert traffic.resolved_shift_fields(rec) == {
        "index": 0,
        "kind": "mix",
        "round": 10,
        "end_round": None,
        "segment": None,
        "creative_id": None,
        "requested_creative_id": None,
        "targets": [],
    }


def test_baselines_share_the_endpoints_users(shifted):
    _, _, _, tr, _ = shifted
    for e in range(E):
        ours = tr.outputs[(e, "linear_ts")]["segment"]
        for name in traffic.BASELINES:
            np.testing.assert_array_equal(tr.outputs[(e, name)]["segment"], ours)


def test_ghost_sees_the_same_world_until_the_first_shift(shifted):
    """No mix shift here, so the ghost's users are the endpoint's throughout;
    the true click probabilities match exactly before the first shift round and
    differ after it (the shifted world). Before the shift the ghost and the fake
    endpoint are also the same LinTS (same params, forget discount and policy
    stream, contracts §2), so their pre-shift metrics agree;
    tests/test_bandit_endpoint_parity.py checks arm-for-arm parity of the ghost
    against the real predictor."""
    _, fake, _, tr, _ = shifted
    r0 = tr.shift_rounds[0]
    for e in range(E):
        ours = tr.outputs[(e, "linear_ts")]
        ghost = tr.outputs[(e, traffic.GHOST_POLICY)]
        np.testing.assert_array_equal(ghost["segment"], ours["segment"])
        np.testing.assert_allclose(ghost["p_all"][:r0], ours["p_all"][:r0], rtol=1e-6)
        assert not np.allclose(ghost["p_all"][r0:], ours["p_all"][r0:])
        np.testing.assert_array_equal(ghost["opt_arm"][:r0], ours["opt_arm"][:r0])
        np.testing.assert_array_equal(ghost["arm"][:r0], ours["arm"][:r0])
    rows = fake.rows("bandit_episode_metrics")

    def before(policy):
        return [
            json.loads(r["shift_response"])[0]["pct_optimal_before"]
            for r in sorted(rows, key=lambda r: r["episode"])
            if r["policy"] == policy
        ]

    assert before("linear_ts") == before(traffic.GHOST_POLICY)
    assert before("oracle") == [1.0] * E


def test_main_runs_shifts_from_env(tmp_path):
    cfg_path = _write_config(tmp_path)
    fake_bq = FakeBQ()
    env = {
        "CONFIG_URI": str(cfg_path),
        "EPISODES": "1",
        "SHIFTS_JSON": json.dumps(SHIFTS),
        "TRAFFIC_RUN": "2",
    }
    assert main.main(["--in-process"], env=env, writer=_writer(fake_bq)) == 0
    rows = fake_bq.rows("bandit_episode_metrics")
    assert {r["traffic_run"] for r in rows} == {2}
    assert traffic.GHOST_POLICY in {r["policy"] for r in rows}
    assert all(r["shift_response"] for r in rows)


def _options(tmp_path, argv=(), **env):
    cfg_path = _write_config(tmp_path)
    args = main.build_parser().parse_args(["--config", str(cfg_path), *argv])
    cfg, _ = main.resolve_config(args, {})
    return main.resolve_run_options(args, env, cfg)


def test_run_options_defaults_and_parsing(tmp_path):
    opts = _options(tmp_path)
    assert opts == main.RunOptions(shifts=(), traffic_run=1, forget=False)
    opts = _options(tmp_path, SHIFTS_JSON=json.dumps(SHIFTS))
    assert len(opts.shifts) == 2 and opts.forget is True and opts.traffic_run == 1
    opts = _options(tmp_path, SHIFTS_JSON=json.dumps(SHIFTS), FORGET="false")
    assert opts.forget is False
    opts = _options(tmp_path, ["--no-forget"], SHIFTS_JSON="[]", FORGET="true")
    assert opts.forget is False and opts.shifts == ()
    opts = _options(tmp_path, SHIFTS_JSON="[]", FORGET="1", TRAFFIC_RUN="4")
    assert (opts.shifts, opts.forget, opts.traffic_run) == ((), True, 4)
    path = tmp_path / "shifts.json"
    path.write_text(json.dumps(SHIFTS[:1]))
    opts = _options(tmp_path, ["--shifts", str(path), "--traffic-run", "5"])
    assert len(opts.shifts) == 1 and opts.traffic_run == 5


@pytest.mark.parametrize(
    "env",
    [
        {"SHIFTS_JSON": "not json"},
        {"SHIFTS_JSON": '{"kind": "mix"}'},  # not a list
        {"SHIFTS_JSON": '[{"kind": "teleport", "at_frac": 0.5}]'},
        {"SHIFTS_JSON": '[{"kind": "promote", "at_frac": 0.5, "creative_id": "zz", '
                        '"lift_pp": 0.01}]'},  # not an arm
        {"SHIFTS_JSON": '[{"kind": "promote", "at_frac": 0.5, "creative_id": "leader",'
                        ' "lift_pp": 0.01}]'},  # leader only for demote / shock
        {"SHIFTS_JSON": '[{"kind": "mix", "at_frac": 0.5, "segment_mix": [1, 1]}]'},
        {"SHIFTS_JSON": '[{"kind": "demote", "at_frac": 0.5, "creative_id": "leader",'
                        ' "drop_pp": 0.015, "segment": "martians"}]'},
        {"TRAFFIC_RUN": "0"},
        {"TRAFFIC_RUN": "two"},
        {"FORGET": "maybe"},
    ],
)  # fmt: skip
def test_main_bad_run_options_exit_2(tmp_path, env):
    cfg_path = _write_config(tmp_path)
    env = {"CONFIG_URI": str(cfg_path), **env}
    argv = ["--in-process", "--dry-run", "--out", str(tmp_path / "o")]
    assert main.main(argv, env=env) == 2
