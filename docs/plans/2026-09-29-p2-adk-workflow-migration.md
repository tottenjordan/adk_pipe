# P2: ADK Graph-Workflow Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` (or `subagent-driven-development`) to implement this plan task-by-task.

**Status:** executing 2026-09-29 via 2026-09-29-p2-execution.md (user-scheduled; gate waived)
**Goal:** Replace every deprecated `SequentialAgent` / `ParallelAgent` in the non-test code with ADK 2.x graph `Workflow`s, and port the two custom `BaseAgent` control-flow wrappers (`RetryUntilKeyAgent`, `RunIfAgent`) so they can compose with `Workflow` nodes. Keep behavior identical: the same tool declarations for the root orchestrators, the same state keys, the same `*__retry_exhausted` markers, and passing evals. Finish by deleting the `DeprecationWarning` filter in `agent_common/__init__.py`.
**Architecture:** The root orchestrators stay `LlmAgent`s with tools (unchanged prompts). Each pipeline that is exposed today as `AgentTool(agent=<SequentialAgent>)` becomes a `Workflow(edges=[...], input_schema=PipelineRequest)`. It goes straight into `tools=[...]`, where `LlmAgent` auto-wraps it in a `NodeTool`. The parallel research becomes a tuple fan-out into an explicit `JoinNode`. `RunIfAgent` becomes a route-emitting function node plus conditional edges. `RetryUntilKeyAgent` gets a `BaseNode` sibling (`RetryUntilKeyNode`) that re-runs a child node through `ctx.run_node`. We build it ourselves because native `RetryConfig` retries only on exceptions, and it *replays* children that already produced output; the evidence is below. First, BigQuery writes become idempotent, because resumable apps re-run failed nodes/tools.
**Tech Stack:** Python 3.13, uv, google-adk 2.10.0 (`google.adk.workflow`), pydantic 2, pytest (offline `InMemoryRunner` tests), `adk eval` (live), ruff, ty.

**Conventions:**
- Branch off `main`, with one PR per task group (idempotency, T0+T1, T2, T3, T4). Squash-merge.
- Commit messages must **NEVER** contain `Co-Authored-By` trailers or any AI/"Generated with Claude Code" attribution. This follows CODE_STANDARDS §1 and a standing user rule, and PR bodies are held to the same rule.
- Run everything through `uv run`. Before each commit, run `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest tests/ -q`, with `GOOGLE_CLOUD_PROJECT=test-project` if `.env` is absent.
- Agent Engine / Cloud Run deploys are out of scope for this plan. Redeploy just-in-time per the batch-sync runbook, and pin `trend-trawler-api` traffic after any api deploy.

---

## Current state

The deprecation notice (verbatim from `google/adk/agents/sequential_agent.py:89` and `parallel_agent.py:246`, via `typing_extensions.deprecated`):

> `SequentialAgent is deprecated in favor of Workflow and will be removed in a future version. Workflow cannot yet be used as an LlmAgent sub-agent.`

`LoopAgent` carries the same text (`loop_agent.py:62`). The repo does not use `LoopAgent`. `agent_common/__init__.py:30-34` silences `r"(Sequential|Parallel)Agent is deprecated in favor of Workflow"`.

| # | file:line | What it is | Target Workflow construct |
|---|---|---|---|
| 1 | `trend_scout/agent.py:111` | `understand_trends_search_and_synthesize` = `SequentialAgent[searcher, synthesizer]` | `Workflow(edges=[("START", searcher, synthesizer)])` |
| 2 | `trend_scout/agent.py:127` | `understand_trends_agent_resilient` = `RetryUntilKeyAgent(output_key="info_gtrends")`, exposed as `AgentTool` | `RetryUntilKeyNode(node=<#1>, input_schema=PipelineRequest)` placed directly in `tools=[...]` |
| 3 | `creative_agent/sub_agents/trend_researcher/agent.py:172` | `gs_search_and_synthesize` Sequential pair | `Workflow` chain |
| 4 | `…/trend_researcher/agent.py:183` | `gs_web_searcher_resilient` RetryUntilKeyAgent | `RetryUntilKeyNode` |
| 5 | `…/trend_researcher/agent.py:190` | `gs_sequential_planner` = Sequential[planner, #4] | `Workflow` chain (branch of #9) |
| 6 | `creative_agent/sub_agents/campaign_researcher/agent.py:192` | `campaign_search_and_synthesize` Sequential pair | `Workflow` chain |
| 7 | `…/campaign_researcher/agent.py:203` | `campaign_web_searcher_resilient` RetryUntilKeyAgent | `RetryUntilKeyNode` |
| 8 | `…/campaign_researcher/agent.py:210` | `ca_sequential_planner` Sequential[planner, #7] | `Workflow` chain (branch of #9) |
| 9 | `creative_agent/agent.py:38` | `parallel_planner_agent` = **ParallelAgent**[gs, ca] | tuple fan-out `("START", (gs, ca), research_join)` + `JoinNode` |
| 10 | `creative_agent/agent.py:55` | `merge_parallel_insights` = Sequential[#9, merge_planners] | folded into #13's edges (`research_join → merge_planners`) |
| 11 | `creative_agent/agent.py:112,125` | `refined_search_and_synthesize` pair + `enhanced_combined_searcher_resilient` | `Workflow` chain + `RetryUntilKeyNode` |
| 12 | `creative_agent/agent.py:187` | `research_refinement_block` = `RunIfAgent(predicate=_base_research_is_degraded)` | function node `refinement_gate` emitting `Event(route="refine"/"skip")` + routing map |
| 13 | `creative_agent/agent.py:199` | `combined_research_pipeline` Sequential (AgentTool on both roots) | top-level `Workflow` with `input_schema=PipelineRequest` |
| 14 | `creative_agent/agent.py:270` | `ad_creative_pipeline` Sequential[drafter, critic] | `Workflow` chain |
| 15 | `creative_agent/agent.py:449` | `visual_generator_resilient` RetryUntilKeyAgent (max 6), shared with interactive | `RetryUntilKeyNode` |
| 16 | `creative_agent/agent.py:459` | `visual_generation_pipeline` Sequential[4 agents] (shared) | `Workflow` chain |
| 17 | `creative_agent/agent.py:476` | `visual_production_pipeline` Sequential[#16, #15] | `Workflow` chain (nested) |
| 18 | `interactive_creative/agent.py:117-123` | `AgentTool(...)` over #13/#14/#16/#15 + `visual_concept_reviser` | bare nodes in `tools=[...]` (T3) |

Structure tests that assert on these (all must be rewritten or kept green):
- `tests/test_pipeline_structure.py`: `test_combined_research_pipeline_sub_agent_order` (l.37, uses `isinstance(gate, RunIfAgent)`, `gate is research_refinement_block`, `isinstance(pair, SequentialAgent)`), `test_ad_creative_pipeline_sub_agent_order` (l.134), `test_visual_generation_pipeline_sub_agent_order` (l.141), `test_visual_production_pipeline_wraps_generator_in_retry` (l.179), `test_parallel_planner_has_both_researchers` (l.199), `test_campaign_producer_is_retry_wrapped` (l.207), `test_trend_producer_is_retry_wrapped` (l.257), `test_interactive_creative_uses_resilient_visual_generator` (l.589), `test_trend_scout_root_has_expected_tools` (l.617), `test_understand_trends_is_retry_wrapped` (l.717, filters `isinstance(t, AgentTool)`), `test_creative_agent_root_has_expected_tools` (l.4).
- `tests/test_retry_agent.py`: uses `SequentialAgent` pairs (l.219-260) as the wrapped child. It stays as the regression suite for the legacy class until T4.
- `tests/test_conditional_agent.py`: uses `SequentialAgent` (l.120, 136) to seed upstream state. It stays until T4 deletes `RunIfAgent`.
- `tests/test_public_api.py`: identity checks on the `creative_agent` facade. These are unaffected, because the module objects stay the same.

---

## Workflow API summary (from installed google-adk 2.10 source)

All paths are relative to `.venv/lib/python3.13/site-packages/google/adk/`. Everything marked **[spike]** is inferred from the source and must be proven by T0's offline tests before T1.

- **Public surface.** `google.adk.workflow.__all__` = `BaseNode, DEFAULT_ROUTE, Edge, FunctionNode, JoinNode, Node, NodeTimeoutError, RetryConfig, START, Workflow, node`.
- **`class Workflow(BaseNode)`** (`workflow/_workflow.py`) has these fields: `edges: list[EdgeItem]`, `max_concurrency: int | None`, `graph: Graph | None` (compiled in `model_post_init` via `Graph.from_edge_items` + `validate_graph`), and `rerun_on_resume: bool = True`.
  - Its docstring says: "_run_impl() IS the graph orchestration loop". Static node state is "reconstructed from session events on resume".
- **`BaseNode`** (`workflow/_base_node.py`) has these fields: `name` (must be `str.isidentifier()`), `description`, `rerun_on_resume=False`, `wait_for_output=False`, `retry_config: RetryConfig | None`, `timeout: float | None`, `input_schema`, `output_schema`, `state_schema`.
  - `BaseAgent` itself subclasses `BaseNode` (`agents/base_agent.py:99`). Its `_run_impl` adapts `run_async(parent_context=ctx.get_invocation_context())`, so **any existing BaseAgent, including our wrappers, is a valid graph node**.
- **Edges** (`workflow/_graph.py`):
  - `EdgeItem = Edge | tuple[ChainElement, ...]`.
  - `ChainElement = NodeLike | tuple[NodeLike, ...] | RoutingMap`.
  - `NodeLike = BaseNode | BaseTool | Callable | Literal["START"]`.
  - `Edge(from_node, to_node, route: RouteValue | list[RouteValue] | None)`, with `RouteValue = bool | int | str` and `DEFAULT_ROUTE = "__DEFAULT__"`.
  - A tuple inside a chain is a **fan-out**. A dict is a **routing map**, e.g. `{"refine": node_a, "skip": node_b}`.
  - Validation (`utils/_graph_validation.py`) rejects duplicate node names, missing START, unconditional cycles, duplicate edges and bad default routes.
- **Node building** (`utils/_workflow_graph_utils.py:build_node`):
  - Plain callables become `FunctionNode`. Params are bound from `ctx.state` by default (`parameter_binding='state'`), and `ctx`/`node_input` are special names.
  - A `BaseTool` becomes `_ToolNode`.
  - An **`LlmAgent` is `clone()`d** with `rerun_on_resume=True`. Its mode defaults to `'single_turn'` if the agent has no `parent_agent`, otherwise `'chat'` (and then `wait_for_output=True`).
  - ⇒ Graph nodes are *copies*, so tests must compare names, not identity. An LlmAgent that is still inside some `sub_agents=[...]` would silently switch to chat mode. Set `mode="single_turn"` explicitly on every agent placed in a graph.
- **Fan-in.** `JoinNode(BaseNode)` has `_requires_all_predecessors = True` and outputs a dict of predecessor outputs. Without it, a node with two incoming edges is triggered **once per predecessor** (`_workflow.py:_buffer_downstream_triggers`). When more than one successor fires, each runs with `use_sub_branch=True`, which gives the same conversation-branch isolation as `ParallelAgent`.
- **Conditional routing.** A node emits `Event(route=...)`; `Event`'s convenience kwargs map `route→actions.route` and `state→actions.state_delta` (`events/event.py:172`). `Graph.get_next_pending_nodes` follows matching routes, falls back to `DEFAULT_ROUTE`, and logs a warning (ending the branch) if nothing matches.
- **Retry.** `workflow.RetryConfig(max_attempts, initial_delay, max_delay, backoff_factor, jitter, exceptions)` retries **only on exceptions** (`exceptions=None` = all).
  - On a `Workflow`: "a failure of any node inside it is a failure of the workflow, so the whole sub-workflow is retried. Children that already produced an output or a state change are **replayed rather than run again**" (`_base_node.py` docstring).
  - ⇒ It cannot express "re-run searcher+synth until `output_key` is non-blank". An empty synthesizer turn is *not* an exception, and the searcher (which did write `*_raw`) would be replayed, not re-run.
  - **Decision:** keep a custom retry node. Native `retry_config` can be *added* for infra exceptions, but it does not replace `RetryUntilKeyAgent`.
- **output_key inside a graph** (`workflow/_llm_agent_wrapper.py:process_llm_agent_output`):
  - The final non-thought text (or `validate_schema(output_schema, text)`) becomes `event.output`, and when set, `ctx.actions.state_delta[agent.output_key] = output`.
  - An empty text turn writes `""`, which `_is_populated("")` already treats as empty.
  - In `single_turn` mode the predecessor's output is **injected as a user event** (`to_user_content(node_input)`), except on resume. **[spike]** confirm token/behavior parity for `include_contents="none"` agents such as `merge_planners`, which would now also see the JoinNode dict.
- **Workflows as tools.**
  - `AgentTool.__init__(agent: BaseAgent, ...)` builds a fresh sub-`Runner` over `InMemorySessionService`, and its type hint rejects a `Workflow`.
  - Instead, `LlmAgent`'s tool validator (`agents/llm_agent.py:1261`) auto-wraps any non-agent `BaseNode` in `NodeTool` (`tools/_node_tool.py`). `NodeTool` **requires an explicit pydantic `input_schema`**, sets `is_long_running = True`, and runs the node via `tool_context.run_node(node, override_branch=f"{branch}.{name}@{fc_id}", raise_on_wait=True)`.
  - That means it runs **in the parent session** (events and state land directly, with no isolated sub-Runner). Exceptions are swallowed into `"Error running node …"` strings, and `NodeInterruptedError` is re-raised.
  - **[spike]** `run_node_internal` raises unless `ctx._node_rerun_on_resume`. Confirm this holds for a tool call from a legacy (non-graph) root `LlmAgent`. Also confirm that `is_long_running=True` with a returned value does **not** pause a resumable App (`trend_scout`, `interactive_creative`).
- **Dynamic child runs.** `Context.run_node(node, node_input=None, *, use_as_output=False, run_id=None, use_sub_branch=False, override_branch=None, override_isolation_scope=None, raise_on_wait=False)`. An explicit `run_id` must contain a non-digit. The caller must have `rerun_on_resume=True`. This is the primitive for `RetryUntilKeyNode`.
- **Runner / App.**
  - `Runner(*, app=None, app_name=None, agent: BaseAgent | None = None, node: BaseNode | None = None, ...)`, and `App.root_agent: InstanceOf[BaseNode]`, so a Workflow can be a root.
  - However, `runners.py:1151` notes that the non-agent node path still lacks "tracing and plugins". **We keep LlmAgent roots**, so this does not bite.
- **Resumability.** `ResumabilityConfig.is_resumable` (`apps/_configs.py:29`) says: "Tool call to resume needs to be idempotent because we only guarantee an at-least-once behavior once resumed."
  - The rehydrator (`utils/_rehydration_utils.py:~396`) treats "the node's outcome [as] whatever its latest attempt recorded", i.e. failed nodes re-run.
  - `LongRunningFunctionTool` (our review checkpoints) is unchanged. `RequestInput` is the graph-native HITL primitive, but we do not adopt it here.

**Docs / timeline (external):**
- adk.dev/2.0 and adk.dev/graphs confirm `Workflow(edges=[("START", a, b)])` chains and `Event(route=...)` routers. Neither publishes Python examples for JoinNode or retry.
- **No removal version has been announced**; the source and docs only say "a future version". **[unverified]** A secondary web result says ADK Python 2.0 went GA on 2026-05-19.
- Known upstream gaps:
  - google/adk-python#5872 (Workflow can't be a `sub_agents` entry; closed without a fix).
  - #5780 (context lost when migrating Sequential→Workflow with AgentTools; closed, fix version unknown **[unverified]**).
- The Google Developer Knowledge MCP was unreachable during research (auth error), so it was not consulted.

---

## Timing / gate

Start only when **one** of these holds:
1. ADK publishes a removal version/date for `SequentialAgent`/`ParallelAgent`. Watch the adk-python release notes for a breaking-change entry.
2. The repo schedules its "ADK 2 showcase" milestone.

The idempotency prerequisites (next section) are worth doing **now regardless**. They fix a real at-least-once hazard in today's resumable apps and CRF retries.

Re-verify the API summary against the then-current ADK before T0: re-run the T0 spike tests after `uv lock --upgrade-package google-adk`.

---

## Idempotency prerequisites

Rule: a run (session) may execute any BigQuery write tool **more than once** and must still leave exactly one logical row. The key is derived deterministically from `tool_context.session.id` (`ReadonlyContext.session`, `agents/readonly_context.py:62`), never from `uuid4`. The write uses `MERGE … WHEN NOT MATCHED THEN INSERT`.

### Task I-1: deterministic `creative_row_uuid` + MERGE in `creative_agent/bq_tools.py:write_trends_to_bq`

**Files:** `creative_agent/bq_tools.py`, `tests/test_tools.py` (class `TestWriteTrendsUuidStash`)

1. Write the failing tests. Add these to `tests/test_tools.py`, reusing the file's `MockToolContext`, `_Job` and `_BQ` shapes:

```python
from types import SimpleNamespace


class TestWriteTrendsIdempotent:
    def _ctx(self, session_id="sess-abc"):
        ctx = MockToolContext()
        ctx.session = SimpleNamespace(id=session_id)
        ctx.state.update(
            {
                "gcs_folder": "f",
                "agent_output_dir": "d",
                "target_search_trends": "tswift engaged",
                "brand": "PRS",
                "target_audience": "musicians",
                "target_product": "SE CE24",
                "key_selling_points": "tone",
            }
        )
        return ctx

    def _patch(self, monkeypatch, captured):
        import creative_agent.bq_tools as t

        class _Job:
            errors = None
            job_id = "j"
            num_dml_affected_rows = 1

            def result(self):
                return None

        class _BQ:
            def query(self, sql, job_config=None):
                captured.append((sql, job_config))
                return _Job()

        monkeypatch.setattr(t, "_get_bigquery_client", lambda: _BQ())
        return t

    def test_same_session_same_uuid(self, monkeypatch):
        captured: list = []
        t = self._patch(monkeypatch, captured)
        a, b = self._ctx(), self._ctx()
        t.write_trends_to_bq(a)
        t.write_trends_to_bq(b)
        assert a.state["creative_row_uuid"] == b.state["creative_row_uuid"]
        assert len(a.state["creative_row_uuid"]) == 8  # schema/CRF-join compat

    def test_different_sessions_differ(self, monkeypatch):
        t = self._patch(monkeypatch, [])
        a, b = self._ctx("s1"), self._ctx("s2")
        t.write_trends_to_bq(a)
        t.write_trends_to_bq(b)
        assert a.state["creative_row_uuid"] != b.state["creative_row_uuid"]

    def test_uses_merge_not_blind_insert(self, monkeypatch):
        captured: list = []
        t = self._patch(monkeypatch, captured)
        t.write_trends_to_bq(self._ctx())
        sql = captured[0][0]
        assert "MERGE" in sql and "WHEN NOT MATCHED" in sql
        assert "tswift engaged" not in sql  # still parameterized
```

2. Run `uv run pytest tests/test_tools.py -k Idempotent -v`. Expect it to FAIL (uuid4 differs; SQL is `INSERT INTO`).
3. Implement:
   - Add a pure helper `stable_row_id(*parts: str, length: int = 8) -> str` (`hashlib.sha256("|".join(parts)).hexdigest()[:length]`) in a new `agent_common/idempotency.py`, exported from `agent_common/__init__.py`.
   - In `write_trends_to_bq`, set `unique_id = stable_row_id(tool_context.session.id, target_trend)`.
   - Replace the `INSERT` with `MERGE \`{table}\` T USING (SELECT @unique_id AS uuid, …) S ON T.uuid = S.uuid WHEN NOT MATCHED THEN INSERT (...) VALUES (...)`.
   - Update the existing `TestWriteTrendsUuidStash` / `TestWriteTrendsRaisesOnBqErrors` contexts to carry `ctx.session`.
4. Add `tests/test_agent_common_idempotency.py` covering determinism, length and separator-collision (`("a|b","c")` ≠ `("a","b|c")`; hash `json.dumps(parts)` instead of `"|".join` if needed).
5. Run the full suite plus ty. Commit: `fix(bq): make creative write_trends_to_bq idempotent (session-derived uuid + MERGE)`

### Task I-2: `write_eval_report_to_bq` becomes idempotent

**Files:** `creative_agent/bq_tools.py`, `tests/test_tools.py`

1. Write the failing test:

```python
class TestWriteEvalReportIdempotent:
    def test_eval_uuid_is_session_derived_and_merge_used(self, monkeypatch):
        import creative_agent.bq_tools as t

        captured: list = []

        class _Job:
            errors = None
            num_dml_affected_rows = 1

            def result(self):
                return None

        class _BQ:
            def query(self, sql, job_config=None):
                captured.append((sql, job_config))
                return _Job()

            def insert_rows_json(self, *a, **k):  # must no longer be used
                raise AssertionError("streaming insert is not idempotent")

        monkeypatch.setattr(t, "_get_bigquery_client", lambda: _BQ())
        ctx = MockToolContext()
        ctx.session = SimpleNamespace(id="sess-1")
        ctx.state.update(
            {
                "creative_evaluation_report": {"summary": {}},
                "creative_row_uuid": "abcd1234",
            }
        )
        r1 = t.write_eval_report_to_bq(ctx)
        r2 = t.write_eval_report_to_bq(ctx)
        assert r1["eval_uuid"] == r2["eval_uuid"]
        assert all("MERGE" in sql for sql, _ in captured)
```

2. Run it and confirm it FAILS.
3. Implement:
   - `eval_uuid = stable_row_id(session.id, "eval")`.
   - Swap `insert_rows_json` for a parameterized `MERGE … ON T.uuid = S.uuid WHEN NOT MATCHED THEN INSERT`, built from `build_eval_bq_row` so the row dict stays the single source of columns.
   - Put the SQL in a pure `_build_eval_merge_sql(table, row)` and unit-test it separately.
   - Keep raising on `job.errors`.
   - This drops the legacy insertId (`row_ids`) approach, which is only best-effort (a ~1-minute dedupe window). **[verify]** MERGE against a table that still has old streaming-buffer rows: an INSERT-only MERGE should be allowed, so confirm once in a scratch dataset.
4. Commit: `fix(bq): make write_eval_report_to_bq idempotent via session-derived id + MERGE`

### Task I-3: `trend_scout/tools.py:write_trends_to_bq`

This is also reachable from a resumable App (`review_trends_tool`), and it inserts one row per trend with a uuid4.

**Files:** `trend_scout/tools.py` (`_build_trend_insert_sql`), `tests/test_tools.py`

- Test: calling it twice with the same `ctx.session.id` yields identical `unique_id` params for each trend, and the SQL contains `MERGE`. The MERGE key is `(uuid, target_trend)`.
- Keep `_build_trend_insert_sql` pure and extend its existing SQL-builder tests (`tests/test_tools.py:141`).
- Commit: `fix(bq): make trend_scout write_trends_to_bq idempotent`

GCS writes (`save_*_to_gcs`, `generate_image`) already overwrite deterministic paths or are guarded (`_images_generated`), so they are acceptable at-least-once. Re-check them in T3.

---

## Task list

### Task T0: API spike as offline tests (no production code)

**Files:** create `tests/test_workflow_api_contract.py`. This pins the upstream behaviors we depend on, so an ADK bump that breaks them fails CI instead of prod. Use `InMemoryRunner` + `asyncio.run` + fake `BaseAgent` producers, following `tests/test_retry_agent.py`. No model calls.

```python
import asyncio

from google.adk.agents import BaseAgent
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.runners import InMemoryRunner
from google.adk.workflow import JoinNode, Workflow
from google.genai import types


class _Writer(BaseAgent):
    key: str
    value: str = "v"

    async def _run_async_impl(self, ctx):
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            actions=EventActions(state_delta={self.key: self.value}),
        )


def _run_node(node):
    runner = InMemoryRunner(node=node, app_name="wf_contract")

    async def go():
        s = await runner.session_service.create_session(
            app_name="wf_contract", user_id="u"
        )
        async for _ in runner.run_async(
            user_id="u",
            session_id=s.id,
            new_message=types.Content(role="user", parts=[types.Part(text="go")]),
        ):
            pass
        return await runner.session_service.get_session(
            app_name="wf_contract", user_id="u", session_id=s.id
        )

    return asyncio.run(go())


def test_fan_out_join_runs_merge_once():
    a, b = _Writer(name="a", key="ka"), _Writer(name="b", key="kb")
    merged = []

    def merge(ctx):
        merged.append((ctx.state.get("ka"), ctx.state.get("kb")))

    wf = Workflow(name="wf", edges=[("START", (a, b), JoinNode(name="join"), merge)])
    _run_node(wf)
    assert merged == [("v", "v")]  # once, after both branches


def test_route_skips_unselected_branch():
    ran = []

    def gate(ctx):
        return Event(route="skip")

    def refine(ctx):
        ran.append("refine")

    def compose(ctx):
        ran.append("compose")

    wf = Workflow(
        name="wf",
        edges=[
            ("START", gate),
            (gate, {"refine": refine, "skip": compose}),
            (refine, compose),
        ],
    )
    _run_node(wf)
    assert ran == ["compose"]
```

Also add these tests, which gate T1:
- (a) `test_workflow_in_llm_agent_tools_is_wrapped_as_nodetool`: build `LlmAgent(name="r", model="gemini-x", tools=[Workflow(..., input_schema=PipelineRequest)])` and assert `isinstance(r.tools[0], NodeTool)`. Assert that `r.tools[0]._get_declaration().parameters_json_schema["properties"]` equals `{"request": {"type": "string", ...}}`, the same shape `AgentTool` declares today.
- (b) `test_nodetool_runs_from_legacy_llm_root`: drive the NodeTool from a root `LlmAgent` with a stub `BaseLlm` that emits one function call and then text (pattern: a fake model class returning canned `LlmResponse`s). Assert that the wrapped workflow's state keys land in the **parent** session and that the run is not paused under `App(resumability_config=ResumabilityConfig(is_resumable=True))`.
- (c) `test_run_node_reexecutes_with_distinct_run_ids`: a flaky child that fails to write its key twice is re-executed, not replayed, when called via `ctx.run_node(child, run_id=f"attempt_{n}")`.

If (b) or (c) fails, **stop**. Record the blocker in this doc and wait for upstream; see the open questions below.

Run `uv run pytest tests/test_workflow_api_contract.py -v`.
Commit: `test: pin ADK Workflow API contracts relied on by the P2 migration`

### Task T1: trend_scout (smallest case)

**Files:**
- `agent_common/retry_node.py` (new), `agent_common/schemas.py` (new: `PipelineRequest(BaseModel): request: str`), `agent_common/__init__.py`
- `trend_scout/agent.py:111-135` plus the `tools=[...]` list
- `tests/test_retry_node.py` (new), `tests/test_pipeline_structure.py:617-740`

1. Write the failing tests for `RetryUntilKeyNode` in `tests/test_retry_node.py`. Port the `test_retry_agent.py` cases one-for-one: recover after N empties, exhaust → `<key>__retry_exhausted`, flag producer, and a split pair where **both** searcher and synthesizer re-run. The child is a `Workflow` pair instead of a `SequentialAgent`:

```python
def test_node_retries_whole_pair_until_populated():
    searcher = _RawSearcher(name="s", raw_key="raw")
    synth = _FlakySynthesizer(name="y", raw_key="raw", output_key="out", fail_first=2)
    pair = Workflow(name="pair", edges=[("START", searcher, synth)])
    wrapper = RetryUntilKeyNode(name="w", node=pair, output_key="out", max_attempts=3)
    session = _run_node(wrapper)
    assert session.state["out"] == "REAL_REPORT"
    assert searcher.runs == 3 and synth.runs == 3  # re-run, not replayed
    assert "out__retry_exhausted" not in session.state
```

   Import the fakes from `tests/test_retry_agent.py`, or move them into `tests/_fakes.py`.
2. Implement `RetryUntilKeyNode(BaseNode)`:
   - Fields: `node: BaseNode`, `output_key: str`, `max_attempts: int = 3`, `rerun_on_resume: bool = True`.
   - `_run_impl` loops `await ctx.run_node(self.node, node_input=node_input, run_id=f"{self.name}_attempt_{n}")` and checks `RetryUntilKeyAgent._is_populated(ctx.state.get(self.output_key))`. Reuse that staticmethod; do not copy it.
   - On exhaustion it yields `Event(state={f"{self.output_key}__retry_exhausted": True})`.
   - Keep the same log lines, so `make_final_state_summary` / `collect_degradation_warnings` stay unchanged.
3. Rewrite `test_understand_trends_is_retry_wrapped`:

```python
def test_understand_trends_is_retry_wrapped():
    from google.adk.tools._node_tool import NodeTool
    from google.adk.workflow import Workflow

    from agent_common import RetryUntilKeyNode
    from trend_scout.agent import root_agent

    nodes = [t.node for t in root_agent.tools if isinstance(t, NodeTool)]
    matching = [
        n
        for n in nodes
        if isinstance(n, RetryUntilKeyNode) and n.output_key == "info_gtrends"
    ]
    assert matching, "no NodeTool wraps a RetryUntilKeyNode producing info_gtrends"
    pair = matching[0].node
    assert isinstance(pair, Workflow)
    names = [n.name for n in pair.graph.nodes if n.name != "__START__"]
    assert names == ["understand_trends_searcher", "understand_trends_synthesizer"]
    by_name = {n.name: n for n in pair.graph.nodes}
    assert by_name["understand_trends_searcher"].output_key == "info_gtrends_raw"
    assert by_name["understand_trends_synthesizer"].output_key == "info_gtrends"
```

   Also add `test_understand_trends_tool_declaration_unchanged`. It asserts the tool name `understand_trends_agent_resilient`, the description equal to `understand_trends_searcher.description`, and a `request: string` parameter. This protects `TREND_SCOUT_INSTR`.
4. Run the tests and confirm they FAIL. Then implement in `trend_scout/agent.py`:
   - `understand_trends_search_and_synthesize = Workflow(name=..., edges=[("START", understand_trends_searcher, understand_trends_synthesizer)])`, with both agents set to `mode="single_turn"`.
   - `understand_trends_agent_resilient = RetryUntilKeyNode(name=..., description=understand_trends_searcher.description, node=..., output_key="info_gtrends", max_attempts=3, input_schema=PipelineRequest)`.
   - In `tools=[...]`, replace `AgentTool(agent=understand_trends_agent_resilient)` with the bare node.
   - Update `test_trend_scout_root_has_expected_tools` (l.617) if it reads `t.agent`.
5. Parity:
   - `uv run pytest tests/test_pipeline_structure.py tests/test_retry_node.py tests/test_workflow_api_contract.py -v`, then the full suite and `uv run ty check`.
   - Then run the live eval. It makes **real API calls** (~5 min per case, 2 cases, quota-bound) and needs ADC + `.env`:
     `PYTHONPATH="$PWD" uv run adk eval trend_scout tests/eval/evalsets/trend_scout_evalset.json --config_file_path=tests/eval/eval_config.json --print_detailed_results`
   - Compare the rubric scores with a baseline run on `main` taken the same day. Accept a result within the run-to-run noise.
   - Check the session log: exactly one `understand_trends_agent_resilient` function response, and no `Error running node` strings.
6. Commit: `refactor(trend_scout): move understand_trends pair to graph Workflow + RetryUntilKeyNode`

### Task T2: creative_agent research (Parallel), then the remaining Sequentials

**Files:**
- `creative_agent/sub_agents/{trend_researcher,campaign_researcher}/agent.py`
- `creative_agent/agent.py:38-210, 270, 449-490`
- `creative_agent/__init__.py` (the facade keeps the same names)
- `tests/test_pipeline_structure.py` (tests listed in "Current state")

**T2a: research graph.** Write the failing structure test first:

```python
def test_combined_research_pipeline_graph():
    from google.adk.workflow import JoinNode, Workflow

    from creative_agent.agent import combined_research_pipeline as wf

    assert isinstance(wf, Workflow)
    names = {n.name for n in wf.graph.nodes}
    assert {
        "gs_sequential_planner",
        "ca_sequential_planner",
        "research_join",
        "merge_planners",
        "refinement_gate",
        "combined_web_evaluator",
        "enhanced_combined_searcher_resilient",
        "combined_report_composer",
    } <= names
    edges = {(e.from_node.name, e.to_node.name, e.route) for e in wf.graph.edges}
    assert ("__START__", "gs_sequential_planner", None) in edges
    assert ("__START__", "ca_sequential_planner", None) in edges  # parallel fan-out
    assert isinstance(
        next(n for n in wf.graph.nodes if n.name == "research_join"), JoinNode
    )
    assert ("refinement_gate", "combined_web_evaluator", "refine") in edges
    assert ("refinement_gate", "combined_report_composer", "skip") in edges
    assert (
        "enhanced_combined_searcher_resilient",
        "combined_report_composer",
        None,
    ) in edges


def test_refinement_gate_routes_on_degradation():
    from creative_agent.agent import refinement_gate_route  # pure helper under test

    assert refinement_gate_route({"combined_web_search_insights": "ok"}) == "skip"
    assert refinement_gate_route({}) == "refine"
    assert (
        refinement_gate_route(
            {
                "combined_web_search_insights": "ok",
                "gs_web_search_insights__retry_exhausted": True,
            }
        )
        == "refine"
    )
```

(`refinement_gate_route(state) -> str` is the pure helper that wraps `_base_research_is_degraded`. Keep `test_research_refinement_gate_predicate` as is.)

Implement:

```python
research_join = JoinNode(name="research_join")

def refinement_gate(ctx) -> Event:
    return Event(route=refinement_gate_route(ctx.state))

combined_research_pipeline = Workflow(
    name="combined_research_pipeline",
    description=<unchanged>,
    input_schema=PipelineRequest,
    edges=[
        ("START", (gs_sequential_planner, ca_sequential_planner), research_join,
         merge_planners, refinement_gate),
        (refinement_gate, {"refine": combined_web_evaluator, "skip": combined_report_composer}),
        (combined_web_evaluator, enhanced_combined_searcher_resilient, combined_report_composer),
    ],
)
```

- `gs_sequential_planner` / `ca_sequential_planner` become `Workflow` chains `(planner, <RetryUntilKeyNode over pair Workflow>)`.
- Delete `parallel_planner_agent` and `merge_parallel_insights`. Update `test_parallel_planner_has_both_researchers` to assert the two START fan-out edges.
- `combined_report_composer` keeps its `after_agent_callback=citation_replacement_callback`. **[verify]** LlmAgent callbacks still fire on the node path; `test_callbacks.py` coverage plus the eval checks this.
- The campaign branch still runs on `campaign_models()` (quota-spread invariant, `test_campaign_pipeline_uses_distinct_global_bucket`), because the models are untouched.

**T2b: remaining Sequentials.**
- `ad_creative_pipeline`, `visual_generation_pipeline` and `visual_production_pipeline` become `Workflow` chains.
- `visual_generator_resilient` becomes `RetryUntilKeyNode(node=visual_generator, output_key="_images_generated", max_attempts=6)`.
- All of them get `input_schema=PipelineRequest`. The root `tools=[...]` drop `AgentTool(...)` for these, while `creative_eval_agent` (an LlmAgent) keeps `AgentTool`.
- Rewrite the order tests to read `[n.name for n in wf.graph.nodes if n.name != "__START__"]`.
- `test_structured_output_producers_carry_schema_retry` must look agents up by name in the graph: nodes are clones, and `retry_config`/`output_schema` survive `clone()`.

**Parity:**
- Full offline suite plus ty.
- Then the live eval (**real API calls**, ~5 min per case, 2 cases, PRO 5-RPM / image 2-RPM quota bound): `PYTHONPATH="$PWD" uv run adk eval creative_agent tests/eval/evalsets/creative_agent_evalset.json --config_file_path=tests/eval/creative_eval_config.json --print_detailed_results`
- Also check a wall-clock regression against a same-day `main` baseline, which must be ≤ +10%. Use `experiments/` latency harness logs if available.

Commits:
- `refactor(creative_agent): research pipeline as graph Workflow (fan-out/JoinNode, routed refinement)`
- `refactor(creative_agent): ad/visual pipelines as graph Workflows; RetryUntilKeyNode for image step`

### Task T3: interactive_creative resume path

**Files:** `interactive_creative/agent.py:117-123`, `tests/test_pipeline_structure.py:589-616, 809-848`, `tests/test_async_runs.py`

Prerequisites: I-1..I-3 merged, and T0(b) proved that NodeTool does not pause a resumable App.

1. Write the failing tests.
   - `test_interactive_creative_uses_resilient_visual_generator` should assert a `NodeTool` whose `.node.name == "visual_generator_resilient"` and whose `.node` is a `RetryUntilKeyNode`. It should also assert that `visual_concept_reviser` (an LlmAgent) is still an `AgentTool`.
   - Add an offline resume test in `tests/test_async_runs.py` style. Run `App(resumability_config=ResumabilityConfig(is_resumable=True))` with a stub LLM that:
     - calls `review_research` (LongRunningFunctionTool) → pause;
     - resume with the function response → calls a Workflow tool whose second node raises once;
     - resume again.
   - Assert that the first node ran **once** (replayed, not re-executed), that the failed node re-ran, and that a patched `write_trends_to_bq` stub called twice produced one logical key (via `stable_row_id`).
2. Implement: swap the four `AgentTool(...)` wrappers for the bare Workflow/RetryUntilKeyNode objects imported from the `creative_agent` facade. There are no other changes; the checkpoint tools stay `LongRunningFunctionTool`.
3. Parity:
   - Offline suite.
   - A manual local run: `ALLOW_ORIGINS=http://localhost:3000 uv run uvicorn deployment.async_app:app --port 8000` plus the frontend. Pass all 3 checkpoints, reload mid-run (it should replay) and edit concepts at checkpoint 3 (`visual_revision_notes` is consumed).
   - Then the creative_agent eval as a smoke test, since interactive has no evalset.
4. Commit: `refactor(interactive_creative): expose shared pipelines as Workflow NodeTools on the resumable app`

### Task T4: cleanup, docs, diagrams

1. **Remove the warning filter.**
   - Delete `warnings.filterwarnings(...)` and the `import warnings` from `agent_common/__init__.py:1-34`.
   - Add a guard test in `tests/test_public_api.py`:

```python
def test_no_deprecated_workflow_agents_instantiated():
    import importlib, warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        for mod in (
            "trend_scout.agent",
            "creative_agent.agent",
            "interactive_creative.agent",
        ):
            importlib.reload(importlib.import_module(mod))
```

   - Also add a lint-style grep test: no `SequentialAgent|ParallelAgent|LoopAgent` in non-test `*.py` outside `.venv`.
2. **Retire the legacy wrappers.**
   - Delete `RunIfAgent` (`agent_common/conditional_agent.py`) and `tests/test_conditional_agent.py`. The routed gate replaces it, and its semantics are covered by T0's route test and T2's gate test.
   - Delete `RetryUntilKeyAgent` once nothing imports it. Move `_is_populated` into `retry_node.py` and port any `test_retry_agent.py` cases not already mirrored.
   - Update `agent_common/__init__.py` `__all__` and `tests/test_public_api.py`.
3. **Update CLAUDE.md.**
   - Replace the "Agent Composition" tree with Workflow terms. For example, `combined_research_pipeline (Workflow): START → (gs_sequential_planner ∥ ca_sequential_planner) → research_join (JoinNode) → merge_planners → refinement_gate ─refine→ evaluator → enhanced_searcher (RetryUntilKeyNode) → composer / ─skip→ composer`.
   - Update "Key ADK patterns used" (`Workflow`, `JoinNode`, routed edges, `NodeTool`-wrapped workflows, `RetryUntilKeyNode`, `LongRunningFunctionTool`).
   - Fix the `agent_common/retry_agent.py` bullet.
4. **Regenerate the diagrams.**
   - Run the `paperbanana-figures` skill for `docs/diagrams/trend_scout_architecture.png` and `docs/diagrams/creative_agent_architecture.png`, so they show graph nodes, fan-out/join and the routed refinement.
   - Update the table text in `docs/diagrams/README.md`, which still says "Sequential/Parallel composition, `AgentTool` wrapping".
   - The Phase 4 P4.6 item in `docs/plans/2026-09-28-repo-refresh.md` is then done.
5. Run the full suite, ty and ruff. Commit: `chore: drop Sequential/Parallel deprecation filter, retire legacy wrappers, refresh docs + diagrams`

---

## Open questions / blockers

1. **NodeTool from a legacy LlmAgent root** (T0(b)): does `run_node_internal`'s `rerun_on_resume` check pass, and does `is_long_running=True` stay non-pausing in resumable Apps? If not, the fallback is to keep `AgentTool` over a thin `BaseAgent` shim that runs the Workflow. That brings back an isolated sub-Runner, but not the deprecated classes.
2. **Context injection in `single_turn` mode:** predecessors' outputs become user turns, a behavior change from Sequential + `include_contents="none"`. There is a token/quality risk on `merge_planners` (it receives the JoinNode dict). The mitigation is a no-output function node between the join and merge. Decide after the T2 eval.
3. **Session-log growth:** NodeTool writes every inner event into the *parent* (Vertex) session instead of an in-memory sub-session. This adds more `append_event` calls, which are already a known transient-500 source. Measure the event count before/after in T2.
4. **Upstream gaps:** #5872 (Workflow not usable as `sub_agents`) does not block us, because we use tools. #5780 (context loss with AgentTools) was closed, but the fix version is **[unverified]**, so T1's eval must watch for it.
5. **Timeline:** no removal date published. Re-check the release notes at kickoff.
