"""CRF worker/orchestrator hardening: structured logs, malformed messages,
client reuse, bounded BigQuery waits and a per-invocation Agent Runtime client.

Imports only the CRF package (the crf-deps CI job runs this file in a venv with
nothing but ``cloud_functions/creative_fanout/requirements.txt`` + pytest).
"""

import io
import json
import logging
import sys

import pytest

from cloud_functions.creative_fanout import main

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
    line = main.CloudRunJsonFormatter().format(_record("hello %s", logging.WARNING))
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
    entry = json.loads(main.CloudRunJsonFormatter().format(record))
    assert entry["severity"] == "ERROR"
    assert entry["message"].startswith("failed row-1\n")
    assert "ValueError: boom" in entry["message"]


def test_json_formatter_merges_json_fields_without_clobbering():
    record = _record(
        "AGENT_RUN_DURATION_SECS row=r",
        json_fields={"row": "r", "secs": 1.5, "severity": "DEBUG", "message": "x"},
    )
    entry = json.loads(main.CloudRunJsonFormatter().format(record))
    assert entry["row"] == "r"
    assert entry["secs"] == 1.5
    assert entry["severity"] == "INFO"
    assert entry["message"] == "AGENT_RUN_DURATION_SECS row=r"


def test_log_handler_writes_json_lines_to_stdout(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    handler = main._json_log_handler()
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
    source = open(main.__file__, encoding="utf-8").read()
    assert 'os.environ["PYTHONUNBUFFERED"]' not in source


@pytest.mark.parametrize("fail", [False, True])
def test_duration_marker_carries_json_fields(monkeypatch, caplog, fail):
    async def _run(**kwargs):
        if fail:
            raise RuntimeError("agent boom")

    monkeypatch.setattr(main, "create_agent_run", _run)
    monkeypatch.setattr(main, "acquire_processing_lock", lambda *a, **k: True)
    monkeypatch.setattr(main, "update_rows_status", lambda **k: None)
    trend = {"entry_timestamp": "2026-07-12T00:00:00", "index": 2}

    import asyncio

    with caplog.at_level(logging.INFO):
        asyncio.run(
            main._execute_agent_and_update_status(trend, "123", object(), "ds", "t")
        )

    (record,) = [
        r for r in caplog.records if r.getMessage().startswith("AGENT_RUN_DURATION")
    ]
    status = "FAILED" if fail else "PROCESSED"
    # The text stays as before so existing log queries still match.
    assert f"index=2 status={status} secs=" in record.getMessage()
    fields = record.json_fields
    assert fields["row"] == "2026-07-12T00:00:00"
    assert fields["index"] == 2
    assert fields["status"] == status
    assert isinstance(fields["secs"], float)


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
    from cloud_functions.creative_fanout.config import AppConfig

    monkeypatch.delenv("CRF_BQ_QUERY_TIMEOUT_SECS", raising=False)
    assert AppConfig().BQ_QUERY_TIMEOUT_SECS == 60.0
    monkeypatch.setenv("CRF_BQ_QUERY_TIMEOUT_SECS", "15")
    assert AppConfig().BQ_QUERY_TIMEOUT_SECS == 15.0


def test_run_query_bounds_the_wait_and_the_job(monkeypatch):
    monkeypatch.setenv("CRF_BQ_QUERY_TIMEOUT_SECS", "42")
    bq = _bq_mock()
    params = [main.bigquery.ScalarQueryParameter("x", "INT64", 1)]

    result = main._run_query(bq, "SELECT @x", params)

    assert result is bq.query_and_wait.return_value
    call = bq.query_and_wait.call_args
    assert call.args == ("SELECT @x",)
    assert call.kwargs["wait_timeout"] == 42.0
    job_config = call.kwargs["job_config"]
    assert int(job_config.job_timeout_ms) == 42_000
    assert job_config.query_parameters == params


@pytest.mark.parametrize(("affected", "locked"), [(1, True), (0, False)])
def test_lock_reads_dml_count_from_bounded_query(affected, locked):
    bq = _bq_mock(affected)
    got = main.acquire_processing_lock(
        bq, main.config.BQ_DATASET_ID, main.config.BQ_TABLE_TARGETS, "2026-07-12"
    )
    assert got is locked
    assert "wait_timeout" in bq.query_and_wait.call_args.kwargs


def test_lock_timeout_propagates_for_retry():
    """A wait past the bound raises (NACK → redelivery), never a silent False."""
    import concurrent.futures

    bq = _bq_mock()
    bq.query_and_wait.side_effect = concurrent.futures.TimeoutError()
    with pytest.raises(concurrent.futures.TimeoutError):
        main.acquire_processing_lock(
            bq, main.config.BQ_DATASET_ID, main.config.BQ_TABLE_TARGETS, "2026-07-12"
        )


def test_status_update_and_reaper_use_bounded_query():
    bq = _bq_mock(2)
    ds, tbl = main.config.BQ_DATASET_ID, main.config.BQ_TABLE_TARGETS
    main.update_rows_status(bq, ds, tbl, ["2026-07-12"], status="PROCESSED")
    assert main.reap_stale_processing_rows(bq, ds, tbl) == 2
    assert bq.query_and_wait.call_count == 2


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
        ("_get_bigquery_client", "_bigquery_client", "bigquery", "Client"),
        ("_get_pubsub_client", "_pubsub_publisher", "pubsub_v1", "PublisherClient"),
    ],
)
def test_client_built_once_across_threads(
    monkeypatch, getter, cache_attr, module_attr, ctor_name
):
    from concurrent.futures import ThreadPoolExecutor

    calls = []
    monkeypatch.setattr(getattr(main, module_attr), ctor_name, _slow_ctor(calls))
    monkeypatch.setattr(main, cache_attr, None)

    with ThreadPoolExecutor(max_workers=16) as pool:
        clients = list(pool.map(lambda _: getattr(main, getter)(), range(32)))

    assert len(calls) == 1
    assert all(c is clients[0] for c in clients)
    assert getattr(main, getter)() is clients[0]  # reused by later invocations


def test_bigquery_client_uses_configured_project(monkeypatch):
    calls = []
    monkeypatch.setattr(main.bigquery, "Client", _slow_ctor(calls))
    monkeypatch.setattr(main, "_bigquery_client", None)
    main._get_bigquery_client()
    assert calls == [{"project": "test-project"}]


def test_import_builds_no_clients():
    """Clients are created on first use, so importing stays credential-free
    (checked in a fresh interpreter with no project configured)."""
    import os
    import subprocess

    code = (
        "from cloud_functions.creative_fanout import main\n"
        "assert main._bigquery_client is None\n"
        "assert main._pubsub_publisher is None\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "GOOGLE_CLOUD_PROJECT"}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run([sys.executable, "-c", code], check=True, cwd=root, env=env)
