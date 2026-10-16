"""Worker reliability tests for the CRF fan-out (source review C1, C3, C6, C7, C8).

- C1: the engine is resolved before the row is locked, so a stale engine id
  (404/403) leaves the row QUEUED instead of failing it, and a transient
  error retries the delivery without burning an attempt.
- C3: every BigQuery statement is bounded by ``BQ_QUERY_TIMEOUT_SECS`` (socket
  timeout and both retry deadlines, not just the polling wait).
- C6: a DML serialization conflict is retried with a jittered backoff.
- C7: the engine get and the session create have timeouts.
- C8: an HTTP 408 is transient.

Drives ``worker.process_row`` / ``worker.handle_worker_message`` directly
(no transport entry point). Coroutines are driven with ``asyncio.run``.
"""

import asyncio
import logging
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud.bigquery import DEFAULT_RETRY
from google.cloud.bigquery.retry import DEFAULT_JOB_RETRY
from google.genai import errors as genai_errors

from cloud_functions.creative_fanout import clients, session, status_store, worker
from cloud_functions.creative_fanout.completion import RunOutcome
from cloud_functions.creative_fanout.config import config
from tests.crf_fakes import FakeStatusStore, bq_results

pytestmark = pytest.mark.usefixtures("gcp_project_env", "crf_engine_env")

DS = config.BQ_DATASET_ID
TBL = config.BQ_TABLE_TARGETS

_TREND = {
    "index": 0,
    "uuid": "uuid-1",
    "target_trend": "trend",
    "entry_timestamp": "2026-10-13T00:00:00+00:00",
    "brand": "BrandX",
    "target_product": "prod",
    "key_selling_point": "ksp",
    "target_audience": "aud",
    "target_search_trend": "trend",
}


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    clock = {"now": 0.0}

    async def _sleep(secs):
        clock["now"] += secs

    monkeypatch.setattr(worker, "_clock", lambda: clock["now"])
    monkeypatch.setattr(worker, "_sleep", _sleep)


def _client_error(code: int) -> genai_errors.ClientError:
    return genai_errors.ClientError(
        code, {"error": {"code": code, "message": "x", "status": "X"}}
    )


def _server_error(code: int) -> genai_errors.ServerError:
    return genai_errors.ServerError(
        code, {"error": {"code": code, "message": "x", "status": "UNAVAILABLE"}}
    )


class _Engine:
    """``clients.new_vertex_client`` stand-in: records ``runtimes.get`` (and
    the thread it ran on) and whether the client was closed."""

    def __init__(self, monkeypatch, events, *, get_exc=None, agent=None):
        self.events = events
        self.get_exc = get_exc
        self.agent = agent if agent is not None else MagicMock(name="remote_agent")
        self.get_kwargs: dict = {}
        self.get_thread: int | None = None
        self.client = MagicMock()
        self.client.runtimes.get.side_effect = self._get
        self.client.aio.aclose = AsyncMock(side_effect=self._close)
        monkeypatch.setattr(clients, "new_vertex_client", lambda: self.client)

    def _get(self, **kwargs):
        self.events.append("get")
        self.get_kwargs = kwargs
        self.get_thread = threading.get_ident()
        if self.get_exc is not None:
            raise self.get_exc
        return self.agent

    def _close(self):
        self.events.append("close")

    @property
    def closed(self) -> bool:
        return "close" in self.events


def _store(monkeypatch, events, **kw) -> FakeStatusStore:
    """A FakeStatusStore that also appends its calls to the shared ``events``."""
    store = FakeStatusStore(monkeypatch, _TREND, **kw)
    lock = status_store.acquire_processing_lock

    def _lock(*args):
        events.append("lock")
        return lock(*args)

    monkeypatch.setattr(status_store, "acquire_processing_lock", _lock)
    return store


def _agent_run(monkeypatch, events):
    calls: list[dict] = []

    async def _run(**kwargs):
        events.append("run")
        calls.append(kwargs)
        return RunOutcome(finalized=True, retried=False, reason="done")

    monkeypatch.setattr(worker, "create_agent_run", _run)
    return calls


def _process_row():
    asyncio.run(
        worker.process_row(_TREND, "agent-123", MagicMock(), DS, TBL, deadline=1e9)
    )


# ============================================================
# C1: resolve the engine before the lock
# ============================================================
@pytest.mark.parametrize("code", [404, 403])
def test_unavailable_engine_leaves_the_row_queued(monkeypatch, caplog, code):
    """A deleted / foreign engine (404/403): ERROR, return (ACK) without
    locking, so the row stays QUEUED and no attempt is burned."""
    events: list[str] = []
    engine = _Engine(monkeypatch, events, get_exc=_client_error(code))
    store = _store(monkeypatch, events)
    runs = _agent_run(monkeypatch, events)
    with caplog.at_level(logging.ERROR):
        _process_row()  # must not raise
    assert store.calls == []
    assert runs == []
    assert events == ["get", "close"]
    assert engine.closed
    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "json_fields", {}).get("reason") == "engine_unavailable"
    ]
    assert record.levelno == logging.ERROR
    assert record.json_fields["uuid"] == "uuid-1"
    assert record.json_fields["http_status"] == code


@pytest.mark.parametrize(
    "exc", [_server_error(503), _client_error(429), _client_error(408), TimeoutError()]
)
def test_transient_engine_error_raises_before_the_lock(monkeypatch, exc):
    """A transient engine get error raises (the delivery is retried); the row
    was never locked, so no attempt is burned. The client is still closed."""
    events: list[str] = []
    engine = _Engine(monkeypatch, events, get_exc=exc)
    store = _store(monkeypatch, events)
    _agent_run(monkeypatch, events)
    with pytest.raises(type(exc)):
        _process_row()
    assert store.calls == []
    assert events == ["get", "close"]
    assert engine.closed


def test_engine_resolved_off_the_loop_then_lock_then_run(monkeypatch):
    """Success: get (in a worker thread, with an HTTP timeout) → lock → run
    on the resolved engine → close."""
    events: list[str] = []
    engine = _Engine(monkeypatch, events)
    store = _store(monkeypatch, events)
    runs = _agent_run(monkeypatch, events)
    _process_row()
    assert events == ["get", "lock", "run", "close"]
    assert store.statuses == ["PROCESSED"]
    assert runs[0]["remote_agent"] is engine.agent
    assert engine.get_kwargs["name"] == (
        f"projects/{config.GOOGLE_CLOUD_PROJECT_NUMBER}/locations/"
        f"{config.GCP_REGION}/reasoningEngines/agent-123"
    )
    # C7: the get is bounded (genai http_options.timeout is in milliseconds)
    # and runs off the event loop's thread (it is synchronous).
    timeout_ms = engine.get_kwargs["config"]["http_options"]["timeout"]
    assert timeout_ms == int(worker.ENGINE_GET_TIMEOUT_SECS * 1000)
    assert engine.get_thread != threading.get_ident()


def test_lost_lock_still_closes_the_client(monkeypatch):
    events: list[str] = []
    engine = _Engine(monkeypatch, events)
    _store(monkeypatch, events, attempt=None)
    runs = _agent_run(monkeypatch, events)
    _process_row()
    assert runs == []
    assert events == ["get", "lock", "close"]
    assert engine.closed


def test_run_error_still_closes_the_client(monkeypatch):
    events: list[str] = []
    engine = _Engine(monkeypatch, events)
    store = _store(monkeypatch, events)

    async def _boom(**kwargs):
        raise RuntimeError("agent boom")

    monkeypatch.setattr(worker, "create_agent_run", _boom)
    _process_row()
    assert store.statuses == ["FAILED"]
    assert engine.closed


def test_handle_worker_message_acks_an_unavailable_engine(monkeypatch):
    events: list[str] = []
    _Engine(monkeypatch, events, get_exc=_client_error(404))
    store = _store(monkeypatch, events)
    monkeypatch.setattr(clients, "get_bigquery_client", MagicMock)
    outcome = worker.handle_worker_message(DS, TBL, dict(_TREND), deadline=1e9)
    assert outcome is worker.Outcome.ACK
    assert store.calls == []


def test_handle_worker_message_retries_a_transient_engine_error(monkeypatch):
    events: list[str] = []
    _Engine(monkeypatch, events, get_exc=_server_error(503))
    store = _store(monkeypatch, events)
    monkeypatch.setattr(clients, "get_bigquery_client", MagicMock)
    outcome = worker.handle_worker_message(DS, TBL, dict(_TREND), deadline=1e9)
    assert outcome is worker.Outcome.RETRY
    assert store.calls == []


# ============================================================
# C7: the session create is bounded
# ============================================================
def _hanging_agent():
    agent = MagicMock()

    async def _create(**kwargs):
        await asyncio.Event().wait()  # never returns

    agent.async_create_session = _create
    return agent


def test_hanging_session_create_times_out(monkeypatch):
    monkeypatch.setattr(session, "CREATE_TIMEOUT_SECS", 0.01)

    async def _enter():
        async with session.agent_session(_hanging_agent(), "u", state={"a": "b"}):
            pytest.fail("the body must not run")  # pragma: no cover

    with pytest.raises(TimeoutError):
        asyncio.run(_enter())


def test_hanging_session_create_requeues_the_row(monkeypatch):
    """Through process_row and the real create_agent_run: a create that hangs
    → SessionNotStarted(TimeoutError) → transient → the row goes back to
    QUEUED (the existing pre-session requeue path, after the lock)."""
    monkeypatch.setattr(session, "CREATE_TIMEOUT_SECS", 0.01)
    events: list[str] = []
    engine = _Engine(monkeypatch, events, agent=_hanging_agent())
    store = _store(monkeypatch, events, attempt=2)
    _process_row()
    assert store.statuses == []
    assert [(c[0], c[2]) for c in store.calls if c[0] == "requeue"] == [("requeue", 2)]
    assert engine.closed


def test_create_timeout_is_a_module_constant():
    assert session.CREATE_TIMEOUT_SECS == 60.0


# ============================================================
# C8: 408 is transient
# ============================================================
@pytest.mark.parametrize(
    "exc,transient",
    [
        (_client_error(408), True),
        (_client_error(429), True),
        (_server_error(503), True),
        (_client_error(404), False),
        (_client_error(403), False),
        (_client_error(400), False),
    ],
)
def test_is_transient_http_codes(exc, transient):
    assert worker._is_transient(exc) is transient


# ============================================================
# C3: every BigQuery statement is bounded by BQ_QUERY_TIMEOUT_SECS
# ============================================================
def test_run_query_bounds_the_socket_and_both_retries():
    bq = bq_results(1)
    status_store._run_query(bq, "SELECT 1", [])
    kwargs = bq.query_and_wait.call_args.kwargs
    timeout = config.BQ_QUERY_TIMEOUT_SECS
    assert kwargs["api_timeout"] == timeout
    assert kwargs["wait_timeout"] == timeout
    assert int(kwargs["job_config"].job_timeout_ms) == int(timeout * 1000)
    retry, job_retry = kwargs["retry"], kwargs["job_retry"]
    assert retry.timeout == timeout
    assert retry._predicate is DEFAULT_RETRY._predicate
    assert job_retry.timeout == timeout
    assert job_retry._predicate is DEFAULT_JOB_RETRY._predicate


# ============================================================
# C6: DML serialization conflicts are retried
# ============================================================
_CONFLICT_MSG = "Could not serialize access to table p:d.t due to concurrent update"


@pytest.fixture
def bq_sleeps(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(status_store, "_sleep", sleeps.append)
    return sleeps


@pytest.mark.parametrize(
    "exc_type", [api_exceptions.BadRequest, api_exceptions.Conflict]
)
def test_serialization_conflict_is_retried(bq_sleeps, caplog, exc_type):
    bq = bq_results(exc_type(_CONFLICT_MSG), 1)
    with caplog.at_level(logging.WARNING):
        result = status_store._run_query(bq, "UPDATE t SET x = 1", [])
    assert result.num_dml_affected_rows == 1
    assert bq.query_and_wait.call_count == 2
    assert len(bq_sleeps) == 1 and 0.5 <= bq_sleeps[0] <= 1.5
    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "json_fields", {}).get("reason") == "bq_serialization_conflict"
    ]
    assert record.levelno == logging.WARNING


def test_serialization_conflict_gives_up_after_three_tries(bq_sleeps, caplog):
    errors = [api_exceptions.BadRequest(_CONFLICT_MSG) for _ in range(3)]
    bq = bq_results(*errors, 1)
    with caplog.at_level(logging.WARNING), pytest.raises(api_exceptions.BadRequest):
        status_store._run_query(bq, "UPDATE t SET x = 1", [])
    assert bq.query_and_wait.call_count == status_store.SERIALIZATION_RETRY_ATTEMPTS
    assert status_store.SERIALIZATION_RETRY_ATTEMPTS == 3
    # Jittered 1 s then 2 s backoff, one WARNING per retry.
    assert len(bq_sleeps) == 2
    assert 0.5 <= bq_sleeps[0] <= 1.5 and 1.0 <= bq_sleeps[1] <= 3.0
    warnings = [r for r in caplog.records if "serialization conflict" in r.getMessage()]
    assert len(warnings) == 2


@pytest.mark.parametrize(
    "exc",
    [
        api_exceptions.BadRequest("Syntax error: Unexpected keyword"),
        api_exceptions.Conflict("Already Exists: Job p:US.job_1"),
        api_exceptions.ServiceUnavailable("down"),
        TimeoutError("wait timed out"),
    ],
)
def test_other_errors_are_never_retried(bq_sleeps, exc):
    bq = bq_results(exc, 1)
    with pytest.raises(type(exc)):
        status_store._run_query(bq, "UPDATE t SET x = 1", [])
    assert bq.query_and_wait.call_count == 1
    assert bq_sleeps == []


def test_serialization_retries_stay_within_the_query_timeout(monkeypatch, bq_sleeps):
    """The retries share ONE BQ_QUERY_TIMEOUT_SECS budget (the worker's
    BUDGET_SAFETY_SECS covers one bounded statement): a retry gets only the
    time left, and none is tried once too little is left."""
    timeout = config.BQ_QUERY_TIMEOUT_SECS
    now = {"t": 0.0}
    monkeypatch.setattr(status_store, "_monotonic", lambda: now["t"])

    def _slow_conflict(*args, **kwargs):
        now["t"] += timeout - 3.0  # the first try used almost all of it
        raise api_exceptions.BadRequest(_CONFLICT_MSG)

    bq = MagicMock()
    bq.query_and_wait.side_effect = _slow_conflict
    with pytest.raises(api_exceptions.BadRequest):
        status_store._run_query(bq, "UPDATE t SET x = 1", [])
    assert bq.query_and_wait.call_count == 1
    assert bq_sleeps == []
