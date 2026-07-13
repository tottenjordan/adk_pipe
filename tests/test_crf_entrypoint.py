"""Tests for the real `crf_entrypoint` orchestrator (issue #46).

Unlike `test_crf_logic.py` (which replicates the logic), these import the real
`cloud_functions.creative_fanout.main` and drive `crf_entrypoint` with fake
CloudEvents, monkeypatching the BigQuery / Pub/Sub client factories. They guard
the fix for #46: a malformed or empty trigger message must not raise
`NameError`/`UnboundLocalError` (which NACKs and causes a redelivery loop).
"""

import base64
import json
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
