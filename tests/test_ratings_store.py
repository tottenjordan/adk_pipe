"""runserver/ratings_store.py: SQL builders, BigQuery store over a fake client,
in-memory store and env selection (no GCP)."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from runserver import ratings_store as rs
from tests._fake_bq import FakeBigQueryClient

T0 = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.UTC)


def _row(**over) -> dict:
    row = {
        "rating_id": rs.rating_id("s1", "visual:X", "a@x.com"),
        "session_id": "s1",
        "app_name": "creative_agent",
        "creative_key": "visual:X",
        "kind": "visual",
        "user_id": "a@x.com",
        "verdict": "pass",
        "score": None,
        "note": "it's 'fine'; DROP TABLE x",
        "judge_overall": 0.8,
        "judge_passed": True,
        "judge_gates_passed": None,
        "judge_model": "m",
        "judge_source": "gcs",
        "judge_version": "2026-10-08",
        "learning_used": False,
        "brand": "prs",
        "visual_style": "Candid 35mm film photo",
        "tone_style": "Humorous",
        "angle_id": "A2",
        "fail_reasons": [],
        "created_at": T0,
        "updated_at": T0,
    }
    row.update(over)
    return row


def test_rating_id_is_stable_and_user_scoped():
    a = rs.rating_id("s1", "visual:X", "a@x.com")
    assert a == rs.rating_id("s1", "visual:X", "a@x.com") and len(a) == 16
    assert a != rs.rating_id("s1", "visual:X", "b@x.com")
    assert a != rs.rating_id("s1", "copy:1", "a@x.com")


def test_upsert_sql_is_fully_parameterised():
    sql, params = rs.build_upsert_sql("p.d.creative_ratings", _row())
    assert "MERGE `p.d.creative_ratings` T" in sql
    assert "ON T.rating_id = S.rating_id" in sql
    assert "WHEN MATCHED THEN" in sql and "WHEN NOT MATCHED THEN" in sql
    matched = sql.split("WHEN MATCHED THEN")[1].split("WHEN NOT MATCHED")[0]
    assert "created_at" not in matched and "rating_id =" not in matched
    assert "updated_at = S.updated_at" in matched
    # user text never reaches the SQL string
    assert "DROP TABLE" not in sql and "a@x.com" not in sql
    by_name = {p.name: p for p in params}
    assert set(by_name) == set(rs.RATING_COLUMN_TYPES)
    assert by_name["score"].type_ == "INT64" and by_name["score"].value is None
    assert by_name["judge_passed"].type_ == "BOOL"
    with pytest.raises(KeyError):
        rs.build_upsert_sql("t", {"rating_id": "x"})


def test_rating_columns_include_learning_context():
    for col, typ in {
        "brand": "STRING",
        "visual_style": "STRING",
        "tone_style": "STRING",
        "angle_id": "STRING",
        "fail_reasons": "ARRAY<STRING>",
    }.items():
        assert rs.RATING_COLUMN_TYPES[col] == typ
        assert col in rs.UPDATABLE


def test_fail_reasons_bound_as_string_array_param():
    from google.cloud import bigquery

    sql, params = rs.build_upsert_sql(
        "t", _row(fail_reasons=["weak_cta", "text_problem"])
    )
    by_name = {p.name: p for p in params}
    fr = by_name["fail_reasons"]
    assert isinstance(fr, bigquery.ArrayQueryParameter)
    assert fr.array_type == "STRING"
    assert fr.values == ["weak_cta", "text_problem"]
    assert "@fail_reasons AS fail_reasons" in sql
    assert "weak_cta" not in sql
    # an absent (None) list binds as an empty array, never NULL
    _, params = rs.build_upsert_sql("t", _row(fail_reasons=None))
    assert {p.name: p for p in params}["fail_reasons"].values == []
    assert isinstance(by_name["brand"], bigquery.ScalarQueryParameter)


def test_in_memory_store_updates_learning_context():
    store = rs.InMemoryRatingsStore()

    async def go():
        await store.upsert(_row())
        await store.upsert(_row(verdict="fail", fail_reasons=["weak_cta"]))
        (row,) = await store.list_for_user("a@x.com")
        assert row["fail_reasons"] == ["weak_cta"] and row["brand"] == "prs"

    asyncio.run(go())


def test_list_sql_parameterised():
    sql, params = rs.build_list_session_sql("t", "a@x.com", "s1")
    assert "@user_id" in sql and "@session_id" in sql and "a@x.com" not in sql
    assert [p.value for p in params] == ["a@x.com", "s1"]
    sql, params = rs.build_list_user_sql("t", "a@x.com")
    assert "WHERE user_id = @user_id" in sql and len(params) == 1


def test_bigquery_store_over_fake_client():
    class Row(dict):
        def items(self):
            return super().items()

    fake = FakeBigQueryClient(results=[Row(_row())])
    store = rs.BigQueryRatingsStore("p.d.r", client_factory=lambda: fake)

    async def go():
        await store.upsert(_row())
        rows = await store.list_for_session("a@x.com", "s1")
        assert rows[0]["creative_key"] == "visual:X"
        await store.list_for_user("a@x.com")

    asyncio.run(go())
    assert fake.sqls[0].lstrip().startswith("MERGE `p.d.r`")
    assert "session_id = @session_id" in fake.sqls[1]
    assert "session_id" not in fake.sqls[2].split("WHERE")[1]


def test_in_memory_store_upsert_keeps_created_at():
    store = rs.InMemoryRatingsStore()
    later = T0 + dt.timedelta(minutes=5)

    async def go():
        await store.upsert(_row())
        await store.upsert(
            _row(verdict="fail", created_at=later, updated_at=later, app_name="z")
        )
        await store.upsert(
            _row(rating_id="other", session_id="s2", updated_at=T0 - dt.timedelta(1))
        )
        await store.upsert(_row(rating_id="bob", user_id="b@x.com"))
        rows = await store.list_for_user("a@x.com")
        assert [r["rating_id"] for r in rows][1] == "other"  # newest first
        mine = rows[0]
        assert mine["verdict"] == "fail" and mine["created_at"] == T0
        assert mine["app_name"] == "creative_agent"  # identity columns kept
        assert len(await store.list_for_session("a@x.com", "s1")) == 1

    asyncio.run(go())


def test_table_name_and_env_selection(caplog):
    env = {"BQ_PROJECT_ID": "p", "BQ_DATASET_ID": "d"}
    assert rs.table_name(env) == "p.d.creative_ratings"
    assert rs.table_name({**env, "BQ_TABLE_RATINGS": "r2"}) == "p.d.r2"
    mode, store = rs.build_store_from_env(env)
    assert mode == "bigquery" and store.table == "p.d.creative_ratings"
    mode, store = rs.build_store_from_env({**env, "RATINGS_STORE": "memory"})
    assert mode == "memory" and isinstance(store, rs.InMemoryRatingsStore)
    mode, _ = rs.build_store_from_env({})
    assert mode == "memory"
    assert "falling back" in caplog.text
    with pytest.raises(RuntimeError):
        rs.build_store_from_env({"RATINGS_STORE": "sqlite"})


def test_rating_columns_include_judge_version_and_learning_used():
    assert rs.RATING_COLUMN_TYPES["judge_version"] == "STRING"
    assert rs.RATING_COLUMN_TYPES["learning_used"] == "BOOL"
    # re-snapshotted on a re-rate, like the other judge_* fields
    assert {"judge_version", "learning_used"} <= set(rs.UPDATABLE)


def test_calibration_sql_reads_judge_version_and_learning_used():
    sql, _ = rs.build_calibration_sql("p.d.r", "a@x.com")
    assert "judge_version" in sql and "learning_used" in sql


def test_in_memory_rerate_updates_judge_version_and_learning_used():
    store = rs.InMemoryRatingsStore()
    asyncio.run(store.upsert(_row(judge_version="", learning_used=False)))
    asyncio.run(store.upsert(_row(judge_version="2026-10-08", learning_used=True)))
    (row,) = store.rows.values()
    assert row["judge_version"] == "2026-10-08" and row["learning_used"] is True


def test_calibration_sql_scoped_or_global():
    sql, params = rs.build_calibration_sql("p.d.r", "a@x.com")
    assert "WHERE user_id = @user_id" in sql and params[0].value == "a@x.com"
    assert "a@x.com" not in sql
    sql, params = rs.build_calibration_sql("p.d.r")
    assert "WHERE" not in sql and params == []


def test_missing_bq_env_fails_loudly_on_cloud_run():
    with pytest.raises(RuntimeError, match="BQ_PROJECT_ID"):
        rs.build_store_from_env({"K_SERVICE": "trend-trawler-api"})
    with pytest.raises(RuntimeError, match="BQ_DATASET_ID"):
        rs.build_store_from_env({"K_SERVICE": "api", "BQ_PROJECT_ID": "p"})
    # an explicit memory store is still allowed on Cloud Run
    mode, _ = rs.build_store_from_env({"K_SERVICE": "api", "RATINGS_STORE": "memory"})
    assert mode == "memory"
    mode, _ = rs.build_store_from_env(
        {"K_SERVICE": "api", "BQ_PROJECT_ID": "p", "BQ_DATASET_ID": "d"}
    )
    assert mode == "bigquery"
