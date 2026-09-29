"""Opt-in Model Armor plugin scoped to the root orchestrators (P4b Section 4).

`agent_common.safety.build_safety_plugins` returns `[]` unless
`MODEL_ARMOR_TEMPLATE` is set, so the default deploy is unchanged. When enabled,
the plugin only screens the ROOT agent's model turns: `AgentTool`/`NodeTool`
propagate App plugins into every sub-agent run, so an unscoped plugin would
screen (and bill) every research/drafter/critic call too.

No GCP credentials needed: the ADK plugin builds its Model Armor client lazily on
first screening call, and the parent callbacks are patched out here.
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from google.adk.integrations.model_armor import ModelArmorPlugin
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from agent_common.safety import ScopedModelArmorPlugin, build_safety_plugins

TEMPLATE = "projects/test-project/locations/us-central1/templates/tt-demo"
RESPONSE_TEMPLATE = "projects/test-project/locations/us-central1/templates/tt-out"

_ENV_KEYS = (
    "MODEL_ARMOR_TEMPLATE",
    "MODEL_ARMOR_RESPONSE_TEMPLATE",
    "MODEL_ARMOR_FAIL_CLOSED",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _only_plugin(plugins):
    assert len(plugins) == 1
    return plugins[0]


# --- build_safety_plugins -----------------------------------------------------


def test_unset_template_returns_no_plugins():
    assert build_safety_plugins(root_agent_names={"root_agent"}) == []


def test_blank_template_returns_no_plugins(monkeypatch):
    monkeypatch.setenv("MODEL_ARMOR_TEMPLATE", "   ")
    assert build_safety_plugins(root_agent_names={"root_agent"}) == []


def test_template_set_builds_single_fail_closed_plugin(monkeypatch):
    monkeypatch.setenv("MODEL_ARMOR_TEMPLATE", TEMPLATE)
    plugin = _only_plugin(build_safety_plugins(root_agent_names={"root_agent"}))

    assert isinstance(plugin, ModelArmorPlugin)
    assert isinstance(plugin, ScopedModelArmorPlugin)
    assert plugin.root_agent_names == frozenset({"root_agent"})
    assert plugin._config.prompt_template_name == TEMPLATE
    assert plugin._config.response_template_name == TEMPLATE
    assert plugin._config.block_on_screening_failure is True


def test_response_template_override(monkeypatch):
    monkeypatch.setenv("MODEL_ARMOR_TEMPLATE", TEMPLATE)
    monkeypatch.setenv("MODEL_ARMOR_RESPONSE_TEMPLATE", RESPONSE_TEMPLATE)
    plugin = _only_plugin(build_safety_plugins(root_agent_names={"root_agent"}))

    assert plugin._config.prompt_template_name == TEMPLATE
    assert plugin._config.response_template_name == RESPONSE_TEMPLATE


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "off"])
def test_fail_closed_can_be_disabled(monkeypatch, value):
    monkeypatch.setenv("MODEL_ARMOR_TEMPLATE", TEMPLATE)
    monkeypatch.setenv("MODEL_ARMOR_FAIL_CLOSED", value)
    plugin = _only_plugin(build_safety_plugins(root_agent_names={"root_agent"}))

    assert plugin._config.block_on_screening_failure is False


def test_env_is_read_at_call_time(monkeypatch):
    assert build_safety_plugins(root_agent_names={"root_agent"}) == []
    monkeypatch.setenv("MODEL_ARMOR_TEMPLATE", TEMPLATE)
    assert len(build_safety_plugins(root_agent_names={"root_agent"})) == 1


# --- root-only scoping ----------------------------------------------------------


def _plugin(monkeypatch):
    monkeypatch.setenv("MODEL_ARMOR_TEMPLATE", TEMPLATE)
    return _only_plugin(build_safety_plugins(root_agent_names={"root_agent"}))


def _ctx(agent_name):
    return SimpleNamespace(agent_name=agent_name)


def _user_text_request(text="make me an ad"):
    return LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text=text)])]
    )


def _function_response_request():
    """A later root turn: the original prompt, a tool call, then its result (which
    ADK sends back as a user-role function_response)."""
    return LlmRequest(
        contents=[
            types.Content(role="user", parts=[types.Part(text="make me an ad")]),
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            name="ad_creative_pipeline", args={}
                        )
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="ad_creative_pipeline", response={"ok": True}
                        )
                    )
                ],
            ),
        ]
    )


def test_sub_agent_before_model_is_not_screened(monkeypatch):
    plugin = _plugin(monkeypatch)
    with patch.object(
        ModelArmorPlugin, "before_model_callback", new_callable=AsyncMock
    ) as parent:
        result = asyncio.run(
            plugin.before_model_callback(
                callback_context=_ctx("combined_report_composer"), llm_request=object()
            )
        )
    assert result is None
    parent.assert_not_called()


def test_sub_agent_after_model_is_not_screened(monkeypatch):
    plugin = _plugin(monkeypatch)
    with patch.object(
        ModelArmorPlugin, "after_model_callback", new_callable=AsyncMock
    ) as parent:
        result = asyncio.run(
            plugin.after_model_callback(
                callback_context=_ctx("combined_report_composer"), llm_response=object()
            )
        )
    assert result is None
    parent.assert_not_called()


def test_root_before_model_delegates_to_model_armor(monkeypatch):
    plugin = _plugin(monkeypatch)
    ctx, request, sentinel = _ctx("root_agent"), _user_text_request(), object()
    with patch.object(
        ModelArmorPlugin,
        "before_model_callback",
        new_callable=AsyncMock,
        return_value=sentinel,
    ) as parent:
        result = asyncio.run(
            plugin.before_model_callback(callback_context=ctx, llm_request=request)
        )
    assert result is sentinel
    parent.assert_awaited_once_with(callback_context=ctx, llm_request=request)


@pytest.mark.parametrize(
    "request_factory",
    [
        _function_response_request,
        lambda: LlmRequest(contents=[]),
        lambda: _user_text_request(text="   "),
        lambda: LlmRequest(
            contents=[
                types.Content(role="user", parts=[types.Part(text="hi")]),
                types.Content(role="model", parts=[types.Part(text="hello")]),
            ]
        ),
    ],
    ids=["function_response", "empty", "blank_text", "last_is_model"],
)
def test_root_before_model_skips_non_fresh_user_turns(monkeypatch, request_factory):
    """Only a fresh user message is screened: on later root turns the newest
    user-role content is a function_response, and ADK's extractor would walk back
    and re-screen the original prompt every turn."""
    plugin = _plugin(monkeypatch)
    with patch.object(
        ModelArmorPlugin, "before_model_callback", new_callable=AsyncMock
    ) as parent:
        result = asyncio.run(
            plugin.before_model_callback(
                callback_context=_ctx("root_agent"), llm_request=request_factory()
            )
        )
    assert result is None
    parent.assert_not_called()


def test_root_after_model_delegates_to_model_armor(monkeypatch):
    plugin = _plugin(monkeypatch)
    ctx, response, sentinel = _ctx("root_agent"), object(), object()
    with patch.object(
        ModelArmorPlugin,
        "after_model_callback",
        new_callable=AsyncMock,
        return_value=sentinel,
    ) as parent:
        result = asyncio.run(
            plugin.after_model_callback(callback_context=ctx, llm_response=response)
        )
    assert result is sentinel
    parent.assert_awaited_once_with(callback_context=ctx, llm_response=response)


def test_plugin_construction_is_lazy_and_picklable(monkeypatch):
    """No Model Armor client (and no creds) at build time; the unused plugin must
    pickle, since Agent Engine deploy cloudpickles the App."""
    import pickle

    plugin = _plugin(monkeypatch)
    assert plugin._client is None
    clone = pickle.loads(pickle.dumps(plugin))
    assert clone.root_agent_names == plugin.root_agent_names
    assert clone._config == plugin._config


# --- wiring: every root is an App carrying the (default-empty) plugin list ------


@pytest.mark.parametrize(
    "module_name",
    ["creative_agent.agent", "interactive_creative.agent", "trend_scout.agent"],
)
def test_agent_apps_carry_empty_plugin_list_by_default(module_name):
    import importlib

    from google.adk.apps import App

    module = importlib.import_module(module_name)
    # Agents are built at import (after load_dotenv): a developer .env that enables
    # Model Armor legitimately yields a non-empty list.
    if os.environ.get("MODEL_ARMOR_TEMPLATE"):
        pytest.skip("MODEL_ARMOR_TEMPLATE set in the environment (e.g. via .env)")
    assert isinstance(module.app, App)
    assert module.app.root_agent is module.root_agent
    assert isinstance(module.app.plugins, list)
    # The test env never sets MODEL_ARMOR_TEMPLATE, so import-time wiring is empty.
    assert module.app.plugins == []


@pytest.mark.parametrize(
    "app_name", ["creative_agent", "interactive_creative", "trend_scout"]
)
def test_canned_agent_loader_serves_the_app(app_name):
    """ADK's canned loader (get_fast_api_app) imports the PACKAGE first and takes
    `app` before `root_agent`. creative_agent's facade exports `root_agent`, so it
    must also export `app` or the loader serves the bare agent (dropping plugins)."""
    import importlib
    from pathlib import Path

    from google.adk.apps import App
    from google.adk.cli.utils.agent_loader import AgentLoader

    agents_dir = Path(__file__).resolve().parent.parent / "agents"
    loaded = AgentLoader(str(agents_dir)).load_agent(app_name)
    assert isinstance(loaded, App)
    assert loaded is importlib.import_module(f"{app_name}.agent").app


def _descendant_names(root):
    """Names of every agent/node reachable below ``root``.

    Generic walk over pydantic fields (sub_agents, tools, AgentTool.agent,
    NodeTool.node, Workflow edges/graph nodes, RetryUntilKeyNode children, ...),
    skipping the ``parent_agent`` back-reference. Only objects with a ``name`` that
    are agents/nodes are recorded; ``root`` itself is excluded."""
    from google.adk.agents import BaseAgent
    from google.adk.tools.base_tool import BaseTool
    from google.adk.workflow import BaseNode
    from pydantic import BaseModel

    names: list[str] = []
    seen: set[int] = set()

    def visit(obj, is_root=False):
        if isinstance(obj, (list, tuple, set, frozenset)):
            for item in obj:
                visit(item)
            return
        if isinstance(obj, dict):
            for item in obj.values():
                visit(item)
            return
        if not isinstance(obj, (BaseModel, BaseTool)) or id(obj) in seen:
            return
        seen.add(id(obj))
        if isinstance(obj, (BaseAgent, BaseNode)) and not is_root:
            names.append(obj.name)
        for key, value in vars(obj).items():
            if key != "parent_agent":
                visit(value)

    visit(root, is_root=True)
    return names


@pytest.mark.parametrize(
    "module_name",
    ["creative_agent.agent", "interactive_creative.agent", "trend_scout.agent"],
)
def test_no_descendant_shares_the_root_name(module_name):
    """Scoping is by agent name, so a sub-agent/node named like its root would be
    screened too (and every root turn's name must stay unique in the graph)."""
    import importlib

    root = importlib.import_module(module_name).root_agent
    names = _descendant_names(root)
    # Sanity: the walk actually reaches into AgentTool/NodeTool/Workflow graphs.
    assert len(names) > 3
    assert root.name not in names
