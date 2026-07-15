"""Conditional-block control-flow wrapper for agent pipelines.

Shared across the agent packages; lives in ``agent_common`` so any engine can
gate an optional stage on session state without pulling in an unrelated agent
package. Import via ``from agent_common import RunIfAgent``.

## Why this exists

A ``SequentialAgent`` runs every sub-agent unconditionally. Some stages are
*optional* — their work only pays off in certain states, and each one is an
extra serial model call (on gemini-3.1-pro-preview that means another draw on a
5 RPM quota → more server-side 429/503 + retry wait). ``RunIfAgent`` wraps such
a block and runs it only when a predicate over ``ctx.session.state`` is truthy;
otherwise the block is skipped entirely — no events, no model calls.

The predicate reads the *live* session state: the runner merges each yielded
event's ``state_delta`` into the same ``session`` object before this agent is
resumed, so a gate placed after an upstream producer sees whatever that producer
wrote (mirrors the state-delta timing ``RetryUntilKeyAgent`` relies on).

## Guard contract

Skipping a block must not strand a downstream consumer on an unset ``{var}``.
Only wrap stages whose outputs are consumed behind optional ``{var?}`` guards
(or not consumed downstream at all). This keeps the research-pipeline landmine
class — missing ``output_key`` → ``KeyError: Context variable not found`` —
from reappearing when the block is skipped.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator, Callable

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.utils.context_utils import Aclosing
from typing_extensions import override

logger = logging.getLogger("google_adk." + __name__)


class RunIfAgent(BaseAgent):
    """Run ``sub_agents`` (in order) only when ``predicate`` is truthy.

    The gated agents are passed as ``sub_agents`` (so they get normal parent
    wiring). ``predicate`` is called with ``ctx.session.state`` and must return a
    truthy value to run the block; a falsy value skips it (a clean no-op that
    yields nothing).
    """

    predicate: Callable[[object], bool]
    """Called with ``ctx.session.state``; truthy → run the block, falsy → skip."""

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        if not self.sub_agents:
            return

        if not self.predicate(ctx.session.state):
            logger.info(
                "%s: predicate false — skipping %d sub-agent(s): %s",
                self.name,
                len(self.sub_agents),
                ", ".join(a.name for a in self.sub_agents),
            )
            return

        for sub in self.sub_agents:
            async with Aclosing(sub.run_async(ctx)) as agen:
                async for event in agen:
                    yield event
