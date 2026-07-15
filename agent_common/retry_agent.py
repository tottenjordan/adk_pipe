"""Retry-on-empty wrapper for research producer agents.

Shared across the agent packages (``creative_agent``, ``trend_scout``, …); lives
in ``agent_common`` so any engine can wrap a flaky producer without pulling in an
unrelated agent package. Import via ``from agent_common import RetryUntilKeyAgent``.

## Why this exists

The research pipelines are brittle: a producer agent that finishes *without*
writing its ``output_key`` makes the next consumer's ``{var}`` instruction
template raise ``KeyError: Context variable not found`` and abort the whole run.
google_search + thinking agents on gemini-3 hit this
intermittently — they can burn the output budget "thinking" (MAX_TOKENS),
return only tool-call parts, or emit a MALFORMED_FUNCTION_CALL, any of which
leaves the final text (and thus the ``output_key``) empty.

``retry_config`` (INFRA_RETRY) does NOT help here: it only retries the model
call on *infra exceptions* (transient 5xx / ServerError). An empty-but-
successful turn raises nothing, so it never triggers a retry.

## What this does (mitigation #1: retry-on-empty at the producer)

``RetryUntilKeyAgent`` wraps a single inner producer agent and re-runs it until
its ``output_key`` is populated in session state, up to ``max_attempts``. This
is quality-preserving — a fresh model turn typically emits the summary the
flaky turn dropped — and it does not touch the successful path (a healthy
producer runs exactly once).

State-delta timing: the runner appends each yielded event and merges its
``state_delta`` into the *same* ``session`` object this agent reads, before our
generator is resumed. So after the inner agent's event stream is exhausted,
``ctx.session.state[output_key]`` reflects whatever it wrote (or didn't).

## Observability (mitigation #3, folded in)

If every attempt fails, the wrapper does NOT write a placeholder into
``output_key`` (that would feed garbage to the downstream consumer). It leaves
the key unset — so a downstream optional-var guard (``{var?}``) degrades
cleanly — but records an observable ``<output_key>__retry_exhausted`` state
marker and logs an error, so the failure is visible instead of silent.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.utils.context_utils import Aclosing
from typing_extensions import override

logger = logging.getLogger("google_adk." + __name__)


class RetryUntilKeyAgent(BaseAgent):
    """Re-run a single inner agent until its ``output_key`` is populated.

    The inner agent is passed as the sole entry in ``sub_agents`` (so it gets
    normal parent wiring). ``output_key`` must match the inner agent's
    ``output_key``. Retries are bounded by ``max_attempts``.
    """

    output_key: str
    """The session-state key the inner agent is expected to populate."""

    max_attempts: int = 3
    """Maximum number of times to run the inner agent (>= 1)."""

    @staticmethod
    def _is_populated(value: object) -> bool:
        """Populated = a non-blank string, or any other truthy value.

        Research producers write a non-blank string summary; the image producer
        (``generate_image``) writes a boolean ``_images_generated`` flag (and a
        non-empty artifact-keys list). A blank/whitespace string, empty list,
        ``False``, ``0`` and ``None`` all count as unpopulated so the wrapper
        retries.
        """
        if isinstance(value, str):
            return bool(value.strip())
        return bool(value)

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        if not self.sub_agents:
            return
        inner = self.sub_agents[0]

        for attempt in range(1, self.max_attempts + 1):
            async with Aclosing(inner.run_async(ctx)) as agen:
                async for event in agen:
                    yield event

            if self._is_populated(ctx.session.state.get(self.output_key)):
                if attempt > 1:
                    logger.info(
                        "%s populated '%s' on attempt %d/%d",
                        inner.name,
                        self.output_key,
                        attempt,
                        self.max_attempts,
                    )
                return

            logger.warning(
                "%s left '%s' empty on attempt %d/%d%s",
                inner.name,
                self.output_key,
                attempt,
                self.max_attempts,
                "; retrying" if attempt < self.max_attempts else "",
            )

        # Exhausted every attempt: make the failure observable (mitigation #3)
        # without corrupting output_key. Downstream must guard with `{var?}`.
        logger.error(
            "%s never populated '%s' after %d attempts; leaving it unset and "
            "recording '%s__retry_exhausted'",
            inner.name,
            self.output_key,
            self.max_attempts,
            self.output_key,
        )
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            branch=ctx.branch,
            actions=EventActions(
                state_delta={f"{self.output_key}__retry_exhausted": True}
            ),
        )
