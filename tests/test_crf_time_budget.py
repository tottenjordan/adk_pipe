"""CRF worker time budget and cleanup (code review R2-delete / R4 / C6 / N3 / N4 / N6).

- the run deadline is taken at request start (Cloud Run's timeout counts from
  there) and includes the safety margin;
- the session delete is best effort: bounded, logged, never raised, so it can't
  turn a finished run into FAILED (the review's experiment b);
- streamed events are logged compactly (text cut, no whole events);
- the session user id names the row, the kick-off message has no indentation,
  and the SDK's ``aclose`` is a coroutine.

Imports only the CRF package (the crf-deps CI job runs this file in a venv with
nothing but ``cloud_functions/creative_fanout/requirements.txt`` + pytest).
"""

import asyncio
import base64
import inspect
import json
import logging
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud_functions.creative_fanout import (
    clients,
    logs,
    main,
    session,
    status_store,
    worker,
)
from cloud_functions.creative_fanout.config import config
from tests.crf_fakes import FakeStatusStore

pytestmark = pytest.mark.usefixtures("gcp_project_env")

_ROW = {
    "entry_timestamp": "2026-10-13T00:00:00",
    "index": 0,
    "brand": "BrandX",
    "target_product": "prod",
    "key_selling_point": "ksp",
    "target_audience": "aud",
    "target_search_trend": "trend",
}


def _event(row=None):
    payload = {
        "bq_dataset": config.BQ_DATASET_ID,
        "bq_table": config.BQ_TABLE_TARGETS,
        "agent_resource_id": "123",
        "row_data": row or _ROW,
    }
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return types.SimpleNamespace(data={"message": {"data": data}})


# ============================================================
# The deadline: request start + worker timeout - safety margin
# ============================================================
def test_run_deadline_subtracts_the_safety_margin(monkeypatch):
    monkeypatch.setenv("CRF_WORKER_TIMEOUT_SECS", "1800")
    monkeypatch.setenv("CRF_BUDGET_SAFETY_SECS", "200")
    assert worker.run_deadline(100.0) == 100.0 + 1800.0 - 200.0


def test_entrypoint_takes_the_deadline_at_request_start(monkeypatch):
    """The lock (and anything before the run) eats into the budget: the
    deadline the run gets is measured from the entry point's first line."""
    clock = {"now": 5000.0}
    monkeypatch.setattr(worker, "_clock", lambda: clock["now"])

    def _slow_client():
        clock["now"] += 30.0  # building the BigQuery client (first use)
        return MagicMock()

    monkeypatch.setattr(clients, "get_bigquery_client", _slow_client)

    def _slow_lock(*args, **kwargs):
        clock["now"] += 120.0  # the lock UPDATE took two minutes
        return 1

    FakeStatusStore(monkeypatch, _ROW)
    monkeypatch.setattr(status_store, "acquire_processing_lock", _slow_lock)
    seen = {}

    async def _run(**kwargs):
        seen.update(kwargs)
        from cloud_functions.creative_fanout.completion import RunOutcome

        return RunOutcome(True, False, "done")

    monkeypatch.setattr(worker, "create_agent_run", _run)

    main.agent_worker_entrypoint(_event())

    expected = 5000.0 + config.WORKER_TIMEOUT_SECS - config.BUDGET_SAFETY_SECS
    assert seen["deadline"] == expected
    assert config.BUDGET_SAFETY_SECS > 0  # the margin is really subtracted


def test_entrypoint_passes_the_deadline_to_process_row(monkeypatch):
    monkeypatch.setattr(worker, "_clock", lambda: 42.0)
    monkeypatch.setattr(clients, "get_bigquery_client", MagicMock)
    seen = {}

    async def _process_row(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(worker, "process_row", _process_row)
    main.agent_worker_entrypoint(_event())
    assert seen["deadline"] == worker.run_deadline(42.0)


# ============================================================
# Best-effort session delete (R2): never replaces the result
# ============================================================
class _Agent:
    def __init__(self, delete):
        self.delete = delete
        self.deletes = 0

    async def async_create_session(self, *, user_id, state=None):
        return {"id": "sess-1"}

    async def async_delete_session(self, *, user_id, session_id):
        self.deletes += 1
        await self.delete()

    async def async_stream_query(self, **kwargs):
        yield {"author": "root_agent", "content": {"parts": [{"text": "ok"}]}}

    async def async_get_session(self, **kwargs):
        return {"id": "sess-1", "state": {"finalize_done": True}, "events": []}


async def _raise_500():
    raise RuntimeError("500 delete failed")


def _wire_agent(monkeypatch, agent):
    fake_vertex = MagicMock()
    fake_vertex.runtimes.get.return_value = agent
    fake_vertex.aio.aclose = AsyncMock()
    monkeypatch.setattr(clients, "new_vertex_client", lambda: fake_vertex)


def test_delete_failure_keeps_a_finalized_run_processed(monkeypatch, caplog):
    """The review's experiment b: finalize_done is true and the delete raises
    → the row is still PROCESSED (the delete is cleanup, not the result)."""
    agent = _Agent(_raise_500)
    _wire_agent(monkeypatch, agent)
    store = FakeStatusStore(monkeypatch, _ROW)

    with caplog.at_level(logging.WARNING):
        asyncio.run(
            worker.process_row(_ROW, "123", MagicMock(), "ds", "t", deadline=1e12)
        )

    assert store.statuses == ["PROCESSED"]
    assert agent.deletes == 1
    assert "500 delete failed" in caplog.text


def test_delete_that_hangs_times_out_without_changing_the_result(monkeypatch):
    async def _hang():
        await asyncio.sleep(10)

    agent = _Agent(_hang)
    _wire_agent(monkeypatch, agent)
    monkeypatch.setattr(config, "SESSION_DELETE_TIMEOUT_SECS", 0.05)
    outcome = asyncio.run(
        worker.create_agent_run("123", dict(_ROW), "u", deadline=1e12)
    )
    assert outcome.finalized
    assert agent.deletes == 1


def test_delete_timeout_capped_at_30_seconds(monkeypatch):
    seen = {}

    class _Timeout:
        def __init__(self, delay):
            seen["delay"] = delay

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(session.asyncio, "timeout", _Timeout)

    async def _ok():
        return None

    assert asyncio.run(session.delete_session_best_effort(_Agent(_ok), "u", "s", 999.0))
    assert seen["delay"] == 30.0


def test_body_exception_still_propagates_and_delete_failure_is_swallowed():
    agent = _Agent(_raise_500)

    async def _run():
        async with session.agent_session(agent, "u"):
            raise ValueError("body boom")

    with pytest.raises(ValueError, match="body boom"):
        asyncio.run(_run())
    assert agent.deletes == 1


def test_delete_does_not_swallow_cancellation():
    async def _cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            session.delete_session_best_effort(_Agent(_cancelled), "u", "s", 1.0)
        )


# ============================================================
# Log volume (C6)
# ============================================================
def test_pretty_print_cuts_long_text(caplog):
    text = "x" * 5000
    with caplog.at_level(logging.INFO):
        logs.pretty_print_event(
            {"author": "root_agent", "content": {"parts": [{"text": text}]}}
        )
    (record,) = caplog.records
    assert len(record.getMessage()) <= logs.MAX_TEXT_CHARS + len("[root_agent]: ")
    assert record.getMessage().endswith("...")
    assert record.json_fields["event_author"] == "root_agent"
    assert record.json_fields["event_kind"] == "text"
    assert record.json_fields["text_chars"] == 5000


def test_pretty_print_short_text_is_kept_whole(caplog):
    with caplog.at_level(logging.INFO):
        logs.pretty_print_event({"author": "a", "content": {"parts": [{"text": "hi"}]}})
    assert caplog.records[0].getMessage() == "[a]: hi"


def test_pretty_print_never_dumps_state_delta(caplog):
    brief = "SECRET BRIEF " * 1000
    keys = {f"key_{i:02d}": brief for i in range(15)}
    event = {"author": "brief_writer", "actions": {"state_delta": keys}}
    with caplog.at_level(logging.INFO):
        logs.pretty_print_event(event)
    (record,) = caplog.records
    message = record.getMessage()
    assert "SECRET BRIEF" not in message
    assert "key_00" in message and "(+5 more)" in message
    assert record.json_fields["event_kind"] == "no_content"
    assert record.json_fields["event_author"] == "brief_writer"
    assert len(record.json_fields["state_keys"]) == logs.MAX_STATE_KEYS


def test_pretty_print_tool_parts_carry_kind(caplog):
    event = {
        "author": "root_agent",
        "content": {
            "parts": [
                {"function_call": {"name": "p", "args": {"a": "y" * 500}}},
                {"function_response": {"name": "p", "response": {"r": "z" * 500}}},
            ]
        },
    }
    with caplog.at_level(logging.INFO):
        logs.pretty_print_event(event)
    kinds = [r.json_fields["event_kind"] for r in caplog.records]
    assert kinds == ["function_call", "function_response"]
    assert all(len(r.getMessage()) < 200 for r in caplog.records)


def test_pretty_print_uses_the_module_logger(caplog):
    with caplog.at_level(logging.INFO):
        logs.pretty_print_event({"author": "a", "content": {"parts": [{"text": "x"}]}})
    assert caplog.records[0].name == logs.logger.name != "root"


# ============================================================
# Nits: row-identity user id (N4), no prompt indentation (N3), aclose (N6)
# ============================================================
def test_session_user_id_names_the_row():
    row = {**_ROW, "uuid": "u-1", "target_trend": "Marathon"}
    other = {**row, "index": 2}
    assert worker.session_user_id(row) == worker.session_user_id(other)
    assert worker.session_user_id(row).startswith(f"{config.AGENT_WORKER_USER_ID}_")
    assert worker.session_user_id(row) != worker.session_user_id(
        {**row, "target_trend": "Other"}
    )
    assert worker.session_user_id(row) != f"{config.AGENT_WORKER_USER_ID}_0"


def test_session_user_id_matches_stable_row_id():
    """Same hash as agent_common.idempotency.stable_row_id(uuid, trend, length=12)
    (recomputed here: the crf-deps venv has no agent_common)."""
    import hashlib

    row = {**_ROW, "uuid": "u-1", "target_trend": "Marathon"}
    digest = hashlib.sha256(json.dumps(["u-1", "Marathon"]).encode()).hexdigest()
    assert worker.session_user_id(row) == f"crf_worker_{digest[:12]}"


@pytest.mark.parametrize(
    "extra",
    [{}, {"uuid": "u-1"}, {"target_trend": "t"}, {"uuid": " ", "target_trend": "t"}],
)
def test_session_user_id_falls_back_to_the_index(extra):
    assert worker.session_user_id({**_ROW, "index": 3, **extra}) == "crf_worker_3"


def test_process_row_uses_the_row_identity_user_id(monkeypatch):
    seen = {}

    async def _run(**kwargs):
        seen.update(kwargs)
        from cloud_functions.creative_fanout.completion import RunOutcome

        return RunOutcome(True, False, "done")

    monkeypatch.setattr(worker, "create_agent_run", _run)
    row = {**_ROW, "uuid": "u-1", "target_trend": "Marathon"}
    FakeStatusStore(monkeypatch, row)
    asyncio.run(worker.process_row(row, "1", MagicMock(), "ds", "t", deadline=9.0))
    assert seen["user_id"] == worker.session_user_id(row)
    assert seen["deadline"] == 9.0


def test_user_query_lines_have_no_indentation():
    query = worker.build_user_query(_ROW)
    assert query.splitlines() == [
        "Brand: BrandX",
        "Target Product: prod",
        "Key Selling Point(s): ksp",
        "Target Audience: aud",
        "Target Search Trend: trend",
    ]


def test_sdk_aclose_is_a_coroutine_function():
    """clients.aclose_vertex_client awaits ``client.aio.aclose()`` directly."""
    from agentplatform._genai.client import AsyncClient, Client

    assert inspect.iscoroutinefunction(AsyncClient.aclose)
    assert inspect.signature(Client.aio.fget).return_annotation in (
        AsyncClient,
        "AsyncClient",
    )


def test_aclose_vertex_client_awaits_the_close():
    client = MagicMock()
    client.aio.aclose = AsyncMock()
    asyncio.run(clients.aclose_vertex_client(client))
    client.aio.aclose.assert_awaited_once()
