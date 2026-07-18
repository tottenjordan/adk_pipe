"""Tests for the real `crf_entrypoint` orchestrator (issue #46).

Unlike `test_crf_logic.py` (which replicates the logic), these import the real
`cloud_functions.creative_fanout.main` and drive `crf_entrypoint` with fake
CloudEvents, monkeypatching the BigQuery / Pub/Sub client factories. They guard
the fix for #46: a malformed or empty trigger message must not raise
`NameError`/`UnboundLocalError` (which NACKs and causes a redelivery loop).
"""

import base64
import json
import logging
import types
from unittest.mock import MagicMock

import pandas as pd
import pytest

from cloud_functions.creative_fanout import main


def _event(data):
    """A minimal CloudEvent stand-in exposing `.data`."""
    return types.SimpleNamespace(data=data)


def _pubsub_event(payload):
    """Wrap a payload (dict → JSON, or raw str) as a base64 Pub/Sub CloudEvent."""
    raw = json.dumps(payload) if isinstance(payload, (dict, list)) else payload
    encoded = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
    return _event({"message": {"data": encoded}})


@pytest.fixture
def mocked_clients(monkeypatch):
    """Patch the BigQuery and Pub/Sub factories with MagicMocks."""
    bq = MagicMock()
    bq.project = "test-project"
    publisher = MagicMock()
    monkeypatch.setattr(main, "_get_bigquery_client", lambda: bq)
    monkeypatch.setattr(main, "_get_pubsub_client", lambda: publisher)
    return bq, publisher


def test_no_message_returns_cleanly(mocked_clients):
    """A CloudEvent with no `message` must not raise and must not dispatch."""
    bq, publisher = mocked_clients
    main.crf_entrypoint(_event({}))
    publisher.publish.assert_not_called()
    bq.query.assert_not_called()


def test_non_json_message_returns_cleanly(mocked_clients):
    """Non-JSON message data must not raise and must not dispatch."""
    bq, publisher = mocked_clients
    main.crf_entrypoint(_pubsub_event("this is not json"))
    publisher.publish.assert_not_called()
    bq.query.assert_not_called()


def test_json_without_bq_dataset_returns_cleanly(mocked_clients):
    """Valid JSON lacking `bq_dataset` must not raise and must not query/dispatch."""
    bq, publisher = mocked_clients
    main.crf_entrypoint(_pubsub_event({"foo": "bar"}))
    publisher.publish.assert_not_called()
    bq.query.assert_not_called()


def test_valid_payload_empty_dataframe_no_dispatch(mocked_clients):
    """Valid payload but no unprocessed rows → query runs, nothing dispatched."""
    bq, publisher = mocked_clients
    bq.query.return_value.to_dataframe.return_value = pd.DataFrame()
    payload = {"bq_dataset": "ds", "bq_table": "tbl", "agent_resource_id": "123"}
    main.crf_entrypoint(_pubsub_event(payload))
    publisher.publish.assert_not_called()


def test_valid_payload_with_rows_dispatches_one_message_per_row(mocked_clients):
    """Valid payload with N unprocessed rows → N worker messages published."""
    bq, publisher = mocked_clients
    df = pd.DataFrame(
        [
            {
                "entry_timestamp": pd.Timestamp("2026-07-12T00:00:00Z"),
                "target_trend": "trend A",
                "brand": "BrandX",
                "target_audience": "aud",
                "target_product": "prod",
                "key_selling_point": "ksp",
            },
            {
                "entry_timestamp": pd.Timestamp("2026-07-12T01:00:00Z"),
                "target_trend": "trend B",
                "brand": "BrandX",
                "target_audience": "aud",
                "target_product": "prod",
                "key_selling_point": "ksp",
            },
        ]
    )
    bq.query.return_value.to_dataframe.return_value = df
    payload = {"bq_dataset": "ds", "bq_table": "tbl", "agent_resource_id": "123"}
    main.crf_entrypoint(_pubsub_event(payload))
    assert publisher.publish.call_count == 2


def _two_row_df():
    return pd.DataFrame(
        [
            {
                "entry_timestamp": pd.Timestamp("2026-07-12T00:00:00Z"),
                "target_trend": "trend A",
                "brand": "BrandX",
                "target_audience": "aud",
                "target_product": "prod",
                "key_selling_point": "ksp",
            },
            {
                "entry_timestamp": pd.Timestamp("2026-07-12T01:00:00Z"),
                "target_trend": "trend B",
                "brand": "BrandX",
                "target_audience": "aud",
                "target_product": "prod",
                "key_selling_point": "ksp",
            },
        ]
    )


def test_requery_recovers_stuck_queued_rows(mocked_clients):
    """The unprocessed-rows SELECT must recover rows orphaned in QUEUED (a prior
    run set them QUEUED then crashed / partially published), not only IS NULL —
    otherwise those rows are never re-selected and never dispatched again."""
    bq, publisher = mocked_clients
    bq.query.return_value.to_dataframe.return_value = pd.DataFrame()
    payload = {"bq_dataset": "ds", "bq_table": "tbl", "agent_resource_id": "123"}
    main.crf_entrypoint(_pubsub_event(payload))
    # empty df → only the SELECT runs (no update/dispatch); it is the first query.
    select_sql = bq.query.call_args_list[0][0][0]
    assert "QUEUED" in select_sql
    assert "IS NULL" in select_sql  # still picks up brand-new rows too


def test_publish_failure_is_counted_and_does_not_crash(mocked_clients, caplog):
    """A worker-message publish that fails must not crash the batch. The failure
    is logged/counted; the row stays QUEUED and is recovered on the next run."""
    bq, publisher = mocked_clients
    bq.query.return_value.to_dataframe.return_value = _two_row_df()

    ok_future = MagicMock()
    bad_future = MagicMock()
    bad_future.result.side_effect = RuntimeError("publish boom")
    publisher.publish.side_effect = [ok_future, bad_future]

    payload = {"bq_dataset": "ds", "bq_table": "tbl", "agent_resource_id": "123"}
    with caplog.at_level(logging.WARNING):
        main.crf_entrypoint(_pubsub_event(payload))  # must NOT raise

    assert publisher.publish.call_count == 2  # both rows attempted
    assert "publishes failed" in caplog.text  # failure surfaced, not swallowed


def test_acquire_processing_lock_true_when_one_row_updated():
    """Real-function coverage for the exactly-once worker lock: 1 affected row
    → True, and the UPDATE gates on the row still being QUEUED."""
    bq = MagicMock()
    bq.project = "test-project"
    bq.query.return_value.result.return_value.num_dml_affected_rows = 1
    got = main.acquire_processing_lock(bq, "ds", "tbl", "2026-07-12T00:00:00")
    assert got is True
    lock_sql = bq.query.call_args[0][0]
    assert "AND processed_status = 'QUEUED'" in lock_sql


def test_acquire_processing_lock_false_when_zero_rows_updated():
    """0 affected rows (another worker won the race) → False."""
    bq = MagicMock()
    bq.project = "test-project"
    bq.query.return_value.result.return_value.num_dml_affected_rows = 0
    got = main.acquire_processing_lock(bq, "ds", "tbl", "2026-07-12T00:00:00")
    assert got is False
