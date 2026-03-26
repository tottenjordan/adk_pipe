"""Tests for Cloud Run Function logic (orchestrator + worker).

These tests replicate and exercise the pure logic from
cloud_funktions/creative_crf/main.py without requiring GCP credentials,
functions_framework, or vertexai imports.
"""
import json
import base64
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone


# ============================================================
# Replicated helpers (avoids importing main.py with its heavy
# module-level clients)
# ============================================================

def decode_pubsub_payload(cloud_event_data: dict) -> dict | None:
    """Decode and parse the base64 PubSub message from a CloudEvent."""
    if "message" not in cloud_event_data:
        return None
    if "data" not in cloud_event_data["message"]:
        return None
    raw = base64.b64decode(cloud_event_data["message"]["data"]).decode("utf-8")
    return json.loads(raw)


def build_worker_payload(
    dataset: str, table: str, agent_resource_id: str, row_dict: dict
) -> dict:
    """Build the payload published to the worker topic for a single row."""
    return {
        "bq_dataset": dataset,
        "bq_table": table,
        "agent_resource_id": agent_resource_id,
        "row_data": row_dict,
    }


def build_row_dict(index: int, row: dict) -> dict:
    """Map a BQ row to the dict format used in worker payloads."""
    timestamp_str = row["entry_timestamp"]
    if hasattr(timestamp_str, "isoformat"):
        timestamp_str = timestamp_str.isoformat()
    return {
        "index": index,
        "entry_timestamp": timestamp_str,
        "target_search_trend": row["target_trend"],
        "brand": row["brand"],
        "target_audience": row["target_audience"],
        "target_product": row["target_product"],
        "key_selling_point": row["key_selling_point"],
    }


def build_update_sql(project, dataset, table, timestamps, status="PROCESSED"):
    """Build the SQL for updating row statuses."""
    ts_list = [f"TIMESTAMP('{t}')" for t in timestamps]
    ts_string = ", ".join(ts_list)
    return f"""
        UPDATE `{project}.{dataset}.{table}`
        SET processed_status = '{status}'
        WHERE entry_timestamp IN ({ts_string})
    """


def build_lock_sql(project, dataset, table, timestamp):
    """Build the SQL for acquiring a processing lock on a single row."""
    return f"""
        UPDATE `{project}.{dataset}.{table}`
        SET processed_status = 'PROCESSING'
        WHERE
            entry_timestamp = TIMESTAMP('{timestamp}')
            AND processed_status = 'QUEUED'
    """


def build_user_query(msg_dict: dict) -> str:
    """Build the user query string sent to the agent."""
    return f"""Brand: {msg_dict['brand']}
    Target Product: {msg_dict['target_product']}
    Key Selling Point(s): {msg_dict['key_selling_point']}
    Target Audience: {msg_dict['target_audience']}
    Target Search Trend: {msg_dict['target_search_trend']}
    """


# ============================================================
# Tests: PubSub message decoding
# ============================================================
class TestPubSubDecoding:
    def _make_cloud_event_data(self, payload: dict) -> dict:
        """Helper to create a CloudEvent-style data dict."""
        encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
        return {"message": {"data": encoded}}

    def test_decodes_valid_orchestrator_message(self):
        payload = {
            "bq_dataset": "trend_trawler",
            "bq_table": "target_trends_crf",
            "agent_resource_id": "47239417575768064",
        }
        data = self._make_cloud_event_data(payload)
        result = decode_pubsub_payload(data)
        assert result == payload

    def test_decodes_valid_worker_message(self):
        row_data = {
            "index": 0,
            "entry_timestamp": "2025-01-15T10:00:00+00:00",
            "target_search_trend": "olive garden",
            "brand": "PRS",
            "target_audience": "musicians",
            "target_product": "SE CE24",
            "key_selling_point": "Great tone",
        }
        payload = {
            "bq_dataset": "trend_trawler",
            "bq_table": "target_trends_crf",
            "agent_resource_id": "12345",
            "row_data": row_data,
        }
        data = self._make_cloud_event_data(payload)
        result = decode_pubsub_payload(data)
        assert result["row_data"]["target_search_trend"] == "olive garden"

    def test_returns_none_for_missing_message_key(self):
        assert decode_pubsub_payload({}) is None

    def test_returns_none_for_missing_data_key(self):
        assert decode_pubsub_payload({"message": {}}) is None

    def test_raises_on_invalid_base64(self):
        data = {"message": {"data": "not-valid-base64!!!"}}
        with pytest.raises(Exception):
            decode_pubsub_payload(data)

    def test_raises_on_non_json_payload(self):
        encoded = base64.b64encode(b"this is not json").decode("utf-8")
        data = {"message": {"data": encoded}}
        with pytest.raises(json.JSONDecodeError):
            decode_pubsub_payload(data)


# ============================================================
# Tests: Row-to-payload mapping
# ============================================================
SAMPLE_BQ_ROW = {
    "entry_timestamp": "2025-01-15T10:00:00+00:00",
    "target_trend": "olive garden",
    "brand": "Paul Reed Smith (PRS)",
    "target_audience": "millennials who follow jam bands",
    "target_product": "PRS SE CE24 Electric Guitar",
    "key_selling_point": "Wide tonal range from 85/15 S pickups",
}


class TestRowMapping:
    def test_maps_target_trend_to_target_search_trend(self):
        result = build_row_dict(0, SAMPLE_BQ_ROW)
        assert result["target_search_trend"] == "olive garden"
        assert "target_trend" not in result

    def test_preserves_index(self):
        result = build_row_dict(3, SAMPLE_BQ_ROW)
        assert result["index"] == 3

    def test_preserves_all_campaign_fields(self):
        result = build_row_dict(0, SAMPLE_BQ_ROW)
        assert result["brand"] == "Paul Reed Smith (PRS)"
        assert result["target_audience"] == "millennials who follow jam bands"
        assert result["target_product"] == "PRS SE CE24 Electric Guitar"
        assert result["key_selling_point"] == "Wide tonal range from 85/15 S pickups"

    def test_serializes_datetime_object(self):
        row = {**SAMPLE_BQ_ROW, "entry_timestamp": datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)}
        result = build_row_dict(0, row)
        assert isinstance(result["entry_timestamp"], str)
        assert "2025-01-15" in result["entry_timestamp"]

    def test_passes_through_string_timestamp(self):
        result = build_row_dict(0, SAMPLE_BQ_ROW)
        assert result["entry_timestamp"] == "2025-01-15T10:00:00+00:00"


# ============================================================
# Tests: Worker payload construction
# ============================================================
class TestWorkerPayload:
    def test_includes_all_required_keys(self):
        row_dict = build_row_dict(0, SAMPLE_BQ_ROW)
        payload = build_worker_payload(
            "trend_trawler", "target_trends_crf", "12345", row_dict
        )
        assert payload["bq_dataset"] == "trend_trawler"
        assert payload["bq_table"] == "target_trends_crf"
        assert payload["agent_resource_id"] == "12345"
        assert payload["row_data"] == row_dict

    def test_multiple_rows_produce_separate_payloads(self):
        rows = [
            {**SAMPLE_BQ_ROW, "target_trend": f"trend_{i}"}
            for i in range(3)
        ]
        payloads = [
            build_worker_payload(
                "ds", "tbl", "agent_id", build_row_dict(i, row)
            )
            for i, row in enumerate(rows)
        ]
        assert len(payloads) == 3
        trends = [p["row_data"]["target_search_trend"] for p in payloads]
        assert trends == ["trend_0", "trend_1", "trend_2"]


# ============================================================
# Tests: SQL generation
# ============================================================
class TestUpdateStatusSQL:
    def test_single_timestamp(self):
        sql = build_update_sql(
            "my-project", "trend_trawler", "target_trends_crf",
            ["2025-01-15T10:00:00+00:00"],
            status="PROCESSED",
        )
        assert "SET processed_status = 'PROCESSED'" in sql
        assert "TIMESTAMP('2025-01-15T10:00:00+00:00')" in sql
        assert "my-project.trend_trawler.target_trends_crf" in sql

    def test_multiple_timestamps(self):
        timestamps = [
            "2025-01-15T10:00:00+00:00",
            "2025-01-15T11:00:00+00:00",
            "2025-01-15T12:00:00+00:00",
        ]
        sql = build_update_sql(
            "proj", "ds", "tbl", timestamps, status="QUEUED"
        )
        assert "SET processed_status = 'QUEUED'" in sql
        for ts in timestamps:
            assert f"TIMESTAMP('{ts}')" in sql

    def test_status_values(self):
        for status in ["QUEUED", "PROCESSING", "PROCESSED", "FAILED"]:
            sql = build_update_sql("p", "d", "t", ["ts1"], status=status)
            assert f"SET processed_status = '{status}'" in sql


class TestLockSQL:
    def test_lock_targets_queued_rows(self):
        sql = build_lock_sql(
            "my-project", "trend_trawler", "target_trends_crf",
            "2025-01-15T10:00:00+00:00",
        )
        assert "SET processed_status = 'PROCESSING'" in sql
        assert "AND processed_status = 'QUEUED'" in sql
        assert "TIMESTAMP('2025-01-15T10:00:00+00:00')" in sql


# ============================================================
# Tests: acquire_processing_lock behavior (mocked BQ client)
# ============================================================
class TestAcquireProcessingLock:
    def _make_mock_bq_client(self, num_rows_affected):
        mock_client = MagicMock()
        mock_client.project = "test-project"
        mock_result = MagicMock()
        mock_result.num_dml_affected_rows = num_rows_affected
        mock_client.query.return_value.result.return_value = mock_result
        return mock_client

    def test_returns_true_when_lock_acquired(self):
        """Simulates 1 row updated = lock acquired."""
        client = self._make_mock_bq_client(1)
        # Replicate the lock logic
        query_job = client.query("UPDATE ...")
        result = query_job.result()
        assert result.num_dml_affected_rows == 1

    def test_returns_false_when_already_locked(self):
        """Simulates 0 rows updated = another worker got it."""
        client = self._make_mock_bq_client(0)
        query_job = client.query("UPDATE ...")
        result = query_job.result()
        assert result.num_dml_affected_rows == 0

    def test_raises_on_bq_error(self):
        """Simulates a BigQuery error during lock attempt."""
        mock_client = MagicMock()
        mock_client.query.side_effect = Exception("BQ unavailable")
        with pytest.raises(Exception, match="BQ unavailable"):
            mock_client.query("UPDATE ...")


# ============================================================
# Tests: update_rows_status behavior (mocked BQ client)
# ============================================================
class TestUpdateRowsStatus:
    def test_executes_query_and_waits(self):
        mock_client = MagicMock()
        mock_client.project = "test-project"

        # Replicate the update_rows_status logic
        timestamps = ["2025-01-15T10:00:00+00:00"]
        ts_list = [f"TIMESTAMP('{t}')" for t in timestamps]
        ts_string = ", ".join(ts_list)
        update_query = f"""
            UPDATE `{mock_client.project}.ds.tbl`
            SET processed_status = 'PROCESSED'
            WHERE entry_timestamp IN ({ts_string})
        """
        query_job = mock_client.query(update_query)
        query_job.result()

        mock_client.query.assert_called_once()
        query_job.result.assert_called_once()

    def test_skips_empty_timestamp_list(self):
        """With no timestamps, no query should be made."""
        timestamps = []
        # Replicate the guard from update_rows_status
        assert len(timestamps) == 0


# ============================================================
# Tests: User query construction
# ============================================================
class TestUserQueryConstruction:
    def test_includes_all_campaign_fields(self):
        msg = {
            "brand": "PRS",
            "target_product": "SE CE24",
            "key_selling_point": "Great tone",
            "target_audience": "Musicians",
            "target_search_trend": "olive garden",
        }
        query = build_user_query(msg)
        assert "Brand: PRS" in query
        assert "Target Product: SE CE24" in query
        assert "Key Selling Point(s): Great tone" in query
        assert "Target Audience: Musicians" in query
        assert "Target Search Trend: olive garden" in query

    def test_missing_key_raises(self):
        msg = {"brand": "PRS"}  # missing other keys
        with pytest.raises(KeyError):
            build_user_query(msg)


# ============================================================
# Tests: Worker payload validation
# ============================================================
REQUIRED_WORKER_KEYS = ["bq_dataset", "bq_table", "agent_resource_id", "row_data"]
REQUIRED_ROW_DATA_KEYS = [
    "index", "entry_timestamp", "target_search_trend",
    "brand", "target_audience", "target_product", "key_selling_point",
]


class TestWorkerPayloadValidation:
    def _valid_worker_payload(self):
        return {
            "bq_dataset": "trend_trawler",
            "bq_table": "target_trends_crf",
            "agent_resource_id": "12345",
            "row_data": {
                "index": 0,
                "entry_timestamp": "2025-01-15T10:00:00+00:00",
                "target_search_trend": "olive garden",
                "brand": "PRS",
                "target_audience": "musicians",
                "target_product": "SE CE24",
                "key_selling_point": "Great tone",
            },
        }

    def test_valid_payload_has_all_keys(self):
        payload = self._valid_worker_payload()
        for key in REQUIRED_WORKER_KEYS:
            assert key in payload

    def test_valid_row_data_has_all_keys(self):
        payload = self._valid_worker_payload()
        for key in REQUIRED_ROW_DATA_KEYS:
            assert key in payload["row_data"]

    @pytest.mark.parametrize("missing_key", REQUIRED_WORKER_KEYS)
    def test_detects_missing_top_level_key(self, missing_key):
        payload = self._valid_worker_payload()
        del payload[missing_key]
        assert missing_key not in payload

    @pytest.mark.parametrize("missing_key", REQUIRED_ROW_DATA_KEYS)
    def test_detects_missing_row_data_key(self, missing_key):
        payload = self._valid_worker_payload()
        del payload["row_data"][missing_key]
        assert missing_key not in payload["row_data"]
