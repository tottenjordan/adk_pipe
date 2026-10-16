"""Shared LLM-request rate-limiting callback.

Extracted from the per-agent ``callbacks.py`` modules — ``trend_scout`` and
``creative_agent`` carried byte-identical copies. The callback closes over an
agent's ``config`` so each package keeps its own ``rpm_quota`` /
``rate_limit_seconds`` values while sharing one implementation.

The callback is ``async`` so a quota wait yields the event loop
(``await asyncio.sleep``) instead of blocking every concurrent agent with
``time.sleep``; ADK 2.10 awaits an awaitable callback result
(``google/adk/utils/_callback_pipeline.py`` ``_run_callbacks``).

The counters live under ``temp:`` keys: ADK applies ``temp:``-prefixed state to
the in-memory session for the current invocation only and trims it from the
event delta before persisting (``BaseSessionService._trim_temp_delta_state``;
https://adk.dev/sessions/state/), so the limiter no longer writes two state keys
to the session store on every model call. The window therefore resets per
invocation (each ``/runs`` segment, resume or CRF retry starts a fresh count).

``rpm_quota`` is a SAFETY CAP, not quota pacing: the default 1000 RPM sits far
above the real per-base-model Vertex quotas (e.g. ~5 RPM for the Pro model), so
in normal runs it never waits. It only bounds a runaway loop of model calls
within one invocation; real quota pressure is handled by spreading agents
across model buckets, the genai HTTP retry on 429 and the Pro producers'
FallbackModel.
"""

import asyncio
import logging
import time

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest

from agent_common.config import BaseAgentConfiguration

# Invocation-scoped counter keys (``temp:`` = never persisted).
TIMER_START_KEY = "temp:rate_limit_timer_start"
REQUEST_COUNT_KEY = "temp:rate_limit_request_count"


def build_rate_limit_callback(config: BaseAgentConfiguration):
    """Build a ``before_model_callback`` that enforces a requests-per-minute quota.

    Args:
        config: The agent configuration supplying ``rpm_quota`` and
            ``rate_limit_seconds``.

    Returns:
        An async ``(callback_context, llm_request) -> None`` callback suitable
        for wiring as an agent's ``before_model_callback``.
    """

    async def rate_limit_callback(
        callback_context: CallbackContext, llm_request: LlmRequest
    ) -> None:
        # pylint: disable=unused-argument
        """Wait (without blocking the loop) once ``rpm_quota`` requests were made
        within ``rate_limit_seconds`` of this invocation's window start."""
        state = callback_context.state
        now = time.time()
        if state.get(TIMER_START_KEY) is None:
            state[TIMER_START_KEY] = now
            state[REQUEST_COUNT_KEY] = 1
            logging.debug(
                "rate_limit_callback [timestamp: %i, req_count: 1, elapsed_secs: 0]",
                now,
            )
            return None

        request_count = state.get(REQUEST_COUNT_KEY, 0) + 1
        elapsed_secs = now - state[TIMER_START_KEY]
        logging.debug(
            "rate_limit_callback [timestamp: %i, request_count: %i, elapsed_secs: %i]",
            now,
            request_count,
            elapsed_secs,
        )

        if request_count > config.rpm_quota:
            delay = config.rate_limit_seconds - elapsed_secs + 1
            if delay > 0:
                logging.debug("Sleeping for %i seconds", delay)
                await asyncio.sleep(delay)
            state[TIMER_START_KEY] = time.time()
            state[REQUEST_COUNT_KEY] = 1
        else:
            state[REQUEST_COUNT_KEY] = request_count

        return None

    return rate_limit_callback
