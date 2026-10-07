"""Fail-soft wrapper for optional graph-Workflow steps.

``RetryUntilKeyNode`` only retries *empty* results: an exception raised by its
child (e.g. ``SCHEMA_RETRY`` exhausted on a large ``output_schema``) propagates
and fails the whole ``Workflow`` — and with it the ``NodeTool`` call — even when
the step is an optional enrichment whose upstream output (a research report) is
already in state.

``FailSoftNode`` runs a child node once and converts any ``Exception`` it raises
into a logged error plus a state delta computed by ``on_error`` (typically a
``<key>__retry_exhausted`` marker and/or a restore of the pre-run value), then
finishes with a truthy output so successors are still triggered and a
``NodeTool`` caller never stalls (see ``retry_node`` for why the output must be
truthy). The healthy path is untouched: the child's output is passed through.

``on_error(state_before, exc)`` receives a snapshot of session state taken
*before* the child ran, so it can restore a value the child may have clobbered,
and the original exception (unwrapped from ADK's ``DynamicNodeFailError``).
Only wrap genuinely optional steps — a fail-soft core step would hide real
failures. ``asyncio.CancelledError`` (a ``BaseException``) is not caught.

Import via ``from agent_common import FailSoftNode``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable, Mapping
from typing import Any, override

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.workflow import BaseNode

logger = logging.getLogger("google_adk." + __name__)

ErrorStateDelta = Callable[[Mapping[str, Any], Exception], dict[str, Any]]


def root_cause(exc: Exception) -> Exception:
    """The original exception behind ADK's dynamic-node wrapper(s).

    ``ctx.run_node`` re-raises a child failure as ``DynamicNodeFailError`` (one
    per nesting level) carrying the original as ``.error``; unwrap it by duck
    typing rather than importing ADK's private ``_errors`` module.
    """
    seen = {id(exc)}
    while isinstance(inner := getattr(exc, "error", None), Exception):
        if id(inner) in seen:
            break
        seen.add(id(inner))
        exc = inner
    return exc


def _no_delta(_state: Mapping[str, Any], _exc: Exception) -> dict[str, Any]:
    return {}


class FailSoftNode(BaseNode):
    """Run ``node``; on an exception, log it and apply ``on_error``'s delta."""

    node: BaseNode
    """The optional child step (an agent, ``RetryUntilKeyNode``, Workflow, ...)."""

    on_error: ErrorStateDelta = _no_delta
    """``(state_before, exc) -> state_delta`` applied when the child raises."""

    rerun_on_resume: bool = True
    """Required by ADK for nodes that call ``ctx.run_node`` (see retry_node)."""

    @override
    async def _run_impl(self, *, ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
        state_before = ctx.state.to_dict()
        try:
            output = await ctx.run_node(
                self.node, node_input=node_input, run_id=f"{self.name}_run"
            )
        except Exception as wrapped:  # fail-soft by design
            exc = root_cause(wrapped)
            logger.error(
                "%s raised %s: %s; continuing without it (fail-soft)",
                self.node.name,
                type(exc).__name__,
                exc,
                exc_info=wrapped,
            )
            yield Event(
                output=self.failed_notice(exc),
                actions=EventActions(state_delta=self.on_error(state_before, exc)),
            )
            return
        yield output if output else f"{self.node.name} finished."

    def failed_notice(self, exc: Exception) -> str:
        """The (truthy) output yielded when the child raised."""
        return (
            f"{self.node.name} failed ({type(exc).__name__}); the run continues "
            "without it."
        )
