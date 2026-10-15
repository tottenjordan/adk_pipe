"""``bandit_traffic.bq``: row builders, column-type maps vs contracts §3 / the DDL,
the progress UPDATE, and the writers (no BigQuery)."""

import datetime as dt
import json
import re
from pathlib import Path

import pytest

from bandit_traffic import bq

ROOT = Path(__file__).resolve().parents[1]
_BQ_TYPES = {"INTEGER": "INT64", "FLOAT": "FLOAT64"}
NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.UTC)


def _contract_columns(table: str) -> dict[str, str]:
    text = (ROOT / "docs/bandit/contracts.md").read_text()
    block = text.split(f"**`{table}`**", 1)[1].split("\n\n", 1)[0]
    body = block.split(":\n", 1)[1]
    return dict(re.findall(r"(\w+) (STRING|INT64|FLOAT64|TIMESTAMP)", body))


def _ddl_columns(env_var: str) -> dict[str, str]:
    text = (ROOT / "deployment/create_bq_tables.sh").read_text()
    schema = text.split(f'make_table "${{{env_var}}}" \\\n', 1)[1].split()[0]
    cols = (part.split(":") for part in schema.split(","))
    return {name: _BQ_TYPES.get(kind, kind) for name, kind in cols}


@pytest.mark.parametrize(
    ("table", "env_var", "types"),
    [
        ("bandit_events", "BQ_TABLE_BANDIT_EVENTS", bq.EVENT_COLUMN_TYPES),
        ("bandit_episode_metrics", "BQ_TABLE_BANDIT_METRICS", bq.METRICS_COLUMN_TYPES),
    ],
)
def test_column_types_match_contract_and_ddl(table, env_var, types):
    contract = _contract_columns(table)
    assert list(contract.items()) == list(types.items())
    assert _ddl_columns(env_var) == types


def _event(**kw):
    base = dict(
        experiment_id="e" * 16,
        episode=1,
        round=7,
        batch=0,
        request_id="x-e1-r7",
        ts=NOW,
        segment="mobile_scrollers",
        context={"devicetype": "mobile", "weekend": True},
        arm="a",
        propensity=0.4,
        reward=1.0,
        clicked=1,
        dwell_s=None,
        p_chosen=0.05,
        p_optimal=0.06,
        optimal_arm="b",
        regret=0.01,
        model_version="v3",
        latency_ms=2.5,
    )
    base.update(kw)
    return bq.build_event_row(**base)


def test_build_event_row():
    row = _event()
    assert list(row) == list(bq.EVENT_COLUMN_TYPES)
    assert row["policy"] == "linear_ts"
    assert row["ts"] == "2026-10-02T12:00:00Z"
    assert json.loads(row["context"]) == {"devicetype": "mobile", "weekend": True}
    assert isinstance(row["episode"], int) and isinstance(row["reward"], float)
    assert row["dwell_s"] is None
    assert row["traffic_run"] == 1  # default
    assert _event(traffic_run=4)["traffic_run"] == 4
    # NaN propensity (unknown) -> NULL; naive datetimes are treated as UTC
    row = _event(propensity=float("nan"), ts=dt.datetime(2026, 1, 1))
    assert row["propensity"] is None and row["ts"] == "2026-01-01T00:00:00Z"


def test_build_episode_metrics_row():
    metrics = {
        "policy": "ucb1",
        "episode": 0,
        "horizon": 100,
        "total_reward": 4.0,
        "total_clicks": 4,
        "cumulative_regret": 0.5,
        "pct_optimal": 0.7,
        "steps_to_converge": None,
        "curve": {"checkpoints": [1, 100], "cum_regret": [0, 0.5]},
        "arm_share": {"a": [1.0, 0.5]},
        "per_segment": {},
        "arm_stats": {"a": {"impressions": 100}},
        "realized_regret": 1.0,  # extra keys are dropped
        "suboptimal_pulls": {"a": 3},
    }
    row = bq.build_episode_metrics_row(metrics, experiment_id="e1", created_at=NOW)
    assert list(row) == list(bq.METRICS_COLUMN_TYPES)
    assert row["experiment_id"] == "e1" and row["steps_to_converge"] is None
    for col in ("curve", "arm_share", "per_segment", "arm_stats"):
        assert isinstance(row[col], str)
    assert json.loads(row["curve"])["checkpoints"] == [1, 100]
    assert row["created_at"] == "2026-10-02T12:00:00Z"
    assert row["traffic_run"] == 1
    assert row["shift_response"] is None and row["regimes"] is None
    assert bq.metrics_row_id(row) == "e1-r1-e0-ucb1"


def test_metrics_row_carries_run_and_shift_payloads():
    metrics = {
        "policy": "linear_ts_unshifted",
        "episode": 2,
        "horizon": 100,
        "shift_response": [{"round": 50, "recovery_rounds": None}],
        "regimes": [{"start": 0, "end": 50, "per_segment": {}, "true_ctr": {}}],
    }
    row = bq.build_episode_metrics_row(
        metrics, experiment_id="e1", created_at=NOW, traffic_run=3
    )
    assert row["traffic_run"] == 3
    assert json.loads(row["shift_response"]) == metrics["shift_response"]
    assert json.loads(row["regimes"]) == metrics["regimes"]
    assert bq.metrics_row_id(row) == "e1-r3-e2-linear_ts_unshifted"
    # a legacy row without a run dedupes as run 1
    assert (
        bq.metrics_row_id({**row, "traffic_run": None})
        == "e1-r1-e2-linear_ts_unshifted"
    )


def test_progress_update_sql_sets_only_progress_and_updated_at():
    sql, params = bq.build_progress_update_sql("p.d.t", "abc", 3, 10, NOW)
    assert sql == (
        "UPDATE `p.d.t` SET progress = @progress, updated_at = @updated_at "
        "WHERE experiment_id = @experiment_id"
    )
    assert params == [
        (
            "progress",
            "STRING",
            '{"episodes_done":3,"episodes_total":10,"rewards_rejected":0}',
        ),
        ("updated_at", "TIMESTAMP", "2026-10-02T12:00:00Z"),
        ("experiment_id", "STRING", "abc"),
    ]


def test_table_names_follow_env():
    env = {"BQ_PROJECT_ID": "p", "BQ_DATASET_ID": "d", "BQ_TABLE_BANDIT_EVENTS": "ev"}
    assert bq.table_names(env) == {
        "experiments": "p.d.bandit_experiments",
        "events": "p.d.ev",
        "metrics": "p.d.bandit_episode_metrics",
    }


class FakeClient:
    def __init__(self, fail_times=0, errors=None, query_failures=()):
        self.fail_times = fail_times
        self.errors = errors or [{"index": 0, "errors": ["backendError"]}]
        self.calls = []
        self.queries = []
        self.insert_timeouts = []
        self.result_timeouts = []
        # exceptions raised by successive progress UPDATEs (then success)
        self.query_failures = list(query_failures)

    def insert_rows_json(self, table, rows, row_ids=None, timeout=None):
        self.calls.append((table, rows, row_ids))
        self.insert_timeouts.append(timeout)
        if self.fail_times:
            self.fail_times -= 1
            return self.errors
        return []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        return self

    def result(self, timeout=None):
        self.result_timeouts.append(timeout)
        if self.query_failures:
            raise self.query_failures.pop(0)
        return []


TABLES = {"experiments": "p.d.x", "events": "p.d.ev", "metrics": "p.d.m"}


def _writer(client, **kw):
    kw.setdefault("sleep", lambda s: None)
    return bq.BigQueryWriter(
        TABLES,
        client_factory=lambda: client,
        job_config_factory=lambda p: {"params": p},
        now=lambda: NOW,
        **kw,
    )


def test_writer_batches_events_with_request_id_insert_ids():
    client = FakeClient()
    rows = [_event(request_id=f"r{i}") for i in range(1203)]
    _writer(client).write_events(rows)
    assert [len(r) for _, r, _ in client.calls] == [500, 500, 203]
    assert all(t == "p.d.ev" for t, _, _ in client.calls)
    assert client.calls[2][2] == [f"r{i}" for i in range(1000, 1203)]


def test_writer_retries_row_errors_then_raises():
    client = FakeClient(fail_times=1)
    _writer(client).write_events([_event()])
    assert len(client.calls) == 2
    with pytest.raises(bq.BigQueryWriteError):
        _writer(FakeClient(fail_times=5), attempts=2).write_events([_event()])


def test_writer_bounds_every_insert_and_query():
    client = FakeClient()
    writer = _writer(client)
    writer.write_events([_event()])
    writer.write_metrics([_event()])
    writer.update_progress("abc", 1, 4)
    assert client.insert_timeouts == [bq.INSERT_TIMEOUT_SECONDS] * 2
    assert client.result_timeouts == [bq.QUERY_TIMEOUT_SECONDS]
    assert (bq.INSERT_TIMEOUT_SECONDS, bq.QUERY_TIMEOUT_SECONDS) == (30, 60)


# A schema mismatch, as insertAll reports it: the bad row "invalid", the rest of
# the request "stopped"; none of the rows are inserted, and a retry can't help.
SCHEMA_MISMATCH = [
    {
        "index": 0,
        "errors": [
            {
                "reason": "invalid",
                "location": "surprise",
                "debugInfo": "",
                "message": "no such field: surprise.",
            }
        ],
    },
    {"index": 1, "errors": [{"reason": "stopped", "location": "", "message": ""}]},
]


def test_writer_does_not_retry_permanent_row_errors(caplog):
    client = FakeClient(fail_times=5, errors=SCHEMA_MISMATCH)
    with pytest.raises(bq.BigQueryWriteError, match="permanent"):
        _writer(client, attempts=3).write_events([_event(), _event()])
    assert len(client.calls) == 1
    assert "no such field: surprise" in caplog.text


# spelled out, not read from the constant, so dropping a reason fails here
@pytest.mark.parametrize(
    "reason", ["invalid", "invalidQuery", "notFound", "accessDenied"]
)
def test_every_permanent_reason_fails_fast(reason):
    errors = [{"index": 0, "errors": [{"reason": reason, "message": "x"}]}]
    client = FakeClient(fail_times=5, errors=errors)
    sleeps = []
    with pytest.raises(bq.BigQueryWriteError, match="permanent"):
        _writer(client, attempts=3, sleep=sleeps.append).write_events([_event()])
    assert len(client.calls) == 1 and sleeps == []


class IndexedFailures(FakeClient):
    """Fails the rows at the given positions of each successive insert call."""

    def __init__(self, fail_indices):
        super().__init__()
        self.fail_indices = list(fail_indices)

    def insert_rows_json(self, table, rows, row_ids=None, timeout=None):
        self.calls.append((table, rows, row_ids))
        idx = self.fail_indices.pop(0) if self.fail_indices else []
        return [{"index": i, "errors": [{"reason": "backendError"}]} for i in idx]


def test_writer_retries_only_the_failed_rows_with_their_insert_ids():
    client = IndexedFailures([[1, 3], [1]])
    sleeps = []
    rows = [_event(request_id=f"r{i}") for i in range(5)]
    _writer(client, attempts=3, sleep=sleeps.append).write_events(rows)
    assert [ids for _, _, ids in client.calls] == [
        ["r0", "r1", "r2", "r3", "r4"],
        ["r1", "r3"],
        ["r3"],
    ]
    assert [r["request_id"] for r in client.calls[2][1]] == ["r3"]
    # jittered exponential backoff between the attempts
    base = bq.INSERT_RETRY_BASE_DELAY
    assert len(sleeps) == 2
    assert 0.5 * base <= sleeps[0] <= base
    assert base <= sleeps[1] <= 2 * base


def test_writer_retries_the_whole_part_when_an_error_has_no_usable_index():
    client = FakeClient(fail_times=1, errors=[{"errors": ["backendError"]}])
    rows = [_event(request_id=f"r{i}") for i in range(3)]
    _writer(client).write_events(rows)
    assert [ids for _, _, ids in client.calls] == [["r0", "r1", "r2"]] * 2


def test_writer_still_retries_transient_object_errors():
    transient = [{"index": 0, "errors": [{"reason": "backendError", "message": "x"}]}]
    client = FakeClient(fail_times=2, errors=transient)
    _writer(client, attempts=3).write_events([_event()])
    assert len(client.calls) == 3


def test_writer_progress_runs_parameterized_update():
    client = FakeClient()
    _writer(client).update_progress("abc", 1, 4)
    sql, cfg = client.queries[0]
    assert sql.startswith("UPDATE `p.d.x` SET progress = @progress")
    assert cfg["params"][0] == (
        "progress",
        "STRING",
        '{"episodes_done":1,"episodes_total":4,"rewards_rejected":0}',
    )


def test_progress_carries_rewards_rejected():
    _, params = bq.build_progress_update_sql("t", "abc", 2, 4, NOW, rewards_rejected=7)
    assert json.loads(params[0][2]) == {
        "episodes_done": 2,
        "episodes_total": 4,
        "rewards_rejected": 7,
    }


class ConcurrentUpdate(Exception):
    """Like BigQuery's 400 "Could not serialize access … concurrent update"."""

    code = 400


def test_writer_progress_retries_with_backoff():
    client = FakeClient(query_failures=[ConcurrentUpdate("serialize")])
    sleeps = []
    _writer(client, sleep=sleeps.append).update_progress("abc", 1, 4)
    assert len(client.queries) == 2 and len(sleeps) == 1


def test_writer_progress_failure_is_a_warning_not_fatal(caplog):
    # progress is advisory (the next episode rewrites it): a failed UPDATE after
    # the retries logs a warning and the run goes on
    client = FakeClient(query_failures=[ConcurrentUpdate("serialize")] * 10)
    with caplog.at_level("WARNING", logger="bandit_traffic.bq"):
        _writer(client, attempts=3).update_progress("abc", 1, 4)
    assert len(client.queries) == 3
    assert "progress update failed" in caplog.text


def test_default_job_config_builds_scalar_parameters():
    _, params = bq.build_progress_update_sql("t", "abc", 1, 2, NOW)
    cfg = bq.default_job_config(params)
    assert [(p.name, p.type_) for p in cfg.query_parameters] == [
        ("progress", "STRING"),
        ("updated_at", "TIMESTAMP"),
        ("experiment_id", "STRING"),
    ]


def test_jsonl_writer(tmp_path):
    w = bq.JsonlWriter(tmp_path, TABLES, now=lambda: NOW)
    w.write_events([_event(), _event(request_id="r2")])
    w.write_metrics([{"a": 1}])
    w.update_progress("abc", 1, 2)
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 2
    assert len((tmp_path / "episode_metrics.jsonl").read_text().splitlines()) == 1
    prog = json.loads((tmp_path / "progress.jsonl").read_text())
    assert prog["sql"].startswith("UPDATE `p.d.x`")
    assert json.loads(prog["params"]["progress"])["episodes_total"] == 2
