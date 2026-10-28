"""Optional inbound Discovery Engine enterprise knowledge-base researcher."""

import asyncio
import logging
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.planners import BuiltInPlanner
from google.adk.tools.discovery_engine_search_tool import DiscoveryEngineSearchTool
from google.adk.tools.function_tool import FunctionTool
from google.adk.workflow import Workflow
from google.cloud import discoveryengine_v1beta as discoveryengine
from google.genai import types
from pydantic import BaseModel, Field

from agent_common import (
    BaseAgentConfiguration,
    FailSoftNode,
    RetryUntilKeyNode,
    build_gemini,
)

from ... import callbacks
from ...config import SCHEMA_RETRY, config
from ...schemas import SearchQuery

logger = logging.getLogger(__name__)

_ENTERPRISE_FAILURE_ISSUE = (
    "Enterprise knowledge base search unavailable; continuing with public web research."
)
_MAX_ENTERPRISE_RESULTS = 6


class MultiDataStoreSearchTool(FunctionTool):
    """Fan-out search wrapper across multiple standalone Discovery Engine data stores."""

    def __init__(
        self,
        tools: tuple[DiscoveryEngineSearchTool, ...],
        *,
        max_results: int = _MAX_ENTERPRISE_RESULTS,
    ) -> None:
        super().__init__(self.discovery_engine_search)
        self.tools = tools
        self._max_results = max_results

    def discovery_engine_search(self, query: str) -> dict[str, Any]:
        """Search all configured Discovery Engine data stores and merge top hits."""
        per_store_results: list[list[dict[str, Any]]] = []
        errors: list[str] = []
        any_success = False
        if len(self.tools) > 1:
            with ThreadPoolExecutor(max_workers=len(self.tools)) as pool:
                responses = list(
                    pool.map(lambda t: t.discovery_engine_search(query), self.tools)
                )
        else:
            responses = [t.discovery_engine_search(query) for t in self.tools]

        for resp in responses:
            if resp.get("status") == "success":
                any_success = True
                items = resp.get("results")
                if isinstance(items, list):
                    per_store_results.append(items)
            else:
                errors.append(str(resp.get("error_message") or "search failed"))

        if not any_success:
            return {
                "status": "error",
                "error_message": "; ".join(errors) or "Enterprise search failed.",
            }

        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        max_len = max((len(items) for items in per_store_results), default=0)
        for idx in range(max_len):
            for store_items in per_store_results:
                if idx >= len(store_items):
                    continue
                item = store_items[idx]
                if not isinstance(item, Mapping):
                    continue
                dedupe_key = (
                    str(item.get("url") or item.get("uri") or ""),
                    str(item.get("title") or ""),
                    str(item.get("content") or item.get("snippet") or ""),
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                merged.append(dict(item))
                if len(merged) >= self._max_results:
                    return {"status": "success", "results": merged}
        return {"status": "success", "results": merged}


EnterpriseSearchTool = DiscoveryEngineSearchTool | MultiDataStoreSearchTool

_enterprise_tool_cache: dict[tuple[tuple[str, ...], str], EnterpriseSearchTool] = {}
_enterprise_tool_cache_lock = threading.Lock()


def build_enterprise_search_tools(
    cfg: BaseAgentConfiguration,
) -> tuple[DiscoveryEngineSearchTool, ...]:
    """Build one or more ``DiscoveryEngineSearchTool`` instances from ``cfg``.

    - When ``cfg.enterprise_search_engine`` is set, builds a single engine-backed
      tool (attaching ``data_store_specs`` when ``cfg.enterprise_data_stores`` is
      also non-empty).
    - When ``cfg.enterprise_search_engine`` is empty, builds one datastore-backed
      ``DiscoveryEngineSearchTool(data_store_id=...)`` per configured datastore
      (ADK rejects ``data_store_specs`` without ``search_engine_id``).
    """
    if not cfg.enterprise_grounding_configured:
        raise ValueError(
            "Enterprise grounding is not configured: set ENTERPRISE_DATA_STORES "
            "or ENTERPRISE_SEARCH_ENGINE."
        )
    if cfg.enterprise_search_engine:
        specs = [
            discoveryengine.SearchRequest.DataStoreSpec(data_store=ds)
            for ds in cfg.enterprise_data_stores
        ] or None
        return (
            DiscoveryEngineSearchTool(
                search_engine_id=cfg.enterprise_search_engine,
                data_store_specs=cast(Any, specs),
                max_results=_MAX_ENTERPRISE_RESULTS,
            ),
        )
    return tuple(
        DiscoveryEngineSearchTool(
            data_store_id=ds,
            max_results=_MAX_ENTERPRISE_RESULTS,
        )
        for ds in cfg.enterprise_data_stores
    )


def build_enterprise_search_tool(
    cfg: BaseAgentConfiguration,
) -> EnterpriseSearchTool:
    """Build an enterprise search tool (single or multi-datastore fan-out) from ``cfg``.

    Never call unconditionally at import time when enterprise grounding is
    unconfigured: ``DiscoveryEngineSearchTool`` validates resource IDs and
    initializes ADC credentials in ``__init__``.
    """
    tools = build_enterprise_search_tools(cfg)
    if len(tools) == 1:
        return tools[0]
    return MultiDataStoreSearchTool(tools, max_results=_MAX_ENTERPRISE_RESULTS)


def _get_enterprise_search_tool(
    cfg: BaseAgentConfiguration,
) -> EnterpriseSearchTool:
    cache_key = (cfg.enterprise_data_stores, cfg.enterprise_search_engine)
    with _enterprise_tool_cache_lock:
        tool = _enterprise_tool_cache.get(cache_key)
        if tool is None:
            tool = build_enterprise_search_tool(cfg)
            _enterprise_tool_cache[cache_key] = tool
        return tool


def _run_enterprise_search_sync(
    cfg: BaseAgentConfiguration, query: str
) -> dict[str, Any]:
    return _get_enterprise_search_tool(cfg).discovery_engine_search(query)


async def search_enterprise_knowledge_base(query: str) -> dict[str, Any]:
    """Search the internal enterprise knowledge base via Discovery Engine.

    Args:
        query: Specific search query for internal brand guidelines, approved
            product claims, compliance mandatories, or product specifications.

    Returns:
        A dictionary with ``status`` and ``results`` containing matching
        enterprise document chunks or records.
    """
    if not config.enterprise_grounding_configured:
        return {"status": "skipped", "results": []}
    return await asyncio.to_thread(_run_enterprise_search_sync, config, query)


class EnterpriseQueryList(BaseModel):
    """Targeted search queries for the enterprise knowledge base."""

    queries: list[SearchQuery] | None = Field(
        default=None,
        description="3-5 targeted queries for internal brand guidelines, approved claims, compliance mandatories, and product specs.",
    )


enterprise_query_planner = Agent(
    model=build_gemini(config.enterprise_research_model),
    name="enterprise_query_planner",
    mode="single_turn",
    include_contents="none",
    description="Generates targeted queries for internal enterprise brand guidelines, approved claims, and product specs.",
    instruction="""Role: You are an enterprise brand-compliance and product-knowledge research planner.
    Generate **3 to 5** targeted search queries to retrieve authoritative internal documents for this campaign.

    <INSTRUCTIONS>
    1. Review the campaign inputs in <CONTEXT>.
    2. Write **3 to 5** concise, keyword-focused queries covering:
       - Brand voice, tone rules, distinctive brand assets, and visual/verbal don'ts for {brand}
       - Approved product claims, substantiation, and technical specifications for {target_product}
       - Legal, regulatory, or brand compliance mandatories relevant to {key_selling_points} and {target_audience}
    3. Return a single JSON object matching `EnterpriseQueryList`.
    </INSTRUCTIONS>

    <CONTEXT>
        <brand>{brand}</brand>
        <target_product>{target_product}</target_product>
        <target_audience>{target_audience}</target_audience>
        <key_selling_points>{key_selling_points}</key_selling_points>
    </CONTEXT>

    ---
    ### Output Format
    **STRICT RULE: Your entire output MUST be a valid JSON object matching the 'EnterpriseQueryList' schema.**
    """,
    output_schema=EnterpriseQueryList,
    retry_config=SCHEMA_RETRY,
    output_key="initial_enterprise_queries",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


enterprise_doc_searcher = Agent(
    model=build_gemini(config.enterprise_research_model),
    name="enterprise_doc_searcher",
    mode="single_turn",
    include_contents="none",
    description="Searches the Discovery Engine enterprise knowledge base and returns raw internal findings.",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""Role: You are an enterprise knowledge-base search operator retrieving raw internal documentation.

    <INSTRUCTIONS>
    1. Read the queries in `initial_enterprise_queries` from <CONTEXT>.
    2. Call `search_enterprise_knowledge_base` for each query.
    3. Report the raw retrieved excerpts grouped by query, preserving exact document titles, URLs/URIs, approved claim wording, product specs, distinctive assets, and compliance rules. Do NOT invent documents or claims not returned by the tool.
    </INSTRUCTIONS>

    <CONTEXT>
        <initial_enterprise_queries>
        {initial_enterprise_queries?}
        </initial_enterprise_queries>
    </CONTEXT>

    ---
    ### Final Instruction
    **Output the raw internal excerpts grouped by query, preserving exact titles, URLs/URIs, and wording.**
    """,
    tools=[search_enterprise_knowledge_base],
    output_key="enterprise_search_raw",
    after_tool_callback=callbacks.collect_enterprise_sources_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


enterprise_doc_synthesizer = Agent(
    model=build_gemini(config.enterprise_research_model),
    name="enterprise_doc_synthesizer",
    mode="single_turn",
    include_contents="none",
    description="Synthesizes retrieved enterprise knowledge-base excerpts into cited internal guidance.",
    instruction="""Role: You are an enterprise brand and compliance synthesis analyst.
    Summarize the retrieved internal knowledge-base findings so downstream strategists can ground the campaign in authoritative enterprise records.

    <INSTRUCTIONS>
    1. Synthesize **only** what was retrieved in `<enterprise_search_raw>` and match each fact to its `ent-N` source identifier in `<enterprise_sources>`.
    2. Cite every internal claim, guideline, mandatory, or specification using `<cite source="ent-N" />` (e.g. `<cite source="ent-1" />`). Never invent an `ent-N` id that is not listed in `<enterprise_sources>`.
    3. If `<enterprise_search_raw>` contains no matching enterprise records, write a single short line noting that no matching internal records were found, with no fabricated claims or citations.
    </INSTRUCTIONS>

    <CONTEXT>
        <brand>{brand}</brand>
        <target_product>{target_product}</target_product>

        <enterprise_search_raw>
        {enterprise_search_raw?}
        </enterprise_search_raw>

        <enterprise_sources>
        {enterprise_sources?}
        </enterprise_sources>
    </CONTEXT>

    <REPORT_STRUCTURE>
    Structure your Markdown summary with Level 2 Headings (`##`):
    1. `## Brand Guidelines & Distinctive Assets`
    2. `## Approved Product Claims & Specifications`
    3. `## Compliance Mandatories & Prohibitions`
    </REPORT_STRUCTURE>
    """,
    output_key="enterprise_research_findings",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


enterprise_search_and_synthesize = Workflow(
    name="enterprise_search_and_synthesize",
    description="Runs enterprise Discovery Engine search then synthesizes cited internal findings.",
    edges=[("START", enterprise_doc_searcher, enterprise_doc_synthesizer)],
)


enterprise_searcher_resilient = RetryUntilKeyNode(
    name="enterprise_searcher_resilient",
    node=enterprise_search_and_synthesize,
    output_key="enterprise_research_findings",
    max_attempts=2,
)


def _enterprise_search_failed(
    state: Mapping[str, Any], exc: Exception
) -> dict[str, Any]:
    return {
        "enterprise_research_findings": "",
        "enterprise_research_findings__issues": [_ENTERPRISE_FAILURE_ISSUE],
    }


def normalize_enterprise_findings(ctx: Context) -> Event:
    """Normalize empty-source enterprise output and record tool-level API failures."""
    delta: dict[str, Any] = {}
    ent_sources = ctx.state.get("enterprise_sources")
    has_sources = isinstance(ent_sources, Mapping) and len(ent_sources) > 0
    if not has_sources:
        ctx.state["enterprise_research_findings"] = ""
        delta["enterprise_research_findings"] = ""

    successes = int(ctx.state.get("_enterprise_search_successes") or 0)
    errors = ctx.state.get("_enterprise_search_errors") or []
    if successes == 0 and errors:
        issues = [_ENTERPRISE_FAILURE_ISSUE]
        ctx.state["enterprise_research_findings__issues"] = issues
        delta["enterprise_research_findings__issues"] = issues

    return Event(actions=EventActions(state_delta=delta))


enterprise_run_workflow = Workflow(
    name="enterprise_run_workflow",
    description="Plans enterprise queries then runs resilient Discovery Engine search and synthesis.",
    edges=[
        (
            "START",
            enterprise_query_planner,
            enterprise_searcher_resilient,
            normalize_enterprise_findings,
        )
    ],
)


enterprise_research_failsoft = FailSoftNode(
    name="enterprise_research_failsoft",
    node=enterprise_run_workflow,
    on_error=_enterprise_search_failed,
)


def enterprise_grounding_gate(ctx: Context) -> Event:
    """Skip enterprise search in 0 ms when disabled or unconfigured."""
    enabled = (
        bool(ctx.state.get("enterprise_grounding_enabled", True))
        and config.enterprise_grounding_configured
    )
    if not enabled:
        skip_delta: dict[str, Any] = {
            "enterprise_research_findings": "",
            "enterprise_research_findings__issues": None,
            "enterprise_research_findings__retry_exhausted": None,
        }
        for key, value in skip_delta.items():
            ctx.state[key] = value
        return Event(
            actions=EventActions(
                route="skip",
                state_delta=skip_delta,
            )
        )
    run_delta: dict[str, Any] = {
        "enterprise_research_findings": "",
        "enterprise_research_findings__issues": None,
        "enterprise_research_findings__retry_exhausted": None,
        "_enterprise_search_successes": 0,
        "_enterprise_search_errors": [],
    }
    for key, value in run_delta.items():
        ctx.state[key] = value
    return Event(actions=EventActions(route="run", state_delta=run_delta))


def enterprise_skipped() -> None:
    """No-op terminal when enterprise grounding is disabled or unconfigured."""
    return None


enterprise_sequential_planner = Workflow(
    name="enterprise_sequential_planner",
    description="Executes gated enterprise knowledge-base research via Discovery Engine.",
    edges=[
        ("START", enterprise_grounding_gate),
        (
            enterprise_grounding_gate,
            {"run": enterprise_research_failsoft, "skip": enterprise_skipped},
        ),
    ],
)
