"""Agent Engine session-lifecycle helper.

The worker creates an Agent Engine session, streams a query against it, then
deletes it. The delete MUST use the SAME ``user_id`` the session was created
under, otherwise Agent Engine returns ``FAILED_PRECONDITION: Session <id> does
not belong to user <...>`` — the exact bug that zeroed out the first p95 batch
(sessions are created per-row as ``f"{_USER_ID}_{index}"`` but were deleted with
the bare constant).

``agent_session`` encapsulates the create→yield→delete triad so ``user_id``
flows through exactly one place and the delete always runs (even when the query
stream raises), via ``finally``.
"""

import logging
from contextlib import asynccontextmanager


@asynccontextmanager
async def agent_session(remote_agent, user_id):
    """Create → yield → delete an Agent Engine session with ONE ``user_id``.

    Args:
        remote_agent: the Agent Engine handle (from ``agent_engines.get``).
        user_id: the user id to create AND delete the session under. Passing it
            once here is what prevents the create/delete drift.

    Yields:
        The created session dict (has an ``"id"`` key).
    """
    session = await remote_agent.async_create_session(user_id=user_id)
    logging.info(f"Created session {session['id']} for user ID: {user_id}")
    try:
        yield session
    finally:
        await remote_agent.async_delete_session(
            user_id=user_id, session_id=session["id"]
        )
        logging.info(f"Deleted session {session['id']} for user ID: {user_id}")
