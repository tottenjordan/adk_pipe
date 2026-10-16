"""Cloud Tasks create behaviour and request-scoped logging for the CRF functions.

- **Tombstoned names (C5):** ``AlreadyExists`` is confirmed with ``get_task``.
  A task that still exists counts as dispatched (dedup intact); one that is
  gone (``NotFound``: it ran or was deleted, but Cloud Tasks keeps its name for
  up to 24 h) is logged as a WARNING and created again without a name.
- **Transient create errors (C9):** ``create_task`` gets a ``Retry`` on
  ``ServiceUnavailable`` / ``DeadlineExceeded`` (safe: the task is named).
- **Trace correlation (C14):** each entry point binds the request's trace
  header, and every JSON log line carries ``logging.googleapis.com/trace``
  (+ ``spanId`` / ``trace_sampled``), also from inside ``asyncio.run``.
- **Cleanups (C16):** module loggers, ``worker.request_deadline()``.
"""

import asyncio
import importlib.util
import io
import json
import logging
import types

import flask
import pytest
from google.api_core import exceptions as gexc
from google.api_core import retry as api_retry

from cloud_functions.creative_fanout import (
    clients,
    logs,
    main,
    messages,
    orchestrator,
    worker,
)
from cloud_functions.creative_fanout.config import config
from tests.crf_fakes import bq_results

pytestmark = pytest.mark.usefixtures("gcp_project_env", "crf_engine_env")

DS = config.BQ_DATASET_ID
TBL = config.BQ_TABLE_TARGETS
URL = "https://creative-worker-crf-abc-uc.a.run.app"
INVOKER = "invoker@test-project.iam.gserviceaccount.com"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_HEX = "00f067aa0ba902b7"
TRACE_FIELD = "logging.googleapis.com/trace"
SPAN_FIELD = "logging.googleapis.com/spanId"
SAMPLED_FIELD = "logging.googleapis.com/trace_sampled"
_app = flask.Flask(__name__)


@pytest.fixture(autouse=True)
def _clear_trace():
    """Each test starts and ends with no trace bound in this context."""
    logs.bind_trace({})
    yield
    logs.bind_trace({})


@pytest.fixture
def tasks_env(monkeypatch):
    monkeypatch.setenv("CRF_WORKER_TRANSPORT", "tasks")
    monkeypatch.setenv("CRF_WORKER_URL", URL)
    monkeypatch.setenv("CRF_TASKS_INVOKER_SA", INVOKER)
    for name in ("CRF_TASKS_QUEUE", "CRF_TASKS_LOCATION", "GCP_REGION"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class _Tasks:
    """A Cloud Tasks client stand-in recording ``create_task`` / ``get_task``.

    ``create_errors`` is a list of exceptions (or None for success) consumed
    one per ``create_task`` call; ``get_error`` is what ``get_task`` raises
    (None: the task exists).
    """

    def __init__(self, create_errors=None, get_error=None):
        self.create_errors = list(create_errors or [])
        self.get_error = get_error
        self.creates: list[tuple[dict, dict]] = []
        self.gets: list[tuple[str, dict]] = []

    def create_task(self, request=None, **kwargs):
        from google.cloud import tasks_v2

        tasks_v2.CreateTaskRequest(request)  # raises on a malformed request
        self.creates.append((request, kwargs))
        err = self.create_errors.pop(0) if self.create_errors else None
        if err is not None:
            raise err
        return request["task"]

    def get_task(self, request=None, *, name=None, **kwargs):
        self.gets.append((name or request["name"], kwargs))
        if self.get_error is not None:
            raise self.get_error
        return {"name": name}


def _row(trend="trend A", uuid="u-a"):
    from datetime import UTC, datetime

    return {
        "uuid": uuid,
        "entry_timestamp": datetime(2026, 10, 13, tzinfo=UTC),
        "target_trend": trend,
        "brand": "B",
        "target_audience": "a",
        "target_product": "p",
        "key_selling_point": "k",
        "processing_attempts": None,
    }


def _send(monkeypatch, tasks, rows=None):
    rows = rows or [_row()]
    monkeypatch.setattr(clients, "get_tasks_client", lambda: tasks)
    row_list = [messages.build_row_dict(i, r) for i, r in enumerate(rows)]
    return orchestrator._send_tasks(DS, TBL, rows, row_list)


# --- C9: create_task retries transient errors --------------------------------


def test_create_task_retries_only_transient_errors(tasks_env):
    tasks = _Tasks()
    assert _send(tasks_env, tasks) == (1, 0)
    (_, kwargs) = tasks.creates[0]
    retry = kwargs["retry"]
    assert isinstance(retry, api_retry.Retry)
    assert kwargs["timeout"] == orchestrator.CREATE_TASK_TIMEOUT_SECS
    assert (retry._initial, retry._maximum, retry.timeout) == (1, 10, 30)
    assert retry._predicate(gexc.ServiceUnavailable("x"))
    assert retry._predicate(gexc.DeadlineExceeded("x"))
    for permanent in (
        gexc.AlreadyExists("x"),
        gexc.NotFound("x"),
        gexc.InvalidArgument("x"),
        gexc.PermissionDenied("x"),
    ):
        assert not retry._predicate(permanent)


def test_create_task_retry_recovers_from_one_service_unavailable(
    tasks_env, monkeypatch
):
    """The recorded Retry, applied the way the client applies it, re-runs a
    call whose first attempt raised ServiceUnavailable."""
    tasks = _Tasks()
    _send(tasks_env, tasks)
    retry = tasks.creates[0][1]["retry"]
    monkeypatch.setattr(api_retry.retry_unary.time, "sleep", lambda s: None)
    calls = []

    def _flaky():
        calls.append(1)
        if len(calls) == 1:
            raise gexc.ServiceUnavailable("tasks briefly down")
        return "created"

    assert retry(_flaky)() == "created"
    assert len(calls) == 2


def test_create_error_after_retries_is_counted_and_logged(tasks_env, caplog):
    tasks = _Tasks(create_errors=[gexc.ServiceUnavailable("still down")])
    with caplog.at_level(logging.ERROR):
        assert _send(tasks_env, tasks) == (0, 1)
    assert tasks.gets == []
    assert "Failed to create worker task" in caplog.text


# --- C5: AlreadyExists is confirmed with get_task ----------------------------


def test_already_exists_with_a_live_task_counts_as_dispatched(tasks_env, caplog):
    tasks = _Tasks(create_errors=[gexc.AlreadyExists("dup")])
    with caplog.at_level(logging.WARNING):
        assert _send(tasks_env, tasks) == (1, 0)
    name = tasks.creates[0][0]["task"]["name"]
    assert [g[0] for g in tasks.gets] == [name]
    assert len(tasks.creates) == 1  # dedup intact: no second task
    assert caplog.records == []


def test_tombstoned_name_creates_the_task_unnamed(tasks_env, caplog):
    tasks = _Tasks(
        create_errors=[gexc.AlreadyExists("dup")], get_error=gexc.NotFound("gone")
    )
    with caplog.at_level(logging.WARNING):
        assert _send(tasks_env, tasks) == (1, 0)
    assert len(tasks.creates) == 2
    (named, named_kwargs), (unnamed, unnamed_kwargs) = tasks.creates
    # Every other field identical; only the name is dropped.
    expected = {k: v for k, v in named["task"].items() if k != "name"}
    assert unnamed["task"] == expected
    assert unnamed["parent"] == named["parent"]
    assert unnamed_kwargs.keys() == named_kwargs.keys()
    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert record.json_fields["reason"] == "task_name_tombstoned"
    assert record.json_fields["task_name"] == named["task"]["name"]
    assert record.name == orchestrator.__name__


def test_tombstoned_name_whose_unnamed_create_fails_is_counted(tasks_env, caplog):
    tasks = _Tasks(
        create_errors=[gexc.AlreadyExists("dup"), gexc.InvalidArgument("bad")],
        get_error=gexc.NotFound("gone"),
    )
    with caplog.at_level(logging.WARNING):
        assert _send(tasks_env, tasks) == (0, 1)
    assert "Failed to create worker task" in caplog.text


def test_get_task_error_other_than_not_found_is_counted(tasks_env, caplog):
    """Existence unconfirmed (e.g. no cloudtasks.tasks.get): logged and
    counted as failed, never re-created (the task may still be queued)."""
    tasks = _Tasks(
        create_errors=[gexc.AlreadyExists("dup")],
        get_error=gexc.PermissionDenied("no tasks.get"),
    )
    with caplog.at_level(logging.WARNING):
        assert _send(tasks_env, tasks) == (0, 1)
    assert len(tasks.creates) == 1
    assert "no tasks.get" in caplog.text


def test_task_name_docstring_says_24_hours():
    doc = orchestrator.task_name.__doc__ or ""
    assert "24 h" in doc
    assert "about an hour" not in doc


# --- C14: trace correlation ---------------------------------------------------


def _format(message="hello"):
    record = logging.LogRecord("t", logging.INFO, __file__, 1, message, None, None)
    return json.loads(logs.CloudRunJsonFormatter().format(record))


def test_cloud_trace_context_header():
    logs.bind_trace({"X-Cloud-Trace-Context": f"{TRACE_ID}/{int(SPAN_HEX, 16)};o=1"})
    entry = _format()
    assert entry[TRACE_FIELD] == f"projects/test-project/traces/{TRACE_ID}"
    # X-Cloud-Trace-Context's SPAN_ID is decimal; Cloud Logging wants 16 hex.
    assert entry[SPAN_FIELD] == SPAN_HEX
    assert entry[SAMPLED_FIELD] is True


def test_cloud_trace_context_without_span_or_options():
    logs.bind_trace({"X-Cloud-Trace-Context": TRACE_ID})
    entry = _format()
    assert entry[TRACE_FIELD] == f"projects/test-project/traces/{TRACE_ID}"
    assert SPAN_FIELD not in entry
    assert entry[SAMPLED_FIELD] is False


def test_traceparent_header_fallback():
    logs.bind_trace({"traceparent": f"00-{TRACE_ID}-{SPAN_HEX}-00"})
    entry = _format()
    assert entry[TRACE_FIELD] == f"projects/test-project/traces/{TRACE_ID}"
    assert entry[SPAN_FIELD] == SPAN_HEX
    assert entry[SAMPLED_FIELD] is False


def test_cloud_trace_context_wins_over_traceparent():
    other = "0af7651916cd43dd8448eb211c80319c"
    logs.bind_trace(
        {
            "X-Cloud-Trace-Context": f"{TRACE_ID}/1;o=1",
            "traceparent": f"00-{other}-{SPAN_HEX}-01",
        }
    )
    assert _format()[TRACE_FIELD].endswith(TRACE_ID)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Cloud-Trace-Context": ""},
        {"X-Cloud-Trace-Context": "not hex!/12"},
        {"traceparent": "garbage"},
        {"traceparent": f"00-{'0' * 32}-{SPAN_HEX}-01"},  # all-zero trace id
    ],
)
def test_missing_or_malformed_header_adds_no_trace(headers):
    logs.bind_trace({"X-Cloud-Trace-Context": f"{TRACE_ID}/1;o=1"})
    logs.bind_trace(headers)  # a new request clears the previous binding
    entry = _format()
    assert TRACE_FIELD not in entry
    assert SPAN_FIELD not in entry
    assert entry["message"] == "hello"


def test_no_project_adds_no_trace(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT")
    logs.bind_trace({"X-Cloud-Trace-Context": f"{TRACE_ID}/1;o=1"})
    assert TRACE_FIELD not in _format()


def test_trace_reaches_a_log_inside_asyncio_run():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.CloudRunJsonFormatter())
    log = logging.getLogger("crf-trace-test")
    log.addHandler(handler)
    log.propagate = False
    try:
        logs.bind_trace({"X-Cloud-Trace-Context": f"{TRACE_ID}/7;o=1"})

        async def _run():
            log.warning("inside the run")

        asyncio.run(_run())
    finally:
        log.removeHandler(handler)
    entry = json.loads(stream.getvalue())
    assert entry[TRACE_FIELD] == f"projects/test-project/traces/{TRACE_ID}"
    assert entry["message"] == "inside the run"


def test_json_fields_cannot_override_the_trace():
    logs.bind_trace({"X-Cloud-Trace-Context": f"{TRACE_ID}/1;o=1"})
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)
    record.json_fields = {TRACE_FIELD: "spoofed"}
    entry = json.loads(logs.CloudRunJsonFormatter().format(record))
    assert entry[TRACE_FIELD].endswith(TRACE_ID)


def test_agent_worker_task_binds_the_trace(monkeypatch):
    seen = {}

    def _handle(dataset, table, row_data, *, deadline):
        seen["trace"] = _format()[TRACE_FIELD]
        return worker.Outcome.ACK

    monkeypatch.setattr(worker, "handle_worker_message", _handle)
    body = {
        "bq_dataset": DS,
        "bq_table": TBL,
        "row_data": {
            "entry_timestamp": "2026-10-13T00:00:00",
            "index": 0,
            "uuid": "u-1",
            "target_trend": "t",
            "brand": "b",
            "target_product": "p",
            "key_selling_point": "k",
            "target_audience": "a",
            "target_search_trend": "t",
        },
    }
    with _app.test_request_context(
        "/",
        method="POST",
        data=json.dumps(body),
        headers={"X-Cloud-Trace-Context": f"{TRACE_ID}/1;o=1"},
    ):
        main.agent_worker_task(flask.request)
    assert seen["trace"] == f"projects/test-project/traces/{TRACE_ID}"


def test_agent_worker_task_takes_the_request_deadline(monkeypatch):
    seen = {}
    monkeypatch.setattr(worker, "request_deadline", lambda: 1234.5)

    def _handle(dataset, table, row_data, *, deadline):
        seen["deadline"] = deadline
        return worker.Outcome.ACK

    monkeypatch.setattr(worker, "handle_worker_message", _handle)
    monkeypatch.setattr(main, "parse_task_request", lambda body: (DS, TBL, {}))
    with _app.test_request_context("/", method="POST", data=b"{}"):
        main.agent_worker_task(flask.request)
    assert seen["deadline"] == 1234.5


def test_request_deadline_is_run_deadline_of_now(monkeypatch):
    monkeypatch.setattr(worker, "_clock", lambda: 777.0)
    assert worker.request_deadline() == worker.run_deadline(777.0)


def _trigger():
    import base64

    data = base64.b64encode(json.dumps({"bq_dataset": DS, "bq_table": TBL}).encode())
    return types.SimpleNamespace(data={"message": {"data": data.decode()}})


def test_crf_entrypoint_binds_the_trace_in_a_request_context(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        orchestrator, "dispatch", lambda payload: seen.update(entry=_format())
    )
    with _app.test_request_context(
        "/", method="POST", headers={"traceparent": f"00-{TRACE_ID}-{SPAN_HEX}-01"}
    ):
        main.crf_entrypoint(_trigger())
    assert seen["entry"][TRACE_FIELD] == f"projects/test-project/traces/{TRACE_ID}"
    assert seen["entry"][SPAN_FIELD] == SPAN_HEX


def test_crf_entrypoint_outside_a_request_context_clears_the_trace(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        orchestrator, "dispatch", lambda payload: seen.update(entry=_format())
    )
    logs.bind_trace({"X-Cloud-Trace-Context": f"{TRACE_ID}/1;o=1"})  # stale
    main.crf_entrypoint(_trigger())  # no Flask request context: no crash
    assert TRACE_FIELD not in seen["entry"]


def test_logs_import_replaces_preexisting_root_handlers():
    """functions-framework may configure the root logger (WARNING) before
    `main` imports `logs`; basicConfig(force=True) still installs the JSON
    handler at INFO instead of silently doing nothing."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    preexisting = logging.StreamHandler(io.StringIO())
    root.addHandler(preexisting)
    root.setLevel(logging.WARNING)
    try:
        spec = importlib.util.spec_from_file_location("_crf_logs_copy", logs.__file__)
        assert spec and spec.loader
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
        assert preexisting not in root.handlers
        assert root.level == logging.INFO
        assert [type(h.formatter).__name__ for h in root.handlers] == [
            "CloudRunJsonFormatter"
        ]
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


# --- C16: module loggers -----------------------------------------------------


def test_messages_logs_through_its_module_logger(caplog):
    with caplog.at_level(logging.INFO):
        assert messages.parse_trigger_message(types.SimpleNamespace(data={})) is None
    assert {r.name for r in caplog.records} == {messages.__name__}


def test_orchestrator_logs_through_its_module_logger(monkeypatch, caplog):
    monkeypatch.setattr(clients, "get_bigquery_client", lambda: bq_results(0, []))
    with caplog.at_level(logging.INFO):
        assert orchestrator.dispatch({"bq_dataset": DS, "bq_table": TBL}) == 0
    names = {r.name for r in caplog.records if r.module == "orchestrator"}
    assert names == {orchestrator.__name__}


def test_malformed_message_docstring_names_both_transports():
    doc = messages.MalformedMessageError.__doc__ or ""
    assert "A Pub/Sub message that" not in doc
