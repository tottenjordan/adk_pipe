"""The worker's Cloud Tasks HTTP entry point (``main.agent_worker_task``).

Cloud Tasks retries a task on any non-2xx response, so the status code IS the
ACK/NACK: a permanent problem (malformed body, unset engine) and every
recorded outcome (lost lock, PROCESSED, FAILED, re-queued) answer 200; an error
before the status write answers 503. The run deadline is taken at request
start.
"""

import json
import logging
import sys
from unittest.mock import AsyncMock, MagicMock

import flask
import functions_framework
import pytest
from functions_framework import _function_registry

from cloud_functions.creative_fanout import clients, main, worker
from cloud_functions.creative_fanout.config import config
from tests.crf_fakes import FakeStatusStore, fake_vertex_client

pytestmark = pytest.mark.usefixtures("gcp_project_env", "crf_engine_env")


@pytest.fixture(autouse=True)
def _fake_agent_runtime(monkeypatch):
    """process_row resolves the engine before the lock (C1): a stub client."""
    monkeypatch.setattr(clients.agentplatform, "Client", fake_vertex_client)


_ROW = {
    "entry_timestamp": "2026-10-13T00:00:00",
    "index": 0,
    "uuid": "u-1",
    "target_trend": "trend",
    "brand": "BrandX",
    "target_product": "prod",
    "key_selling_point": "ksp",
    "target_audience": "aud",
    "target_search_trend": "trend",
}
_TASK_HEADERS = {
    "X-CloudTasks-TaskName": "crf-0123abcd",
    "X-CloudTasks-TaskRetryCount": "0",
    "X-CloudTasks-QueueName": "creative-worker-queue",
}
_app = flask.Flask(__name__)


def _payload(**overrides):
    payload = {
        "bq_dataset": config.BQ_DATASET_ID,
        "bq_table": config.BQ_TABLE_TARGETS,
        "row_data": dict(_ROW),
    }
    payload.update(overrides)
    return payload


def _call(body, headers=None):
    """POST ``body`` (dict -> JSON; str/bytes as-is) to the entry point and
    return ``(text, status)``."""
    data = json.dumps(body) if isinstance(body, dict | list) else body
    with _app.test_request_context(
        "/",
        method="POST",
        data=data,
        headers={"Content-Type": "application/json", **(headers or _TASK_HEADERS)},
    ):
        response = _app.make_response(main.agent_worker_task(flask.request))
    return response.get_data(as_text=True), response.status_code


@pytest.fixture
def run_row(monkeypatch):
    """process_row stubbed; returns the kwargs it was called with."""
    monkeypatch.setattr(clients, "get_bigquery_client", MagicMock)
    seen: dict = {}

    async def _process_row(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(worker, "process_row", _process_row)
    return seen


def test_registered_as_an_http_function():
    assert _function_registry.REGISTRY_MAP["agent_worker_task"] == "http"
    # Cloud Tasks is the only transport: the Pub/Sub worker entry is gone.
    assert "agent_worker_entrypoint" not in _function_registry.REGISTRY_MAP
    assert not hasattr(main, "agent_worker_entrypoint")


def test_valid_task_runs_the_row_and_answers_200(run_row):
    _, status = _call(_payload())
    assert status == 200
    assert run_row["agent_id"] == config.CREATIVE_AGENT_ENGINE_ID == "123"
    assert run_row["trend_dict"]["uuid"] == "u-1"
    assert (run_row["dataset"], run_row["table"]) == (
        config.BQ_DATASET_ID,
        config.BQ_TABLE_TARGETS,
    )


@pytest.mark.parametrize(
    "body",
    [
        "this is not json",
        b"\xff\xfe",
        "[1, 2]",
        "",
        {"bq_dataset": "trend_trawler"},  # missing fields
        {"bq_dataset": "evil", "bq_table": "x", "row_data": _ROW},  # not allowed
        {
            "bq_dataset": "trend_trawler",
            "bq_table": "target_trends_crf",
            "row_data": {**_ROW, "entry_timestamp": "not-a-timestamp"},
        },
    ],
)
def test_malformed_body_answers_200_without_running(monkeypatch, caplog, body):
    """A body that can never be processed must not be retried by Cloud Tasks."""
    process_row = AsyncMock()
    monkeypatch.setattr(worker, "process_row", process_row)
    with caplog.at_level(logging.ERROR):
        _, status = _call(body)
    assert status == 200
    process_row.assert_not_called()
    assert "Rejecting worker task" in caplog.text


def test_unset_engine_answers_200_before_the_lock(monkeypatch, caplog):
    monkeypatch.delenv("CREATIVE_AGENT_ENGINE_ID", raising=False)
    bq = MagicMock()
    monkeypatch.setattr(clients, "get_bigquery_client", lambda: bq)
    process_row = AsyncMock()
    monkeypatch.setattr(worker, "process_row", process_row)
    with caplog.at_level(logging.ERROR):
        _, status = _call(_payload())
    assert status == 200
    process_row.assert_not_called()
    bq.query_and_wait.assert_not_called()
    assert "CREATIVE_AGENT_ENGINE_ID" in caplog.text


@pytest.mark.parametrize(
    "store_kwargs, expected",
    [
        ({"attempt": None}, []),  # lost lock: nothing written
        ({"row": None}, ["FAILED"]),  # row gone after the lock
        ({"fence": False, "row": None}, ["FAILED"]),  # lost fence: logged only
    ],
)
def test_recorded_outcomes_answer_200(monkeypatch, store_kwargs, expected):
    monkeypatch.setattr(clients, "get_bigquery_client", MagicMock)
    store = FakeStatusStore(monkeypatch, _ROW, **store_kwargs)
    _, status = _call(_payload())
    assert status == 200
    assert store.statuses == expected


def test_requeued_answers_200(monkeypatch):
    """A transient error before a session existed puts the row back to QUEUED
    (the next trigger re-dispatches it): the task itself is done."""
    monkeypatch.setattr(clients, "get_bigquery_client", MagicMock)
    store = FakeStatusStore(monkeypatch, _ROW)

    async def _no_session(**kwargs):
        raise worker.SessionNotStarted("no session") from TimeoutError()

    monkeypatch.setattr(worker, "create_agent_run", _no_session)
    _, status = _call(_payload())
    assert status == 200
    assert [c[0] for c in store.calls] == ["lock", "read", "requeue"]


def test_failed_run_answers_200(monkeypatch):
    monkeypatch.setattr(clients, "get_bigquery_client", MagicMock)
    store = FakeStatusStore(monkeypatch, _ROW)
    monkeypatch.setattr(
        worker, "create_agent_run", AsyncMock(side_effect=ValueError("agent blew up"))
    )
    _, status = _call(_payload())
    assert status == 200
    assert store.statuses == ["FAILED"]


def test_processed_answers_200(monkeypatch):
    monkeypatch.setattr(clients, "get_bigquery_client", MagicMock)

    async def _process_row(**kwargs):
        return None  # process_row records PROCESSED and returns (ACK)

    monkeypatch.setattr(worker, "process_row", _process_row)
    assert _call(_payload())[1] == 200


def test_error_before_the_status_write_answers_503(monkeypatch, caplog):
    """A BigQuery error before the lock (process_row raises): Cloud Tasks
    must retry the task under its policy."""
    monkeypatch.setattr(clients, "get_bigquery_client", MagicMock)
    monkeypatch.setattr(
        worker, "process_row", AsyncMock(side_effect=RuntimeError("bq 500"))
    )
    with caplog.at_level(logging.ERROR):
        _, status = _call(_payload())
    assert status == 503
    assert "bq 500" in caplog.text


def test_client_construction_error_answers_503(monkeypatch):
    def _boom():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(clients, "get_bigquery_client", _boom)
    monkeypatch.setattr(worker, "process_row", AsyncMock())
    assert _call(_payload())[1] == 503


def test_deadline_is_taken_at_request_start(monkeypatch):
    clock = {"now": 5000.0}
    monkeypatch.setattr(worker, "_clock", lambda: clock["now"])

    def _slow_client():
        clock["now"] += 120.0  # time spent before the run starts
        return MagicMock()

    monkeypatch.setattr(clients, "get_bigquery_client", _slow_client)
    seen = {}

    async def _process_row(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(worker, "process_row", _process_row)
    _call(_payload())
    assert seen["deadline"] == worker.run_deadline(5000.0)


def test_task_headers_logged_as_json_fields(run_row, caplog):
    headers = {**_TASK_HEADERS, "X-CloudTasks-TaskRetryCount": "3"}
    with caplog.at_level(logging.INFO):
        _call(_payload(), headers=headers)
    fields = [
        r.json_fields
        for r in caplog.records
        if isinstance(getattr(r, "json_fields", None), dict)
        and "cloud_tasks_task_name" in r.json_fields
    ]
    assert fields, "no log line carries the task name"
    assert fields[0]["cloud_tasks_task_name"] == "crf-0123abcd"
    assert fields[0]["cloud_tasks_retry_count"] == 3


def test_missing_task_headers_still_run(run_row):
    _, status = _call(_payload(), headers={"X-Other": "1"})
    assert status == 200
    assert run_row["trend_dict"]["uuid"] == "u-1"


def test_image_load_path_serves_the_http_target(monkeypatch):
    """The image's ``functions-framework --source=main.py --target=
    agent_worker_task --signature-type=http`` load: a real POST through the
    framework's Flask app maps a malformed body to 200."""
    before = set(sys.modules)
    try:
        app = functions_framework.create_app(
            target="agent_worker_task",
            source=str(main.__file__),
            signature_type="http",
        )
        response = app.test_client().post(
            "/", data="not json", headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 200
    finally:
        for name in set(sys.modules) - before:
            del sys.modules[name]
