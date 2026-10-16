"""Bounded drain of the api's detached tasks at lifespan shutdown.

Cloud Run sends SIGTERM, then SIGKILL 10 s later. The shutdown budget:

- uvicorn first waits up to ``--timeout-graceful-shutdown`` (6 s, set in
  ``deployment/backend_entrypoint.sh``; ``UVICORN_GRACEFUL_SECONDS`` here) for
  open HTTP connections, plus a fixed 0.1 s, before it runs the lifespan shutdown;
- our lifespan then cancels the experiments reaper and every detached ``/runs``
  and ``/variants`` task and waits at most ``SHUTDOWN_DRAIN_SECONDS`` (3 s) for
  their cancellation handlers to write the interrupted terminal marker
  (``async_runs._drive_run``) / ``failed``/``interrupted`` record
  (``variants._run_job``);
- ADK's own lifespan then closes its runners; the remaining ~0.9 s is headroom.

A task still running when the drain times out is left to asyncio's loop
teardown (and SIGKILL); pollers then fall back to their stall timeout.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from runserver import async_runs, variants

log = logging.getLogger(__name__)

# Must match --timeout-graceful-shutdown in deployment/backend_entrypoint.sh
# (tests/test_backend_entrypoint.py checks both, and that the sum stays < 10 s).
UVICORN_GRACEFUL_SECONDS = 6
SHUTDOWN_DRAIN_SECONDS = 3.0


def detached_tasks() -> list[asyncio.Task]:
    """Every live detached ``/runs`` (start + resume) and ``/variants`` task."""
    return [*async_runs._BACKGROUND_TASKS, *variants._TASKS.values()]


async def cancel_and_wait(
    tasks: Iterable[asyncio.Task], timeout: float | None = None
) -> set[asyncio.Task]:
    """Cancel ``tasks`` and wait up to ``timeout`` seconds (default
    ``SHUTDOWN_DRAIN_SECONDS``) for them to finish; return the still-pending ones.
    Never raises (the tasks' own exceptions/cancellations are not retrieved here)."""
    pending = {t for t in tasks if not t.done()}
    if not pending:
        return set()
    for task in pending:
        task.cancel()
    bound = SHUTDOWN_DRAIN_SECONDS if timeout is None else timeout
    _done, still = await asyncio.wait(pending, timeout=bound)
    if still:
        log.error(
            "shutdown: %d of %d detached task(s) still running after %.1fs",
            len(still),
            len(pending),
            bound,
        )
    else:
        log.info("shutdown: drained %d detached task(s)", len(pending))
    return still
