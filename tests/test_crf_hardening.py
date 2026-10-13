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
