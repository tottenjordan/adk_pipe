"""Row-state tests for the CRF fan-out (code review R1/R2/R5/R7/C1/C2/C7).

The trend row's ``processed_status`` changes only through
``status_store.TRANSITIONS``: every UPDATE checks the row's from-state, the
worker's writes are fenced by the attempt its lock returned, rows are matched
by ``(uuid, target_trend)``, the worker builds the run from the row it
re-reads after the lock, a finished run is never recorded FAILED, and a
transient error before a session exists puts the row back to QUEUED.

No BigQuery: SQL-level tests read the statements and parameters
``query_and_wait`` receives (``crf_fakes.bq_results``); worker tests replace
the statements with ``crf_fakes.FakeStatusStore``. Coroutines are driven with
``asyncio.run``.
"""

import asyncio
import base64
import json
import logging
import types
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from google.api_core import exceptions as api_exceptions

from cloud_functions.creative_fanout import (
    clients,
    main,
    messages,
    orchestrator,
    status_store,
    worker,
)
from cloud_functions.creative_fanout.completion import RunOutcome
from cloud_functions.creative_fanout.config import config
from tests.crf_fakes import FakeStatusStore, bq_results, queued, row_for

pytestmark = pytest.mark.usefixtures("gcp_project_env")

DS = config.BQ_DATASET_ID
TBL = config.BQ_TABLE_TARGETS
TS = "2026-10-13T00:00:00+00:00"
KEY = status_store.RowKey("uuid-1", "trend", TS)

_TREND = {
    "index": 0,
    "uuid": "uuid-1",
    "target_trend": "trend",
    "entry_timestamp": TS,
    "brand": "BrandX",
    "target_product": "prod",
    "key_selling_point": "ksp",
    "target_audience": "aud",
    "target_search_trend": "trend",
}


def _sql(call) -> str:
    return " ".join(call.args[0].split())


def _params(call) -> dict:
    return {p.name: p for p in call.kwargs["job_config"].query_parameters}


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    clock = {"now": 0.0}

    async def _sleep(secs):
        clock["now"] += secs

    monkeypatch.setattr(worker, "_clock", lambda: clock["now"])
    monkeypatch.setattr(worker, "_sleep", _sleep)


# ============================================================
# The transition table: every UPDATE checks its from-state
# ============================================================
def test_transition_table_is_the_complete_state_machine():
    assert set(status_store.TRANSITIONS) == {
        "mark_queued",
        "lock",
        "mark_final",
        "requeue",
        "reap",
    }


@pytest.mark.parametrize(
    ("transition", "build"),
    [
        (
            "mark_queued",
            lambda: status_store._build_mark_queued_sql("p", DS, TBL, [KEY]),
        ),
        ("lock", lambda: status_store._build_lock_sql("p", DS, TBL, KEY, 0)),
        (
            "mark_final",
            lambda: status_store._build_fenced_update_sql(
                "p", DS, TBL, KEY, "PROCESSED", 1, "mark_final"
            ),
        ),
        (
            "requeue",
            lambda: status_store._build_fenced_update_sql(
                "p", DS, TBL, KEY, "QUEUED", 1, "requeue"
            ),
        ),
        ("reap", lambda: status_store._build_reap_sql("p", DS, TBL, 45, 3)),
    ],
)
def test_every_update_carries_its_from_state_guard(transition, build):
    sql, _ = build()
    sql = " ".join(sql.split())
    assert sql.startswith("UPDATE ")
    assert f"AND {status_store.TRANSITIONS[transition].guard}" in sql or (
        f"WHERE {status_store.TRANSITIONS[transition].guard}" in sql
    )


def test_queued_mark_never_overwrites_processing_or_processed():
    """The orchestrator's QUEUED mark matches only NULL/QUEUED rows, so a row
    a worker locked or finished since the SELECT keeps its state (R1)."""
    sql, _ = status_store._build_mark_queued_sql("p", DS, TBL, [KEY])
    sql = " ".join(sql.split())
    assert "AND (processed_status IS NULL OR processed_status = 'QUEUED')" in sql, sql
    for later in ("PROCESSING", "PROCESSED", "FAILED"):
        assert later not in sql


def test_fenced_writes_match_only_the_locked_attempt():
    for transition, status in (("mark_final", "FAILED"), ("requeue", "QUEUED")):
        sql, params = status_store._build_fenced_update_sql(
            "p", DS, TBL, KEY, status, 4, transition
        )
        sql = " ".join(sql.split())
        assert (
            "AND processed_status = 'PROCESSING' AND processing_attempts = @attempt"
            in sql
        )
        assert {p.name: p.value for p in params}["attempt"] == 4


# ============================================================
# The lock: compare-and-set, returns the fencing token
# ============================================================
def test_lock_compare_and_sets_on_the_attempt_it_read():
    bq = bq_results(queued(2), 1)
    assert status_store.acquire_processing_lock(bq, DS, TBL, KEY) == 3
    read, lock = bq.query_and_wait.call_args_list
    assert _sql(read).startswith("SELECT processed_status, processing_attempts")
    assert "AND COALESCE(processing_attempts, 0) = @prev_attempt" in _sql(lock)
    params = _params(lock)
    assert params["prev_attempt"].value == 2
    assert params["attempt"].value == 3


@pytest.mark.parametrize("status", ["PROCESSING", "PROCESSED", "FAILED", None])
def test_lock_lost_when_the_row_is_not_queued(status):
    """Not QUEUED (even NULL: the orchestrator never marked it) → lost, and no
    UPDATE is attempted."""
    bq = bq_results([{"processed_status": status, "processing_attempts": 1}])
    assert status_store.acquire_processing_lock(bq, DS, TBL, KEY) is None
    assert bq.query_and_wait.call_count == 1


def test_lock_lost_when_the_row_is_gone():
    bq = bq_results([])
    assert status_store.acquire_processing_lock(bq, DS, TBL, KEY) is None
    assert bq.query_and_wait.call_count == 1


def test_lock_matching_two_rows_is_an_error_not_a_lost_lock():
    """M10: an UPDATE that changed 2 rows is an error (never 'lock failed')."""
    bq = bq_results(queued(0), 2)
    with pytest.raises(status_store.AmbiguousRowKeyError):
        status_store.acquire_processing_lock(bq, DS, TBL, KEY)


def test_lock_read_of_two_rows_raises_before_any_update():
    """Two rows sharing a (legacy) key: raise before changing either (R5)."""
    legacy = status_store.RowKey(None, None, TS)
    bq = bq_results(queued(0) + queued(0))
    with pytest.raises(status_store.AmbiguousRowKeyError):
        status_store.acquire_processing_lock(bq, DS, TBL, legacy)
    assert bq.query_and_wait.call_count == 1


def test_fenced_write_matching_two_rows_raises():
    with pytest.raises(status_store.AmbiguousRowKeyError):
        status_store.mark_final(bq_results(2), DS, TBL, KEY, "PROCESSED", 1)


def test_lost_fence_changes_nothing_and_is_logged(caplog):
    """A late worker's final write after the reaper re-queued (and maybe
    re-locked) its row matches 0 rows: False, logged as lost_fence."""
    with caplog.at_level(logging.WARNING):
        got = status_store.mark_final(bq_results(0), DS, TBL, KEY, "PROCESSED", 1)
    assert got is False
    (record,) = [r for r in caplog.records if "Lost fence" in r.getMessage()]
    assert record.json_fields["reason"] == "lost_fence"
    assert record.json_fields["uuid"] == "uuid-1"


@pytest.mark.parametrize(("attempt", "status"), [(1, "QUEUED"), (2, "QUEUED")])
def test_requeue_under_the_cap(monkeypatch, attempt, status):
    monkeypatch.setattr(config, "MAX_PROCESSING_ATTEMPTS", 3)
    bq = bq_results(1)
    assert status_store.requeue(bq, DS, TBL, KEY, attempt) == status
    assert _params(bq.query_and_wait.call_args)["status"].value == status


def test_requeue_at_the_cap_fails_the_row(monkeypatch):
    monkeypatch.setattr(config, "MAX_PROCESSING_ATTEMPTS", 3)
    bq = bq_results(1)
    assert status_store.requeue(bq, DS, TBL, KEY, 3) == "FAILED"
    assert _params(bq.query_and_wait.call_args)["status"].value == "FAILED"


def test_requeue_lost_fence_returns_none():
    assert status_store.requeue(bq_results(0), DS, TBL, KEY, 1) is None


def test_read_locked_row_reapplies_the_sensitive_predicate():
    bq = bq_results([])
    assert status_store.read_locked_row(bq, DS, TBL, KEY, 2) is None
    sql = _sql(bq.query_and_wait.call_args)
    assert f"NOT ({status_store._NOT_SENSITIVE_SQL}) AS crf_sensitive" in sql
    assert "processed_status = 'PROCESSING' AND processing_attempts = @attempt" in sql
    assert "uuid = @uuid AND target_trend = @target_trend" in sql
    # The same predicate the orchestrator's SELECT filters on.
    select_sql, _ = status_store._build_select_unprocessed_sql("p", DS, TBL, 3)
    assert status_store._NOT_SENSITIVE_SQL in select_sql


def test_read_locked_row_of_two_rows_raises():
    with pytest.raises(status_store.AmbiguousRowKeyError):
        status_store.read_locked_row(bq_results([{}, {}]), DS, TBL, KEY, 1)


# ============================================================
# Orchestrator: keys, QUEUED before publish, bounded publish
# ============================================================
def _bq_row(ts, trend, uuid):
    return {
        "uuid": uuid,
        "entry_timestamp": ts,
        "target_trend": trend,
        "brand": "B",
        "target_audience": "a",
        "target_product": "p",
        "key_selling_point": "k",
    }


_ROWS = [
    # Two rows with the SAME timestamp: the key tells them apart (R5).
    _bq_row(datetime(2026, 10, 13, tzinfo=UTC), "trend A", "u-a"),
    _bq_row(datetime(2026, 10, 13, tzinfo=UTC), "trend B", "u-b"),
]


def _trigger(**extra):
    payload = {"bq_dataset": DS, "bq_table": TBL, "agent_resource_id": "1", **extra}
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return types.SimpleNamespace(data={"message": {"data": data}})


@pytest.fixture
def orchestrator_env(monkeypatch):
    """reap (0) → SELECT (_ROWS) → QUEUED mark (2), with every BigQuery call
    and publish recorded in one ordered log."""
    events = []
    bq = bq_results(0, list(_ROWS), 2)
    results = bq.query_and_wait.side_effect  # an iterator over the results

    def _query(sql, **kw):
        events.append(("bq", " ".join(sql.split())))
        return next(results)

    bq.query_and_wait.side_effect = _query
    publisher = MagicMock()

    def _publish(topic, data):
        events.append(("publish", json.loads(data)))
        return MagicMock()

    publisher.publish.side_effect = _publish
    monkeypatch.setattr(clients, "get_bigquery_client", lambda: bq)
    monkeypatch.setattr(clients, "get_pubsub_client", lambda: publisher)
    return events, bq, publisher


def test_rows_marked_queued_before_the_first_publish(orchestrator_env):
    """M24/M24b: the QUEUED mark targets exactly the dispatched rows' keys and
    runs before any worker message is published (the worker's lock only takes
    a QUEUED row, so an unmarked brand-new row would never run)."""
    events, bq, _ = orchestrator_env
    main.crf_entrypoint(_trigger())

    kinds = [e[0] for e in events]
    assert kinds == ["bq", "bq", "bq", "publish", "publish"]
    mark_sql = events[2][1]
    assert (
        mark_sql.startswith("UPDATE") and "SET processed_status = 'QUEUED'" in mark_sql
    )
    mark_call = bq.query_and_wait.call_args_list[2]
    keys = [
        (v.struct_values["uuid"], v.struct_values["target_trend"])
        for v in _params(mark_call)["row_keys"].values
    ]
    published = [
        (e[1]["row_data"]["uuid"], e[1]["row_data"]["target_trend"]) for e in events[3:]
    ]
    assert keys == published == [("u-a", "trend A"), ("u-b", "trend B")]


def test_worker_message_carries_the_row_key(orchestrator_env):
    events, _, _ = orchestrator_env
    main.crf_entrypoint(_trigger())
    row_data = events[3][1]["row_data"]
    assert row_data["uuid"] == "u-a"
    assert row_data["target_trend"] == "trend A"
    assert row_data["entry_timestamp"] == "2026-10-13T00:00:00+00:00"


def test_publish_wait_is_bounded(orchestrator_env, caplog):
    """C7: each publish future is awaited with a timeout; one that times out
    is counted as failed (its row stays QUEUED for the next re-query)."""
    _, _, publisher = orchestrator_env
    ok, slow = MagicMock(), MagicMock()
    slow.result.side_effect = TimeoutError()
    publisher.publish.side_effect = [ok, slow]
    with caplog.at_level(logging.WARNING):
        main.crf_entrypoint(_trigger())  # must not raise
    for future in (ok, slow):
        future.result.assert_called_once_with(timeout=30.0)
    assert orchestrator.PUBLISH_TIMEOUT_SECS == 30.0
    assert "1/2 worker publishes failed" in caplog.text


# ============================================================
# Worker messages: the row key, and the legacy fallback
# ============================================================
def _worker_event(row_data):
    payload = {
        "bq_dataset": DS,
        "bq_table": TBL,
        "agent_resource_id": "123",
        "row_data": row_data,
    }
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return types.SimpleNamespace(data={"message": {"data": data}})


def test_parse_accepts_a_legacy_message_without_the_key():
    legacy = {k: v for k, v in _TREND.items() if k not in ("uuid", "target_trend")}
    *_, row_data = messages.parse_worker_message(_worker_event(legacy))
    assert status_store.RowKey.from_mapping(row_data).legacy


@pytest.mark.parametrize(
    "bad",
    [{"uuid": "u", "target_trend": None}, {"uuid": ""}, {"uuid": 7}],
)
def test_parse_rejects_a_malformed_key(bad):
    with pytest.raises(messages.MalformedMessageError):
        messages.parse_worker_message(_worker_event({**_TREND, **bad}))


# ============================================================
# Worker: lock → re-read → run → fenced status
# ============================================================
def _run_row(trend=_TREND, **kw):
    kw.setdefault("deadline", 1e9)
    asyncio.run(worker.process_row(trend, "agent-123", MagicMock(), DS, TBL, **kw))


def _agent_run(monkeypatch, result=None, exc=None):
    calls = []

    async def _run(**kwargs):
        calls.append(kwargs)
        if exc is not None:
            raise exc
        return result or RunOutcome(finalized=True, retried=False, reason="done")

    monkeypatch.setattr(worker, "create_agent_run", _run)
    return calls


def test_lost_lock_acks_without_running(monkeypatch):
    """M26: a lost lock returns normally (ACK); nothing runs or is written."""
    store = FakeStatusStore(monkeypatch, _TREND, attempt=None)
    calls = _agent_run(monkeypatch)
    _run_row()  # must not raise
    assert calls == []
    assert [c[0] for c in store.calls] == ["lock"]


def test_failed_write_targets_the_locked_row_and_attempt(monkeypatch):
    """M15: FAILED goes to this row's key at the attempt the lock returned."""
    store = FakeStatusStore(monkeypatch, _TREND, attempt=7)
    _agent_run(monkeypatch, exc=RuntimeError("agent boom"))
    _run_row()
    (mark,) = [c for c in store.calls if c[0] == "mark_final"]
    _, key, status, attempt = mark
    assert status == "FAILED"
    assert attempt == 7
    assert (key.uuid, key.target_trend) == ("uuid-1", "trend")


def test_unfinished_run_is_failed_with_its_reason(monkeypatch, caplog):
    store = FakeStatusStore(monkeypatch, _TREND, attempt=2)
    _agent_run(monkeypatch, RunOutcome(finalized=False, retried=True, reason="idle"))
    with caplog.at_level(logging.ERROR):
        _run_row()
    assert store.statuses == ["FAILED"]
    (record,) = [r for r in caplog.records if "Failed processing" in r.getMessage()]
    assert record.json_fields["reason"] == "idle"


def test_the_re_read_row_drives_the_run_not_the_message(monkeypatch):
    """C2: campaign fields come from the row read after the lock."""
    row = row_for(
        _TREND,
        brand="RowBrand",
        target_product="row product",
        trend_context='{"hook":"h"}',
    )
    FakeStatusStore(monkeypatch, _TREND, row=row)
    calls = _agent_run(monkeypatch)
    _run_row({**_TREND, "brand": "MessageBrand", "target_product": "msg product"})
    (call,) = calls
    msg = call["msg_dict"]
    assert msg["brand"] == "RowBrand"
    assert msg["target_product"] == "row product"
    assert msg["trend_context"] == '{"hook":"h"}'
    assert worker.build_campaign_state(msg)["brand"] == "RowBrand"


@pytest.mark.parametrize(
    ("row", "reason"),
    [(None, "row_missing"), ("sensitive", "row_sensitive")],
)
def test_missing_or_sensitive_row_is_failed_and_acked(monkeypatch, caplog, row, reason):
    if row == "sensitive":
        row = row_for(_TREND, crf_sensitive=True)
    store = FakeStatusStore(monkeypatch, _TREND, attempt=3, row=row)
    calls = _agent_run(monkeypatch)
    with caplog.at_level(logging.ERROR):
        _run_row()  # must not raise (ACK)
    assert calls == []
    assert [c for c in store.calls if c[0] == "mark_final"] == [
        ("mark_final", status_store.RowKey.from_mapping(_TREND), "FAILED", 3)
    ]
    (record,) = [r for r in caplog.records if "Not running row" in r.getMessage()]
    assert record.json_fields["reason"] == reason


def test_processed_write_retries_and_never_becomes_failed(monkeypatch):
    """R2: a transient error on the PROCESSED write is retried; FAILED is
    never written for a finished run."""
    store = FakeStatusStore(
        monkeypatch,
        _TREND,
        mark_errors=[RuntimeError("503"), TimeoutError(), None],
    )
    _agent_run(monkeypatch)
    _run_row()
    assert store.statuses == ["PROCESSED", "PROCESSED", "PROCESSED"]


def test_processed_write_that_keeps_failing_leaves_processing(monkeypatch, caplog):
    store = FakeStatusStore(monkeypatch, _TREND, mark_errors=[RuntimeError("503")] * 3)
    _agent_run(monkeypatch)
    with caplog.at_level(logging.ERROR):
        _run_row()  # ACK: the reaper re-runs the row
    assert store.statuses == ["PROCESSED"] * worker.PROCESSED_WRITE_ATTEMPTS
    assert "FAILED" not in store.statuses
    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "json_fields", {}).get("reason") == "status_write_failed"
    ]
    assert record.json_fields["status"] == "PROCESSING"


@pytest.mark.parametrize(("spare", "tries"), [(-1.0, 1), (1.0, 2), (5.0, 3)])
def test_processed_write_retries_only_while_a_bq_wait_fits(monkeypatch, spare, tries):
    """A retry runs only while one more bounded BigQuery wait (plus its
    backoff) fits before the Cloud Run timeout (deadline + safety margin);
    the clock stands at 0 here."""
    store = FakeStatusStore(monkeypatch, _TREND, mark_errors=[RuntimeError("503")] * 3)
    _agent_run(monkeypatch)
    # Time left at the first retry decision = BQ wait + 2 s backoff + spare.
    deadline = (
        config.BQ_QUERY_TIMEOUT_SECS
        + worker.PROCESSED_WRITE_BACKOFF_SECS
        + spare
        - config.BUDGET_SAFETY_SECS
    )
    _run_row(deadline=deadline)
    assert store.statuses == ["PROCESSED"] * tries
    assert "FAILED" not in store.statuses


def test_deadline_is_passed_through(monkeypatch):
    FakeStatusStore(monkeypatch, _TREND)
    calls = _agent_run(monkeypatch)
    _run_row(deadline=1234.5)
    assert calls[0]["deadline"] == 1234.5


# --- errors before a session exists (C1) ------------------------------------
def _vertex(monkeypatch, *, get_exc=None, create_exc=None):
    agent = MagicMock()

    async def _create(**kwargs):
        if create_exc is not None:
            raise create_exc
        return {"id": "s-1"}

    agent.async_create_session = _create
    client = MagicMock()
    if get_exc is not None:
        client.runtimes.get.side_effect = get_exc
    else:
        client.runtimes.get.return_value = agent
    monkeypatch.setattr(clients, "new_vertex_client", lambda: client)


def _genai_503():
    from google.genai import errors

    return errors.ServerError(503, {"error": {"message": "x", "status": "UNAVAILABLE"}})


@pytest.mark.parametrize(
    "where,exc",
    [
        ("get", api_exceptions.TooManyRequests("quota")),
        ("get", api_exceptions.ServiceUnavailable("down")),
        ("create", api_exceptions.TooManyRequests("quota")),
        ("create", TimeoutError()),
        ("create", _genai_503()),
    ],
)
def test_transient_error_before_a_session_requeues(monkeypatch, caplog, where, exc):
    store = FakeStatusStore(monkeypatch, _TREND, attempt=2)
    _vertex(monkeypatch, **{f"{where}_exc": exc})
    with caplog.at_level(logging.WARNING):
        _run_row()  # ACK
    assert store.statuses == []
    assert [
        (c[1].uuid, c[1].target_trend, c[2]) for c in store.calls if c[0] == "requeue"
    ] == [("uuid-1", "trend", 2)]
    (record,) = [r for r in caplog.records if "before a session" in r.getMessage()]
    assert record.json_fields["reason"] == "session_not_started"


@pytest.mark.parametrize(
    "where,exc",
    [
        ("get", api_exceptions.NotFound("no such engine")),
        ("create", api_exceptions.PermissionDenied("nope")),
        ("create", ValueError("bad state")),
    ],
)
def test_permanent_error_before_a_session_fails(monkeypatch, where, exc):
    store = FakeStatusStore(monkeypatch, _TREND)
    _vertex(monkeypatch, **{f"{where}_exc": exc})
    _run_row()
    assert store.statuses == ["FAILED"]
    assert not [c for c in store.calls if c[0] == "requeue"]


def test_transient_error_after_the_session_started_fails(monkeypatch):
    """A run that actually started is FAILED, never re-queued."""
    store = FakeStatusStore(monkeypatch, _TREND)
    _agent_run(monkeypatch, exc=api_exceptions.ServiceUnavailable("mid-run"))
    _run_row()
    assert store.statuses == ["FAILED"]
    assert not [c for c in store.calls if c[0] == "requeue"]


def test_create_agent_run_marks_only_pre_session_errors(monkeypatch):
    _vertex(monkeypatch, create_exc=TimeoutError())
    with pytest.raises(worker.SessionNotStarted) as info:
        asyncio.run(worker.create_agent_run("1", {**_TREND}, "u", deadline=1e9))
    assert isinstance(info.value.__cause__, TimeoutError)


# --- the legacy message (no row key) ----------------------------------------
def test_legacy_message_locks_by_timestamp_once_then_uses_the_real_key(
    monkeypatch, caplog
):
    legacy = {k: v for k, v in _TREND.items() if k not in ("uuid", "target_trend")}
    store = FakeStatusStore(
        monkeypatch, _TREND, row=row_for(_TREND, uuid="uuid-real", target_trend="t")
    )
    _agent_run(monkeypatch)
    with caplog.at_level(logging.WARNING):
        _run_row(legacy)
    (_, lock_key), (_, read_key, _) = store.calls[:2]
    assert lock_key.legacy and read_key.legacy
    assert lock_key.entry_timestamp == TS
    (mark,) = [c for c in store.calls if c[0] == "mark_final"]
    assert (mark[1].uuid, mark[1].target_trend) == ("uuid-real", "t")
    assert "legacy_row_key" in [
        getattr(r, "json_fields", {}).get("reason") for r in caplog.records
    ]


# ============================================================
# End to end through the real statements (BigQuery mocked at query_and_wait)
# ============================================================
def _bq_row_for_lock():
    row = row_for(_TREND)
    row["crf_sensitive"] = False
    return row


def test_late_worker_after_a_reaper_requeue_writes_nothing(monkeypatch, caplog):
    """lock (attempt 1) → re-read → finished run → the PROCESSED write finds
    the row re-queued/re-locked (0 rows): no exception, no FAILED, logged."""
    bq = bq_results(queued(0), 1, [_bq_row_for_lock()], 0)
    _agent_run(monkeypatch)
    with caplog.at_level(logging.WARNING):
        asyncio.run(worker.process_row(_TREND, "agent-123", bq, DS, TBL, deadline=1e9))
    calls = bq.query_and_wait.call_args_list
    assert len(calls) == 4  # no retry, no FAILED write
    final = calls[3]
    assert _params(final)["status"].value == "PROCESSED"
    assert _params(final)["attempt"].value == 1
    assert "Lost fence" in caplog.text


def test_two_rows_on_the_lock_nack(monkeypatch):
    """M10 through the worker: an ambiguous lock raises (NACK), nothing runs."""
    bq = bq_results(queued(0), 2)
    calls = _agent_run(monkeypatch)
    with pytest.raises(status_store.AmbiguousRowKeyError):
        asyncio.run(worker.process_row(_TREND, "agent-123", bq, DS, TBL, deadline=1e9))
    assert calls == []
