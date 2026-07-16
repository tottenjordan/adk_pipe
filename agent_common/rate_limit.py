"""Shared LLM-request rate-limiting callback.

Extracted verbatim from the per-agent ``callbacks.py`` modules — ``trend_scout``
and ``creative_agent`` carried byte-identical copies. The callback closes over an
agent's ``config`` so each package keeps its own ``rpm_quota`` /
``rate_limit_seconds`` values while sharing one implementation.
"""

import time
import logging

from google.adk.models.llm_request import LlmRequest
from google.adk.agents.callback_context import CallbackContext

from agent_common.config import BaseAgentConfiguration


def build_rate_limit_callback(config: BaseAgentConfiguration):
    """Build a ``before_model_callback`` that enforces a requests-per-minute quota.

    Args:
        config: The agent configuration supplying ``rpm_quota`` and
            ``rate_limit_seconds``.

    Returns:
        A ``(callback_context, llm_request) -> None`` callback suitable for
        wiring as an agent's ``before_model_callback``.
    """

    def rate_limit_callback(
        callback_context: CallbackContext, llm_request: LlmRequest
    ) -> None:
        # pylint: disable=unused-argument
        """Callback function that implements a query rate limit.

        Args:
          callback_context: A CallbackContext object representing the active
                  callback context.
          llm_request: A LlmRequest object representing the active LLM request.
        """
        now = time.time()
        if "timer_start" not in callback_context.state:
            callback_context.state["timer_start"] = now
            callback_context.state["request_count"] = 1
            logging.debug(
                "rate_limit_callback [timestamp: %i, req_count: 1, elapsed_secs: 0]",
                now,
            )
            return

        request_count = callback_context.state["request_count"] + 1
        elapsed_secs = now - callback_context.state["timer_start"]
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
                time.sleep(delay)
            callback_context.state["timer_start"] = now
            callback_context.state["request_count"] = 1
        else:
            callback_context.state["request_count"] = request_count

        return

    return rate_limit_callback
