"""Retry-on-empty wrapper for graph-Workflow research producers.

Shared across the agent packages (``creative_agent``, ``trend_scout``,
``interactive_creative``); lives in ``agent_common`` so any engine can wrap a
flaky producer without pulling in an unrelated agent package. Import via
``from agent_common import RetryUntilKeyNode``.

## Why this exists

The research pipelines are brittle: a producer that finishes *without* writing
its ``output_key`` makes the next consumer's ``{var}`` instruction template
raise ``KeyError: Context variable not found`` and abort the whole run.
google_search + thinking agents on gemini-3 hit this intermittently — they can
burn the output budget "thinking" (MAX_TOKENS), return only tool-call parts, or
emit a MALFORMED_FUNCTION_CALL, any of which leaves the final text (and thus the
``output_key``) empty.

``retry_config`` (INFRA_RETRY) does NOT help here: it only retries the model
call on *infra exceptions* (transient 5xx / ServerError). An empty-but-
successful turn raises nothing, so it never triggers a retry.

``RetryUntilKeyNode`` re-runs a child ``BaseNode`` (typically a searcher ->
synthesizer ``Workflow`` pair) until its ``output_key`` is populated (see
``is_populated``), up to ``max_attempts``. This is quality-preserving — a fresh
model turn typically emits the summary the flaky turn dropped — and it does not
touch the successful path (a healthy producer runs exactly once).

If every attempt fails, the node does NOT write a placeholder into
``output_key`` (that would feed garbage to the downstream consumer). It leaves
the key unset — so a downstream optional-var guard (``{var?}``) degrades cleanly
— but records an observable ``<output_key>__retry_exhausted`` state marker and
logs an error, which ``observability.make_final_state_summary`` /
``collect_degradation_warnings`` surface.

It is a ``BaseNode`` (not a ``BaseAgent``) because graph Workflows and
``NodeTool`` take ``BaseNode``s, and ``NodeTool`` rejects any ``BaseAgent``.
(It replaced the pre-P2 ``BaseAgent`` wrapper, retired in P2 T4.)

## Graph-specific contract

- Each attempt runs the child via ``ctx.run_node`` with a distinct ``run_id``
  (``<name>_attempt_<n>``); a distinct run id forces a fresh execution of the
  whole child, not a replay of the first attempt's result.
- It ALWAYS finishes by yielding a *truthy* output value: ``state[output_key]``
  when populated, else a short exhaustion notice (``exhausted_notice``). A
  tool-exposed node that yields no output stalls the calling root agent's turn
  (``NodeInterruptedError`` inside ``NodeTool``; see
  tests/test_workflow_api_contract.py), and so does a falsy one: ``NodeTool`` is
  long-running, and ADK skips the function response for a long-running tool
  that returns a falsy result (``flows/llm_flows/tools/_caller.py``), so ``""``
  would stall too. The notice goes only to the caller as the tool result;
  ``output_key`` itself stays unset for downstream ``{var?}`` guards.
- Retries cover empty results only; an exception raised by the child propagates
  (not retried).
- Known limitation: a value already present in ``output_key`` from an earlier
  turn counts as populated, so the first attempt succeeds even if this run's
  child wrote nothing.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any, override

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.workflow import BaseNode
from pydantic import Field

logger = logging.getLogger("google_adk." + __name__)


def is_populated(value: object) -> bool:
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


class RetryUntilKeyNode(BaseNode):
    """Re-run a child node until ``output_key`` is populated in session state.

    ``output_key`` must match the key the child's final producer writes. Retries
    are bounded by ``max_attempts``. Set ``input_schema`` (e.g.
    ``PipelineRequest``) when exposing the node as a tool.
    """

    node: BaseNode
    """The child node (usually a ``Workflow`` pair) re-run on each attempt."""

    output_key: str
    """The session-state key the child is expected to populate."""

    max_attempts: int = Field(default=3, ge=1)
    """Maximum number of times to run the child (>= 1)."""

    rerun_on_resume: bool = True
    """Required by ADK: ``ctx.run_node`` raises ``ValueError`` unless the calling
    node has ``rerun_on_resume=True`` (``_dynamic_node_scheduler.py``); do not
    override."""

    @override
    async def _run_impl(self, *, ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
        for attempt in range(1, self.max_attempts + 1):
            await ctx.run_node(
                self.node,
                node_input=node_input,
                run_id=f"{self.name}_attempt_{attempt}",
            )

            value = ctx.state.get(self.output_key)
            if is_populated(value):
                if attempt > 1:
                    logger.info(
                        "%s populated '%s' on attempt %d/%d",
                        self.node.name,
                        self.output_key,
                        attempt,
                        self.max_attempts,
                    )
                yield value
                return

            logger.warning(
                "%s left '%s' empty on attempt %d/%d%s",
                self.node.name,
                self.output_key,
                attempt,
                self.max_attempts,
                "; retrying" if attempt < self.max_attempts else "",
            )

        # Exhausted every attempt: record the marker without corrupting
        # output_key (downstream must guard with `{var?}`), and still yield an
        # output so a NodeTool caller gets a function response.
        logger.error(
            "%s never populated '%s' after %d attempts; leaving it unset and "
            "recording '%s__retry_exhausted'",
            self.node.name,
            self.output_key,
            self.max_attempts,
            self.output_key,
        )
        yield Event(
            output=self.exhausted_notice(),
            actions=EventActions(
                state_delta={f"{self.output_key}__retry_exhausted": True}
            ),
        )

    def exhausted_notice(self) -> str:
        """The tool result yielded on exhaustion (non-empty; see module doc)."""
        return (
            f"{self.node.name} did not produce '{self.output_key}' after "
            f"{self.max_attempts} attempts; it is unavailable for this run "
            f"(recorded as '{self.output_key}__retry_exhausted')."
        )
