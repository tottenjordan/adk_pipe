"""runserver/shutdown.py: the api's bounded lifespan-shutdown drain (offline).

Cloud Run SIGKILLs 10 s after SIGTERM, so on shutdown the api cancels its
detached ``/runs`` and ``/variants`` tasks and waits a bounded time for their
cancellation handlers to persist the interrupted terminal marker.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from google.adk.sessions import InMemorySessionService

from runserver import async_runs, shutdown
from runserver import variants as vr
from runserver.async_runs import RUN_ERROR_KEY, RUN_STATUS_KEY, start_run


def test_budget_fits_cloud_run_grace_with_uvicorn_wait():
    # backend_entrypoint.sh passes --timeout-graceful-shutdown 6; uvicorn waits up
    # to that long for open connections BEFORE the lifespan shutdown runs.
    assert shutdown.UVICORN_GRACEFUL_SECONDS + shutdown.SHUTDOWN_DRAIN_SECONDS < 10


def test_detached_tasks_lists_runs_and_variants():
    async def go():
        run_task = asyncio.create_task(asyncio.sleep(3600))
        variant_task = asyncio.create_task(asyncio.sleep(3600))
        async_runs._BACKGROUND_TASKS.add(run_task)
        vr._TASKS[("a", "u", "s", "c", "k")] = variant_task
        try:
            found = shutdown.detached_tasks()
            assert run_task in found and variant_task in found
        finally:
            async_runs._BACKGROUND_TASKS.discard(run_task)
            vr._TASKS.pop(("a", "u", "s", "c", "k"), None)
            run_task.cancel()
            variant_task.cancel()
            await asyncio.gather(run_task, variant_task, return_exceptions=True)

    asyncio.run(go())


def test_cancel_and_wait_awaits_cancellation_handlers():
    async def go():
        recorded: list[str] = []

        async def detached():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)  # e.g. the marker append
                recorded.append("marker")
                raise

        task = asyncio.create_task(detached())
        await asyncio.sleep(0)
        pending = await shutdown.cancel_and_wait([task], timeout=2)
        assert pending == set()
        assert task.cancelled()
        assert recorded == ["marker"]

    asyncio.run(go())


def test_cancel_and_wait_is_time_bounded():
    async def go():
        async def stubborn():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(5)  # a wedged marker write (> the bound)

        task = asyncio.create_task(stubborn())
        await asyncio.sleep(0)
        start = time.monotonic()
        pending = await shutdown.cancel_and_wait([task], timeout=0.2)
        elapsed = time.monotonic() - start
        assert pending == {task}
        assert elapsed < 1.0
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())


def test_cancel_and_wait_skips_done_tasks():
    async def go():
        done = asyncio.create_task(asyncio.sleep(0))
        await done
        assert await shutdown.cancel_and_wait([done], timeout=0.1) == set()

    asyncio.run(go())


class _HangingRunner:
    async def run_async(self, **kwargs):
        await asyncio.Event().wait()
        yield  # pragma: no cover — makes this an async generator


def test_shutdown_drain_persists_interrupted_marker_for_a_detached_run():
    """A real detached ``_drive_run`` task cancelled by the shutdown drain
    appends its ``error`` / interrupted marker through the session service."""

    async def go():
        svc = InMemorySessionService()
        real_append = svc.append_event
        svc.append_event = AsyncMock(side_effect=real_append)
        _r, task = await start_run(
            app_name="creative_agent",
            user_id="u",
            session_id="s",
            message="hello",
            session_service=svc,
            runner_factory=lambda app_name: _HangingRunner(),
        )
        await asyncio.sleep(0)
        assert task in shutdown.detached_tasks()
        pending = await shutdown.cancel_and_wait(shutdown.detached_tasks(), timeout=2)
        assert pending == set()
        assert task.cancelled()
        markers = [
            call.args[1].actions.state_delta
            for call in svc.append_event.await_args_list
            if RUN_STATUS_KEY in (call.args[1].actions.state_delta or {})
        ]
        assert markers and markers[-1][RUN_STATUS_KEY] == "error"
        assert "interrupted" in markers[-1][RUN_ERROR_KEY]

    asyncio.run(go())


@pytest.fixture(autouse=True)
def _clean_registries():
    yield
    async_runs._BACKGROUND_TASKS.clear()
    async_runs._ACTIVE_RUNS.clear()
    vr._TASKS.clear()
