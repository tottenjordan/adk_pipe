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
        ("progress", "STRING", '{"episodes_done":3,"episodes_total":10}'),
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
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.calls = []
        self.queries = []

    def insert_rows_json(self, table, rows, row_ids=None):
        self.calls.append((table, rows, row_ids))
        if self.fail_times:
            self.fail_times -= 1
            return [{"index": 0, "errors": ["backendError"]}]
        return []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        return self

    def result(self):
        return []


TABLES = {"experiments": "p.d.x", "events": "p.d.ev", "metrics": "p.d.m"}


def _writer(client, **kw):
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


def test_writer_progress_runs_parameterized_update():
    client = FakeClient()
    _writer(client).update_progress("abc", 1, 4)
    sql, cfg = client.queries[0]
    assert sql.startswith("UPDATE `p.d.x` SET progress = @progress")
    assert cfg["params"][0] == (
        "progress",
        "STRING",
        '{"episodes_done":1,"episodes_total":4}',
    )


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
