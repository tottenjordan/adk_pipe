"""runserver/experiments_store.py: SQL builders, param types and both stores."""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from pathlib import Path

import pytest

from runserver.experiments_store import (
    EXPERIMENT_COLUMN_TYPES,
    BigQueryExperimentStore,
    InMemoryExperimentStore,
    build_acquire_lease_sql,
    build_creative_segments_sql,
    build_creative_series_sql,
    build_get_sql,
    build_list_active_sql,
    build_list_sql,
    build_metrics_sql,
    build_release_lease_sql,
    build_renew_lease_sql,
    build_segment_winners_sql,
    build_true_ctr_sql,
    build_upsert_sql,
    decode_row,
    encode_row,
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
        "scenario_overrides", "deploy_lease_until", "deploy_lease_owner",
    ]  # fmt: skip
    ddl = (Path(__file__).parents[1] / "deployment/create_bq_tables.sh").read_text()
    schema = re.search(r'BANDIT_EXPERIMENTS}" \\\n\s+(\S+)', ddl).group(1)
    assert dict(c.split(":") for c in schema.split(",")) == EXPERIMENT_COLUMN_TYPES


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


def test_upsert_ignores_unknown_fetched_columns_not_being_written(caplog):
    """Rolling deploys: a row read from a table migrated by a newer revision may
    carry columns this code doesn't know. A partial update must not crash on them."""
    row = {**_row(), "future_col": "x"}
    sql, params = build_upsert_sql(T, row, fields=["status", "updated_at"])
    assert "future_col" not in sql
    assert "future_col" not in {p.name for p in params}
    assert "status = S.status" in sql
    # still raises when the unknown column is explicitly written
    with pytest.raises(KeyError):
        build_upsert_sql(T, row, fields=["future_col"])
    with pytest.raises(KeyError):
        build_upsert_sql(T, row)  # full write names every column


def test_in_memory_upsert_ignores_unknown_fetched_columns():
    store = InMemoryExperimentStore()

    async def go():
        await store.upsert(_row())
        await store.upsert({**_row(status="ready"), "future_col": 1}, fields=["status"])
        with pytest.raises(KeyError):
            await store.upsert({**_row(), "future_col": 1}, fields=["future_col"])
        return await store.get("e1")

    got = asyncio.run(go())
    assert got["status"] == "ready" and "future_col" not in got


def test_lease_sql_builders():
    sql, params = build_acquire_lease_sql(T, "e1", "rev-1/abc", 180)
    by_name = {p.name: p for p in params}
    assert sql.split()[0] == "UPDATE" and f"`{T}`" in sql
    assert "WHERE experiment_id = @experiment_id" in sql
    assert "status = @status" in sql and by_name["status"].value == "deploying"
    assert (
        "deploy_lease_until IS NULL OR deploy_lease_until < CURRENT_TIMESTAMP()" in sql
    )
    assert "INTERVAL @ttl_seconds SECOND" in sql
    assert by_name["ttl_seconds"].type_ == "INT64"
    assert by_name["ttl_seconds"].value == 180
    assert by_name["owner"].value == "rev-1/abc"
    assert "rev-1/abc" not in sql and "e1" not in sql.replace(T, "")
    sql, params = build_renew_lease_sql(T, "e1", "rev-1/abc", 180)
    assert "deploy_lease_owner = @owner" in sql.split("WHERE")[1]
    assert "INTERVAL @ttl_seconds SECOND" in sql
    sql, params = build_release_lease_sql(T, "e1", "rev-1/abc")
    assert "deploy_lease_until = NULL" in sql and "deploy_lease_owner = NULL" in sql
    assert "deploy_lease_owner = @owner" in sql.split("WHERE")[1]
    assert {p.name for p in params} == {"experiment_id", "owner"}


def test_bigquery_store_lease_checks_affected_rows():
    fake = _FakeBQ([])
    store = BigQueryExperimentStore(
        tables={"experiments": T, "events": "x", "metrics": "m"},
        client_factory=lambda: fake,
    )

    async def go():
        fake.affected = 1
        won = await store.acquire_deploy_lease("e1", "o", 180)
        renewed = await store.renew_deploy_lease("e1", "o", 180)
        fake.affected = 0
        lost = await store.acquire_deploy_lease("e1", "o2", 180)
        not_renewed = await store.renew_deploy_lease("e1", "o2", 180)
        await store.release_deploy_lease("e1", "o")
        return won, renewed, lost, not_renewed

    assert asyncio.run(go()) == (True, True, False, False)
    assert [c[0].split()[0] for c in fake.calls] == ["UPDATE"] * 5


def test_bigquery_store_lease_on_unmigrated_table_degrades(caplog):
    from google.api_core import exceptions as gexc

    class _Unmigrated(_FakeBQ):
        def query(self, sql, job_config):
            raise gexc.BadRequest("Unrecognized name: deploy_lease_until at [3:17]")

    store = BigQueryExperimentStore(
        tables={"experiments": T, "events": "x", "metrics": "m"},
        client_factory=lambda: _Unmigrated([]),
    )

    async def go():
        won = await store.acquire_deploy_lease("e1", "o", 180)
        renewed = await store.renew_deploy_lease("e1", "o", 180)
        await store.release_deploy_lease("e1", "o")
        return won, renewed

    assert asyncio.run(go()) == (True, True)
    assert "migration" in caplog.text


def test_in_memory_lease_semantics():
    store = InMemoryExperimentStore()

    async def go():
        await store.upsert(_row())
        first = await store.acquire_deploy_lease("e1", "a", 180)
        second = await store.acquire_deploy_lease("e1", "b", 180)
        renew_foreign = await store.renew_deploy_lease("e1", "b", 180)
        before = store.rows["e1"]["deploy_lease_until"]
        await asyncio.sleep(0.01)
        renew_own = await store.renew_deploy_lease("e1", "a", 180)
        extended = store.rows["e1"]["deploy_lease_until"] > before
        await store.release_deploy_lease("e1", "b")  # not the owner: no-op
        held = store.rows["e1"]["deploy_lease_owner"]
        await store.release_deploy_lease("e1", "a")
        after_release = await store.acquire_deploy_lease("e1", "b", 180)
        # expiry: a stale lease can be taken over
        store.rows["e1"]["deploy_lease_until"] = NOW
        takeover = await store.acquire_deploy_lease("e1", "c", 180)
        await store.upsert(_row(status="ready"), fields=["status"])
        store.rows["e1"]["deploy_lease_until"] = None
        not_deploying = await store.acquire_deploy_lease("e1", "d", 180)
        missing = await store.acquire_deploy_lease("nope", "d", 180)
        return (first, second, renew_foreign, renew_own, extended, held,
                after_release, takeover, not_deploying, missing)  # fmt: skip

    assert asyncio.run(go()) == (
        True, False, False, True, True, "a", True, True, False, False,
    )  # fmt: skip


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


def test_creative_segments_sql():
    sql, params = build_creative_segments_sql("p.d.ev", "e1")
    assert "FROM `p.d.ev`" in sql
    assert "WHERE experiment_id = @experiment_id AND policy = @policy" in sql
    assert "GROUP BY arm, segment" in sql
    for expr in (
        "COUNT(*) AS impressions",
        "SUM(IFNULL(clicked, 0)) AS clicks",  # INT64 0/1, not BOOL
        "SUM(p_chosen) AS p_sum",
        "COUNT(p_chosen) AS p_n",
        "SUM(IFNULL(regret, 0)) AS regret_sum",
        "SUM(IFNULL(dwell_s, 0)) AS dwell_sum",
    ):
        assert expr in sql
    assert "e1" not in sql
    assert _params(params) == {
        "experiment_id": ("STRING", "e1"),
        "policy": ("STRING", "linear_ts"),
    }


def test_bigquery_store_creative_series_runs_four_queries():
    fake = _FakeBQ([])
    store = BigQueryExperimentStore(
        tables={"experiments": T, "events": "p.d.ev", "metrics": "p.d.m"},
        client_factory=lambda: fake,
    )
    got = asyncio.run(store.creative_series_rows("e1"))
    assert got == {
        "series": [],
        "segments": [],
        "true_ctr": [],
        "creative_segments": [],
    }
    assert len(fake.calls) == 4 and all("`p.d.ev`" in c[0] for c in fake.calls)
    assert any("GROUP BY arm, segment" in c[0] for c in fake.calls)


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
    ov = decode_row({"scenario_overrides": '{"gap_scale": 1.5}'})
    assert ov["scenario_overrides"] == {"gap_scale": 1.5}
    assert decode_row({"scenario_overrides": ""})["scenario_overrides"] is None
    assert encode_row({"scenario_overrides": {"gap_scale": 1.5}}) == {
        "scenario_overrides": '{"gap_scale": 1.5}'
    }


class _FakeRow(dict):
    pass


class _FakeBQ:
    def __init__(self, result):
        self.calls, self.result = [], result
        self.affected = 0

    def query(self, sql, job_config):
        self.calls.append((sql, job_config.query_parameters))
        result = self.result
        affected = self.affected

        class _Job:
            num_dml_affected_rows = affected

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
