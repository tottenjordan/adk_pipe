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
``finalize_pipeline`` terminal on every path, truthy only).

**When to retry (the end-of-invocation signal).** The worker sends its ONE
retry message on the same session (the creative_agent root re-calls
``creative_pipeline`` on it, ``<RETRY_AFTER_ERROR>``) only when the session
shows the invocation is over: the last content event is the root agent's
final text (non-thought text, no function call or response, not partial) and
every function call since the last user message has a response
(``invocation_ended``; the same idea as runserver's ``should_auto_continue``,
duplicated because the CRF must not import runserver), and that state is
unchanged for one poll interval. Quiet time alone never retries: several
creative steps write no event for minutes (the render node, the paced judge),
so a quiet run may still be going server-side and a retry would start a second
pipeline on the session. A run with no end signal is polled until it finishes,
the budget runs out, or it has been quiet for ``idle_secs`` (>= 600 s), which
only gives up (``stalled`` -> FAILED), never retries.

Every wait is bounded by the deadline (taken at request start by the entry
point): each session read by ``min(read_timeout_secs, remaining)``.

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

# Default bound on each session read (the SDK's async client has no default
# timeout); the worker passes `config.SESSION_READ_TIMEOUT_SECS`.
SESSION_READ_TIMEOUT_SECS = 60.0
# A retry is only worth sending with at least this much budget left.
MIN_RETRY_BUDGET_SECS = 300.0
# The default root agent name (creative_agent's / interactive_creative's root).
ROOT_AGENT_NAME = "root_agent"


@dataclass(frozen=True)
class RunOutcome:
    """How one row's run ended: ``finalized`` decides PROCESSED vs FAILED."""

    finalized: bool
    retried: bool
    reason: str


@dataclass(frozen=True)
class _Check:
    done: bool
    # "done" | "ended" | "stalled" | "budget_exhausted" | "session_unreadable"
    reason: str


def _field(obj, snake: str, camel: str | None = None):
    """A key of a serialized event/part (snake_case or camelCase)."""
    if not isinstance(obj, dict):
        return None
    if snake in obj:
        return obj[snake]
    return obj.get(camel) if camel else None


def _parts(event) -> list[dict]:
    parts = _field(_field(event, "content"), "parts")
    return [p for p in parts if isinstance(p, dict)] if isinstance(parts, list) else []


def _call_key(call: dict) -> str:
    """Match calls to responses by id (ADK sets one), else by name."""
    call_id = _field(call, "id")
    return f"id:{call_id}" if call_id else f"name:{_field(call, 'name')}"


def _has_unanswered_call(events: list[dict]) -> bool:
    pending: set[str] = set()
    for event in events:
        for part in _parts(event):
            call = _field(part, "function_call", "functionCall")
            if isinstance(call, dict):
                pending.add(_call_key(call))
            response = _field(part, "function_response", "functionResponse")
            if isinstance(response, dict):
                pending.discard(_call_key(response))
    return bool(pending)


def _is_root_final_text(event: dict, root_author: str) -> bool:
    if _field(event, "author") != root_author or _field(event, "partial"):
        return False
    has_text = False
    for part in _parts(event):
        if _field(part, "function_call", "functionCall") is not None:
            return False
        if _field(part, "function_response", "functionResponse") is not None:
            return False
        text = _field(part, "text")
        if isinstance(text, str) and text.strip() and not _field(part, "thought"):
            has_text = True
    return has_text


def invocation_ended(session, root_author: str = ROOT_AGENT_NAME) -> bool:
    """The positive end-of-invocation signal (see the module docstring).

    True only when the session's last content event (events without content
    parts, e.g. state-only or lifecycle events, and partial chunks are
    skipped) is ``root_author``'s final text and no function call since the
    last user message is unanswered (a NodeTool pipeline's call stays
    unanswered while it runs, so its sub-agents' text never counts).
    """
    events = session.get("events") if isinstance(session, dict) else None
    if not isinstance(events, list):
        return False
    events = [e for e in events if isinstance(e, dict)]
    content_events = [e for e in events if _parts(e) and not _field(e, "partial")]
    if not content_events or not _is_root_final_text(content_events[-1], root_author):
        return False
    start = 0
    for i, event in enumerate(events):
        if _field(event, "author") == "user":
            start = i + 1
    return not _has_unanswered_call(events[start:])


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
        read_timeout_secs: float = SESSION_READ_TIMEOUT_SECS,
        root_author: str = ROOT_AGENT_NAME,
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
        self.read_timeout_secs = read_timeout_secs
        self.root_author = root_author
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
        """The session dict, or None when it can't be read (logged).

        Bounded by ``min(read_timeout_secs, remaining())``; with no budget
        left it doesn't read at all."""
        timeout = min(self.read_timeout_secs, self.remaining())
        if timeout <= 0:
            return None
        try:
            async with asyncio.timeout(timeout):
                session = await self.remote_agent.async_get_session(
                    user_id=self.user_id,
                    session_id=self.session_id,
                    http_options={"timeout": max(1, int(timeout * 1000))},
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

    async def check(self) -> _Check:
        """Wait until the run is done, has ended unfinished, or must give up.

        ``ended`` (the only reason that allows a retry) needs the positive
        end-of-invocation signal on two reads one poll interval apart with no
        new events in between. Without it the session is polled while it
        progresses; ``idle_secs`` without progress gives up (``stalled``, or
        ``session_unreadable`` when no read succeeded), as does the budget
        (``budget_exhausted``, re-tested after every read).
        """
        session = await self.read_session()
        if session is not None and _state_done(session):
            return _Check(True, "done")
        readable = session is not None
        last_fp = _fingerprint(session) if readable else None
        ended_fp = (
            last_fp
            if readable and invocation_ended(session, self.root_author)
            else None
        )
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
                ended = invocation_ended(session, self.root_author)
                if ended and fp == ended_fp:
                    return _Check(False, "ended")
                ended_fp = fp if ended else None
                if fp != last_fp:
                    last_fp = fp
                    last_progress = self.clock()
            if self.remaining() <= 0:
                return _Check(False, "budget_exhausted")
            if self.clock() - last_progress >= self.idle_secs:
                return _Check(False, "stalled" if readable else "session_unreadable")

    async def run(self, message: str) -> RunOutcome:
        """Stream ``message``, check, and retry once if the run ended unfinished.

        The retry needs the positive end signal (``ended``) whether the stream
        ended cleanly or with an exception, and ``MIN_RETRY_BUDGET_SECS`` left.
        """
        await self.stream(message)
        result = await self.check()
        if result.done:
            return RunOutcome(True, False, "done")
        if result.reason != "ended":
            return RunOutcome(False, False, result.reason)
        if self.remaining() < MIN_RETRY_BUDGET_SECS:
            return RunOutcome(False, False, "budget_exhausted")
        logger.warning(
            f"Session {self.session_id} ended without {COMPLETION_KEY}; "
            "sending one retry message.",
            extra={"json_fields": {"retried": True}},
        )
        await self.stream(RETRY_MESSAGE)
        result = await self.check()
        if result.done:
            return RunOutcome(True, True, "done_after_retry")
        reason = (
            "not_finalized_after_retry" if result.reason == "ended" else result.reason
        )
        return RunOutcome(False, True, reason)
