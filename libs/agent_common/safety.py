"""Opt-in Model Armor screening, scoped to the root orchestrators.

``build_safety_plugins(root_agent_names=...)`` returns the plugin list each agent's
``App(plugins=...)`` is wired with. It is empty unless ``MODEL_ARMOR_TEMPLATE``
is set, so the default deploy behaves exactly as before.

Env (read at call time, i.e. at agent-module import):

- ``MODEL_ARMOR_TEMPLATE`` — full template name
  ``projects/{p}/locations/{loc}/templates/{t}``; screens root prompts and, unless
  overridden, root responses. Unset/blank = no plugin.
- ``MODEL_ARMOR_RESPONSE_TEMPLATE`` — optional separate response template (must
  live in the same location: one client targets one regional endpoint).
- ``MODEL_ARMOR_FAIL_CLOSED`` — default ``true``: a failed screening call blocks
  the turn. ``false``/``0``/``no``/``off`` lets it through (fail-open).

Why root-only: ``AgentTool``/``NodeTool`` propagate App plugins into every
sub-agent run, so an unscoped plugin would screen (and pay for) every research,
drafter, and critic call. The user-facing trust boundary is the root turn — what
the user typed and what the orchestrator says back — so sub-agent turns are
skipped.

Only a *fresh* user message is screened on input: the root's first model call of
a turn, when the newest content is the user's text. Later root calls in the same
turn end with a tool's user-role ``function_response``; ADK's extractor would walk
back past it and re-screen the same original prompt on every call.

Known gaps (by design of root-only, fresh-input screening):

- Interactive checkpoint edits / revision notes reach the root as
  ``function_response`` data on resume, so they are not screened on input.
- ``NodeTool`` sub-branch outputs (pipeline results) are not screened; only what
  the root model itself says back is (``after_model_callback``).
- ADK's Model Armor client is a ``grpc.aio`` client bound to the first event loop
  that uses it, so drive agents through the async path (``Runner.run_async`` /
  Agent Engine ``async_stream_query``) — every caller in this repo does.

The ADK plugin builds its Model Armor client lazily on first screening call, so
constructing it needs no credentials and an unused instance pickles cleanly
(Agent Engine deploy cloudpickles the App).
"""

import os
from collections.abc import Iterable
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.integrations.model_armor import ModelArmorConfig, ModelArmorPlugin
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin

_FALSY = frozenset({"false", "0", "no", "off"})


class ScopedModelArmorPlugin(ModelArmorPlugin):
    """A ``ModelArmorPlugin`` that only screens the named root agents' turns."""

    def __init__(self, *, root_agent_names: Iterable[str], **kwargs: Any):
        super().__init__(**kwargs)
        self.root_agent_names: frozenset[str] = frozenset(root_agent_names)

    def _in_scope(self, callback_context: CallbackContext) -> bool:
        return callback_context.agent_name in self.root_agent_names

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        # Screen only a fresh user message. On later root calls the newest
        # user-role content is a tool's function_response, and ADK's extractor
        # walks back to re-screen the same original prompt every call: wasted
        # Model Armor calls, and with fail-closed a transient screening failure
        # mid-pipeline would replace the root turn with a refusal after the
        # expensive work already ran.
        if not self._in_scope(callback_context) or not _is_fresh_user_turn(llm_request):
            return None
        return await super().before_model_callback(
            callback_context=callback_context, llm_request=llm_request
        )

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        if not self._in_scope(callback_context):
            return None
        return await super().after_model_callback(
            callback_context=callback_context, llm_response=llm_response
        )


def _is_fresh_user_turn(llm_request: LlmRequest) -> bool:
    """True iff the request's LAST content is a user message with non-empty text."""
    if not llm_request.contents:
        return False
    last = llm_request.contents[-1]
    if last.role != "user":
        return False
    return any(part.text and part.text.strip() for part in last.parts or [])


def build_safety_plugins(root_agent_names: Iterable[str]) -> list[BasePlugin]:
    """The App plugin list: ``[]`` unless ``MODEL_ARMOR_TEMPLATE`` is set."""
    template = os.getenv("MODEL_ARMOR_TEMPLATE", "").strip()
    if not template:
        return []
    response_template = (
        os.getenv("MODEL_ARMOR_RESPONSE_TEMPLATE", "").strip() or template
    )
    fail_closed = (
        os.getenv("MODEL_ARMOR_FAIL_CLOSED", "true").strip().lower() not in _FALSY
    )
    config = ModelArmorConfig(
        prompt_template_name=template,
        response_template_name=response_template,
        block_on_screening_failure=fail_closed,
    )
    return [ScopedModelArmorPlugin(config=config, root_agent_names=root_agent_names)]
