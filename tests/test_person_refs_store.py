"""runserver/person_refs_store.py: SQL builders, BigQuery store over a fake client,
in-memory store and env selection (no GCP)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

import pytest

from runserver import person_refs_store as ps
from tests._fake_bq import FakeBigQueryClient

T0 = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.UTC)
SCHEMA = (
    Path(__file__).resolve().parents[1] / "deployment/bq_schemas/person_references.json"
)
URI = "gs://b/person-refs/a_x_com/me.jpg"


def _row(**over) -> dict:
    row = {
        "consent_id": "cid_AAAAAAAAAAAA",
        "owner_user": "a@x.com",
        "photo_uri": URI,
        "label": "Me'; DROP TABLE x",
        "subject": "self",
        "adult_attested": True,
        "allow_public_share": False,
        "consent_text_version": "2026-10-09",
        "created_at": T0,
        "revoked_at": None,
        "person_renders": [],
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
    assert got == ps.PERSON_REF_COLUMN_TYPES
    assert list(got) == [
        "consent_id",
        "owner_user",
        "photo_uri",
        "label",
        "subject",
        "adult_attested",
        "allow_public_share",
        "consent_text_version",
        "created_at",
        "revoked_at",
        "person_renders",
    ]


def test_put_sql_is_fully_parameterised():
    from google.cloud import bigquery

    sql, params = ps.build_put_sql("p.d.person_references", _row())
    assert "MERGE `p.d.person_references` T" in sql
    assert "ON T.consent_id = S.consent_id" in sql
    assert "WHEN NOT MATCHED THEN" in sql and "WHEN MATCHED" not in sql
    # no value is interpolated into the SQL text
    for value in ("DROP TABLE", "a@x.com", URI, "2026-10-09", "'self'"):
        assert value not in sql
    by_name = {p.name: p for p in params}
    assert set(by_name) == set(ps.PERSON_REF_COLUMN_TYPES)
    assert by_name["label"].value == "Me'; DROP TABLE x"
    assert by_name["adult_attested"].type_ == "BOOL"
    renders = by_name["person_renders"]
    assert isinstance(renders, bigquery.ArrayQueryParameter)
    assert renders.array_type == "STRING" and renders.values == []
    # an absent render list binds as [], never NULL
    _, params = ps.build_put_sql("t", _row(person_renders=None))
    assert {p.name: p for p in params}["person_renders"].values == []
    with pytest.raises(KeyError):
        ps.build_put_sql("t", {"consent_id": "x"})


def test_list_get_active_revoke_sql_parameterised():
    sql, params = ps.build_list_sql("t", "a@x.com")
    assert "owner_user = @owner_user" in sql and "revoked_at IS NULL" in sql
    assert "ORDER BY created_at DESC" in sql and "a@x.com" not in sql
    assert [p.value for p in params] == ["a@x.com"]
    sql, params = ps.build_get_sql("t", "cid_XYZ")
    assert "consent_id = @consent_id" in sql and "cid_XYZ" not in sql
    assert [p.value for p in params] == ["cid_XYZ"]
    sql, params = ps.build_active_sql("t", "cid_XYZ", "a@x.com")
    assert "consent_id = @consent_id AND owner_user = @owner_user" in sql
    assert "revoked_at IS NULL" in sql and "a@x.com" not in sql
    assert {p.name: p.value for p in params} == {
        "consent_id": "cid_XYZ",
        "owner_user": "a@x.com",
    }
    sql, params = ps.build_revoke_sql("t", "cid_XYZ", "a@x.com", T0)
    assert sql.lstrip().startswith("UPDATE `t`")
    assert "consent_id = @consent_id AND owner_user = @owner_user" in sql
    assert "COALESCE(revoked_at, @revoked_at)" in sql
    assert {p.name: p.value for p in params} == {
        "consent_id": "cid_XYZ",
        "owner_user": "a@x.com",
        "revoked_at": T0,
    }


def test_in_memory_store():
    store = ps.InMemoryPersonRefsStore()

    async def go():
        await store.put(_row())
        await store.put(_row(consent_id="c2", created_at=T0 + dt.timedelta(minutes=1)))
        await store.put(_row(consent_id="c3", owner_user="b@x.com"))
        mine = await store.list_for("a@x.com")
        assert [r["consent_id"] for r in mine] == ["c2", "cid_AAAAAAAAAAAA"]
        assert (await store.get("c3"))["owner_user"] == "b@x.com"
        assert await store.get("nope") is None
        assert (await store.active_for("c2", "a@x.com"))["consent_id"] == "c2"
        # another owner's consent is never active for you
        assert await store.active_for("c3", "a@x.com") is None
        assert await store.active_for("nope", "a@x.com") is None
        # not the owner's: refused, nothing changes
        assert await store.revoke("c3", "a@x.com") is False
        assert await store.revoke("nope", "a@x.com") is False
        assert await store.active_for("c3", "b@x.com") is not None
        assert await store.revoke("c2", "a@x.com") is True
        assert await store.active_for("c2", "a@x.com") is None
        assert [r["consent_id"] for r in await store.list_for("a@x.com")] == [
            "cid_AAAAAAAAAAAA"
        ]
        first = (await store.get("c2"))["revoked_at"]
        assert first is not None
        # idempotent: a second revoke still succeeds and keeps the first time
        assert await store.revoke("c2", "a@x.com") is True
        assert (await store.get("c2"))["revoked_at"] == first

    run(go())


def test_add_renders_sql_parameterised_and_deduped():
    from google.cloud import bigquery

    sql, params = ps.build_add_renders_sql(
        "t", "cid", "a@x.com", ["gs://b/x.png", "gs://b/x'; DROP"]
    )
    assert sql.lstrip().startswith("UPDATE `t`")
    assert "consent_id = @consent_id AND owner_user = @owner_user" in sql
    assert "DISTINCT" in sql and "ARRAY_CONCAT" in sql
    # revoked consents too: a render recorded mid-revoke must still be deletable
    assert "revoked_at" not in sql
    assert "DROP" not in sql and "a@x.com" not in sql
    by_name = {p.name: p for p in params}
    assert isinstance(by_name["uris"], bigquery.ArrayQueryParameter)
    assert by_name["uris"].values == ["gs://b/x.png", "gs://b/x'; DROP"]


def test_in_memory_add_renders():
    store = ps.InMemoryPersonRefsStore()

    async def go():
        await store.put(_row())
        assert await store.add_renders("cid_AAAAAAAAAAAA", "a@x.com", ["gs://b/1.png"])
        assert await store.add_renders(
            "cid_AAAAAAAAAAAA", "a@x.com", ["gs://b/1.png", "gs://b/2.png"]
        )
        got = await store.get("cid_AAAAAAAAAAAA")
        assert got["person_renders"] == ["gs://b/1.png", "gs://b/2.png"]
        # not the owner's / unknown: refused, nothing changes
        assert not await store.add_renders("cid_AAAAAAAAAAAA", "b@x.com", ["gs://b/3"])
        assert not await store.add_renders("nope", "a@x.com", ["gs://b/3.png"])
        # a revoked consent still records (so a retried revoke deletes it)
        await store.revoke("cid_AAAAAAAAAAAA", "a@x.com")
        assert await store.add_renders("cid_AAAAAAAAAAAA", "a@x.com", ["gs://b/4.png"])
        got = await store.get("cid_AAAAAAAAAAAA")
        assert got["person_renders"][-1] == "gs://b/4.png"

    run(go())


def test_in_memory_rows_are_copies():
    store = ps.InMemoryPersonRefsStore()
    row = _row(person_renders=["gs://b/x.png"])
    run(store.put(row))
    row["person_renders"].append("gs://b/y.png")
    got = run(store.get(row["consent_id"]))
    assert got["person_renders"] == ["gs://b/x.png"]
    got["label"] = "changed"
    assert run(store.get(row["consent_id"]))["label"] == "Me'; DROP TABLE x"
    # a re-put of the same consent_id is a no-op
    run(store.put(_row(label="other")))
    assert run(store.get(row["consent_id"]))["label"] == "Me'; DROP TABLE x"


def test_bigquery_store_over_fake_client():
    fake = FakeBigQueryClient(results=[_row()])
    store = ps.BigQueryPersonRefsStore("p.d.r", client_factory=lambda: fake)

    async def go():
        await store.put(_row())
        assert (await store.list_for("a@x.com"))[0]["consent_id"] == "cid_AAAAAAAAAAAA"
        assert (await store.get("cid"))["subject"] == "self"
        assert (await store.active_for("cid", "a@x.com"))["photo_uri"] == URI
        assert await store.revoke("cid", "a@x.com") is True
        fake.num_dml_affected_rows = 0
        assert await store.revoke("cid", "b@x.com") is False
        fake.results = []
        assert await store.get("missing") is None
        assert await store.active_for("cid", "a@x.com") is None
        fake.num_dml_affected_rows = 1
        assert await store.add_renders("cid", "a@x.com", ["gs://b/1.png"]) is True
        fake.num_dml_affected_rows = 0
        assert await store.add_renders("cid", "b@x.com", ["gs://b/1.png"]) is False

    run(go())
    assert "ARRAY_CONCAT" in fake.sqls[-1]
    assert fake.sqls[0].lstrip().startswith("MERGE `p.d.r`")
    assert "owner_user = @owner_user" in fake.sqls[1]
    assert "consent_id = @consent_id" in fake.sqls[2]
    assert "revoked_at IS NULL" in fake.sqls[3]
    assert fake.sqls[4].lstrip().startswith("UPDATE `p.d.r`")


def test_table_name_and_env_selection(caplog):
    env = {"BQ_PROJECT_ID": "p", "BQ_DATASET_ID": "d"}
    assert ps.table_name(env) == "p.d.person_references"
    assert ps.table_name({**env, "BQ_TABLE_PERSON_REFS": "r2"}) == "p.d.r2"
    mode, store = ps.build_store_from_env(env)
    assert mode == "bigquery" and store.table == "p.d.person_references"
    mode, store = ps.build_store_from_env({**env, "PERSON_REFS_STORE": "memory"})
    assert mode == "memory" and isinstance(store, ps.InMemoryPersonRefsStore)
    mode, _ = ps.build_store_from_env({})
    assert mode == "memory"
    assert "falling back" in caplog.text
    with pytest.raises(RuntimeError):
        ps.build_store_from_env({"PERSON_REFS_STORE": "sqlite"})


def test_missing_bq_env_fails_loudly_on_cloud_run():
    with pytest.raises(RuntimeError, match="BQ_PROJECT_ID"):
        ps.build_store_from_env({"K_SERVICE": "trend-trawler-api"})
    mode, _ = ps.build_store_from_env(
        {"K_SERVICE": "api", "PERSON_REFS_STORE": "memory"}
    )
    assert mode == "memory"
    mode, _ = ps.build_store_from_env(
        {"K_SERVICE": "api", "BQ_PROJECT_ID": "p", "BQ_DATASET_ID": "d"}
    )
    assert mode == "bigquery"
