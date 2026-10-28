"""Unit tests for optional inbound Discovery Engine enterprise grounding."""

import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock

import google.auth
import pytest
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import google_search
from google.adk.workflow import BaseNode
from google.cloud import discoveryengine_v1beta as discoveryengine

from agent_common.config import (
    BaseAgentConfiguration,
    parse_enterprise_data_stores,
    parse_enterprise_search_engine,
)
from agent_common.observability import collect_degradation_warnings
from creative_agent import callbacks, citations
from creative_agent.agent import brief_gate_decision, combined_research_pipeline
from creative_agent.brief_check import check_brief
from creative_agent.config import ResearchConfiguration
from creative_agent.run_policy import RunPolicy
from creative_agent.sub_agents.enterprise_researcher import agent as ent_agent
from runserver.async_runs import merge_research_edit
from tests._fakes import (
    RecordingLlm,
    fc_response,
    text_response,
    user_message,
    walk_nodes,
)

_SEED_CAMPAIGN: dict[str, Any] = {
    "brand": "PRS Guitars",
    "target_product": "PRS SE CE24",
    "target_audience": "Guitarists",
    "key_selling_points": "85/15 S humbuckers with coil-split",
    "target_search_trends": "powerball jackpot",
}


def _run_node(
    node: BaseNode, state: dict[str, Any] | None = None
) -> tuple[list[Any], dict[str, Any]]:
    async def go() -> tuple[list[Any], dict[str, Any]]:
        svc = InMemorySessionService()
        runner = Runner(
            node=node, app_name="enterprise_grounding_test", session_service=svc
        )
        session = await svc.create_session(
            app_name="enterprise_grounding_test",
            user_id="u",
            state={**_SEED_CAMPAIGN, **(state or {})},
        )
        events = [
            e
            async for e in runner.run_async(
                user_id="u", session_id=session.id, new_message=user_message("go")
            )
        ]
        final = await svc.get_session(
            app_name="enterprise_grounding_test", user_id="u", session_id=session.id
        )
        assert final is not None
        return events, dict(final.state)

    return asyncio.run(go())


def _sample_brief(source_id: str) -> dict[str, Any]:
    return {
        "objective": "Drive awareness of the PRS SE CE24.",
        "audience": "Guitarists who play multiple styles.",
        "insight": "Players want studio versatility, but hate hauling three guitars.",
        "single_minded_proposition": "One PRS SE CE24 nails every tone in the set.",
        "reasons_to_believe": [
            {
                "claim": "85/15 S pickups split cleanly to single-coil.",
                "source_id": source_id,
            },
            {"claim": "Bolt-on maple neck gives crisp attack.", "source_id": "brief"},
        ],
        "brand": {
            "tone_of_voice": "craft-first, warm, confident",
            "distinctive_assets": ["bird inlays", "PRS headstock"],
            "do_not": ["mock beginner players"],
        },
        "trend_bridge": {
            "fit_score": 3,
            "fit_mode": "cultural",
            "bridge": "Winning-rig daydream meets jackpot buzz.",
            "motifs": ["numbered lottery balls", "jackpot ticket slip"],
            "risks": ["gambling promises"],
        },
        "mandatories": ["PRS SE CE24"],
        "avoid": ["guaranteed winnings"],
        "desired_response": "Try the SE CE24.",
        "angles": [
            {
                "angle_id": "A1",
                "name": "Dream Rig",
                "tension": "Players price out dream rigs they cannot justify.",
                "route": "Show SE CE24 delivering custom-shop versatility today.",
            },
            {
                "angle_id": "A2",
                "name": "One Case",
                "tension": "Gigging players dread carrying backup guitars.",
                "route": "Highlight coil-split covering humbucker and single-coil sets.",
            },
            {
                "angle_id": "A3",
                "name": "Lucky Draw",
                "tension": "Every band argues over the setlist genre shift.",
                "route": "Pull any genre ball and nail the tone on one guitar.",
            },
        ],
    }


class TestEnterpriseConfigParsing:
    def test_parse_enterprise_data_stores(self):
        assert parse_enterprise_data_stores(None) == ()
        assert parse_enterprise_data_stores("") == ()
        assert parse_enterprise_data_stores("  ,  , ") == ()
        ds1 = "projects/p/locations/global/collections/default_collection/dataStores/brand-ds"
        ds2 = "projects/p/locations/global/collections/default_collection/dataStores/specs-ds"
        assert parse_enterprise_data_stores(f" {ds1} , , {ds2} ") == (ds1, ds2)

    def test_parse_enterprise_search_engine(self):
        assert parse_enterprise_search_engine(None) == ""
        assert parse_enterprise_search_engine("   ") == ""
        eng = (
            "projects/p/locations/global/collections/default_collection/engines/ent-eng"
        )
        assert parse_enterprise_search_engine(f"  {eng}  ") == eng

    def test_enterprise_grounding_configured_property(self, monkeypatch):
        monkeypatch.delenv("ENTERPRISE_DATA_STORES", raising=False)
        monkeypatch.delenv("ENTERPRISE_SEARCH_ENGINE", raising=False)
        cfg = BaseAgentConfiguration()
        assert cfg.enterprise_data_stores == ()
        assert cfg.enterprise_search_engine == ""
        assert cfg.enterprise_grounding_configured is False

        monkeypatch.setenv(
            "ENTERPRISE_DATA_STORES", "projects/p/locations/global/dataStores/ds1"
        )
        cfg_ds = BaseAgentConfiguration()
        assert cfg_ds.enterprise_grounding_configured is True

        monkeypatch.delenv("ENTERPRISE_DATA_STORES", raising=False)
        monkeypatch.setenv(
            "ENTERPRISE_SEARCH_ENGINE", "projects/p/locations/global/engines/e1"
        )
        cfg_eng = BaseAgentConfiguration()
        assert cfg_eng.enterprise_grounding_configured is True

    def test_enterprise_research_model_and_deploy_env_forwarding(self, monkeypatch):
        import deployment.deploy_agent as da

        monkeypatch.delenv("ENTERPRISE_RESEARCH_MODEL", raising=False)
        monkeypatch.delenv("ENTERPRISE_DATA_STORES", raising=False)
        monkeypatch.delenv("ENTERPRISE_SEARCH_ENGINE", raising=False)
        rcfg = ResearchConfiguration()
        assert rcfg.enterprise_research_model == "gemini-3.1-flash-lite"

        default_env = da.build_env_vars()
        assert "ENTERPRISE_DATA_STORES" not in default_env
        assert "ENTERPRISE_SEARCH_ENGINE" not in default_env

        monkeypatch.setenv(
            "ENTERPRISE_DATA_STORES", " projects/p/locations/global/dataStores/ds1 "
        )
        monkeypatch.setenv(
            "ENTERPRISE_SEARCH_ENGINE", "projects/p/locations/global/engines/e1"
        )
        monkeypatch.setenv("ENTERPRISE_RESEARCH_MODEL", "gemini-3.5-flash-lite")
        forwarded = da.build_env_vars()
        assert (
            forwarded["ENTERPRISE_DATA_STORES"]
            == "projects/p/locations/global/dataStores/ds1"
        )
        assert (
            forwarded["ENTERPRISE_SEARCH_ENGINE"]
            == "projects/p/locations/global/engines/e1"
        )
        assert forwarded["ENTERPRISE_RESEARCH_MODEL"] == "gemini-3.5-flash-lite"

        # If ENV_VAR_DICT captured values from .env at import time and the env vars
        # are subsequently unset/blanked, build_env_vars() must pop them cleanly.
        monkeypatch.setitem(
            da.ENV_VAR_DICT,
            "ENTERPRISE_DATA_STORES",
            "projects/p/locations/global/dataStores/stale-ds",
        )
        monkeypatch.setitem(
            da.ENV_VAR_DICT,
            "ENTERPRISE_SEARCH_ENGINE",
            "projects/p/locations/global/engines/stale-eng",
        )
        monkeypatch.setitem(da.ENV_VAR_DICT, "ENTERPRISE_RESEARCH_MODEL", "stale-model")
        monkeypatch.delenv("ENTERPRISE_DATA_STORES", raising=False)
        monkeypatch.setenv("ENTERPRISE_SEARCH_ENGINE", "   ")
        monkeypatch.delenv("ENTERPRISE_RESEARCH_MODEL", raising=False)
        cleared = da.build_env_vars()
        assert "ENTERPRISE_DATA_STORES" not in cleared
        assert "ENTERPRISE_SEARCH_ENGINE" not in cleared
        assert "ENTERPRISE_RESEARCH_MODEL" not in cleared


class TestBuildEnterpriseSearchTool:
    def _stub_discovery_auth(self, monkeypatch):
        ent_agent._enterprise_tool_cache.clear()
        creds = MagicMock()
        creds.quota_project_id = "test-project"
        monkeypatch.setattr(google.auth, "default", lambda: (creds, "test-project"))
        client_cls = MagicMock()
        monkeypatch.setattr(discoveryengine, "SearchServiceClient", client_cls)
        return client_cls

    def test_unconfigured_raises_value_error(self):
        cfg = BaseAgentConfiguration(
            enterprise_data_stores=(), enterprise_search_engine=""
        )
        with pytest.raises(ValueError, match="Enterprise grounding is not configured"):
            ent_agent.build_enterprise_search_tool(cfg)

    def test_single_data_store_without_engine_uses_data_store_id(self, monkeypatch):
        self._stub_discovery_auth(monkeypatch)
        ds = "projects/p/locations/global/collections/default_collection/dataStores/brand-ds"
        cfg = BaseAgentConfiguration(
            enterprise_data_stores=(ds,), enterprise_search_engine=""
        )
        tool = ent_agent.build_enterprise_search_tool(cfg)
        assert isinstance(tool, ent_agent.DiscoveryEngineSearchTool)
        assert tool._serving_config == f"{ds}/servingConfigs/default_config"
        assert tool._search_engine_id is None
        assert tool._data_store_specs is None
        assert tool._max_results == 6

    def test_multiple_data_stores_without_engine_fans_out_and_merges(self, monkeypatch):
        self._stub_discovery_auth(monkeypatch)
        ds1 = (
            "projects/p/locations/global/collections/default_collection/dataStores/ds-1"
        )
        ds2 = (
            "projects/p/locations/global/collections/default_collection/dataStores/ds-2"
        )
        cfg = ResearchConfiguration(
            enterprise_data_stores=(ds1, ds2),
            enterprise_search_engine="",
        )
        tools = ent_agent.build_enterprise_search_tools(cfg)
        assert len(tools) == 2
        assert tools[0]._serving_config == f"{ds1}/servingConfigs/default_config"
        assert tools[1]._serving_config == f"{ds2}/servingConfigs/default_config"

        multi_tool = ent_agent.build_enterprise_search_tool(cfg)
        assert isinstance(multi_tool, ent_agent.MultiDataStoreSearchTool)

        monkeypatch.setattr(
            multi_tool.tools[0],
            "discovery_engine_search",
            lambda q: {
                "status": "success",
                "results": [
                    {
                        "title": f"S1-{i}",
                        "url": f"https://s1.example/{i}",
                        "content": f"c{i}",
                    }
                    for i in range(4)
                ],
            },
        )
        monkeypatch.setattr(
            multi_tool.tools[1],
            "discovery_engine_search",
            lambda q: {
                "status": "success",
                "results": [
                    # Duplicate of S1-0 should be deduplicated
                    {"title": "S1-0", "url": "https://s1.example/0", "content": "c0"},
                    *(
                        {
                            "title": f"S2-{i}",
                            "url": f"https://s2.example/{i}",
                            "content": f"d{i}",
                        }
                        for i in range(4)
                    ),
                ],
            },
        )
        ent_agent._enterprise_tool_cache[
            (cfg.enterprise_data_stores, cfg.enterprise_search_engine)
        ] = multi_tool
        monkeypatch.setattr(ent_agent, "config", cfg)

        assert inspect.iscoroutinefunction(ent_agent.search_enterprise_knowledge_base)
        merged = asyncio.run(ent_agent.search_enterprise_knowledge_base("brand claims"))
        assert merged["status"] == "success"
        assert len(merged["results"]) == 6
        titles = [r["title"] for r in merged["results"]]
        assert "S1-0" in titles
        assert "S2-0" in titles
        assert titles.count("S1-0") == 1

    def test_search_engine_without_data_stores_omits_specs(self, monkeypatch):
        self._stub_discovery_auth(monkeypatch)
        eng = "projects/p/locations/global/collections/default_collection/engines/brand-engine"
        cfg = BaseAgentConfiguration(
            enterprise_data_stores=(),
            enterprise_search_engine=eng,
        )
        tool = ent_agent.build_enterprise_search_tool(cfg)
        assert isinstance(tool, ent_agent.DiscoveryEngineSearchTool)
        assert tool._serving_config == f"{eng}/servingConfigs/default_config"
        assert tool._search_engine_id == eng
        assert tool._data_store_specs is None
        assert tool._max_results == 6

    def test_search_engine_with_multiple_data_stores_builds_specs(self, monkeypatch):
        self._stub_discovery_auth(monkeypatch)
        eng = "projects/p/locations/global/collections/default_collection/engines/brand-engine"
        ds1 = (
            "projects/p/locations/global/collections/default_collection/dataStores/ds-1"
        )
        ds2 = (
            "projects/p/locations/global/collections/default_collection/dataStores/ds-2"
        )
        cfg = BaseAgentConfiguration(
            enterprise_data_stores=(ds1, ds2),
            enterprise_search_engine=eng,
        )
        tool = ent_agent.build_enterprise_search_tool(cfg)
        assert isinstance(tool, ent_agent.DiscoveryEngineSearchTool)
        assert tool._serving_config == f"{eng}/servingConfigs/default_config"
        assert tool._search_engine_id == eng
        assert tool._max_results == 6
        assert tool._data_store_specs is not None
        assert [s.data_store for s in tool._data_store_specs] == [ds1, ds2]

    def test_function_tool_returns_skipped_when_unconfigured(self, monkeypatch):
        unconfigured = ResearchConfiguration(
            enterprise_data_stores=(), enterprise_search_engine=""
        )
        monkeypatch.setattr(ent_agent, "config", unconfigured)
        assert asyncio.run(
            ent_agent.search_enterprise_knowledge_base("brand guidelines")
        ) == {
            "status": "skipped",
            "results": [],
        }

    def test_tool_cache_is_thread_safe_under_concurrent_access(self, monkeypatch):
        ent_agent._enterprise_tool_cache.clear()
        build_calls = 0

        def _fake_build(cfg: BaseAgentConfiguration):
            nonlocal build_calls
            build_calls += 1
            return MagicMock()

        monkeypatch.setattr(ent_agent, "build_enterprise_search_tool", _fake_build)
        cfg = BaseAgentConfiguration(
            enterprise_data_stores=(
                "projects/p/locations/global/collections/default_collection/dataStores/ds-1",
            ),
            enterprise_search_engine="",
        )
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: ent_agent._get_enterprise_search_tool(cfg), range(16)
                )
            )
        assert build_calls == 1
        assert len({id(r) for r in results}) == 1


class TestEnterpriseGroundingGateAndFailSoft:
    def _patch_enterprise_llms(self, monkeypatch) -> dict[str, RecordingLlm]:
        llms: dict[str, RecordingLlm] = {}
        for node in walk_nodes(ent_agent.enterprise_sequential_planner):
            if isinstance(node, LlmAgent):
                llm = llms.setdefault(node.name, RecordingLlm())
                monkeypatch.setattr(node, "model", llm)
                monkeypatch.setattr(node, "planner", None)
        return llms

    def test_gate_skips_with_zero_rpcs_when_unconfigured(self, monkeypatch):
        unconfigured = ResearchConfiguration(
            enterprise_data_stores=(), enterprise_search_engine=""
        )
        monkeypatch.setattr(ent_agent, "config", unconfigured)
        llms = self._patch_enterprise_llms(monkeypatch)

        _, state = _run_node(
            ent_agent.enterprise_sequential_planner,
            {"enterprise_grounding_enabled": True},
        )
        assert state["enterprise_research_findings"] == ""
        for name, llm in llms.items():
            assert len(llm.requests) == 0, (
                f"{name} should not be called when unconfigured"
            )

    def test_gate_skips_with_zero_rpcs_when_disabled_in_state(self, monkeypatch):
        configured = ResearchConfiguration(
            enterprise_data_stores=(
                "projects/p/locations/global/collections/default_collection/dataStores/ds1",
            ),
            enterprise_search_engine="",
        )
        monkeypatch.setattr(ent_agent, "config", configured)
        llms = self._patch_enterprise_llms(monkeypatch)

        _, state = _run_node(
            ent_agent.enterprise_sequential_planner,
            {"enterprise_grounding_enabled": False},
        )
        assert state["enterprise_research_findings"] == ""
        for name, llm in llms.items():
            assert len(llm.requests) == 0, f"{name} should not be called when disabled"

    def test_failsoft_degrades_gracefully_when_discovery_engine_raises(
        self, monkeypatch
    ):
        configured = ResearchConfiguration(
            enterprise_data_stores=(
                "projects/p/locations/global/collections/default_collection/dataStores/ds1",
            ),
            enterprise_search_engine="",
        )
        monkeypatch.setattr(ent_agent, "config", configured)
        llms = self._patch_enterprise_llms(monkeypatch)

        llms["enterprise_query_planner"].push(
            text_response('{"queries": [{"search_query": "PRS brand rules"}]}')
        )

        def raising_tool(query: str) -> dict[str, Any]:
            raise RuntimeError("Discovery Engine 503 Unavailable")

        for node in walk_nodes(ent_agent.enterprise_sequential_planner):
            if isinstance(node, LlmAgent) and node.name == "enterprise_doc_searcher":
                monkeypatch.setattr(node, "tools", [raising_tool])

        # RetryUntilKeyNode has max_attempts=2; push a tool call for each attempt so the
        # tool raises out of RetryUntilKeyNode into enterprise_searcher_failsoft.
        llms["enterprise_doc_searcher"].push(
            fc_response("raising_tool", {"query": "PRS brand rules"}, "fc1"),
            fc_response("raising_tool", {"query": "PRS brand rules"}, "fc2"),
        )

        _, state = _run_node(
            ent_agent.enterprise_sequential_planner,
            {"enterprise_grounding_enabled": True},
        )
        assert state["enterprise_research_findings"] == ""
        assert state["enterprise_research_findings__issues"] == [
            "Enterprise knowledge base search unavailable; continuing with public web research."
        ]
        warnings = collect_degradation_warnings(state)
        assert any(
            "Enterprise knowledge base has unresolved issues" in w for w in warnings
        )

    def test_failsoft_degrades_gracefully_when_query_planner_raises(self, monkeypatch):
        class _RaisingLlm(RecordingLlm):
            async def generate_content_async(self, llm_request, stream: bool = False):
                self._requests.append(llm_request)
                raise RuntimeError("Enterprise planner 503 Unavailable")
                yield  # pragma: no cover

        configured = ResearchConfiguration(
            enterprise_data_stores=(
                "projects/p/locations/global/collections/default_collection/dataStores/ds1",
            ),
            enterprise_search_engine="",
        )
        monkeypatch.setattr(ent_agent, "config", configured)
        llms = self._patch_enterprise_llms(monkeypatch)
        raising_llm = _RaisingLlm()
        for node in walk_nodes(ent_agent.enterprise_sequential_planner):
            if isinstance(node, LlmAgent) and node.name == "enterprise_query_planner":
                monkeypatch.setattr(node, "model", raising_llm)

        _, state = _run_node(
            ent_agent.enterprise_sequential_planner,
            {"enterprise_grounding_enabled": True},
        )
        assert len(raising_llm.requests) >= 1
        assert state["enterprise_research_findings"] == ""
        assert state["enterprise_research_findings__issues"] == [
            "Enterprise knowledge base search unavailable; continuing with public web research."
        ]
        assert len(llms["enterprise_doc_searcher"].requests) == 0
        assert len(llms["enterprise_doc_synthesizer"].requests) == 0

    def test_error_dict_from_discovery_engine_clears_findings_and_records_issues(
        self, monkeypatch
    ):
        configured = ResearchConfiguration(
            enterprise_data_stores=(
                "projects/p/locations/global/collections/default_collection/dataStores/ds1",
            ),
            enterprise_search_engine="",
        )
        monkeypatch.setattr(ent_agent, "config", configured)
        llms = self._patch_enterprise_llms(monkeypatch)

        llms["enterprise_query_planner"].push(
            text_response('{"queries": [{"search_query": "PRS brand rules"}]}')
        )

        async def error_dict_tool(query: str) -> dict[str, Any]:
            return {"status": "error", "error_message": "403 PermissionDenied"}

        for node in walk_nodes(ent_agent.enterprise_sequential_planner):
            if isinstance(node, LlmAgent) and node.name == "enterprise_doc_searcher":
                monkeypatch.setattr(node, "tools", [error_dict_tool])

        llms["enterprise_doc_searcher"].push(
            fc_response("error_dict_tool", {"query": "PRS brand rules"}, "fc1"),
            text_response(
                "Search returned 403 PermissionDenied; no excerpts available."
            ),
        )
        llms["enterprise_doc_synthesizer"].push(
            text_response("No matching internal records were found.")
        )

        _, state = _run_node(
            ent_agent.enterprise_sequential_planner,
            {
                "enterprise_grounding_enabled": True,
                "enterprise_research_findings": "stale previous turn findings",
            },
        )
        assert state["enterprise_research_findings"] == ""
        assert state["enterprise_research_findings__issues"] == [
            "Enterprise knowledge base search unavailable; continuing with public web research."
        ]
        warnings = collect_degradation_warnings(state)
        assert any(
            "Enterprise knowledge base has unresolved issues" in w for w in warnings
        )

    def test_empty_results_from_healthy_search_normalizes_findings_without_issues(
        self, monkeypatch
    ):
        configured = ResearchConfiguration(
            enterprise_data_stores=(
                "projects/p/locations/global/collections/default_collection/dataStores/ds1",
            ),
            enterprise_search_engine="",
        )
        monkeypatch.setattr(ent_agent, "config", configured)
        llms = self._patch_enterprise_llms(monkeypatch)

        llms["enterprise_query_planner"].push(
            text_response('{"queries": [{"search_query": "PRS brand rules"}]}')
        )

        async def empty_ok_tool(query: str) -> dict[str, Any]:
            return {"status": "success", "results": []}

        for node in walk_nodes(ent_agent.enterprise_sequential_planner):
            if isinstance(node, LlmAgent) and node.name == "enterprise_doc_searcher":
                monkeypatch.setattr(node, "tools", [empty_ok_tool])

        llms["enterprise_doc_searcher"].push(
            fc_response("empty_ok_tool", {"query": "PRS brand rules"}, "fc1"),
            text_response("No matching internal documents."),
        )
        llms["enterprise_doc_synthesizer"].push(
            text_response("No matching internal records were found.")
        )

        _, state = _run_node(
            ent_agent.enterprise_sequential_planner,
            {
                "enterprise_grounding_enabled": True,
                "enterprise_research_findings__issues": ["stale old issue"],
            },
        )
        assert state["enterprise_research_findings"] == ""
        assert state.get("enterprise_research_findings__issues") is None


class TestCollectEnterpriseSourcesAndStateInit:
    def test_initial_state_seeds_enterprise_keys_without_clobbering_caller_flag(
        self, monkeypatch
    ):
        configured = ResearchConfiguration(
            enterprise_data_stores=(
                "projects/p/locations/global/collections/default_collection/dataStores/ds1",
            ),
            enterprise_search_engine="",
        )
        monkeypatch.setattr(callbacks, "config", configured)

        seeded_false: dict[str, Any] = {"enterprise_grounding_enabled": False}
        callbacks._set_initial_states({}, seeded_false)
        assert seeded_false["enterprise_grounding_enabled"] is False
        assert seeded_false["enterprise_research_findings"] == ""
        assert seeded_false["enterprise_sources"] == {}
        assert seeded_false["url_to_ent_id"] == {}

        unseeded: dict[str, Any] = {}
        callbacks._set_initial_states({}, unseeded)
        assert unseeded["enterprise_grounding_enabled"] is True

    def test_collect_enterprise_sources_mints_and_dedupes_ent_ids(self):
        tool_ctx = MagicMock()
        tool_ctx.state = {"enterprise_sources": {}, "url_to_ent_id": {}}

        batch_1 = {
            "status": "success",
            "results": [
                {
                    "title": "PRS Brand Guidelines 2026",
                    "url": "https://intranet.prs.example/brand-guidelines",
                    "content": "Always show bird inlays on hero electric guitar visuals.",
                },
                {
                    "title": "SE CE24 Approved Spec Sheet",
                    "url": "gs://prs-enterprise-kb/specs/se-ce24.pdf",
                    "content": "85/15 S humbuckers with push/pull tone coil-tap.",
                },
            ],
        }
        batch_2 = {
            "status": "success",
            "results": [
                # Duplicate URL from batch_1 -> should keep ent-1, not mint ent-3
                {
                    "title": "PRS Brand Guidelines 2026",
                    "url": "https://intranet.prs.example/brand-guidelines",
                    "content": "Duplicate hit.",
                },
                # URL-less internal chunk with chunk_id
                {
                    "title": "Legal Compliance Memo",
                    "url": "",
                    "chunk_id": "doc-legal-9#chunk-2",
                    "content": "Never imply guaranteed lottery winnings.",
                },
                # Title-less GCS chunk -> fallback title uses basename, domain strips gs://
                {
                    "title": "",
                    "url": "gs://prs-enterprise-kb/guidelines/tone-rules.pdf",
                    "content": "Never use competitor trademarks.",
                },
            ],
        }

        assert (
            callbacks.collect_enterprise_sources_callback(
                MagicMock(), {"query": "brand"}, tool_ctx, batch_1
            )
            is None
        )
        callbacks.collect_enterprise_sources_callback(
            MagicMock(), {"query": "specs"}, tool_ctx, batch_2
        )
        # Error payloads are ignored cleanly
        callbacks.collect_enterprise_sources_callback(
            MagicMock(),
            {"query": "fail"},
            tool_ctx,
            {"status": "error", "error_message": "boom"},
        )

        ent_sources = tool_ctx.state["enterprise_sources"]
        assert list(ent_sources.keys()) == ["ent-1", "ent-2", "ent-3", "ent-4"]
        assert ent_sources["ent-1"] == {
            "short_id": "ent-1",
            "title": "PRS Brand Guidelines 2026",
            "url": "https://intranet.prs.example/brand-guidelines",
            "domain": "intranet.prs.example",
            "snippet": "Always show bird inlays on hero electric guitar visuals.",
            "chunk_id": "",
            "source_type": "enterprise",
        }
        assert ent_sources["ent-2"]["url"] == "gs://prs-enterprise-kb/specs/se-ce24.pdf"
        assert ent_sources["ent-2"]["domain"] == "prs-enterprise-kb"
        assert ent_sources["ent-3"]["chunk_id"] == "doc-legal-9#chunk-2"
        assert ent_sources["ent-3"]["source_type"] == "enterprise"
        assert ent_sources["ent-4"]["title"] == "tone-rules.pdf"
        assert ent_sources["ent-4"]["domain"] == "prs-enterprise-kb"
        assert "gs://" not in ent_sources["ent-4"]["title"]
        assert "gs://" not in ent_sources["ent-4"]["domain"]

        rendered = citations.replace_citations(
            'Follow internal tone <cite source="ent-4" />.',
            {},
            enterprise_sources=ent_sources,
        )
        assert "[Enterprise: tone-rules.pdf]" in rendered
        assert "- **ent-4**: tone-rules.pdf" in rendered
        assert "gs://" not in rendered


class TestEnterpriseBriefCheckAndCitations:
    @pytest.mark.parametrize(
        "raw_source_id",
        ["ent-1", "[ent-1]", "ENT_01", "ent 1", "src-1, ENT-01"],
    )
    def test_brief_check_normalizes_and_accepts_known_ent_ids(self, raw_source_id):
        web_sources = {"src-1": {"title": "Web", "url": "https://example.com"}}
        ent_sources = {"ent-1": {"title": "Brand Guide", "url": "gs://kb/brand.pdf"}}
        merged = {**web_sources, **ent_sources}
        assert (
            check_brief(_sample_brief(raw_source_id), RunPolicy(sources=merged)) == []
        )
        assert (
            check_brief(
                _sample_brief(raw_source_id),
                RunPolicy(sources=web_sources, enterprise_sources=ent_sources),
            )
            == []
        )

    def test_brief_check_flags_unknown_and_zero_ent_ids(self):
        ent_sources = {"ent-1": {"title": "Brand Guide", "url": "gs://kb/brand.pdf"}}
        policy = RunPolicy(enterprise_sources=ent_sources)
        (unknown_issue,) = check_brief(_sample_brief("ent-9"), policy)
        assert "unknown sources" in unknown_issue and "'ent-9'" in unknown_issue

        (invalid_issue,) = check_brief(_sample_brief("ent-0"), policy)
        assert "invalid source_id" in invalid_issue and "'ent-0'" in invalid_issue

    def test_brief_gate_merges_web_and_enterprise_sources(self):
        state = {
            "creative_brief": _sample_brief("ENT_02"),
            "sources": {"src-1": {"title": "Web", "url": "https://example.com"}},
            "enterprise_sources": {
                "ent-2": {"title": "Spec Sheet", "url": "gs://kb/specs.pdf"}
            },
        }
        route, delta = brief_gate_decision(state, max_rounds=1)
        assert route == "ok"
        assert delta["creative_brief__issues"] is None

        bad_state = {**state, "creative_brief": _sample_brief("ent-99")}
        bad_route, bad_delta = brief_gate_decision(bad_state, max_rounds=1)
        assert bad_route == "revise"
        assert "ent-99" in bad_delta["brief_issues"]

    def test_replace_citations_formats_http_gcs_and_bibliography(self):
        web_sources = {
            "src-1": {"title": "Guitar World", "url": "https://guitarworld.example/prs"}
        }
        ent_sources = {
            "ent-1": {
                "title": "PRS Brand Portal",
                "url": "https://intranet.prs.example/brand",
            },
            "ent-2": {
                "title": "SE CE24 Spec Sheet",
                "url": "gs://prs-kb/se-ce24.pdf",
            },
            "ent-3": {
                "title": "Compliance Policy",
                "url": "",
            },
        }
        report = (
            "# Report\n\n"
            "## Summary\n"
            'Tone versatility <cite source="src-1, ent-1" /> and coil-tap <cite source="ent-2" />. '
            "Follow policy [ent-3]."
        )
        rendered = citations.replace_citations(
            report, web_sources, enterprise_sources=ent_sources
        )
        assert "[Guitar World](https://guitarworld.example/prs)" in rendered
        assert "[PRS Brand Portal](https://intranet.prs.example/brand)" in rendered
        assert "[Enterprise: SE CE24 Spec Sheet]" in rendered
        assert "[Enterprise: Compliance Policy]" in rendered
        assert "gs://" not in rendered
        assert "### Enterprise Knowledge Sources" in rendered
        assert (
            "- **ent-1**: [PRS Brand Portal](https://intranet.prs.example/brand)"
            in rendered
        )
        assert "- **ent-2**: SE CE24 Spec Sheet" in rendered
        assert "- **ent-3**: Compliance Policy" in rendered

    def test_replace_citations_sanitizes_adversarial_titles_urls_and_schemes(self):
        web_sources = {
            "src-1": {
                "title": "News](https://evil.example)\n[Headline]",
                "url": "https://news.example/story (1)",
            },
            "src-2": {
                "title": "XSS Attempt",
                "url": "javascript:alert(1)",
            },
            "src-3": {
                "title": "Missing URL",
            },
        }
        ent_sources = {
            "ent-1": {
                "title": "Spec](https://evil.example)\r\n[Draft]",
                "url": "HTTPS://portal.example/docs/Brand Guide (2026).pdf",
            },
            "ent-2": {
                "title": "Vault](https://evil.example) [Doc]",
                "url": "gs://kb/vault.pdf",
            },
        }
        report = (
            'Web <cite source="src-1, src-2, src-3" /> and '
            'enterprise <cite source="ent-1, ent-2" />.'
        )
        rendered = citations.replace_citations(
            report, web_sources, enterprise_sources=ent_sources
        )
        assert "javascript:" not in rendered.lower()
        assert (
            "[News(https://evil.example) Headline](https://news.example/story%20%281%29)"
            in rendered
        )
        assert (
            "[Spec(https://evil.example) Draft]"
            "(HTTPS://portal.example/docs/Brand%20Guide%20%282026%29.pdf)" in rendered
        )
        assert "[Enterprise: Vault(https://evil.example) Doc]" in rendered
        assert "](https://evil.example)" not in rendered

    def test_merge_research_edit_preserves_enterprise_citations(self):
        state = {
            "combined_final_cited_report": "Old report",
            "sources": {"src-1": {"title": "Web", "url": "https://w.example"}},
            "enterprise_sources": {
                "ent-1": {"title": "Internal Spec", "url": "gs://kb/spec.pdf"}
            },
        }
        delta = merge_research_edit(
            state,
            [
                {
                    "field": "combined_final_cited_report",
                    "value": 'Updated claim <cite source="ent-1"/>.',
                }
            ],
        )
        assert "[Enterprise: Internal Spec]" in delta["final_report_with_citations"]
        assert (
            "### Enterprise Knowledge Sources" in delta["final_report_with_citations"]
        )
        assert "gs://" not in delta["final_report_with_citations"]


class TestDisjointResearchBucketsAndToolIsolation:
    def test_three_parallel_research_branches_use_disjoint_worker_buckets(self):
        by_name = {
            n.name: n
            for n in walk_nodes(combined_research_pipeline)
            if isinstance(n, LlmAgent)
        }
        gs_model = by_name["gs_web_searcher"].model.model
        ca_model = by_name["campaign_web_searcher"].model.model
        ent_model = by_name["enterprise_doc_searcher"].model.model

        assert gs_model == "gemini-3.8-flash"
        assert ca_model == "gemini-3.5-flash"
        assert ent_model == "gemini-3.1-flash-lite"
        assert len({gs_model, ca_model, ent_model}) == 3

    def test_enterprise_doc_searcher_tools_are_strictly_isolated_from_google_search(
        self,
    ):
        by_name = {
            n.name: n
            for n in walk_nodes(combined_research_pipeline)
            if isinstance(n, LlmAgent)
        }
        ent_searcher = by_name["enterprise_doc_searcher"]
        assert google_search not in ent_searcher.tools
        assert ent_searcher.tools == [ent_agent.search_enterprise_knowledge_base]
        assert (
            ent_searcher.after_tool_callback
            is callbacks.collect_enterprise_sources_callback
        )
