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
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from google.adk.integrations.model_armor import ModelArmorPlugin

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
    ctx, request, sentinel = _ctx("root_agent"), object(), object()
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
