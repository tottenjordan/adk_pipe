"""CRF worker/orchestrator hardening: structured logs, malformed messages,
client reuse, bounded BigQuery waits and a per-invocation Agent Runtime client.

Imports only the CRF package (the crf-deps CI job runs this file in a venv with
nothing but ``cloud_functions/creative_fanout/requirements.txt`` + pytest).
"""

import asyncio
import base64
import io
import json
import logging
import sys
import types

import pytest

from cloud_functions.creative_fanout import (
    clients,
    logs,
    main,
    messages,
    status_store,
    worker,
)
from cloud_functions.creative_fanout.config import config
from tests.crf_fakes import FakeStatusStore, bq_results, queued

# The CRF config reads the (required) project at use time; pin a dummy.
pytestmark = pytest.mark.usefixtures("gcp_project_env")


# ============================================================
# Structured (JSON) logging for Cloud Run
# ============================================================
def _record(msg, level=logging.INFO, **extra):
    logger = logging.getLogger("crf.test")
    record = logger.makeRecord(
        logger.name, level, __file__, 1, msg, (), None, extra=extra or None
    )
    return record


def test_json_formatter_emits_severity_and_message():
    line = logs.CloudRunJsonFormatter().format(_record("hello %s", logging.WARNING))
    entry = json.loads(line)
    assert entry["severity"] == "WARNING"
    assert entry["message"] == "hello %s"
    assert "\n" not in line  # one JSON object per line


def test_json_formatter_formats_args_and_appends_traceback():
    logger = logging.getLogger("crf.test")
    try:
        raise ValueError("boom")
    except ValueError:
        record = logger.makeRecord(
            logger.name,
            logging.ERROR,
            __file__,
            1,
            "failed %s",
            ("row-1",),
            sys.exc_info(),
        )
    entry = json.loads(logs.CloudRunJsonFormatter().format(record))
    assert entry["severity"] == "ERROR"
    assert entry["message"].startswith("failed row-1\n")
    assert "ValueError: boom" in entry["message"]


def test_json_formatter_merges_json_fields_without_clobbering():
    record = _record(
        "AGENT_RUN_DURATION_SECS row=r",
        json_fields={"row": "r", "secs": 1.5, "severity": "DEBUG", "message": "x"},
    )
    entry = json.loads(logs.CloudRunJsonFormatter().format(record))
    assert entry["row"] == "r"
    assert entry["secs"] == 1.5
    assert entry["severity"] == "INFO"
    assert entry["message"] == "AGENT_RUN_DURATION_SECS row=r"


def test_log_handler_writes_json_lines_to_stdout(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    handler = logs._json_log_handler()
    logger = logging.getLogger("crf.test.handler")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.error("on stdout")
    finally:
        logger.removeHandler(handler)
    assert json.loads(buf.getvalue())["message"] == "on stdout"


def test_no_runtime_pythonunbuffered_assignment():
    """Setting PYTHONUNBUFFERED after start-up does nothing (the Dockerfile's
    ENV sets it for the interpreter); the import-time assignment is gone."""
    from pathlib import Path

    for path in Path(main.__file__).parent.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert 'os.environ["PYTHONUNBUFFERED"]' not in source, path.name


@pytest.mark.parametrize("fail", [False, True])
def test_duration_marker_carries_json_fields(monkeypatch, caplog, fail):
    from cloud_functions.creative_fanout.completion import RunOutcome

    async def _run(**kwargs):
        if fail:
            raise RuntimeError("agent boom")
        return RunOutcome(finalized=True, retried=False, reason="done")

    monkeypatch.setattr(worker, "create_agent_run", _run)
    trend = {"entry_timestamp": "2026-07-12T00:00:00", "index": 2}
    FakeStatusStore(monkeypatch, trend)

    import asyncio

    with caplog.at_level(logging.INFO):
        asyncio.run(worker.process_row(trend, "123", object(), "ds", "t", deadline=1e9))

    (record,) = [
        r for r in caplog.records if r.getMessage().startswith("AGENT_RUN_DURATION")
    ]
    status = "FAILED" if fail else "PROCESSED"
    # The text stays as before so existing log queries still match.
    assert f"index=2 status={status} secs=" in record.getMessage()
    fields = record.json_fields
    assert fields["row"] == "2026-07-12T00:00:00+00:00"  # the re-read row's
    assert fields["index"] == 2
    assert fields["status"] == status
    assert isinstance(fields["secs"], float)
    assert fields["finalized"] is (not fail)
    assert fields["retried"] is False


# ============================================================
# Bounded BigQuery waits
# ============================================================
def _bq_mock(num_rows=1):
    from unittest.mock import MagicMock

    bq = MagicMock()
    bq.project = "test-project"
    bq.query_and_wait.return_value.num_dml_affected_rows = num_rows
    bq.query.side_effect = AssertionError("use query_and_wait (bounded wait)")
    return bq


def test_bq_timeout_default_and_env(monkeypatch):
    from cloud_functions.creative_fanout.config import load_config

    monkeypatch.delenv("CRF_BQ_QUERY_TIMEOUT_SECS", raising=False)
    assert load_config().BQ_QUERY_TIMEOUT_SECS == 60.0
    monkeypatch.setenv("CRF_BQ_QUERY_TIMEOUT_SECS", "15")
    assert load_config().BQ_QUERY_TIMEOUT_SECS == 15.0


def test_run_query_bounds_the_wait_and_the_job(monkeypatch):
    monkeypatch.setenv("CRF_BQ_QUERY_TIMEOUT_SECS", "42")
    bq = _bq_mock()
    params = [clients.bigquery.ScalarQueryParameter("x", "INT64", 1)]

    result = status_store._run_query(bq, "SELECT @x", params)

    assert result is bq.query_and_wait.return_value
    call = bq.query_and_wait.call_args
    assert call.args == ("SELECT @x",)
    assert call.kwargs["wait_timeout"] == 42.0
    job_config = call.kwargs["job_config"]
    assert int(job_config.job_timeout_ms) == 42_000
    assert job_config.query_parameters == params


_KEY = status_store.RowKey("uuid-1", "trend", "2026-07-12")


@pytest.mark.parametrize(("affected", "locked"), [(1, 3), (0, None)])
def test_lock_reads_dml_count_from_bounded_query(affected, locked):
    bq = bq_results(queued(2), affected)
    got = status_store.acquire_processing_lock(
        bq, config.BQ_DATASET_ID, config.BQ_TABLE_TARGETS, _KEY
    )
    assert got == locked
    for call in bq.query_and_wait.call_args_list:
        assert "wait_timeout" in call.kwargs


def test_lock_timeout_propagates_for_retry():
    """A wait past the bound raises (NACK → redelivery), never a silent None."""
    import concurrent.futures

    bq = bq_results(queued(), concurrent.futures.TimeoutError())
    with pytest.raises(concurrent.futures.TimeoutError):
        status_store.acquire_processing_lock(
            bq, config.BQ_DATASET_ID, config.BQ_TABLE_TARGETS, _KEY
        )


def test_status_writes_and_reaper_use_bounded_query():
    bq = bq_results(1, [], 1, 1, 2)
    ds, tbl = config.BQ_DATASET_ID, config.BQ_TABLE_TARGETS
    status_store.mark_queued(bq, ds, tbl, [_KEY])
    status_store.read_locked_row(bq, ds, tbl, _KEY, 1)
    status_store.mark_final(bq, ds, tbl, _KEY, "PROCESSED", 1)
    status_store.requeue(bq, ds, tbl, _KEY, 1)
    assert status_store.reap_stale_processing_rows(bq, ds, tbl) == 2
    assert bq.query_and_wait.call_count == 5
    for call in bq.query_and_wait.call_args_list:
        assert "wait_timeout" in call.kwargs


# ============================================================
# BigQuery / Pub/Sub clients: lazy, thread-safe, reused singletons
# ============================================================
def _slow_ctor(calls):
    """A constructor that yields the GIL mid-construction (widens any race)."""
    import time

    def ctor(*args, **kwargs):
        calls.append(kwargs)
        time.sleep(0.05)
        return object()

    return ctor


@pytest.mark.parametrize(
    ("getter", "cache_attr", "module_attr", "ctor_name"),
    [
        ("get_bigquery_client", "_bigquery_client", "bigquery", "Client"),
        ("get_pubsub_client", "_pubsub_publisher", "pubsub_v1", "PublisherClient"),
    ],
)
def test_client_built_once_across_threads(
    monkeypatch, getter, cache_attr, module_attr, ctor_name
):
    from concurrent.futures import ThreadPoolExecutor

    calls = []
    monkeypatch.setattr(getattr(clients, module_attr), ctor_name, _slow_ctor(calls))
    monkeypatch.setattr(clients, cache_attr, None)

    with ThreadPoolExecutor(max_workers=16) as pool:
        built = list(pool.map(lambda _: getattr(clients, getter)(), range(32)))

    assert len(calls) == 1
    assert all(c is built[0] for c in built)
    assert getattr(clients, getter)() is built[0]  # reused by later invocations


def test_bigquery_client_uses_configured_project(monkeypatch):
    calls = []
    monkeypatch.setattr(clients.bigquery, "Client", _slow_ctor(calls))
    monkeypatch.setattr(clients, "_bigquery_client", None)
    clients.get_bigquery_client()
    assert calls == [{"project": "test-project"}]


def test_import_builds_no_clients():
    """Clients are created on first use, so importing stays credential-free
    (checked in a fresh interpreter with no project configured)."""
    import os
    import subprocess

    code = (
        "from cloud_functions.creative_fanout import clients, main\n"
        "assert clients._bigquery_client is None\n"
        "assert clients._pubsub_publisher is None\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "GOOGLE_CLOUD_PROJECT"}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run([sys.executable, "-c", code], check=True, cwd=root, env=env)


# ============================================================
# Agent Runtime client: one per invocation (event-loop bound)
# ============================================================
class _LoopBoundFakeClient:
    """Stands in for ``agentplatform.Client`` without aiohttp: google-genai's
    async path then reuses ONE ``httpx.AsyncClient``, bound to the event loop
    of its first request, so a later ``asyncio.run`` (a new loop) on the same
    client fails with ``RuntimeError: Event loop is closed``."""

    instances: list["_LoopBoundFakeClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.loop = None
        self.closed = False
        self.runtimes = _FakeRuntimes(self)
        self.aio = _FakeAio(self)
        type(self).instances.append(self)

    def check_loop(self):
        loop = asyncio.get_running_loop()
        if self.closed:
            raise RuntimeError("client used after aclose()")
        if self.loop is None:
            self.loop = loop
        elif self.loop is not loop:
            raise RuntimeError("Event loop is closed")


class _FakeAio:
    def __init__(self, client):
        self.client = client

    async def aclose(self):
        self.client.check_loop()
        self.client.closed = True


class _FakeRuntimes:
    def __init__(self, client):
        self.client = client

    def get(self, *, name):
        return _FakeAgent(self.client)


class _FakeAgent:
    def __init__(self, client):
        self.client = client

    async def async_create_session(self, *, user_id, state=None):
        self.client.check_loop()
        return {"id": "sess-1"}

    async def async_delete_session(self, *, user_id, session_id):
        self.client.check_loop()

    async def async_stream_query(self, **kwargs):
        self.client.check_loop()
        yield {"author": "root_agent", "content": {"parts": [{"text": "ok"}]}}

    async def async_get_session(self, *, user_id, session_id, **kwargs):
        self.client.check_loop()
        return {"id": session_id, "state": {"finalize_done": True}, "events": []}


_ROW_DATA = {
    "entry_timestamp": "2026-07-12T00:00:00",
    "brand": "BrandX",
    "target_product": "prod",
    "key_selling_point": "ksp",
    "target_audience": "aud",
    "target_search_trend": "trend",
}


def _worker_payload(index=0, **overrides):
    payload = {
        "bq_dataset": config.BQ_DATASET_ID,
        "bq_table": config.BQ_TABLE_TARGETS,
        "row_data": {"index": index, **_ROW_DATA},
    }
    payload.update(overrides)
    return payload


def _event(payload):
    """A Pub/Sub CloudEvent stand-in (dict/list → JSON; str sent as-is)."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    data = base64.b64encode(raw.encode()).decode()
    return types.SimpleNamespace(data={"message": {"data": data}})


def test_two_worker_invocations_in_one_process_both_succeed(monkeypatch):
    """Regression: a warm instance's 2nd invocation (a new ``asyncio.run`` loop)
    must not reuse the 1st invocation's loop-bound Agent Runtime client."""
    _LoopBoundFakeClient.instances = []
    monkeypatch.setattr(clients.agentplatform, "Client", _LoopBoundFakeClient)
    monkeypatch.setattr(clients, "get_bigquery_client", lambda: _bq_mock())
    store = FakeStatusStore(monkeypatch, _worker_payload()["row_data"])

    main.agent_worker_entrypoint(_event(_worker_payload(0)))
    main.agent_worker_entrypoint(_event(_worker_payload(1)))

    assert store.statuses == ["PROCESSED", "PROCESSED"]
    assert len(_LoopBoundFakeClient.instances) == 2
    assert all(c.closed for c in _LoopBoundFakeClient.instances)
    assert _LoopBoundFakeClient.instances[0].kwargs == {
        "project": "test-project",
        "location": config.GCP_REGION,
    }


def test_agent_runtime_client_closed_even_when_the_run_fails(monkeypatch):
    _LoopBoundFakeClient.instances = []
    monkeypatch.setattr(clients.agentplatform, "Client", _LoopBoundFakeClient)

    async def _boom(self, **kwargs):
        raise RuntimeError("create boom")

    monkeypatch.setattr(_FakeAgent, "async_create_session", _boom)

    with pytest.raises(RuntimeError, match="create boom"):
        asyncio.run(
            worker.create_agent_run(
                agent_id="123",
                msg_dict={"index": 0, **_ROW_DATA},
                user_id="u",
                deadline=1e9,
            )
        )
    (client,) = _LoopBoundFakeClient.instances
    assert client.closed


def test_agent_runtime_client_close_failure_does_not_fail_the_run(monkeypatch, caplog):
    _LoopBoundFakeClient.instances = []
    monkeypatch.setattr(clients.agentplatform, "Client", _LoopBoundFakeClient)

    async def _bad_close(self):
        raise OSError("close boom")

    monkeypatch.setattr(_FakeAio, "aclose", _bad_close)
    with caplog.at_level(logging.WARNING):
        asyncio.run(
            worker.create_agent_run(
                agent_id="123",
                msg_dict={"index": 0, **_ROW_DATA},
                user_id="u",
                deadline=1e9,
            )
        )
    assert "close boom" in caplog.text


# ============================================================
# Malformed messages are permanent: log an error and ACK (return)
# ============================================================
@pytest.fixture
def no_side_effects(monkeypatch):
    """BigQuery / Pub/Sub / Agent Runtime doubles that must stay untouched."""
    from unittest.mock import MagicMock

    bq = _bq_mock()
    publisher = MagicMock()
    vertex = MagicMock(side_effect=AssertionError("no agent run expected"))
    monkeypatch.setattr(clients, "get_bigquery_client", lambda: bq)
    monkeypatch.setattr(clients, "get_pubsub_client", lambda: publisher)
    monkeypatch.setattr(clients.agentplatform, "Client", vertex)
    yield
    bq.query_and_wait.assert_not_called()
    publisher.publish.assert_not_called()


def _drop(payload, key):
    return {k: v for k, v in payload.items() if k != key}


def _row_without(key):
    row = {"index": 0, **_ROW_DATA}
    del row[key]
    return _worker_payload(row_data=row)


_BAD_WORKER_MESSAGES = {
    "no row_data": _drop(_worker_payload(), "row_data"),
    "no bq_table": _drop(_worker_payload(), "bq_table"),
    "no bq_dataset": _drop(_worker_payload(), "bq_dataset"),
    "row_data not an object": _worker_payload(row_data="row"),
    "row_data list": _worker_payload(row_data=[1, 2]),
    "no entry_timestamp": _row_without("entry_timestamp"),
    "no index": _row_without("index"),
    "no brand": _row_without("brand"),
    "no target_search_trend": _row_without("target_search_trend"),
    "index not an int": _worker_payload(row_data={"index": "0", **_ROW_DATA}),
    "index bool": _worker_payload(row_data={"index": True, **_ROW_DATA}),
    "dataset not a string": _worker_payload(bq_dataset=["trend_trawler"]),
    "payload is a list": [1, 2, 3],
    "payload is a string": "just text with bq_dataset in it",
}


@pytest.mark.usefixtures("no_side_effects")
@pytest.mark.parametrize(
    "payload", list(_BAD_WORKER_MESSAGES.values()), ids=list(_BAD_WORKER_MESSAGES)
)
def test_worker_acks_malformed_message(payload, caplog):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    with caplog.at_level(logging.ERROR):
        main.agent_worker_entrypoint(_event(raw))  # must NOT raise (ACK)
    assert any(
        r.levelno == logging.ERROR and "Rejecting worker message" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.usefixtures("no_side_effects")
@pytest.mark.parametrize(
    "data",
    [
        {},
        {"message": {}},
        {"message": {"data": "%%% not base64 %%%"}},
        {"message": {"data": base64.b64encode(b"\xff\xfe").decode()}},
        {"message": {"data": base64.b64encode(b"{not json").decode()}},
        None,
    ],
    ids=["no message", "no data", "bad base64", "not utf-8", "not json", "null"],
)
def test_worker_acks_undecodable_message(data, caplog):
    with caplog.at_level(logging.ERROR):
        main.agent_worker_entrypoint(types.SimpleNamespace(data=data))
    assert "Rejecting worker message" in caplog.text


def _trigger(**overrides):
    payload = {
        "bq_dataset": config.BQ_DATASET_ID,
        "bq_table": config.BQ_TABLE_TARGETS,
    }
    payload.update(overrides)
    return payload


_BAD_TRIGGER_MESSAGES = {
    "no bq_table": _drop(_trigger(), "bq_table"),
    "table not a string": _trigger(bq_table={"t": 1}),
    "payload is a string": "xbq_datasetx",
}


@pytest.mark.usefixtures("no_side_effects")
@pytest.mark.parametrize(
    "payload", list(_BAD_TRIGGER_MESSAGES.values()), ids=list(_BAD_TRIGGER_MESSAGES)
)
def test_orchestrator_acks_malformed_message(payload, caplog):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    with caplog.at_level(logging.ERROR):
        main.crf_entrypoint(_event(raw))  # must NOT raise (ACK)
    assert any(
        r.levelno == logging.ERROR and "Rejecting trigger message" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.usefixtures("no_side_effects")
def test_orchestrator_acks_bad_base64(caplog):
    with caplog.at_level(logging.ERROR):
        main.crf_entrypoint(types.SimpleNamespace(data={"message": {"data": "%%%"}}))
    assert "Rejecting trigger message" in caplog.text


def test_valid_worker_message_still_runs(monkeypatch):
    """The stricter parsing accepts what the orchestrator actually sends."""
    _LoopBoundFakeClient.instances = []
    monkeypatch.setattr(clients.agentplatform, "Client", _LoopBoundFakeClient)
    monkeypatch.setattr(clients, "get_bigquery_client", lambda: _bq_mock())
    row = messages.build_row_dict(
        3,
        {
            "uuid": "uuid-3",
            "entry_timestamp": __import__("datetime").datetime(2026, 7, 12),
            "target_trend": "t",
            "brand": None,
            "target_audience": "a",
            "target_product": "p",
            "key_selling_point": "k",
        },
    )
    store = FakeStatusStore(monkeypatch, row)
    main.agent_worker_entrypoint(_event(_worker_payload(row_data=row)))
    assert store.statuses == ["PROCESSED"]
    (_, key) = store.calls[0]
    assert key == status_store.RowKey("uuid-3", "t", row["entry_timestamp"])


# ============================================================
# The engine id is no longer part of the messages (worker env instead)
# ============================================================
def _parse_trigger(payload):
    return messages.parse_trigger_message(_event(payload))


def test_trigger_without_agent_resource_id_is_valid(caplog):
    with caplog.at_level(logging.WARNING):
        payload = _parse_trigger(_trigger())
    assert payload == _trigger()
    assert "agent_resource_id" not in caplog.text


@pytest.mark.parametrize("legacy", ["a/b", None, 123, "999"])
def test_trigger_legacy_engine_id_is_ignored_with_a_warning(
    monkeypatch, caplog, legacy
):
    monkeypatch.setattr(config, "CREATIVE_AGENT_ENGINE_ID", "123")
    with caplog.at_level(logging.WARNING):
        payload = _parse_trigger(_trigger(agent_resource_id=legacy))
    assert payload is not None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("agent_resource_id" in r.getMessage() for r in warnings)


def test_trigger_legacy_engine_id_equal_to_the_env_is_silent(monkeypatch, caplog):
    monkeypatch.setattr(config, "CREATIVE_AGENT_ENGINE_ID", "123")
    with caplog.at_level(logging.WARNING):
        _parse_trigger(_trigger(agent_resource_id="123"))
    assert "agent_resource_id" not in caplog.text


def test_parse_worker_message_returns_dataset_table_row():
    dataset, table, row_data = messages.parse_worker_message(_event(_worker_payload(3)))
    assert (dataset, table) == (config.BQ_DATASET_ID, config.BQ_TABLE_TARGETS)
    assert row_data == {"index": 3, **_ROW_DATA}


@pytest.mark.parametrize("legacy", ["123", "1/../../x", 123])
def test_parse_worker_message_ignores_a_legacy_engine_id(legacy):
    """Messages already in flight across the deploy still carry the id."""
    parsed = messages.parse_worker_message(
        _event(_worker_payload(agent_resource_id=legacy))
    )
    assert len(parsed) == 3
    assert parsed[2] == {"index": 0, **_ROW_DATA}
