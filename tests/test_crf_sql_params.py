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
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest
from google.cloud import bigquery

from cloud_functions.creative_fanout import main
from cloud_functions.creative_fanout.config import config

DS = config.BQ_DATASET_ID
TBL = config.BQ_TABLE_TARGETS
TS = "2026-07-12T00:00:00+00:00"


def _bq(num_rows=1):
    bq = MagicMock()
    bq.project = "test-project"
    bq.query.return_value.result.return_value.num_dml_affected_rows = num_rows
    return bq


def _params(bq_call):
    """{name: (type, value)} for the query parameters of one `bq.query` call."""
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
            main._build_update_status_sql("p", dataset, table, [TS], "PROCESSED")
        with pytest.raises(ValueError):
            main._build_lock_sql("p", dataset, table, TS)
        with pytest.raises(ValueError):
            main._build_reap_sql("p", dataset, table, 45, 3)
        with pytest.raises(ValueError):
            main._build_select_unprocessed_sql("p", dataset, table)

    def test_update_rows_status_rejects_bad_table_without_querying(self):
        bq = _bq()
        with pytest.raises(ValueError):
            main.update_rows_status(bq, DS, "not_allowed", [TS], status="FAILED")
        bq.query.assert_not_called()

    def test_acquire_lock_rejects_bad_table_without_querying(self):
        bq = _bq()
        with pytest.raises(ValueError):
            main.acquire_processing_lock(bq, DS, "not_allowed", TS)
        bq.query.assert_not_called()

    def test_rejects_malformed_project(self):
        with pytest.raises(ValueError):
            main._build_lock_sql("p`.x", DS, TBL, TS)

    def test_identifiers_are_backtick_quoted(self):
        sql, _ = main._build_lock_sql("test-project", DS, TBL, TS)
        assert f"`test-project.{DS}.{TBL}`" in sql

    def test_extra_allowed_table_via_config(self, monkeypatch):
        """Load-test tables (e.g. `<target>_p95`) are opt-in via config."""
        monkeypatch.setattr(
            config, "ALLOWED_BQ_TABLES", frozenset({TBL, "target_trends_crf_p95"})
        )
        sql, _ = main._build_lock_sql("p", DS, "target_trends_crf_p95", TS)
        assert "target_trends_crf_p95" in sql

    def test_status_outside_known_set_rejected(self):
        with pytest.raises(ValueError):
            main._build_update_status_sql("p", DS, TBL, [TS], "DONE'; --")


# ============================================================
# (b) values are query parameters, never interpolated
# ============================================================
class TestValuesAreParameterized:
    def test_update_rows_status_params(self):
        bq = _bq()
        ts2 = "2026-07-12T01:00:00+00:00"
        main.update_rows_status(bq, DS, TBL, [TS, ts2], status="QUEUED")
        call = bq.query.call_args
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
        main.acquire_processing_lock(bq, DS, TBL, "2026-07-12T00:00:00")
        typ, val = _params(bq.query.call_args)["entry_timestamp"]
        assert typ == "TIMESTAMP"
        assert val == datetime(2026, 7, 12, tzinfo=UTC)

    def test_garbage_timestamp_rejected(self):
        with pytest.raises(ValueError):
            main._build_lock_sql("p", DS, TBL, "2026-07-12') OR TRUE --")

    def test_lock_params(self):
        bq = _bq()
        assert main.acquire_processing_lock(bq, DS, TBL, TS) is True
        call = bq.query.call_args
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
        main.reap_stale_processing_rows(bq, DS, TBL)
        call = bq.query.call_args
        sql = call.args[0]
        assert "45" not in sql and ">= 3" not in sql
        assert "INTERVAL @stale_minutes MINUTE" in sql
        assert ">= @max_attempts" in sql
        params = _params(call)
        assert params["stale_minutes"] == ("INT64", 45)
        assert params["max_attempts"] == ("INT64", 3)

    def test_orchestrator_select_uses_allow_listed_quoted_table(self, monkeypatch):
        bq = _bq()
        bq.query.return_value.to_dataframe.return_value = pd.DataFrame()
        monkeypatch.setattr(main, "_get_bigquery_client", lambda: bq)
        monkeypatch.setattr(main, "_get_pubsub_client", MagicMock)
        payload = {"bq_dataset": DS, "bq_table": TBL, "agent_resource_id": "1"}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        main.crf_entrypoint(types.SimpleNamespace(data={"message": {"data": encoded}}))
        select_sql = bq.query.call_args_list[1].args[0]
        assert f"FROM `test-project.{DS}.{TBL}`" in select_sql


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
    monkeypatch.setattr(main, "_get_bigquery_client", lambda: bq)
    monkeypatch.setattr(main, "_get_pubsub_client", lambda: publisher)
    payload = {"bq_dataset": DS, "bq_table": "evil", "agent_resource_id": "1"}
    main.crf_entrypoint(_event(payload))  # must not raise
    bq.query.assert_not_called()
    publisher.publish.assert_not_called()


def test_worker_acks_non_allow_listed_table(monkeypatch):
    bq = _bq()
    monkeypatch.setattr(main, "_get_bigquery_client", lambda: bq)
    payload = {
        "bq_dataset": DS,
        "bq_table": "evil",
        "agent_resource_id": "1",
        "row_data": {"entry_timestamp": TS, "index": 0},
    }
    main.agent_worker_entrypoint(_event(payload))  # must not raise
    bq.query.assert_not_called()


def test_worker_acks_malformed_entry_timestamp(monkeypatch, caplog):
    """A malformed timestamp can never parse on redelivery → log error + ACK."""
    bq = _bq()
    monkeypatch.setattr(main, "_get_bigquery_client", lambda: bq)
    payload = {
        "bq_dataset": DS,
        "bq_table": TBL,
        "agent_resource_id": "1",
        "row_data": {"entry_timestamp": "not-a-timestamp", "index": 0},
    }
    with caplog.at_level("ERROR"):
        main.agent_worker_entrypoint(_event(payload))  # must not raise
    bq.query.assert_not_called()
    assert any(
        r.levelname == "ERROR" and "not-a-timestamp" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("bad", [12345, None, "2026-07-12') OR TRUE --"])
def test_worker_acks_non_string_or_injected_timestamp(monkeypatch, bad):
    bq = _bq()
    monkeypatch.setattr(main, "_get_bigquery_client", lambda: bq)
    payload = {
        "bq_dataset": DS,
        "bq_table": TBL,
        "agent_resource_id": "1",
        "row_data": {"entry_timestamp": bad, "index": 0},
    }
    main.agent_worker_entrypoint(_event(payload))  # must not raise
    bq.query.assert_not_called()


# ============================================================
# real orchestrator serialization round-trips through the builders
# ============================================================
_REAL_INSTANT = datetime(2026, 9, 1, 12, 34, 56, 123456, tzinfo=UTC)


def _orchestrator_serialize(ts):
    """Mirror crf_entrypoint: `row["entry_timestamp"].isoformat()` on the
    tz-aware pandas Timestamp BigQuery's to_dataframe returns, then JSON."""
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
    serialized = _orchestrator_serialize(pd.Timestamp(raw))

    _, lock_params = main._build_lock_sql("p", DS, TBL, serialized)
    (lock_param,) = lock_params
    assert lock_param.type_ == "TIMESTAMP"
    val = lock_param.value
    assert val == _REAL_INSTANT
    assert val.microsecond == 123456
    assert val.utcoffset().total_seconds() == 0
    assert val.astimezone(UTC).isoformat() == "2026-09-01T12:34:56.123456+00:00"

    _, update_params = main._build_update_status_sql(
        "p", DS, TBL, [serialized], "PROCESSED"
    )
    ts_param = next(p for p in update_params if p.name == "timestamps")
    assert ts_param.array_type == "TIMESTAMP"
    (uval,) = ts_param.values
    assert uval == _REAL_INSTANT
    assert uval.microsecond == 123456
    assert uval.utcoffset().total_seconds() == 0
