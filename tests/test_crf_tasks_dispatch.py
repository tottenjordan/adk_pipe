"""The orchestrator's Cloud Tasks transport (``CRF_WORKER_TRANSPORT=tasks``).

One HTTP task per selected row (OIDC to the worker URL, the worker message as
the JSON body, a deterministic name per row attempt so a re-dispatch of the
same attempt is de-duplicated by Cloud Tasks), created only after the rows are
marked QUEUED. ``AlreadyExists`` counts as dispatched; any other error is
logged and leaves the row QUEUED for the next trigger.
"""

import base64
import hashlib
import json
import logging
import threading
import types
from datetime import UTC, datetime, timedelta

import pytest
from google.api_core import exceptions as gexc

from cloud_functions.creative_fanout import clients, main, orchestrator, status_store
from cloud_functions.creative_fanout.config import config
from tests.crf_fakes import FakeTasksClient, bq_results

pytestmark = pytest.mark.usefixtures("gcp_project_env", "crf_engine_env")

DS = config.BQ_DATASET_ID
TBL = config.BQ_TABLE_TARGETS
URL = "https://creative-worker-crf-abc-uc.a.run.app"
INVOKER = "invoker@test-project.iam.gserviceaccount.com"
PARENT = "projects/test-project/locations/us-central1/queues/creative-worker-queue"
TS = datetime(2026, 10, 13, tzinfo=UTC)


def _bq_row(trend, uuid, attempts=None, ts=TS):
    return {
        "uuid": uuid,
        "entry_timestamp": ts,
        "target_trend": trend,
        "brand": "B",
        "target_audience": "a",
        "target_product": "p",
        "key_selling_point": "k",
        "processing_attempts": attempts,
    }


ROWS = [_bq_row("trend A", "u-a"), _bq_row("trend B", "u-b", attempts=2)]


def _trigger():
    payload = {"bq_dataset": DS, "bq_table": TBL}
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return types.SimpleNamespace(data={"message": {"data": data}})


def _expected_name(*parts):
    digest = hashlib.sha256(json.dumps(list(parts)).encode()).hexdigest()
    return f"{PARENT}/tasks/crf-{digest[:32]}"


@pytest.fixture
def tasks_env(monkeypatch):
    monkeypatch.setenv("CRF_WORKER_TRANSPORT", "tasks")
    monkeypatch.setenv("CRF_WORKER_URL", URL)
    monkeypatch.setenv("CRF_TASKS_INVOKER_SA", INVOKER)
    for name in ("CRF_TASKS_QUEUE", "CRF_TASKS_LOCATION", "GCP_REGION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CRF_TASK_DISPATCH_DEADLINE_SECS", raising=False)
    monkeypatch.delenv("CRF_WORKER_TIMEOUT_SECS", raising=False)
    return monkeypatch


def _wire(monkeypatch, rows, errors=None):
    """reap (0) -> SELECT (rows) -> QUEUED mark, all logged in one order."""
    events: list = []
    bq = bq_results(0, list(rows), len(rows))
    results = bq.query_and_wait.side_effect

    def _query(sql, **kw):
        events.append(("bq", " ".join(sql.split())))
        return next(results)

    bq.query_and_wait.side_effect = _query
    tasks = FakeTasksClient(errors=errors, events=events)
    publisher = types.SimpleNamespace(
        publish=lambda *a, **k: pytest.fail("tasks transport must not publish")
    )
    monkeypatch.setattr(clients, "get_bigquery_client", lambda: bq)
    monkeypatch.setattr(clients, "get_pubsub_client", lambda: publisher)
    monkeypatch.setattr(clients, "get_tasks_client", lambda: tasks)
    return events, tasks


def test_one_task_per_row_with_oidc_body_and_deadline(tasks_env):
    _, tasks = _wire(tasks_env, ROWS)
    main.crf_entrypoint(_trigger())

    assert len(tasks.requests) == 2
    for request, row in zip(tasks.requests, ROWS, strict=True):
        assert request["parent"] == PARENT
        task = request["task"]
        assert task["dispatch_deadline"] == timedelta(seconds=1800)
        http = task["http_request"]
        assert http["url"] == URL
        assert http["http_method"] == orchestrator.tasks_v2.HttpMethod.POST
        assert http["headers"] == {"Content-Type": "application/json"}
        assert http["oidc_token"] == {
            "service_account_email": INVOKER,
            "audience": URL,
        }
        body = json.loads(http["body"])
        assert set(body) == {"bq_dataset", "bq_table", "row_data"}
        assert (body["bq_dataset"], body["bq_table"]) == (DS, TBL)
        assert body["row_data"]["uuid"] == row["uuid"]
        assert body["row_data"]["target_trend"] == row["target_trend"]
    assert tasks.timeouts == [30, 30]


def test_task_body_equals_the_pubsub_message(tasks_env):
    """Both transports send the same worker message (one shared builder)."""
    _, tasks = _wire(tasks_env, ROWS[:1])
    main.crf_entrypoint(_trigger())
    body = json.loads(tasks.requests[0]["task"]["http_request"]["body"])
    from cloud_functions.creative_fanout.messages import build_row_dict

    assert body == orchestrator._worker_payload(DS, TBL, build_row_dict(0, ROWS[0]))


def test_task_names_are_deterministic_per_row_attempt(tasks_env):
    _, tasks = _wire(tasks_env, ROWS)
    main.crf_entrypoint(_trigger())
    names = [r["task"]["name"] for r in tasks.requests]
    assert names == [
        _expected_name("u-a", "trend A", 0),  # NULL attempts -> 0
        _expected_name("u-b", "trend B", 2),
    ]


def test_task_name_changes_with_the_attempt(tasks_env):
    """A reaped/requeued row (attempts bumped by its lock) gets a new task;
    the same attempt re-dispatched gets the same name (de-duplicated)."""
    names = [
        orchestrator.task_name(PARENT, _bq_row("t", "u", attempts=a))
        for a in (None, 0, 1, 2)
    ]
    assert names[0] == names[1]
    assert len(set(names[1:])) == 3


def test_task_name_separates_rows_sharing_a_timestamp(tasks_env):
    a = orchestrator.task_name(PARENT, _bq_row("trend A", "u-a"))
    b = orchestrator.task_name(PARENT, _bq_row("trend B", "u-b"))
    assert a != b


def test_legacy_row_without_key_names_its_task_by_timestamp(tasks_env):
    legacy = _bq_row("trend L", None)
    assert orchestrator.task_name(PARENT, legacy) == _expected_name(TS.isoformat(), 0)


def test_task_id_is_a_valid_cloud_tasks_id(tasks_env):
    name = orchestrator.task_name(PARENT, ROWS[0])
    task_id = name.rsplit("/", 1)[1]
    assert len(task_id) <= 500
    assert all(c.isalnum() or c in "-_" for c in task_id)


def test_rows_marked_queued_before_the_first_task(tasks_env):
    events, _ = _wire(tasks_env, ROWS)
    main.crf_entrypoint(_trigger())
    kinds = [e[0] for e in events]
    assert kinds == ["bq", "bq", "bq", "create_task", "create_task"]
    assert "SET processed_status = 'QUEUED'" in events[2][1]


def test_queued_mark_failure_creates_no_task(tasks_env):
    bq = bq_results(0, list(ROWS), RuntimeError("bq down"))
    tasks = FakeTasksClient()
    tasks_env.setattr(clients, "get_bigquery_client", lambda: bq)
    tasks_env.setattr(clients, "get_tasks_client", lambda: tasks)
    with pytest.raises(RuntimeError):
        main.crf_entrypoint(_trigger())  # NACK: the trigger is retried
    assert tasks.requests == []


def test_already_exists_counts_as_dispatched(tasks_env, caplog):
    """The same row attempt already has a task (an orphaned QUEUED row
    re-selected while its task waits in the queue): not a second task, not a
    failure."""
    first = _expected_name("u-a", "trend A", 0)
    _, tasks = _wire(tasks_env, ROWS, errors={first: gexc.AlreadyExists("dup")})
    with caplog.at_level(logging.WARNING):
        sent = orchestrator.dispatch({"bq_dataset": DS, "bq_table": TBL})
    assert sent == 2
    assert "failed" not in caplog.text
    assert len(tasks.requests) == 2  # one attempt each, no retry under a new name


def test_other_create_errors_are_counted_and_leave_the_row_queued(tasks_env, caplog):
    first = _expected_name("u-a", "trend A", 0)
    events, tasks = _wire(
        tasks_env, ROWS, errors={first: gexc.ServiceUnavailable("tasks down")}
    )
    with caplog.at_level(logging.WARNING):
        sent = orchestrator.dispatch({"bq_dataset": DS, "bq_table": TBL})
    assert sent == 1
    assert len(tasks.requests) == 2  # the batch continues past a failure
    assert "1/2 worker tasks failed" in caplog.text
    # No status write after the QUEUED mark: the failed row stays QUEUED.
    assert [e[0] for e in events].count("bq") == 3


def test_every_create_failing_does_not_raise(tasks_env):
    _, tasks = _wire(tasks_env, ROWS, errors={"*": RuntimeError("boom")})
    assert orchestrator.dispatch({"bq_dataset": DS, "bq_table": TBL}) == 0
    assert len(tasks.requests) == 2


def test_queue_location_and_deadline_from_config(tasks_env):
    tasks_env.setenv("CRF_TASKS_QUEUE", "q2")
    tasks_env.setenv("CRF_TASKS_LOCATION", "europe-west1")
    tasks_env.setenv("CRF_TASK_DISPATCH_DEADLINE_SECS", "900")
    _, tasks = _wire(tasks_env, ROWS[:1])
    main.crf_entrypoint(_trigger())
    request = tasks.requests[0]
    assert request["parent"] == "projects/test-project/locations/europe-west1/queues/q2"
    assert request["task"]["name"].startswith(request["parent"] + "/tasks/crf-")
    assert request["task"]["dispatch_deadline"] == timedelta(seconds=900)


def test_pubsub_transport_creates_no_task(monkeypatch):
    monkeypatch.delenv("CRF_WORKER_TRANSPORT", raising=False)
    bq = bq_results(0, list(ROWS), 2)
    published = []

    class _Publisher:
        def publish(self, topic, data):
            published.append(json.loads(data))
            return types.SimpleNamespace(result=lambda timeout=None: "id")

    monkeypatch.setattr(clients, "get_bigquery_client", lambda: bq)
    monkeypatch.setattr(clients, "get_pubsub_client", lambda: _Publisher())
    monkeypatch.setattr(
        clients, "get_tasks_client", lambda: pytest.fail("pubsub must not use tasks")
    )
    assert orchestrator.dispatch({"bq_dataset": DS, "bq_table": TBL}) == 2
    assert [p["row_data"]["uuid"] for p in published] == ["u-a", "u-b"]


def test_tasks_without_url_falls_back_to_pubsub(tasks_env):
    tasks_env.delenv("CRF_WORKER_URL")
    assert config.WORKER_TRANSPORT == "pubsub"


def test_select_unprocessed_returns_processing_attempts():
    """The task name needs the row's attempt count: the SELECT returns it."""
    sql, _ = status_store._build_select_unprocessed_sql("p", DS, TBL, 3)
    head = sql.split("FROM", 1)[0]
    assert "SELECT *" in head or "processing_attempts" in head


def test_tasks_client_is_a_lazy_thread_safe_singleton(monkeypatch):
    built = []

    def _ctor():
        built.append(1)
        return object()

    monkeypatch.setattr(clients.tasks_v2, "CloudTasksClient", _ctor)
    monkeypatch.setattr(clients, "_tasks_client", None)
    got = []
    threads = [
        threading.Thread(target=lambda: got.append(clients.get_tasks_client()))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1
    assert len({id(g) for g in got}) == 1
