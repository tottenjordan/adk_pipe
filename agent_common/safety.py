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

The ADK plugin builds its Model Armor client lazily on first screening call, so
constructing it needs no credentials and an unused instance pickles cleanly
(Agent Engine deploy cloudpickles the App).
"""

import os
from collections.abc import Iterable
from typing import Any

from google.adk.integrations.model_armor import ModelArmorConfig, ModelArmorPlugin
from google.adk.plugins.base_plugin import BasePlugin

_FALSY = frozenset({"false", "0", "no", "off"})


class ScopedModelArmorPlugin(ModelArmorPlugin):
    """A ``ModelArmorPlugin`` that only screens the named root agents' turns."""

    def __init__(self, *, root_agent_names: Iterable[str], **kwargs: Any):
        super().__init__(**kwargs)
        self.root_agent_names: frozenset[str] = frozenset(root_agent_names)

    def _in_scope(self, callback_context: Any) -> bool:
        return callback_context.agent_name in self.root_agent_names

    async def before_model_callback(self, *, callback_context, llm_request):
        if not self._in_scope(callback_context):
            return None
        return await super().before_model_callback(
            callback_context=callback_context, llm_request=llm_request
        )

    async def after_model_callback(self, *, callback_context, llm_response):
        if not self._in_scope(callback_context):
            return None
        return await super().after_model_callback(
            callback_context=callback_context, llm_response=llm_response
        )


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
