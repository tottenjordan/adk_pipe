"""Shared fakes for the CRF worker tests (not a test module).

``FakeStatusStore`` replaces the row-state statements ``worker.process_row``
calls (``status_store.acquire_processing_lock`` / ``read_locked_row`` /
``mark_final`` / ``requeue``) and records them, so worker tests can drive the
lock -> re-read -> run -> status flow without BigQuery. Importable in the CRF
CI's clean venv (only the Cloud Function's requirements + pytest).
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from cloud_functions.creative_fanout import status_store

# message key -> BigQuery column, for the row the fake re-read returns
_ROW_COLUMNS = {
    "uuid": "uuid",
    "target_search_trend": "target_trend",
    "brand": "brand",
    "target_product": "target_product",
    "key_selling_point": "key_selling_point",
    "target_audience": "target_audience",
    "refresh_date": "refresh_date",
    "trend_context": "trend_context",
}


class _Result(list):
    """A ``query_and_wait`` result: iterable rows + ``num_dml_affected_rows``."""

    num_dml_affected_rows = None


def bq_results(*results):
    """A BigQuery client mock whose successive ``query_and_wait`` calls return
    ``results`` in order: a list = the SELECT's rows, an int = a DML's
    ``num_dml_affected_rows``, an exception = raised."""
    out = []
    for r in results:
        if isinstance(r, BaseException):
            out.append(r)
        elif isinstance(r, int):
            res = _Result()
            res.num_dml_affected_rows = r
            out.append(res)
        else:
            out.append(_Result(r))
    bq = MagicMock()
    bq.project = "test-project"
    bq.query_and_wait.side_effect = out
    bq.query.side_effect = AssertionError("use query_and_wait (bounded wait)")
    return bq


def queued(attempts=None) -> list[dict]:
    """The lock's pre-read result for a QUEUED row with ``attempts``."""
    return [{"processed_status": "QUEUED", "processing_attempts": attempts}]


def row_for(trend: dict, **overrides) -> dict:
    """The BigQuery row (as a mapping) a worker message ``trend`` names."""
    ts = datetime.fromisoformat(trend["entry_timestamp"])
    row = {"entry_timestamp": ts if ts.tzinfo else ts.replace(tzinfo=UTC)}
    for msg_key, column in _ROW_COLUMNS.items():
        row[column] = trend.get(msg_key)
    row["target_trend"] = trend.get("target_trend", row["target_trend"])
    row["crf_sensitive"] = False
    row.update(overrides)
    return row


class FakeStatusStore:
    """Records every row-state call; configurable lock / re-read / write results.

    ``row="auto"`` re-reads ``row_for(trend)``; ``None`` = the row is gone.
    ``mark_errors`` is a list of exceptions raised by successive ``mark_final``
    calls (``None`` entries succeed).
    """

    def __init__(
        self,
        monkeypatch,
        trend=None,
        *,
        attempt=1,
        row="auto",
        mark_errors=(),
        fence=True,
    ):
        self.trend = trend
        self.attempt = attempt
        self.row = row
        self.mark_errors = list(mark_errors)
        self.fence = fence
        self.calls: list[tuple] = []
        monkeypatch.setattr(status_store, "acquire_processing_lock", self._lock)
        monkeypatch.setattr(status_store, "read_locked_row", self._read)
        monkeypatch.setattr(status_store, "mark_final", self._mark)
        monkeypatch.setattr(status_store, "requeue", self._requeue)

    def _lock(self, bq, dataset, table, key):
        self.calls.append(("lock", key))
        return self.attempt

    def _read(self, bq, dataset, table, key, attempt):
        self.calls.append(("read", key, attempt))
        if self.row != "auto":
            return self.row
        row = row_for(self.trend)
        row["uuid"] = row["uuid"] or "uuid-1"
        return row

    def _mark(self, bq, dataset, table, key, status, attempt):
        self.calls.append(("mark_final", key, status, attempt))
        if self.mark_errors:
            err = self.mark_errors.pop(0)
            if err is not None:
                raise err
        return self.fence

    def _requeue(self, bq, dataset, table, key, attempt):
        self.calls.append(("requeue", key, attempt))
        return "QUEUED" if self.fence else None

    @property
    def statuses(self) -> list[str]:
        """The final statuses written, in order (incl. failed attempts)."""
        return [c[2] for c in self.calls if c[0] == "mark_final"]


class FakeTasksClient:
    """A Cloud Tasks client stand-in: records every ``create_task`` call.

    Each request is validated by building the real ``CreateTaskRequest``, so a
    request shape the API would reject fails the test. ``errors`` maps a task
    name (or ``"*"`` for every call) to the exception ``create_task`` raises;
    ``events`` (optional) is a shared, ordered call log.
    """

    def __init__(self, errors=None, events=None):
        self.errors = dict(errors or {})
        self.events = events if events is not None else []
        self.requests: list[dict] = []
        self.timeouts: list = []

    def create_task(self, request=None, *, timeout=None, **_):
        from google.cloud import tasks_v2

        tasks_v2.CreateTaskRequest(request)  # raises on a malformed request
        self.requests.append(request)
        self.timeouts.append(timeout)
        name = request["task"]["name"]
        self.events.append(("create_task", name))
        err = self.errors.get(name) or self.errors.get("*")
        if err is not None:
            raise err
        return request["task"]
