"""runserver/shares_store.py: SQL builders, BigQuery store over a fake client,
in-memory store and env selection (no GCP)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

import pytest

from runserver import shares_store as ss
from tests._fake_bq import FakeBigQueryClient

T0 = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.UTC)
SCHEMA = (
    Path(__file__).resolve().parents[1] / "deployment/bq_schemas/creative_shares.json"
)


def _row(**over) -> dict:
    row = {
        "token": "tok_AAAAAAAAAAAAAAAA",
        "owner_user": "a@x.com",
        "app_name": "creative_agent",
        "session_id": "s1",
        "scope": "slate",
        "concept_names": ["A", "B'; DROP TABLE x"],
        "include_eval": False,
        "title": "PRS × Powerball",
        "created_at": T0,
        "revoked_at": None,
    }
    row.update(over)
    return row


def run(coro):
    return asyncio.run(coro)


def test_schema_file_matches_store_columns():
    bq = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}
    got = {}
    for f in json.loads(SCHEMA.read_text()):
        typ = bq.get(f["type"], f["type"])
        got[f["name"]] = f"ARRAY<{typ}>" if f.get("mode") == "REPEATED" else typ
    assert got == ss.SHARE_COLUMN_TYPES
    assert list(got) == [
        "token",
        "owner_user",
        "app_name",
        "session_id",
        "scope",
        "concept_names",
        "include_eval",
        "title",
        "created_at",
        "revoked_at",
    ]


def test_put_sql_is_fully_parameterised_with_array_param():
    from google.cloud import bigquery

    sql, params = ss.build_put_sql("p.d.creative_shares", _row())
    assert "MERGE `p.d.creative_shares` T" in sql
    assert "ON T.token = S.token" in sql
    assert "WHEN NOT MATCHED THEN" in sql and "WHEN MATCHED" not in sql
    assert "DROP TABLE" not in sql and "a@x.com" not in sql
    by_name = {p.name: p for p in params}
    assert set(by_name) == set(ss.SHARE_COLUMN_TYPES)
    names = by_name["concept_names"]
    assert isinstance(names, bigquery.ArrayQueryParameter)
    assert names.array_type == "STRING" and names.values == ["A", "B'; DROP TABLE x"]
    assert by_name["include_eval"].type_ == "BOOL"
    assert by_name["revoked_at"].value is None
    # a slate share stores no names: an empty array, never NULL
    _, params = ss.build_put_sql("t", _row(concept_names=None))
    assert {p.name: p for p in params}["concept_names"].values == []
    with pytest.raises(KeyError):
        ss.build_put_sql("t", {"token": "x"})


def test_list_get_revoke_sql_parameterised():
    sql, params = ss.build_list_sql("t", "a@x.com")
    assert "owner_user = @owner_user" in sql and "revoked_at IS NULL" in sql
    assert "ORDER BY created_at DESC" in sql and "a@x.com" not in sql
    assert [p.value for p in params] == ["a@x.com"]
    sql, params = ss.build_get_sql("t", "tok")
    assert "token = @token" in sql and [p.value for p in params] == ["tok"]
    sql, params = ss.build_revoke_sql("t", "tok", "a@x.com", T0)
    assert sql.lstrip().startswith("UPDATE `t`")
    assert "token = @token AND owner_user = @owner_user" in sql
    assert "COALESCE(revoked_at, @revoked_at)" in sql
    assert {p.name: p.value for p in params} == {
        "token": "tok",
        "owner_user": "a@x.com",
        "revoked_at": T0,
    }


def test_in_memory_store():
    store = ss.InMemorySharesStore()

    async def go():
        await store.put(_row())
        await store.put(
            _row(token="t2", created_at=T0 + dt.timedelta(minutes=1), scope="creative")
        )
        await store.put(_row(token="t3", owner_user="b@x.com"))
        mine = await store.list_for("a@x.com")
        assert [r["token"] for r in mine] == ["t2", "tok_AAAAAAAAAAAAAAAA"]
        assert (await store.get("t3"))["owner_user"] == "b@x.com"
        assert await store.get("nope") is None
        # not the owner's: refused, nothing changes
        assert await store.revoke("t3", "a@x.com") is False
        assert await store.revoke("nope", "a@x.com") is False
        assert await store.revoke("t2", "a@x.com") is True
        assert [r["token"] for r in await store.list_for("a@x.com")] == [
            "tok_AAAAAAAAAAAAAAAA"
        ]
        revoked = await store.get("t2")
        assert revoked["revoked_at"] is not None
        first = revoked["revoked_at"]
        # idempotent: a second revoke still succeeds and keeps the first time
        assert await store.revoke("t2", "a@x.com") is True
        assert (await store.get("t2"))["revoked_at"] == first
        assert [r["token"] for r in await store.list_for("b@x.com")] == ["t3"]

    run(go())


def test_in_memory_rows_are_copies():
    store = ss.InMemorySharesStore()
    row = _row()
    run(store.put(row))
    row["concept_names"].append("C")
    got = run(store.get(row["token"]))
    assert got["concept_names"] == ["A", "B'; DROP TABLE x"]
    got["title"] = "changed"
    assert run(store.get(row["token"]))["title"] == "PRS × Powerball"


def test_bigquery_store_over_fake_client():
    fake = FakeBigQueryClient(results=[_row()])
    store = ss.BigQuerySharesStore("p.d.s", client_factory=lambda: fake)

    async def go():
        await store.put(_row())
        assert (await store.list_for("a@x.com"))[0]["token"] == _row()["token"]
        assert (await store.get("tok"))["scope"] == "slate"
        assert await store.revoke("tok", "a@x.com") is True
        fake.num_dml_affected_rows = 0
        assert await store.revoke("tok", "b@x.com") is False
        fake.results = []
        assert await store.get("missing") is None

    run(go())
    assert fake.sqls[0].lstrip().startswith("MERGE `p.d.s`")
    assert "owner_user = @owner_user" in fake.sqls[1]
    assert "token = @token" in fake.sqls[2]
    assert fake.sqls[3].lstrip().startswith("UPDATE `p.d.s`")


def test_table_name_and_env_selection(caplog):
    env = {"BQ_PROJECT_ID": "p", "BQ_DATASET_ID": "d"}
    assert ss.table_name(env) == "p.d.creative_shares"
    assert ss.table_name({**env, "BQ_TABLE_SHARES": "s2"}) == "p.d.s2"
    mode, store = ss.build_store_from_env(env)
    assert mode == "bigquery" and store.table == "p.d.creative_shares"
    mode, store = ss.build_store_from_env({**env, "SHARES_STORE": "memory"})
    assert mode == "memory" and isinstance(store, ss.InMemorySharesStore)
    mode, _ = ss.build_store_from_env({})
    assert mode == "memory"
    assert "falling back" in caplog.text
    with pytest.raises(RuntimeError):
        ss.build_store_from_env({"SHARES_STORE": "sqlite"})


def test_missing_bq_env_fails_loudly_on_cloud_run():
    with pytest.raises(RuntimeError, match="BQ_PROJECT_ID"):
        ss.build_store_from_env({"K_SERVICE": "trend-trawler-api"})
    mode, _ = ss.build_store_from_env({"K_SERVICE": "api", "SHARES_STORE": "memory"})
    assert mode == "memory"
    mode, _ = ss.build_store_from_env(
        {"K_SERVICE": "api", "BQ_PROJECT_ID": "p", "BQ_DATASET_ID": "d"}
    )
    assert mode == "bigquery"
