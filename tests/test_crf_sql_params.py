"""SQL-safety tests for the CRF fan-out (real `creative_fanout.main`).

Every BigQuery statement the orchestrator/worker issues must:

* take its *identifiers* (dataset/table, which arrive in the Pub/Sub payload)
  only from a config-derived allow-list, backtick-quoted — BigQuery cannot
  parameterize identifiers, so validation is the only defense; and
* pass every *value* (timestamps, statuses, reaper knobs) as a query parameter
  (`ScalarQueryParameter` / `ArrayQueryParameter`), never interpolated.
"""

import base64
import json
import types
from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest
from google.cloud import bigquery
from google.cloud.bigquery import Row

from cloud_functions.creative_fanout import (
    clients,
    main,
    messages,
    orchestrator,
    status_store,
    worker,
)
from cloud_functions.creative_fanout.config import config

DS = config.BQ_DATASET_ID
TBL = config.BQ_TABLE_TARGETS
TS = "2026-07-12T00:00:00+00:00"


def _bq(num_rows=1):
    bq = MagicMock()
    bq.project = "test-project"
    bq.query_and_wait.return_value.num_dml_affected_rows = num_rows
    return bq


def _params(bq_call):
    """{name: (type, value)} for the query parameters of one `bq.query_and_wait` call."""
    job_config = bq_call.kwargs["job_config"]
    out = {}
    for p in job_config.query_parameters:
        if isinstance(p, bigquery.ArrayQueryParameter):
            out[p.name] = (f"ARRAY<{p.array_type}>", list(p.values))
        else:
            out[p.name] = (p.type_, p.value)
    return out


# ============================================================
# (a) identifier allow-list
# ============================================================
class TestIdentifierAllowList:
    def test_allow_list_defaults_come_from_config(self):
        assert TBL in config.ALLOWED_BQ_TABLES
        assert DS in config.ALLOWED_BQ_DATASETS

    @pytest.mark.parametrize(
        "dataset,table",
        [
            (DS, "trend_creatives"),  # real table, but not a CRF target
            (DS, "tbl`; DROP TABLE x; --"),
            ("other_dataset", TBL),
            ("ds` WHERE 1=1 --", TBL),
        ],
    )
    def test_builders_reject_non_allow_listed_identifiers(self, dataset, table):
        with pytest.raises(ValueError):
            status_store._build_update_status_sql(
                "p", dataset, table, [TS], "PROCESSED"
            )
        with pytest.raises(ValueError):
            status_store._build_lock_sql("p", dataset, table, TS)
        with pytest.raises(ValueError):
            status_store._build_reap_sql("p", dataset, table, 45, 3)
        with pytest.raises(ValueError):
            status_store._build_select_unprocessed_sql("p", dataset, table, max_rows=10)

    def test_update_rows_status_rejects_bad_table_without_querying(self):
        bq = _bq()
        with pytest.raises(ValueError):
            status_store.update_rows_status(
                bq, DS, "not_allowed", [TS], status="FAILED"
            )
        bq.query_and_wait.assert_not_called()

    def test_acquire_lock_rejects_bad_table_without_querying(self):
        bq = _bq()
        with pytest.raises(ValueError):
            status_store.acquire_processing_lock(bq, DS, "not_allowed", TS)
        bq.query_and_wait.assert_not_called()

    def test_rejects_malformed_project(self):
        with pytest.raises(ValueError):
            status_store._build_lock_sql("p`.x", DS, TBL, TS)

    def test_identifiers_are_backtick_quoted(self):
        sql, _ = status_store._build_lock_sql("test-project", DS, TBL, TS)
        assert f"`test-project.{DS}.{TBL}`" in sql

    def test_extra_allowed_table_via_config(self, monkeypatch):
        """Load-test tables (e.g. `<target>_p95`) are opt-in via config."""
        monkeypatch.setattr(
            config, "ALLOWED_BQ_TABLES", frozenset({TBL, "target_trends_crf_p95"})
        )
        sql, _ = status_store._build_lock_sql("p", DS, "target_trends_crf_p95", TS)
        assert "target_trends_crf_p95" in sql

    def test_status_outside_known_set_rejected(self):
        with pytest.raises(ValueError):
            status_store._build_update_status_sql("p", DS, TBL, [TS], "DONE'; --")


# ============================================================
# (b) values are query parameters, never interpolated
# ============================================================
class TestValuesAreParameterized:
    def test_update_rows_status_params(self):
        bq = _bq()
        ts2 = "2026-07-12T01:00:00+00:00"
        status_store.update_rows_status(bq, DS, TBL, [TS, ts2], status="QUEUED")
        call = bq.query_and_wait.call_args
        sql = call.args[0]
        assert TS not in sql and ts2 not in sql
        assert "QUEUED" not in sql
        assert "@status" in sql and "UNNEST(@timestamps)" in sql
        params = _params(call)
        assert params["status"] == ("STRING", "QUEUED")
        typ, values = params["timestamps"]
        assert typ == "ARRAY<TIMESTAMP>"
        assert values == [
            datetime(2026, 7, 12, 0, tzinfo=UTC),
            datetime(2026, 7, 12, 1, tzinfo=UTC),
        ]

    def test_naive_timestamp_treated_as_utc(self):
        """Old SQL did TIMESTAMP('<naive>') which BigQuery reads as UTC."""
        bq = _bq()
        status_store.acquire_processing_lock(bq, DS, TBL, "2026-07-12T00:00:00")
        typ, val = _params(bq.query_and_wait.call_args)["entry_timestamp"]
        assert typ == "TIMESTAMP"
        assert val == datetime(2026, 7, 12, tzinfo=UTC)

    def test_garbage_timestamp_rejected(self):
        with pytest.raises(ValueError):
            status_store._build_lock_sql("p", DS, TBL, "2026-07-12') OR TRUE --")

    def test_lock_params(self):
        bq = _bq()
        assert status_store.acquire_processing_lock(bq, DS, TBL, TS) is True
        call = bq.query_and_wait.call_args
        sql = call.args[0]
        assert TS not in sql
        assert "entry_timestamp = @entry_timestamp" in sql
        assert _params(call)["entry_timestamp"] == (
            "TIMESTAMP",
            datetime(2026, 7, 12, tzinfo=UTC),
        )

    def test_reap_params(self, monkeypatch):
        monkeypatch.setattr(config, "REAP_STALE_PROCESSING_MINUTES", 45)
        monkeypatch.setattr(config, "MAX_PROCESSING_ATTEMPTS", 3)
        bq = _bq(num_rows=0)
        status_store.reap_stale_processing_rows(bq, DS, TBL)
        call = bq.query_and_wait.call_args
        sql = call.args[0]
        assert "45" not in sql and ">= 3" not in sql
        assert "INTERVAL @stale_minutes MINUTE" in sql
        assert ">= @max_attempts" in sql
        params = _params(call)
        assert params["stale_minutes"] == ("INT64", 45)
        assert params["max_attempts"] == ("INT64", 3)

    def test_orchestrator_select_uses_allow_listed_quoted_table(self, monkeypatch):
        bq = _bq()
        # result() iterates empty (a bare MagicMock); a pandas regression fails.
        bq.query_and_wait.return_value.to_dataframe.side_effect = AssertionError(
            "to_dataframe needs pandas; iterate rows"
        )
        monkeypatch.setattr(clients, "get_bigquery_client", lambda: bq)
        monkeypatch.setattr(clients, "get_pubsub_client", MagicMock)
        payload = {"bq_dataset": DS, "bq_table": TBL, "agent_resource_id": "1"}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        main.crf_entrypoint(types.SimpleNamespace(data={"message": {"data": encoded}}))
        select_sql = bq.query_and_wait.call_args_list[1].args[0]
        assert f"FROM `test-project.{DS}.{TBL}`" in select_sql


# ============================================================
# orchestrator row limit (caps how many rows one trigger dispatches)
# ============================================================
class TestOrchestratorRowLimit:
    def _run(self, monkeypatch, payload):
        bq = _bq()
        # result() iterates empty (a bare MagicMock); a pandas regression fails.
        bq.query_and_wait.return_value.to_dataframe.side_effect = AssertionError(
            "to_dataframe needs pandas; iterate rows"
        )
        monkeypatch.setattr(clients, "get_bigquery_client", lambda: bq)
        monkeypatch.setattr(clients, "get_pubsub_client", MagicMock)
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        main.crf_entrypoint(types.SimpleNamespace(data={"message": {"data": encoded}}))
        return bq.query_and_wait.call_args_list[1]  # [0] is the reap UPDATE

    def test_select_sql_binds_limit_as_param(self):
        sql, params = status_store._build_select_unprocessed_sql(
            "p", DS, TBL, max_rows=7
        )
        assert "LIMIT @max_rows" in sql
        assert sql.index("ORDER BY") < sql.index("LIMIT")
        (p,) = params
        assert (p.name, p.type_, p.value) == ("max_rows", "INT64", 7)

    def test_select_sql_excludes_trends_flagged_sensitive(self):
        sql, params = status_store._build_select_unprocessed_sql(
            "p", DS, TBL, max_rows=3
        )
        flat = " ".join(sql.split())
        assert (
            "COALESCE(JSON_VALUE(trend_context, '$.sensitive'), 'false')"
            " != 'true'" in flat
        )
        # The status OR is parenthesised so the sensitive filter applies to both
        # brand-new and orphaned QUEUED rows.
        assert (
            "WHERE (processed_status IS NULL OR processed_status = 'QUEUED')"
            " AND COALESCE(" in flat
        )
        assert flat.index("COALESCE(") < flat.index("ORDER BY")
        # BigQuery rejects SAFE.JSON_VALUE ("SAFE with function json_value is not
        # supported"); plain JSON_VALUE already yields NULL for bad JSON.
        assert "SAFE." not in sql
        # No extra parameters: the exclusion is a literal, not a bound value.
        assert [p.name for p in params] == ["max_rows"]

    def test_entrypoint_logs_sensitive_exclusion(self, monkeypatch, caplog):
        payload = {"bq_dataset": DS, "bq_table": TBL, "agent_resource_id": "1"}
        with caplog.at_level("INFO"):
            self._run(monkeypatch, payload)
        assert any("sensitive" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(
        ("requested", "expected"),
        [
            (None, 10),  # unset -> config cap
            (3, 3),  # lower than the cap -> honored
            (500, 10),  # above the cap -> clamped
            ("4", 4),  # numeric string -> honored
            (0, 10),  # non-positive -> cap
            (-2, 10),
            ("lots", 10),  # garbage -> cap
            (True, 10),  # bool is not a row count
        ],
    )
    def test_resolve_max_rows(self, requested, expected):
        assert orchestrator._resolve_max_rows(requested, cap=10) == expected

    def test_default_cap_comes_from_config(self, monkeypatch):
        monkeypatch.setattr(config, "CRF_MAX_ROWS_PER_RUN", 5)
        payload = {"bq_dataset": DS, "bq_table": TBL, "agent_resource_id": "1"}
        call = self._run(monkeypatch, payload)
        assert _params(call)["max_rows"] == ("INT64", 5)

    def test_message_max_rows_lowers_limit_but_cannot_exceed_cap(self, monkeypatch):
        monkeypatch.setattr(config, "CRF_MAX_ROWS_PER_RUN", 5)
        base = {"bq_dataset": DS, "bq_table": TBL, "agent_resource_id": "1"}
        call = self._run(monkeypatch, {**base, "max_rows": 2})
        assert _params(call)["max_rows"] == ("INT64", 2)
        call = self._run(monkeypatch, {**base, "max_rows": 999})
        assert _params(call)["max_rows"] == ("INT64", 5)


# ============================================================
# entrypoints ACK (don't loop) on a non-allow-listed payload
# ============================================================
def _event(payload):
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return types.SimpleNamespace(data={"message": {"data": encoded}})


def test_orchestrator_acks_non_allow_listed_table(monkeypatch):
    """A bad identifier can never become valid on redelivery → log + ACK."""
    bq = _bq()
    publisher = MagicMock()
    monkeypatch.setattr(clients, "get_bigquery_client", lambda: bq)
    monkeypatch.setattr(clients, "get_pubsub_client", lambda: publisher)
    payload = {"bq_dataset": DS, "bq_table": "evil", "agent_resource_id": "1"}
    main.crf_entrypoint(_event(payload))  # must not raise
    bq.query_and_wait.assert_not_called()
    publisher.publish.assert_not_called()


# The rest of a well-formed worker row_data, so each test below fails only on
# the field it is about (a row missing these is rejected on its own).
_CAMPAIGN = {
    "brand": "b",
    "target_product": "p",
    "key_selling_point": "k",
    "target_audience": "a",
    "target_search_trend": "t",
}


def test_worker_acks_non_allow_listed_table(monkeypatch):
    bq = _bq()
    monkeypatch.setattr(clients, "get_bigquery_client", lambda: bq)
    payload = {
        "bq_dataset": DS,
        "bq_table": "evil",
        "agent_resource_id": "1",
        "row_data": {"entry_timestamp": TS, "index": 0, **_CAMPAIGN},
    }
    main.agent_worker_entrypoint(_event(payload))  # must not raise
    bq.query_and_wait.assert_not_called()


def test_worker_acks_malformed_entry_timestamp(monkeypatch, caplog):
    """A malformed timestamp can never parse on redelivery → log error + ACK."""
    bq = _bq()
    monkeypatch.setattr(clients, "get_bigquery_client", lambda: bq)
    payload = {
        "bq_dataset": DS,
        "bq_table": TBL,
        "agent_resource_id": "1",
        "row_data": {"entry_timestamp": "not-a-timestamp", "index": 0, **_CAMPAIGN},
    }
    with caplog.at_level("ERROR"):
        main.agent_worker_entrypoint(_event(payload))  # must not raise
    bq.query_and_wait.assert_not_called()
    assert any(
        r.levelname == "ERROR" and "not-a-timestamp" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("bad", [12345, None, "2026-07-12') OR TRUE --"])
def test_worker_acks_non_string_or_injected_timestamp(monkeypatch, bad):
    bq = _bq()
    monkeypatch.setattr(clients, "get_bigquery_client", lambda: bq)
    payload = {
        "bq_dataset": DS,
        "bq_table": TBL,
        "agent_resource_id": "1",
        "row_data": {"entry_timestamp": bad, "index": 0, **_CAMPAIGN},
    }
    main.agent_worker_entrypoint(_event(payload))  # must not raise
    bq.query_and_wait.assert_not_called()


# ============================================================
# real orchestrator serialization round-trips through the builders
# ============================================================
_REAL_INSTANT = datetime(2026, 9, 1, 12, 34, 56, 123456, tzinfo=UTC)


def _orchestrator_serialize(ts):
    """Mirror crf_entrypoint: `row["entry_timestamp"].isoformat()` on the
    tz-aware datetime a BigQuery Row holds for a TIMESTAMP, then JSON."""
    return json.loads(json.dumps({"entry_timestamp": ts.isoformat()}))[
        "entry_timestamp"
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "2026-09-01T12:34:56.123456+00:00",
        # Same instant expressed with a non-UTC offset.
        "2026-09-01T14:34:56.123456+02:00",
    ],
)
def test_orchestrator_timestamp_round_trips_through_builders(raw):
    serialized = _orchestrator_serialize(datetime.fromisoformat(raw))

    _, lock_params = status_store._build_lock_sql("p", DS, TBL, serialized)
    (lock_param,) = lock_params
    assert lock_param.type_ == "TIMESTAMP"
    val = lock_param.value
    assert val == _REAL_INSTANT
    assert val.microsecond == 123456
    assert val.utcoffset().total_seconds() == 0
    assert val.astimezone(UTC).isoformat() == "2026-09-01T12:34:56.123456+00:00"

    _, update_params = status_store._build_update_status_sql(
        "p", DS, TBL, [serialized], "PROCESSED"
    )
    ts_param = next(p for p in update_params if p.name == "timestamps")
    assert ts_param.array_type == "TIMESTAMP"
    (uval,) = ts_param.values
    assert uval == _REAL_INSTANT
    assert uval.microsecond == 123456
    assert uval.utcoffset().total_seconds() == 0


# ============================================================
# (d) row → worker message: trend_scout's detection date + context ride along
# ============================================================
def _df_rows(extra: dict | None = None, drop: tuple[str, ...] = ()):
    """Two (index, Row) pairs, as crf_entrypoint enumerates QueryJob.result()."""
    base = {
        "entry_timestamp": [datetime(2026, 10, 11, 12, tzinfo=UTC)] * 2,
        "target_trend": ["Texas Football", "Golden Dip"],
        "brand": ["PRS", "PRS"],
        "target_audience": ["fans", "fans"],
        "target_product": ["SE CE24", "SE CE24"],
        "key_selling_point": ["tone", "tone"],
        "refresh_date": [date(2026, 10, 10), None],
        "trend_context": ['{"hook":"Game day"}', None],
    }
    base.update(extra or {})
    for key in drop:
        base.pop(key)
    field_to_index = {name: i for i, name in enumerate(base)}
    columns = list(base.values())
    return [
        (i, Row(tuple(col[i] for col in columns), field_to_index)) for i in range(2)
    ]


def test_row_dict_forwards_refresh_date_and_trend_context():
    (i0, r0), (i1, r1) = _df_rows()
    first = messages.build_row_dict(i0, r0)
    assert first["refresh_date"] == "2026-10-10"
    assert first["trend_context"] == '{"hook":"Game day"}'
    assert first["target_search_trend"] == "Texas Football"
    second = messages.build_row_dict(i1, r1)
    assert second["refresh_date"] == ""  # NULL → blank
    assert second["trend_context"] == ""  # NULL → blank, never "None"
    # The worker payload must stay JSON-serializable.
    json.dumps({"row_data": first})
    json.dumps({"row_data": second})


def test_row_dict_handles_timestamp_refresh_date():
    # A TIMESTAMP-typed refresh_date: a datetime is also a date, so the
    # datetime branch must come first to drop the time part.
    rows = _df_rows({"refresh_date": [datetime(2026, 10, 9, 23, 30, tzinfo=UTC), None]})
    assert messages.build_row_dict(*rows[0])["refresh_date"] == "2026-10-09"
    assert messages.build_row_dict(*rows[1])["refresh_date"] == ""


def test_row_dict_without_trend_context_column():
    """Before the ALTER runs, SELECT * has no trend_context column."""
    (i0, r0), _ = _df_rows(drop=("trend_context",))
    assert messages.build_row_dict(i0, r0)["trend_context"] == ""


def test_campaign_state_maps_trend_fields_and_skips_blanks():
    msg = {
        "index": 0,
        "brand": "PRS",
        "target_product": "SE CE24",
        "key_selling_point": "tone",
        "target_audience": "fans",
        "target_search_trend": "Texas Football",
        "refresh_date": "2026-10-10",
        "trend_context": '{"hook":"Game day"}',
    }
    state = worker.build_campaign_state(msg)
    assert state["trend_detected_on"] == "2026-10-10"
    assert state["trend_scout_context"] == '{"hook":"Game day"}'
    assert "refresh_date" not in state and "trend_context" not in state

    bare = worker.build_campaign_state(
        {**msg, "refresh_date": "", "trend_context": None}
    )
    assert "trend_detected_on" not in bare
    assert "trend_scout_context" not in bare
    # An old message without the new keys still works.
    old = {k: v for k, v in msg.items() if k not in ("refresh_date", "trend_context")}
    assert set(worker.build_campaign_state(old)) == {
        "brand",
        "target_product",
        "key_selling_points",
        "target_audience",
        "target_search_trends",
    }
