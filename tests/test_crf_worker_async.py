"""Tests for the async worker path of the CRF (issue #45).

Guard the fix for #45: a streaming failure inside `async_send_message` must
propagate so `_execute_agent_and_update_status` marks the row `FAILED` instead
of silently marking it `PROCESSED`. Coroutines are driven with `asyncio.run`
(no pytest-asyncio in this project).
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from cloud_funktions.creative_crf import main
from cloud_funktions.creative_crf.session import agent_session


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
    """End-to-end: a streaming failure marks the row FAILED (never PROCESSED)."""

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
    fake_vertex.agent_engines.get.return_value = remote_agent
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

    with pytest.raises(RuntimeError, match="stream boom"):
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
    per-row user id (e.g. ``Ima_CloudRun_jr_0``) but `my_delete_task` used the
    bare module constant ``_USER_ID`` (``Ima_CloudRun_jr``), so the delete never
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
    fake_vertex.agent_engines.get.return_value = remote_agent
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
    fake_vertex.agent_engines.get.return_value = remote_agent
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
