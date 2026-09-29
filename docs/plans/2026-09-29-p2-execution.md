# P2 Execution Plan: ADK Graph-Workflow Migration (I + T0–T4, with deploys + live evals)

> **For Claude:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (or `executing-plans`) to implement this plan task-by-task.
> Approved 2026-09-29; P2 doc Status updated accordingly.

**Goal:** Carry out `docs/plans/2026-09-29-p2-adk-workflow-migration.md` (the "P2 doc") end to end. That means:
- make the BigQuery writes idempotent;
- pin the ADK Workflow API contracts with offline tests;
- move every `SequentialAgent`/`ParallelAgent`/`RunIfAgent` onto `google.adk.workflow` graphs, plus a new `RetryUntilKeyNode`;
- retire the deprecation filter and the legacy wrappers;
- deploy after each PR group, with live-eval parity checks.

**Architecture:** Unchanged from the P2 doc. The root orchestrators stay `LlmAgent`s. Each pipeline becomes a `Workflow(input_schema=PipelineRequest)` exposed to the root as a node tool. Parallel research becomes a tuple fan-out into a `JoinNode`. The refinement gate becomes a routed function node. Retry-on-empty becomes a `BaseNode` that re-runs its child via `ctx.run_node(run_id=...)`.

**Tech Stack:** Python 3.13, uv, google-adk 2.10.0 (latest on PyPI as of 2026-09-29), pydantic 2, pytest + `InMemoryRunner`, `adk eval`, ruff, ty, Agent Runtime (AgentPlatform SDK), Cloud Run.

---

## Context

`SequentialAgent`/`ParallelAgent` are `@deprecated` in ADK 2.x in favor of graph `Workflow`s. Today we silence the warning with a filter in `agent_common/__init__.py:25-34`. No removal date has been published, and the P2 doc gated the start on one. **The user chose on 2026-09-29 to run the full migration now** (this is the "ADK 2 showcase" milestone), with a deploy after each merge and same-day live-eval baselines.

Nothing in the P2 doc has started yet:
- The line numbers in its "Current state" table still match `main` (34d12a4 → 4129a61).
- `creative_agent/bq_tools.py:76,90,178,190` and `trend_scout/tools.py:266,332` still use `uuid4` + `INSERT`/`insert_rows_json`.

The P2 doc is the source of truth for the per-task code and tests. **This plan adds execution order, deploy/eval steps and the corrections below.** Read the P2 doc's task section before each task.

## Corrections to the P2 doc (verified in the installed ADK 2.10.0 source, 2026-09-29)

Paths are relative to `.venv/lib/python3.13/site-packages/google/adk/`.

1. **`NodeTool` is always long-running, and that may pause the root (the biggest risk).**
   - `NodeTool` sets `is_long_running = True` (`tools/_node_tool.py:118`).
   - A returned value still produces a function response (`flows/llm_flows/tools/_caller.py:895-906`), and the resume logic continues (`core/_resume.py`).
   - But `LlmAgent._run_async_impl` sets `should_pause=True` and skips the end-of-agent state event after a long-running call (`agents/llm_agent.py:629-643`). `NodeRunner` also records `_interrupt_ids` that nothing clears (`workflow/_node_runner.py:342`).
   - **T0(b) must test both a non-resumable root (`creative_agent`) and a resumable `App` root.** The pass criterion is that the root calls the model again after the tool returns and emits its final text.
   - **Planned fallback if (b) fails:** add `agent_common/pipeline_tool.py: class PipelineTool(NodeTool)` that sets `self.is_long_running = False` after `super().__init__`, and pass `PipelineTool(node=...)` explicitly in `tools=[...]`. The validator only auto-wraps bare `BaseNode`s (`agents/llm_agent.py:147-159, 1254-1264`); an explicit `BaseTool` passes through. Re-run T0(b) against `PipelineTool`.
   - The P2 doc's other fallback (an `AgentTool` over a `BaseAgent` shim) is last resort only.
2. **`NodeTool` rejects `BaseAgent`** (`_node_tool.py:87-91`), and the validator raises for any `BaseAgent` passed as a tool. So:
   - `RetryUntilKeyNode` **must subclass `BaseNode`, not `BaseAgent`**, as the P2 doc already says.
   - LlmAgents exposed as tools (`gather_trends_agent`, `pick_trends_agent`, `creative_eval_agent`, `visual_concept_reviser`) **keep `AgentTool`**.
3. **There is no replay on the tool path.** A `NodeTool` called from a legacy root runs through `DynamicNodeScheduler(enable_replay=False)` (`workflow/_dynamic_node_scheduler.py:694-707`).
   - Inside a tool call, a failed or resumed workflow therefore **re-runs from scratch** rather than replaying completed children.
   - T3's "first node ran once (replayed)" assertion is replaced by a *characterization* test: record the actual count, and assert that idempotent BQ writes leave one logical row whatever that count is.
   - The rerun check `ctx._node_rerun_on_resume` passes on this path (`agents/context.py:214`).
4. **`single_turn` forces `include_contents='none'`** unless it is set explicitly (`workflow/_llm_agent_wrapper.py:408-410`), and it injects the predecessor output as a user event (`:311-355`).
   - Any graph agent that today relies on the Sequential conversation history, rather than `{state}` templating, loses that history.
   - **T1/T2 step:** before moving a stage, grep its instruction for dependence on prior-turn text. It should read only `{key}` / `{key?}` state tokens. Add the P2 doc's "no-output function node" between `research_join` and `merge_planners` **up front**, so the JoinNode dict isn't injected as a user turn. Don't wait for the eval.
5. **Distinct `run_id` ⇒ fresh execution** (`_dynamic_node_scheduler.py:321-376`). This confirms the `RetryUntilKeyNode` design: `run_id=f"{self.name}_attempt_{n}"` contains a non-digit, as required.

## Conventions (restate in every subagent dispatch)

- Branch off `main`, one PR per group: **G1** idempotency, **G2** T0+T1, **G3** T2, **G4** T3, **G5** T4. Squash-merge only after CI is green.
- **Never add `Co-Authored-By` trailers or any AI attribution to commits, and never put "Generated with Claude Code" in PR bodies.**
- Loop before every commit: `uv run ruff format --check . && uv run ruff check . && uv run ty check && uv run pytest tests/ -q`. `.env` provides `GOOGLE_CLOUD_PROJECT`.
- Graph nodes are `clone()`s (`workflow/utils/_workflow_graph_utils.py:120-131`), so tests compare **names**, not identity.
- Set `mode="single_turn"` explicitly on every `LlmAgent` placed in a graph.
- Keep module-level names unchanged (`combined_research_pipeline`, `ad_creative_pipeline`, `visual_generation_pipeline`, `visual_production_pipeline`, `visual_generator_resilient`, `understand_trends_agent_resilient`). The `creative_agent/__init__.py` facade and `tests/test_public_api.py` rely on them, and so do the tool **names** in the root prompts.
- Do not hand-edit `.env`. `deploy_agent.py --create` rewrites the `*_AGENT_ENGINE_ID` lines itself, which is the accepted P1b precedent.

## Standard deploy + verify block ("DEPLOY(targets)")

Run this from `main` after each merge, for only the affected targets:

- **api backend** (`trend-trawler-api`): follow the safe-redeploy recipe in `deployment/README.md → Frontend + api_server on Cloud Run`, which keeps the env + `--no-cpu-throttling --min-instances 1`.
  - Then **always pin traffic**. Find the newest revision by timestamp; the success line prints the old one. Run `gcloud run services update-traffic trend-trawler-api --region us-central1 --to-revisions <new>=100`.
  - Record the rollback revision (currently `00053-pc7`).
- **Agent Runtime engines:** `deploy_agent.py` has no update op.
  - Run `uv run python deployment/deploy_agent.py --version=v3 --agent=<agent> --create`. Use v3 for G1, then v4, and so on; one version namespace per group.
  - Then run `uv run python deployment/integration_test.py --check all`.
  - When it passes, delete the previous engine for that agent (`--resource_id=<old> --delete`), per the user's "no 7-day wait" rule.
- **CRF worker** (only when the `creative_agent` engine ID changes): rerun the README §3 worker deploy with the **full** `--set-env-vars` line (it replaces the whole env and must include `GOOGLE_CLOUD_PROJECT`) plus the new engine ID. Check that the Eventarc triggers are intact.
- Update memory (`adk-pipe-work-status`) with the new revisions and engine IDs.

## Standard live-eval block ("EVAL(agent)")

These are real Vertex calls, about 5 min per case, bound by the pro 5-RPM and image 2-RPM quotas. Run them sequentially, never in parallel.

1. Take a **same-day baseline on `main`** before merging the group, and save the output to `/tmp/p2-eval/<agent>-main.txt`:
   - trend_scout: `PYTHONPATH="$PWD" uv run adk eval trend_scout tests/eval/evalsets/trend_scout_evalset.json --config_file_path=tests/eval/eval_config.json --print_detailed_results`
   - creative_agent: `PYTHONPATH="$PWD" uv run adk eval creative_agent tests/eval/evalsets/creative_agent_evalset.json --config_file_path=tests/eval/creative_eval_config.json --print_detailed_results`
2. Run the same command on the branch.
3. Accept if the rubric scores are within run-to-run noise (no metric drops by more than 0.1, and every case that passed on main still passes).
4. Also check wall-clock (≤ +10% for creative_agent), that no `Error running node` strings appear, that no run ended paused or with a missing final text, and the session event count (P2 open question 3; record the before/after count in the PR).

---

## Task 0: Commit this plan

1. `git checkout -b fix/p2-bq-idempotency`
2. Copy this file to `docs/plans/2026-09-29-p2-execution.md`.
3. In the P2 doc, set `**Status:**` to `executing 2026-09-29 via 2026-09-29-p2-execution.md (user-scheduled; gate waived)`.
4. Commit: `docs(p2): execution plan (gate waived, deploy + eval per group)`

## G1: Idempotency (P2 doc Tasks I-1, I-2, I-3)

Implement exactly as specified in the P2 doc: add `agent_common/idempotency.py: stable_row_id` with `json.dumps(parts)` hashing, then use session-derived ids + `MERGE … WHEN NOT MATCHED THEN INSERT` in `creative_agent/bq_tools.py:write_trends_to_bq` / `write_eval_report_to_bq` and `trend_scout/tools.py:write_trends_to_bq` / `_build_trend_insert_sql`. Use one commit per task, with the P2 doc's messages. Follow TDD: write the failing test first, then the implementation.

- **Before implementing I-2, run the [verify] step against a scratch dataset:** check that an INSERT-only `MERGE` works on a table with streaming-buffer rows (`bq mk --dataset <project>:p2_scratch`, then `insert_rows_json` one row, then MERGE, then drop the dataset). If it fails, keep `insert_rows_json` with `row_ids=[eval_uuid]` and document the ~1-min best-effort dedupe instead.
- Check that the CRF join (`creative_uuid` 8-char) and the `creative_evals` schema are unchanged. The SQL builders' tests must stay parameterized: no literal values in the SQL.
- Open the PR, merge on green, then run **DEPLOY(api, trend_scout v3, creative_agent v3, interactive_creative v3, CRF worker)**. No EVAL is needed (the agent graph is unchanged); the integration `smoke` covers the BQ writes.
  - Then run a live BQ check: after the smoke, `SELECT uuid, COUNT(*) FROM trend_creatives WHERE uuid=<smoke uuid> GROUP BY 1` returns 1.

## G2: T0 contract spike + T1 trend_scout

Branch `refactor/p2-t0-t1-trend-scout`.

**T0.** Create `tests/test_workflow_api_contract.py` per the P2 doc (fan-out/join once, route skip, plus tests (a), (b) and (c)). Change (b) per Correction 1:
- (b1) root `LlmAgent` in a plain `Runner(agent=...)`;
- (b2) the same root under `App(resumability_config=ResumabilityConfig(is_resumable=True))`.

Each uses a stub `BaseLlm` returning a canned function call, then text. Assert that:
- the stub was called **twice**;
- the final text event exists;
- the workflow's state keys landed in the parent session;
- nothing is left paused (for b2, a follow-up `run_async` with a new message is processed normally).

Add (d), a characterization of Correction 3: a `NodeTool` workflow whose second node raises once, re-invoked. Record how many times the first node ran, and assert only that the tool returns an error string rather than raising.

Decision point:
- **If (b1)/(b2) fail with bare `NodeTool`:** implement `PipelineTool` (Correction 1) with its own unit test (`is_long_running is False`, declaration identical to `NodeTool`'s), re-point (b) at it, and use `PipelineTool(node=...)` everywhere this plan says "bare node in `tools=[...]`".
- **If `PipelineTool` also fails, STOP:** record the blocker in the P2 doc's open questions and report to the user.
- If (c) fails, STOP as well.

Commit: `test: pin ADK Workflow API contracts relied on by the P2 migration`

**T1.** Implement per the P2 doc Task T1:
- add `agent_common/schemas.py: PipelineRequest`;
- add `agent_common/retry_node.py: RetryUntilKeyNode(BaseNode)` reusing `RetryUntilKeyAgent._is_populated` and the same log lines + `__retry_exhausted` marker;
- move the shared fakes into `tests/_fakes.py`;
- add `tests/test_retry_node.py`, with the one-for-one port of `test_retry_agent.py`;
- in `trend_scout/agent.py:111-135,188-190`, make the pair a Workflow, wrap it in `RetryUntilKeyNode(input_schema=PipelineRequest)`, and put the bare node (or `PipelineTool`) in the tools;
- rewrite `test_understand_trends_is_retry_wrapped` and `test_trend_scout_root_has_expected_tools`, and add `test_understand_trends_tool_declaration_unchanged`.

Before moving the stages, do the Correction 4 instruction check on `understand_trends_searcher` / `understand_trends_synthesizer` (`trend_scout/prompts.py`).

Commit per the P2 doc. Then run **EVAL(trend_scout)** baseline + branch → PR → merge → **DEPLOY(api, trend_scout v4)** → integration `--check all`.

## G3: T2 creative_agent

Branch `refactor/p2-t2-creative-agent`. Implement P2 doc Task T2a, then T2b, as two commits with the P2 doc's messages:
- `creative_agent/sub_agents/{trend,campaign}_researcher/agent.py:172-215`
- `creative_agent/agent.py:38-210, 270, 449-497`

Additions:
- `refinement_gate_route(state)` is a pure module-level helper over the existing `_base_research_is_degraded`.
- Insert a no-output function node `research_barrier` between `research_join` and `merge_planners` (Correction 4). Add it to the structure test's node set and edges.
- Do the Correction 4 instruction check for every moved LlmAgent. Pay extra attention to `combined_web_evaluator`, `combined_report_composer`, and the visual finalizer/critic, which may read prior turns.
- Assert that `combined_report_composer.after_agent_callback` survives the clone (by name lookup), and that `citation_replacement_callback` fires in an offline graph test using a stub model.
- `creative_eval_agent` keeps `AgentTool`.
- `tests/test_pipeline_structure.py`: rewrite every test listed in the P2 doc "Current state". Keep `test_campaign_pipeline_uses_distinct_global_bucket` and `test_structured_output_producers_carry_schema_retry` green via name lookup.
- `tests/test_retry_agent.py` and `test_conditional_agent.py` stay as they are until G5.

Then run **EVAL(creative_agent)** baseline + branch, including the wall-clock and event-count checks → PR → merge → **DEPLOY(api, creative_agent v5, CRF worker)** → integration `--check all`, and a one-trend CRF smoke if there's an unprocessed row. If there isn't one, report the no-op and don't insert a row without asking.

## G4: T3 interactive_creative

Branch `refactor/p2-t3-interactive`. Implement P2 doc Task T3 with the Correction 3 change: in `tests/test_async_runs.py`, the resume test **characterizes** the re-execution count and asserts one logical BQ key via `stable_row_id`.

- In `interactive_creative/agent.py:117-123`, swap the `AgentTool`s over `combined_research_pipeline`, `ad_creative_pipeline`, `visual_generation_pipeline` and `visual_generator_resilient` for the bare nodes (or `PipelineTool`).
- `visual_concept_reviser` and `creative_eval_agent` stay `AgentTool`.
- Update `test_interactive_creative_uses_resilient_visual_generator` and the tests at `tests/test_pipeline_structure.py:589-616, 809-848`.

Manual local run:
1. `ALLOW_ORIGINS=http://localhost:3000 uv run uvicorn deployment.async_app:app --port 8000` + `cd frontend && npm run dev`.
2. Pass all 3 checkpoints.
3. Reload mid-run; it should replay from `since=0`.
4. Edit a concept at checkpoint 3 and confirm `visual_revision_notes` is consumed.

Then run **EVAL(creative_agent)** as a smoke test → PR → merge → **DEPLOY(api, interactive_creative v6)** → integration `--check all` (it handles paused interactive runs, per #173).

## G5: T4 cleanup, docs, diagrams

Branch `chore/p2-t4-cleanup`. Implement P2 doc Task T4 in full:
- remove the filter from `agent_common/__init__.py`;
- add the no-deprecation reload guard test + the grep test in `tests/test_public_api.py`;
- delete `agent_common/conditional_agent.py` + `tests/test_conditional_agent.py`;
- delete `RetryUntilKeyAgent` after moving `_is_populated` into `retry_node.py` and porting any remaining cases;
- update `agent_common/__init__.py` `__all__`;
- update the CLAUDE.md "Agent Composition" tree, "Key ADK patterns used" and the `agent_common` bullets (`retry_agent.py` / `conditional_agent.py` become `retry_node.py` / `schemas.py` / `idempotency.py` / `pipeline_tool.py` if used);
- update `creative_agent/config.py:44` and `trend_scout/config.py:35`, whose docstrings mention `ParallelAgent`;
- regenerate `docs/diagrams/{trend_scout,creative_agent}_architecture.png` via the `paperbanana-figures` skill, update `docs/diagrams/README.md`, and mark P4.6 done in `docs/plans/2026-09-28-repo-refresh.md`;
- set the P2 doc's `**Status:**` to complete.

Then PR → merge → **DEPLOY(api)** only. The engines are behavior-identical, and the removed filter only affects warnings. Run integration `--check health`.

---

## Stop conditions (report to the user; don't improvise)

- T0(b) fails for both `NodeTool` and `PipelineTool`, or T0(c) fails.
- A live eval regresses beyond the tolerance, and one fix attempt (e.g. an explicit `include_contents`, or a barrier node) doesn't recover it.
- Integration fails after a deploy. Roll back: re-pin api traffic to the recorded revision, and keep the previous engine (don't delete it) with its ID restored in the CRF worker env.
- The scratch MERGE check fails and the `row_ids` fallback is also unacceptable.

## Verification (end to end)

- CI is green on every PR; offline suite + `ty` + ruff pass locally.
- `grep -rnE "SequentialAgent|ParallelAgent|LoopAgent|RunIfAgent|RetryUntilKeyAgent" --include=*.py . | grep -v .venv` finds nothing outside the historical docs.
- Importing all three agent modules under `warnings.simplefilter("error", DeprecationWarning)` succeeds (the G5 guard test).
- Evals for trend_scout (G2) and creative_agent (G3, G4) are within tolerance of same-day `main` baselines, with the results recorded in each PR body.
- After each deploy: integration `--check all` passes, api traffic is pinned to the new revision, old engines are deleted, and the CRF worker points at the current creative engine.
- BQ: one row per smoke run in `trend_creatives` / `creative_evals`, even after a resumed run.
- Memory is updated with the final revisions/engine IDs and P2 marked complete.
