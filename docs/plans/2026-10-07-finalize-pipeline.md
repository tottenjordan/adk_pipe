# Deterministic finalize step: fewer root decisions, no "stopped before Evaluation & save"

**Status: shipped.** `finalize_pipeline` + research-PDF tail merged in #271 (c814ee0); #272 (17eb8d2)
then chained every stage into one `creative_pipeline` Workflow, so the creative_agent root now
makes a **single** workflow call after `memorize` (not the 4 calls this plan describes) and the
roots drop replayed sub-agent turns (`agent_common/history.py`). The completion key became
`finalize_done` (set by `finalize_ready` on every path) rather than `eval_bq_row_uuid`, and
`RUN_MAX_AUTO_CONTINUES` defaults to 2. Current design: [CLAUDE.md → Agent Composition](../../CLAUDE.md#agent-composition).


## Context
**Why:** the creative roots (gemini-3.1-pro-preview) end runs early by returning empty turns, typically after long tool results. In session `8242212012491276288`:
- after the images, the root went empty 3× (the first try plus 2 in-call retries);
- auto-continue got it through the eval and 3 saves;
- it went empty again before the final BigQuery write.

PR #269 shrank the pipeline results and made auto-continue cover the whole workflow. The structural fix is to stop asking the root to make five separate decisions after rendering.

**User decisions (2026-10-07):**
- **Scope:** add **one `finalize_pipeline` tool** that runs evaluation, the GCS eval report, the HTML gallery, the trends BigQuery row and the eval BigQuery row deterministically. Also **fold the research-PDF save into the research pipeline's tail**.
- **Eval call:** call the scoring function **directly**, dropping the extra Pro `creative_eval_agent` turn.

**Result:** after `memorize`, the creative_agent root makes **4 tool calls** (research, ads, visuals, finalize) instead of 9. interactive_creative reuses both changes; its checkpoints and the reviser/render after checkpoint 3 stay root decisions.

## Design
**Key enabler:** in ADK 2.10, `ToolContext` is just another name for `Context` (`google/adk/tools/tool_context.py`). A graph function node can therefore call the existing tool functions with its own `ctx`:
- `ctx.state` writes become the emitted event's `state_delta`;
- `ctx.save_artifact` records `artifact_delta`;
- `ctx.session.id` is available for `stable_row_id`.

No tool rewrites are needed.

### Task 1: `finalize_pipeline` Workflow (creative_agent)
New nodes in `creative_agent/agent.py`, next to the other result nodes. Prompt-free; the logic lives in a small new module, `creative_agent/finalize.py`, so it can be unit-tested.

1. **`evaluate_creatives_node(ctx)`** (async function node):
   - Runs `creative_eval.agent.evaluate_all_creatives` inside `await asyncio.to_thread(...)`, so the sync judge pool (`EVAL_MAX_WORKERS`=2, about 65–72 s) doesn't block the event loop.
   - Passes a plain state snapshot to the thread, then writes `creative_evaluation_report` back on the loop. A sync node runs inline (`google/adk/workflow/_function_node.py:514`), so the thread must not touch `ctx.state` directly.
   - If the function returns `{"status":"error"}` (no creatives), record `creative_evaluation_report__retry_exhausted` and continue.
2. **`persist_node(ctx)`** (async):
   - Concurrently (`asyncio.gather(..., return_exceptions=True)`): `save_eval_report_to_gcs`, `save_creative_gallery_html` and `write_trends_to_bq`, as the root's parallel step 6 does today. These are the existing functions in `creative_agent/gcs_tools.py`, `tools.py` and `bq_tools.py`.
   - Then, sequentially, `write_eval_report_to_bq`, which needs `creative_row_uuid` and `eval_report_gcs_uri` from step 6.
   - Each call is isolated: an exception is logged and recorded as a `<step>__issues` warning (the generic convention in `agent_common/observability.py`), never raised, so one failed save doesn't block the others. Skip the eval writes when no report exists.
   - The gallery returns its URI but stores no state key. Write `creative_gallery_gcs_uri`, which `deployment/headless_run.py:129-162` already expects and nothing writes today.
3. **`finalize_ready(ctx)`** (truthy terminal, as the NodeTool contract requires):
   - Returns a compact string for the root's final summary: pass rate, average ad-copy/visual scores, weakest dimensions, number of creatives, the eval-report / gallery / PDF URIs, and any failed steps.
   - Uses `_missing_notice`-style wording when evaluation produced nothing.
4. **`finalize_pipeline = Workflow(name="finalize_pipeline", description=..., input_schema=PipelineRequest, edges=[("START", evaluate_creatives_node, persist_node, finalize_ready)])`**.
   - Export it on the `creative_agent/__init__.py` facade for interactive.
   - Optional steps go through `FailSoftNode` (`agent_common/fail_soft_node.py`) only if a node-level exception could escape; the per-call isolation in step 2 should make that unnecessary.

**Ordering note:** today `write_eval_report_to_bq` writes empty `creative_row_uuid` / `eval_report_gcs_uri` if it runs too early. The fixed order removes that data-quality risk.

### Task 2: fold the research PDF into `combined_research_pipeline`
- New async node `save_research_pdf_node(ctx)` that calls `await gcs_tools.save_draft_report_artifact(ctx)` (it uses `ctx.save_artifact` and writes `research_report_gcs_uri`).
- Wire it between `brief_gate` (on its "ok" exit) and `research_report_ready`.
- Make it fail-soft: skip it when `final_report_with_citations` is missing, and on error record `research_report_gcs_uri__issues` without raising.
- `research_report_ready` mentions the PDF URI.
- **Interactive impact:** checkpoint 1 (`review_research`) comes right after the PDF save today, so the order is preserved. Its "if `report_edited`, re-run `save_draft_report_artifact`" path still needs the tool, so **interactive keeps `save_draft_report_artifact` as a root tool for the edit path only**. creative_agent drops it.

### Task 3: root wiring + prompts
- **creative_agent root tools** (`creative_agent/agent.py:1028-1062`): `combined_research_pipeline, ad_creative_pipeline, visual_production_pipeline, finalize_pipeline, memorize`. Remove `AgentTool(creative_eval_agent)` and the 5 persistence tools.
- **`ROOT_AGENT_INSTR`** (`creative_agent/prompts.py:841-902`):
  - The steps become: memorize missing fields → research (the PDF is saved inside it) → ads → visuals → finalize → final summary. The summary is built from finalize's result plus the `{gcs_bucket}/{gcs_folder}/{agent_output_dir}` URI.
  - Keep the "never an empty or text-only response until finalize has returned" rule, renamed to the new step.
- **interactive_creative** (`interactive_creative/agent.py:80-96`, `interactive_creative/prompts.py:92-97`):
  - Steps 9–13 (eval + 4 saves) become one `finalize_pipeline` call after the reviser/render.
  - Remove its 4 persistence tools and `creative_eval_agent`.
  - Keep `save_draft_report_artifact` for the checkpoint-1 edit path, and `visual_concept_reviser` / `visual_generator_resilient`.
- **`creative_eval_agent`:** keep it exported from `creative_eval` (other users and tests may import it), but no root wires it any more.

### Task 4: everything coupled to the old tool names
- **Tests:**
  - `tests/test_pipeline_structure.py`: the root tool lists (:10-30), the interactive NodeTool/AgentTool sets (:930-947), the `creative_eval_agent` callback test (:879-883, move or keep for the standalone agent), and new finalize/research-tail structure tests (exact edges, truthy terminal, `input_schema`, description).
  - `tests/test_creative_root_prompt.py` (:18-62): new step list and final-message phrases.
  - `tests/test_interactive_prompts.py`.
  - `tests/test_empty_turn_retry.py:34` (fixture name only).
- **Graph tests** (`tests/test_creative_agent_graph.py`), with recorded or fake judge, GCS and BigQuery:
  - finalize happy path: state keys `creative_evaluation_report`, `eval_report_gcs_uri`, `creative_gallery_gcs_uri`, `creative_row_uuid`, `eval_bq_row_uuid`;
  - no creatives: error notice, truthy end;
  - the gallery raising doesn't block the BigQuery writes and records a warning;
  - the eval-row write runs after the trends row and GCS save.
- **Eval CI:**
  - `tests/eval/creative_eval_config.json` (:30,36): the tool-use rubric lists the new 4-call sequence.
  - `tests/eval/evalsets/creative_agent_evalset.json` (:20-32, 57-69): expected calls become memorize×N, research, ads, visuals, finalize. This also fixes the already-stale missing `write_eval_report_to_bq`.
  - `docs/baselines/eval_efficiency.json`: fewer calls pass the gate, but refresh it with `efficiency_gate.py --update-baseline` after a passing nightly so regressions stay detectable.
- **Runtime couplings:**
  - `runserver/async_runs.py` `_COMPLETION_KEYS` stays at `eval_bq_row_uuid` (written inside finalize).
  - Frontend `run-stages.ts:50` / `run-completion.ts` / `run-history.ts` keep `eval_report_gcs_uri`. No frontend change is needed beyond verifying the stage spine still advances, now in one jump at finalize.
- **Latency harness:** `experiments/creative_latency/parse_run.py` (`_EXACT_PHASES` :32, `_SPAN_TOOLS` :73-83) maps `finalize_pipeline` / its nodes to the eval and persistence phases, with `tests/test_experiment_parse.py` updated.
- **Scripts/docs:** `deployment/headless_run.py` reads tool responses by the old names; switch it to state keys (`eval_report_gcs_uri`, `creative_gallery_gcs_uri`, `creative_row_uuid`). CLAUDE.md composition tree + root description, and deployment/README.md mentions.

### Conventions
- **Commits:** one commit per task, TDD. Never add Co-Authored-By or AI attribution (the user rule overrides the harness default).
- **Staging:** don't stage `.agents/`, `.claude/`, `skills-lock.json` or `creative-quality-research.md`.
- **Gate:** `uv run ruff format . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 4`.
- **Single PR:** `feat/finalize-pipeline`.

## Verification
1. **Unit + graph tests** as listed above. The full suite passes.
2. **Local end-to-end:** `TRUST_CLIENT_USER_ID=1 SESSION_SERVICE_URI=memory:// … uvicorn deployment.async_app:app` with `npm run dev`. Run creative_agent and check:
   - the root's event log shows exactly research → ads → visuals → finalize → text;
   - all of `research_report_gcs_uri`, `eval_report_gcs_uri`, `creative_gallery_gcs_uri`, `creative_row_uuid` and `eval_bq_row_uuid` are set (the BigQuery rows are visible in `creative_evals` / `trend_creatives`);
   - the results page is complete with no "stopped before" banner.
3. **Interactive:** run through all 3 checkpoints, including a checkpoint-1 report edit (the PDF is re-saved through the kept tool). Finalize runs after the render.
4. **Deployed:** redeploy the creative and interactive engines, api and web. Run `integration_test.py --check smoke --agent creative_agent`, then dispatch the nightly `adk-eval.yml` for creative_agent. Confirm it PASSES with lower call counts, then refresh the efficiency baseline.
5. **Before/after measurement:** compare `__auto_continues` and empty-turn warnings (`Model gemini-3.1-pro-preview returned an empty turn`) across ~5 UI runs. Expect auto-continues to drop to about 0.
