"""Agent Engine session-lifecycle helper.

The worker creates an Agent Engine session, streams a query against it, then
deletes it. The delete MUST use the SAME ``user_id`` the session was created
under, otherwise Agent Engine returns ``FAILED_PRECONDITION: Session <id> does
not belong to user <...>`` — the exact bug that zeroed out the first p95 batch
(sessions were created per row but deleted with the bare constant).

``agent_session`` encapsulates the create→yield→delete triad so ``user_id``
flows through exactly one place and the delete always runs (even when the query
stream raises), via ``finally``.

The delete is **cleanup, not part of the result**: it is bounded
(``asyncio.timeout``, at most ``MAX_DELETE_TIMEOUT_SECS``), and a failure or
timeout is logged and swallowed, so it can neither replace the body's return
value (a finished run recorded FAILED because a delete returned 500) nor its
exception, nor run past the worker's time budget.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# The delete is bounded by min(delete_timeout_secs, this).
MAX_DELETE_TIMEOUT_SECS = 30.0


async def delete_session_best_effort(
    remote_agent, user_id, session_id, timeout_secs: float = MAX_DELETE_TIMEOUT_SECS
) -> bool:
    """Delete the session within ``timeout_secs`` (<= 30 s); never raises
    (except cancellation). Returns whether the delete succeeded."""
    timeout = max(0.0, min(timeout_secs, MAX_DELETE_TIMEOUT_SECS))
    try:
        async with asyncio.timeout(timeout):
            await remote_agent.async_delete_session(
                user_id=user_id, session_id=session_id
            )
    except Exception as e:  # noqa: BLE001 — cleanup only, never the result
        logger.warning(
            f"Deleting session {session_id} for user ID {user_id} failed: "
            f"{type(e).__name__}: {e}",
            extra={
                "json_fields": {
                    "session_delete": "failed",
                    "error": type(e).__name__,
                }
            },
        )
        return False
    logger.info(f"Deleted session {session_id} for user ID: {user_id}")
    return True


@asynccontextmanager
async def agent_session(
    remote_agent,
    user_id,
    state=None,
    *,
    delete_timeout_secs: float = MAX_DELETE_TIMEOUT_SECS,
):
    """Create → yield → delete an Agent Engine session with ONE ``user_id``.

    Args:
        remote_agent: the Agent Engine handle (from ``client.runtimes.get``).
        user_id: the user id to create AND delete the session under. Passing it
            once here is what prevents the create/delete drift.
        state: optional initial session state (e.g. the campaign fields), passed
            through to ``async_create_session(state=...)``.
        delete_timeout_secs: bound on the best-effort delete (<= 30 s).

    Yields:
        The created session dict (has an ``"id"`` key).
    """
    if state is None:
        session = await remote_agent.async_create_session(user_id=user_id)
    else:
        session = await remote_agent.async_create_session(user_id=user_id, state=state)
    logger.info(f"Created session {session['id']} for user ID: {user_id}")
    try:
        yield session
    finally:
        await delete_session_best_effort(
            remote_agent, user_id, session["id"], delete_timeout_secs
        )
