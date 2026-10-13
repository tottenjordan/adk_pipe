"""Completion check for one worker run: PROCESSED only when the run finished.

A clean end of ``async_stream_query`` does not mean the creative pipeline
finished. Spike 2026-10-13 (three engine runs through the worker's code path):

- a transient model error (``499 CANCELLED`` under contention) failed
  ``creative_pipeline``; the root restated the error and ended the stream
  cleanly, so the worker would have marked the row PROCESSED with no outputs;
- a client disconnect does not stop the run: it continues server-side and
  reaches ``finalize_done`` later.

So after the stream ends (cleanly or with an exception) the worker reads the
session and trusts only ``state["finalize_done"]`` (set by creative_agent's
``finalize_pipeline`` terminal on every path). While events are still being
appended it polls; once the run is idle and unfinished it sends ONE retry
message on the same session (the creative_agent root re-calls
``creative_pipeline`` on it, ``<RETRY_AFTER_ERROR>``), then checks once more.

Everything takes an injectable ``clock`` / ``sleep`` so tests never wait.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# The creative apps' completion key (creative_agent/finalize.py finalize_ready).
COMPLETION_KEY = "finalize_done"

# Copy of runserver.async_runs.AUTO_RETRY_MESSAGE (the CRF must not import
# runserver; tests/test_worker_retry_message_drift.py keeps them equal).
RETRY_MESSAGE = (
    "The last pipeline step failed with an error. Continue the WORKFLOW: call "
    "that same step again now."
)

# Each session read is bounded (the SDK's async client has no default timeout).
SESSION_READ_TIMEOUT_SECS = 60.0
# A retry is only worth sending with at least this much budget left.
MIN_RETRY_BUDGET_SECS = 300.0


@dataclass(frozen=True)
class RunOutcome:
    """How one row's run ended: ``finalized`` decides PROCESSED vs FAILED."""

    finalized: bool
    retried: bool
    reason: str


@dataclass(frozen=True)
class _Check:
    done: bool
    # "done" | "idle" | "budget_exhausted" | "session_unreadable"
    reason: str


def _state_done(session) -> bool:
    state = session.get("state") if isinstance(session, dict) else None
    return isinstance(state, dict) and bool(state.get(COMPLETION_KEY))


def _fingerprint(session) -> tuple:
    """What changes while the run is still appending events."""
    if not isinstance(session, dict):
        return (None, None)
    events = session.get("events") or []
    last_update = session.get("last_update_time", session.get("lastUpdateTime"))
    return (len(events) if isinstance(events, list) else None, last_update)


class RunMonitor:
    """Drives one session to completion: stream, poll, at most one retry."""

    def __init__(
        self,
        remote_agent,
        user_id: str,
        session_id: str,
        *,
        deadline: float,
        stream_timeout_secs: float,
        poll_interval_secs: float,
        idle_secs: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_event: Callable[[dict], None] | None = None,
    ):
        self.remote_agent = remote_agent
        self.user_id = user_id
        self.session_id = session_id
        self.deadline = deadline
        self.stream_timeout_secs = stream_timeout_secs
        self.poll_interval_secs = poll_interval_secs
        self.idle_secs = idle_secs
        self.clock = clock
        self.sleep = sleep
        self.on_event = on_event

    def remaining(self) -> float:
        return self.deadline - self.clock()

    async def stream(self, message: str) -> bool:
        """Stream one message; True on a clean end, False on any exception.

        Bounded twice: ``http_options.timeout`` (ms; the SDK sets no client
        timeout and also sends it as ``X-Server-Timeout``) and a total
        ``asyncio.timeout`` (httpx applies the former per read, not in total).
        A timeout or cut is not a failure here: the completion check decides.
        """
        timeout = max(1.0, min(self.stream_timeout_secs, self.remaining()))
        try:
            async with asyncio.timeout(timeout):
                async for event in self.remote_agent.async_stream_query(
                    user_id=self.user_id,
                    session_id=self.session_id,
                    message=message,
                    http_options={"timeout": int(timeout * 1000)},
                ):
                    if self.on_event is not None:
                        self.on_event(event)
            return True
        except Exception as e:  # noqa: BLE001 — the completion check decides
            logger.warning(
                f"Stream ended with {type(e).__name__}: {e}; checking the session.",
                extra={"json_fields": {"stream_error": type(e).__name__}},
            )
            return False

    async def read_session(self):
        """The session dict, or None when it can't be read (logged)."""
        timeout = SESSION_READ_TIMEOUT_SECS
        try:
            async with asyncio.timeout(timeout):
                session = await self.remote_agent.async_get_session(
                    user_id=self.user_id,
                    session_id=self.session_id,
                    http_options={"timeout": int(timeout * 1000)},
                )
        except Exception as e:  # noqa: BLE001 — treated as "no progress"
            logger.warning(f"Reading session {self.session_id} failed: {e!r}")
            return None
        if isinstance(session, str):  # defensive: a JSON string payload
            try:
                session = json.loads(session)
            except ValueError:
                return None
        return session if isinstance(session, dict) else None

    async def check(self, clean_end: bool) -> _Check:
        """Wait until the run is done, idle, or the budget runs out.

        After a clean stream end the invocation is over, so one quiet poll
        interval confirms idle; after an exception (timeout / cut / error) the
        run may still be going server-side, so it must stay quiet for
        ``idle_secs``.
        """
        quiet_needed = self.poll_interval_secs if clean_end else self.idle_secs
        session = await self.read_session()
        if session is not None and _state_done(session):
            return _Check(True, "done")
        readable = session is not None
        last_fp = _fingerprint(session) if readable else None
        last_progress = self.clock()
        while True:
            if self.remaining() < self.poll_interval_secs:
                return _Check(False, "budget_exhausted")
            await self.sleep(self.poll_interval_secs)
            session = await self.read_session()
            if session is not None:
                if _state_done(session):
                    return _Check(True, "done")
                readable = True
                fp = _fingerprint(session)
                if fp != last_fp:
                    last_fp = fp
                    last_progress = self.clock()
            if self.clock() - last_progress >= quiet_needed:
                return _Check(False, "idle" if readable else "session_unreadable")

    async def run(self, message: str) -> RunOutcome:
        """Stream ``message``, check, and retry once if the run went idle."""
        clean = await self.stream(message)
        result = await self.check(clean)
        if result.done:
            return RunOutcome(True, False, "done")
        if result.reason != "idle":
            return RunOutcome(False, False, result.reason)
        if self.remaining() < MIN_RETRY_BUDGET_SECS:
            return RunOutcome(False, False, "budget_exhausted")
        logger.warning(
            f"Session {self.session_id} is idle without {COMPLETION_KEY}; "
            "sending one retry message.",
            extra={"json_fields": {"retried": True}},
        )
        clean = await self.stream(RETRY_MESSAGE)
        result = await self.check(clean)
        if result.done:
            return RunOutcome(True, True, "done_after_retry")
        reason = (
            "not_finalized_after_retry" if result.reason == "idle" else result.reason
        )
        return RunOutcome(False, True, reason)
