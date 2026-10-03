"""runserver/experiments_store.py: SQL builders, param types and both stores."""

from __future__ import annotations

import asyncio
import datetime as dt
import re

import pytest

from runserver.experiments_store import (
    EXPERIMENT_COLUMN_TYPES,
    BigQueryExperimentStore,
    InMemoryExperimentStore,
    build_creative_series_sql,
    build_get_sql,
    build_list_active_sql,
    build_list_sql,
    build_metrics_sql,
    build_segment_winners_sql,
    build_true_ctr_sql,
    build_upsert_sql,
    decode_row,
    table_names,
)

T = "p.d.bandit_experiments"
NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.UTC)


def _row(**over):
    row = {
        "experiment_id": "e1",
        "user_id": "alice@example.com",
        "session_id": "s1",
        "app_name": "creative_agent",
        "created_at": NOW,
        "updated_at": NOW,
        "status": "deploying",
        "scenario": "clear_winner",
        "ctr_mode": "demo",
        "reward_mode": "click",
        "arms": [{"creativeId": "a", "index": 0}],
        "config_uri": "gs://b/bandit/e1/experiment.json",
        "model_resource": None,
        "endpoint_id": None,
        "deployed_model_id": None,
        "ttl_expires_at": NOW + dt.timedelta(minutes=120),
        "stopped_at": None,
        "traffic_execution": None,
        "progress": None,
        "error": None,
    }
    row.update(over)
    return row


def test_column_map_matches_contract_and_ddl():
    assert list(EXPERIMENT_COLUMN_TYPES) == [
        "experiment_id", "user_id", "session_id", "app_name", "created_at",
        "updated_at", "status", "scenario", "ctr_mode", "reward_mode", "arms",
        "config_uri", "model_resource", "endpoint_id", "deployed_model_id",
        "ttl_expires_at", "stopped_at", "traffic_execution", "progress", "error",
    ]  # fmt: skip


def test_upsert_sql_full_row_binds_typed_params():
    sql, params = build_upsert_sql(T, _row())
    assert f"MERGE `{T}` T" in sql
    assert "ON T.experiment_id = S.experiment_id" in sql
    by_name = {p.name: p for p in params}
    assert by_name["created_at"].type_ == "TIMESTAMP"
    assert by_name["created_at"].value == NOW
    assert by_name["arms"].type_ == "STRING"
    assert by_name["arms"].value == '[{"creativeId": "a", "index": 0}]'
    assert by_name["progress"].value is None  # typed NULL, not "null"
    assert by_name["endpoint_id"].type_ == "STRING"
    set_clause = re.search(r"UPDATE SET (.*)", sql).group(1)
    assert "experiment_id =" not in set_clause and "created_at =" not in set_clause
    assert "status = S.status" in set_clause
    assert "alice@example.com" not in sql  # values are never interpolated


def test_upsert_sql_partial_update_sets_only_fields():
    sql, _ = build_upsert_sql(T, _row(), fields=["status", "updated_at"])
    set_clause = re.search(r"UPDATE SET (.*)", sql).group(1)
    assert set_clause.strip() == "status = S.status, updated_at = S.updated_at"
    assert "progress" not in set_clause
    # the insert branch still carries every column
    assert "INSERT (experiment_id, user_id" in sql


def test_upsert_sql_rejects_unknown_columns():
    with pytest.raises(KeyError):
        build_upsert_sql(T, {**_row(), "bogus": 1})
    with pytest.raises(KeyError):
        build_upsert_sql(T, _row(), fields=["nope"])


def test_read_sql_builders():
    sql, (p,) = build_get_sql(T, "e1")
    assert "WHERE experiment_id = @experiment_id" in sql and p.value == "e1"
    sql, (p,) = build_list_sql(T, "alice@example.com")
    assert "WHERE user_id = @user_id" in sql and "ORDER BY created_at DESC" in sql
    assert p.type_ == "STRING"
    sql, (p,) = build_list_active_sql(T)
    assert "IN UNNEST(@statuses)" in sql
    assert p.array_type == "STRING"
    assert set(p.values) == {"deploying", "ready", "running_traffic", "stopping"}
    sql, (p,) = build_metrics_sql("p.d.m", "e1")
    assert "`p.d.m`" in sql and "ORDER BY episode, policy" in sql


def _params(params):
    return {p.name: (p.type_, p.value) for p in params}


def test_creative_series_sql_window_expression_and_params():
    sql, params = build_creative_series_sql("p.d.ev", "e1", windows=20)
    assert "FROM `p.d.ev`" in sql
    assert "WHERE experiment_id = @experiment_id AND policy = @policy" in sql
    assert "MAX(round) + 1 AS horizon" in sql
    assert "LEAST(@windows, MAX(round) + 1) AS nw" in sql
    assert "LEAST(DIV(ev.round * h.nw, h.horizon), h.nw - 1) AS win" in sql
    assert "GROUP BY arm, episode, win" in sql
    assert "e1" not in sql  # bound, never interpolated
    assert _params(params) == {
        "experiment_id": ("STRING", "e1"),
        "policy": ("STRING", "linear_ts"),
        "windows": ("INT64", 20),
    }
    with pytest.raises(ValueError):
        build_creative_series_sql("p.d.ev", "e1", windows=0)


def test_segment_and_true_ctr_sql():
    sql, params = build_segment_winners_sql("p.d.ev", "e1")
    assert "GROUP BY segment, optimal_arm" in sql and "COUNT(*) AS n" in sql
    assert "policy = @policy" in sql
    assert _params(params)["policy"] == ("STRING", "linear_ts")
    sql, params = build_true_ctr_sql("p.d.ev", "e1")
    assert "AVG(p_chosen) AS true_ctr" in sql and "GROUP BY arm" in sql
    assert _params(params)["experiment_id"] == ("STRING", "e1")


def test_bigquery_store_creative_series_runs_three_queries():
    fake = _FakeBQ([])
    store = BigQueryExperimentStore(
        tables={"experiments": T, "events": "p.d.ev", "metrics": "p.d.m"},
        client_factory=lambda: fake,
    )
    got = asyncio.run(store.creative_series_rows("e1"))
    assert got == {"series": [], "segments": [], "true_ctr": []}
    assert len(fake.calls) == 3 and all("`p.d.ev`" in c[0] for c in fake.calls)


def test_in_memory_store_creative_series_rows():
    store = InMemoryExperimentStore()
    assert asyncio.run(store.creative_series_rows("e1"))["series"] == []
    store.add_events(
        "e1",
        [
            {"policy": "linear_ts", "episode": 0, "round": r, "arm": "a", "clicked": 1}
            for r in range(3)
        ],
    )
    rows = asyncio.run(store.creative_series_rows("e1"))["series"]
    assert [(r["win"], r["impressions"], r["horizon"]) for r in rows] == [
        (0, 1, 3),
        (1, 1, 3),
        (2, 1, 3),
    ]


def test_table_names_from_env():
    names = table_names({"BQ_PROJECT_ID": "p", "BQ_DATASET_ID": "d"})
    assert names == {
        "experiments": "p.d.bandit_experiments",
        "events": "p.d.bandit_events",
        "metrics": "p.d.bandit_episode_metrics",
    }
    names = table_names(
        {"BQ_PROJECT_ID": "p", "BQ_DATASET_ID": "d", "BQ_TABLE_BANDIT_METRICS": "m"}
    )
    assert names["metrics"] == "p.d.m"


def test_decode_row_parses_json_columns():
    row = decode_row({"arms": '[{"creativeId": "a"}]', "progress": ""})
    assert row["arms"] == [{"creativeId": "a"}] and row["progress"] is None
    assert decode_row({"arms": None})["arms"] == []


class _FakeRow(dict):
    pass


class _FakeBQ:
    def __init__(self, result):
        self.calls, self.result = [], result

    def query(self, sql, job_config):
        self.calls.append((sql, job_config.query_parameters))
        result = self.result

        class _Job:
            def result(self):
                return [_FakeRow(r) for r in result]

        return _Job()


def test_bigquery_store_executes_via_client():
    fake = _FakeBQ([{**_row(), "arms": '[{"creativeId": "a"}]', "progress": None}])
    store = BigQueryExperimentStore(
        tables={"experiments": T, "events": "x", "metrics": "p.d.m"},
        client_factory=lambda: fake,
    )

    async def go():
        await store.upsert(_row(), fields=["status"])
        got = await store.get("e1")
        listed = await store.list_for_user("alice@example.com")
        await store.list_active()
        await store.metrics_rows("e1")
        return got, listed

    got, listed = asyncio.run(go())
    assert got["arms"] == [{"creativeId": "a"}]
    assert len(listed) == 1
    assert [c[0].split()[0] for c in fake.calls] == [
        "MERGE",
        "SELECT",
        "SELECT",
        "SELECT",
        "SELECT",
    ]


def test_in_memory_store_partial_update_and_ordering():
    store = InMemoryExperimentStore()

    async def go():
        await store.upsert(_row(progress={"episodes_done": 1}))
        await store.upsert(
            _row(experiment_id="e2", created_at=NOW + dt.timedelta(seconds=1))
        )
        await store.upsert(_row(status="ready", progress=None), fields=["status"])
        e1 = await store.get("e1")
        listed = await store.list_for_user("alice@example.com")
        active = await store.list_active()
        return e1, listed, active

    e1, listed, active = asyncio.run(go())
    assert e1["status"] == "ready"
    assert e1["progress"] == {"episodes_done": 1}  # untouched by the partial update
    assert [r["experiment_id"] for r in listed] == ["e2", "e1"]
    assert len(active) == 2
