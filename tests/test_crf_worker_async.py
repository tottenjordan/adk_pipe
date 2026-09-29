"""Tests for the async worker path of the CRF (issue #45).

Guard the fix for #45: a streaming failure inside `async_send_message` must
propagate so `_execute_agent_and_update_status` marks the row `FAILED` instead
of silently marking it `PROCESSED`. Once FAILED is written the worker returns
(ACK) rather than re-raising: a redelivery can't re-acquire the QUEUED->PROCESSING
lock on a FAILED row, so a NACK would only add a no-op retry. Coroutines are driven with `asyncio.run`
(no pytest-asyncio in this project).
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from cloud_functions.creative_fanout import main
from cloud_functions.creative_fanout.session import agent_session

# The CRF config reads the (required) project at use time; pin a dummy.
pytestmark = pytest.mark.usefixtures("gcp_project_env")


def test_async_send_message_reraises_streaming_error():
    """A failure while streaming events must propagate, not be swallowed."""

    async def _raising_stream(**kwargs):
        raise RuntimeError("stream boom")
        yield  # pragma: no cover - makes this an async generator

    remote_agent = MagicMock()
    remote_agent.async_stream_query = _raising_stream
    session = {"id": "sess-1"}

    with pytest.raises(RuntimeError, match="stream boom"):
        asyncio.run(main.async_send_message(remote_agent, "user-1", session, "hi"))


def test_streaming_error_marks_row_failed_end_to_end(monkeypatch):
    """End-to-end: a streaming failure marks the row FAILED (never PROCESSED)
    and, once FAILED is durably written, returns instead of re-raising."""

    async def _raising_stream(**kwargs):
        raise RuntimeError("stream boom")
        yield  # pragma: no cover - makes this an async generator

    async def _create_session(**kwargs):
        return {"id": "sess-1"}

    async def _delete_session(**kwargs):
        return None

    remote_agent = MagicMock()
    remote_agent.async_stream_query = _raising_stream
    remote_agent.async_create_session = _create_session
    remote_agent.async_delete_session = _delete_session

    fake_vertex = MagicMock()
    fake_vertex.runtimes.get.return_value = remote_agent
    monkeypatch.setattr(main, "_get_vertex_client", lambda: fake_vertex)

    # Win the lock so we proceed into the agent run.
    monkeypatch.setattr(main, "acquire_processing_lock", lambda *a, **k: True)
    update_mock = MagicMock()
    monkeypatch.setattr(main, "update_rows_status", update_mock)

    trend = {
        "entry_timestamp": "2026-07-12T00:00:00",
        "index": 0,
        "brand": "BrandX",
        "target_product": "prod",
        "key_selling_point": "ksp",
        "target_audience": "aud",
        "target_search_trend": "trend",
    }
    bq = MagicMock()

    # FAILED was written successfully → return normally so the worker message is
    # ACKed (a redelivery could never re-lock a FAILED row; it'd be a no-op).
    asyncio.run(
        main._execute_agent_and_update_status(trend, "agent-123", bq, "ds", "tbl")
    )

    statuses = [c.kwargs.get("status") for c in update_mock.call_args_list]
    assert "FAILED" in statuses
    assert "PROCESSED" not in statuses


def test_session_created_and_deleted_with_same_user_id(monkeypatch):
    """The session must be deleted with the SAME user_id it was created with.

    Regression for the Agent Engine `FAILED_PRECONDITION: Session <id> does not
    belong to user <...>` error: `create_agent_run` creates the session with a
    per-row user id (e.g. ``crf_worker_0``) but `my_delete_task` used the
    bare module constant ``_USER_ID`` (``crf_worker``), so the delete never
    matched the session owner.
    """

    created = {}
    deleted = {}

    async def _create_session(*, user_id):
        created["user_id"] = user_id
        return {"id": "sess-42"}

    async def _stream(**kwargs):
        return
        yield  # pragma: no cover - makes this an async generator

    async def _delete_session(*, user_id, session_id):
        deleted["user_id"] = user_id
        deleted["session_id"] = session_id

    remote_agent = MagicMock()
    remote_agent.async_create_session = _create_session
    remote_agent.async_stream_query = _stream
    remote_agent.async_delete_session = _delete_session

    fake_vertex = MagicMock()
    fake_vertex.runtimes.get.return_value = remote_agent
    monkeypatch.setattr(main, "_get_vertex_client", lambda: fake_vertex)

    msg = {
        "index": 0,
        "brand": "BrandX",
        "target_product": "prod",
        "key_selling_point": "ksp",
        "target_audience": "aud",
        "target_search_trend": "trend",
    }
    user_id = f"{main._USER_ID}_{msg['index']}"

    asyncio.run(
        main.create_agent_run(agent_id="agent-123", msg_dict=msg, user_id=user_id)
    )

    assert created["user_id"] == user_id
    assert deleted["user_id"] == user_id
    assert deleted["session_id"] == "sess-42"


def test_agent_session_deletes_on_success():
    """The context manager creates then deletes with the same user_id/session_id."""

    created = {}
    deleted = {}

    async def _create_session(*, user_id):
        created["user_id"] = user_id
        return {"id": "sess-99"}

    async def _delete_session(*, user_id, session_id):
        deleted["user_id"] = user_id
        deleted["session_id"] = session_id

    remote_agent = MagicMock()
    remote_agent.async_create_session = _create_session
    remote_agent.async_delete_session = _delete_session

    async def _run():
        async with agent_session(remote_agent, "user-7") as session:
            assert session["id"] == "sess-99"

    asyncio.run(_run())

    assert created["user_id"] == "user-7"
    assert deleted == {"user_id": "user-7", "session_id": "sess-99"}


def test_agent_session_deletes_on_error():
    """The session is deleted with the creating user_id even when the body raises."""

    deleted = {}

    async def _create_session(*, user_id):
        return {"id": "sess-err"}

    async def _delete_session(*, user_id, session_id):
        deleted["user_id"] = user_id
        deleted["session_id"] = session_id

    remote_agent = MagicMock()
    remote_agent.async_create_session = _create_session
    remote_agent.async_delete_session = _delete_session

    async def _run():
        async with agent_session(remote_agent, "user-9"):
            raise RuntimeError("body boom")

    with pytest.raises(RuntimeError, match="body boom"):
        asyncio.run(_run())

    # finally: ran despite the error, using the SAME user_id it created with.
    assert deleted == {"user_id": "user-9", "session_id": "sess-err"}


def test_create_agent_run_deletes_session_on_stream_error(monkeypatch):
    """create_agent_run must still delete the session (right user_id) if streaming raises."""

    deleted = {}

    async def _create_session(*, user_id):
        return {"id": "sess-stream"}

    async def _raising_stream(**kwargs):
        raise RuntimeError("stream boom")
        yield  # pragma: no cover - makes this an async generator

    async def _delete_session(*, user_id, session_id):
        deleted["user_id"] = user_id
        deleted["session_id"] = session_id

    remote_agent = MagicMock()
    remote_agent.async_create_session = _create_session
    remote_agent.async_stream_query = _raising_stream
    remote_agent.async_delete_session = _delete_session

    fake_vertex = MagicMock()
    fake_vertex.runtimes.get.return_value = remote_agent
    monkeypatch.setattr(main, "_get_vertex_client", lambda: fake_vertex)

    msg = {
        "index": 3,
        "brand": "BrandX",
        "target_product": "prod",
        "key_selling_point": "ksp",
        "target_audience": "aud",
        "target_search_trend": "trend",
    }
    user_id = f"{main._USER_ID}_{msg['index']}"

    with pytest.raises(RuntimeError, match="stream boom"):
        asyncio.run(
            main.create_agent_run(agent_id="agent-123", msg_dict=msg, user_id=user_id)
        )

    assert deleted == {"user_id": user_id, "session_id": "sess-stream"}


# ============================================================
# Worker error contract (ACK after FAILED is durably written)
# ============================================================
_TREND = {
    "entry_timestamp": "2026-07-12T00:00:00",
    "index": 0,
    "brand": "BrandX",
    "target_product": "prod",
    "key_selling_point": "ksp",
    "target_audience": "aud",
    "target_search_trend": "trend",
}


def _failing_agent_run(monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("agent boom")

    monkeypatch.setattr(main, "create_agent_run", _boom)
    monkeypatch.setattr(main, "acquire_processing_lock", lambda *a, **k: True)


def test_agent_failure_after_failed_write_returns_and_logs(monkeypatch, caplog):
    _failing_agent_run(monkeypatch)
    update_mock = MagicMock()
    monkeypatch.setattr(main, "update_rows_status", update_mock)

    with caplog.at_level("ERROR"):
        # must NOT raise
        asyncio.run(
            main._execute_agent_and_update_status(
                _TREND, "agent-123", MagicMock(), "ds", "tbl"
            )
        )

    assert [c.kwargs["status"] for c in update_mock.call_args_list] == ["FAILED"]
    assert _TREND["entry_timestamp"] in caplog.text
    assert "agent boom" in caplog.text


def test_failed_status_write_error_still_reraises(monkeypatch):
    """If the FAILED write itself fails, the row is stranded in PROCESSING —
    re-raise so Pub/Sub redelivers (and the reaper is the backstop)."""
    _failing_agent_run(monkeypatch)

    def _update(**kwargs):
        raise RuntimeError("bq write boom")

    monkeypatch.setattr(main, "update_rows_status", _update)

    with pytest.raises(RuntimeError, match="bq write boom"):
        asyncio.run(
            main._execute_agent_and_update_status(
                _TREND, "agent-123", MagicMock(), "ds", "tbl"
            )
        )


def test_error_before_status_write_reraises(monkeypatch):
    """An unexpected error before any status write (e.g. the lock query) must
    still propagate so Pub/Sub retries."""

    def _lock(*a, **k):
        raise RuntimeError("lock boom")

    monkeypatch.setattr(main, "acquire_processing_lock", _lock)
    update_mock = MagicMock()
    monkeypatch.setattr(main, "update_rows_status", update_mock)

    with pytest.raises(RuntimeError, match="lock boom"):
        asyncio.run(
            main._execute_agent_and_update_status(
                _TREND, "agent-123", MagicMock(), "ds", "tbl"
            )
        )
    update_mock.assert_not_called()


def test_worker_entrypoint_acks_after_failed_write(monkeypatch):
    """Through the real entrypoint: agent failure + successful FAILED write → ACK."""
    import base64
    import json
    import types

    from cloud_functions.creative_fanout.config import config

    _failing_agent_run(monkeypatch)
    update_mock = MagicMock()
    monkeypatch.setattr(main, "update_rows_status", update_mock)
    monkeypatch.setattr(main, "_get_bigquery_client", MagicMock)

    payload = {
        "bq_dataset": config.BQ_DATASET_ID,
        "bq_table": config.BQ_TABLE_TARGETS,
        "agent_resource_id": "agent-123",
        "row_data": _TREND,
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    main.agent_worker_entrypoint(
        types.SimpleNamespace(data={"message": {"data": encoded}})
    )  # must NOT raise
    assert [c.kwargs["status"] for c in update_mock.call_args_list] == ["FAILED"]


# ============================================================
# Agent Runtime SDK (google-cloud-agentplatform) client surface
# ============================================================
def test_worker_uses_runtimes_api_not_agent_engines(monkeypatch):
    """The worker resolves the engine via ``client.runtimes.get`` — the
    agentplatform 2.x surface — never the deprecated ``agent_engines``."""

    async def _create_session(*, user_id):
        return {"id": "sess-1"}

    async def _stream(**kwargs):
        return
        yield  # pragma: no cover - makes this an async generator

    async def _delete_session(*, user_id, session_id):
        return None

    remote_agent = MagicMock()
    remote_agent.async_create_session = _create_session
    remote_agent.async_stream_query = _stream
    remote_agent.async_delete_session = _delete_session

    # spec'd so touching `.agent_engines` raises AttributeError.
    fake_client = MagicMock(spec=["runtimes"])
    fake_client.runtimes.get.return_value = remote_agent
    monkeypatch.setattr(main, "_get_vertex_client", lambda: fake_client)

    msg = {
        "index": 0,
        "brand": "BrandX",
        "target_product": "prod",
        "key_selling_point": "ksp",
        "target_audience": "aud",
        "target_search_trend": "trend",
    }

    asyncio.run(
        main.create_agent_run(
            agent_id="123", msg_dict=msg, user_id=f"{main._USER_ID}_0"
        )
    )

    name = fake_client.runtimes.get.call_args.kwargs["name"]
    assert name.endswith("/reasoningEngines/123")


def test_vertex_client_is_agentplatform_client(monkeypatch):
    """The lazy client is an ``agentplatform.Client`` on the regional location."""
    ctor = MagicMock()
    monkeypatch.setattr(main.agentplatform, "Client", ctor)
    monkeypatch.setattr(main, "_vertex_client", None)

    main._get_vertex_client()

    ctor.assert_called_once_with(
        project=main.config.GOOGLE_CLOUD_PROJECT, location=main.config.GCP_REGION
    )


# ------------------------------------------------------------
# pretty_print_event: deployed AdkApp engines stream snake_case dicts
# (`function_call`/`function_response`, nulls dropped); camelCase must still work.
# ------------------------------------------------------------


@pytest.mark.parametrize(
    ("call_key", "response_key"),
    [
        ("function_call", "function_response"),
        ("functionCall", "functionResponse"),
    ],
)
def test_pretty_print_event_logs_tool_calls_both_spellings(
    caplog, call_key, response_key
):
    event = {
        "author": "root_agent",
        "content": {
            "parts": [
                {call_key: {"name": "save_to_gcs", "args": {"path": "a"}}},
                {response_key: {"name": "save_to_gcs", "response": {"ok": 1}}},
            ]
        },
    }
    with caplog.at_level("INFO"):
        main.pretty_print_event(event)

    assert "[root_agent]: Function call: save_to_gcs" in caplog.text
    assert '"path": "a"' in caplog.text
    assert "[root_agent]: Function response: save_to_gcs" in caplog.text
    assert '"ok": 1' in caplog.text


def test_pretty_print_event_text_part_and_null_content(caplog):
    with caplog.at_level("INFO"):
        main.pretty_print_event(
            {"author": "a", "content": {"parts": [{"text": "hello"}]}}
        )
        main.pretty_print_event({"author": "b", "content": None})

    assert "[a]: hello" in caplog.text
    assert "[b]: " in caplog.text
    assert "'content': None" in caplog.text


def test_pretty_print_event_null_text_with_function_call_logs_call(caplog):
    """A part with a null ``text`` alongside ``function_call`` still logs the call."""
    part = {"text": None, "function_call": {"name": "memorize", "args": {}}}
    with caplog.at_level("INFO"):
        main.pretty_print_event({"author": "a", "content": {"parts": [part]}})

    assert "[a]: Function call: memorize" in caplog.text
    assert "[a]: None" not in caplog.text


def test_pretty_print_event_null_parts_does_not_raise():
    main.pretty_print_event({"author": "a", "content": {"parts": None}})
