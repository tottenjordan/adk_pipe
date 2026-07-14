# Workstream 2 — Split Research Producers (searcher + synthesizer) — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate the intermittent "empty-turn" flake at its root by splitting each of the **four** combined `google_search`+thinking+synthesis research producers (three in `creative_agent` + `trend_scout`'s `understand_trends_agent`) into two agents — a tool-using **searcher** (runs `google_search`, emits raw findings) and a tool-free **synthesizer** (shapes the raw findings into the existing report, no tools/no planner) — while preserving the WS1 retry safety net and the WS3 observability surfaces.

**Architecture:** For each producer, replace the single `Agent(planner+tools=[google_search]→output_key)` with `SequentialAgent[X_searcher → X_synthesizer]`, wrapped by the existing `RetryUntilKeyAgent` (now watching the synthesizer's key). The searcher writes a new intermediate `*_raw` key; the synthesizer reads `{*_raw?}` and writes the existing consumer-facing `*_insights` key. Neither turn does "think + search + author a full report" at once, which is what burned the output budget and left the key unset. Keep the retry wrapper (belt-and-suspenders) so the `*__retry_exhausted` markers that WS3's warnings / `research_gaps` / HTML banner depend on still fire.

**Tech Stack:** google-adk 2.x (`Agent`, `SequentialAgent`, `BuiltInPlanner`, `RetryUntilKeyAgent`, `{var?}` optional templates), pydantic v2, pytest (offline `test_retry_agent.py` + creds-gated `test_pipeline_structure.py`), `uv` / `uvx ruff`, the isolated tagged-Cloud-Run-revision smoke harness + SA-impersonation auth recipe.

---

## Context (why this change)

The four research producers (three in `creative_agent`, one in `trend_scout`) each do `google_search` + a thinking `BuiltInPlanner` + full-report synthesis in **one** agent turn. On gemini-3 this intermittently finishes with no final text (thinking burns the output budget → MAX_TOKENS, or the turn returns only tool-call parts), leaving the `output_key` unset and crashing the next consumer's `{var}` template with `KeyError`. (`trend_scout`'s `understand_trends_agent` → `info_gtrends` is the same anti-pattern; PR #71 already wrapped it in `RetryUntilKeyAgent` and guarded its consumer with `{info_gtrends?}`, so it fits WS2's split cleanly.) WS1 (PR #70) added a bounded retry-on-empty (`RetryUntilKeyAgent`) and WS3 (PR #72) surfaced exhaustion to the user — both are **mitigations**. WS2 is the **durable root-cause fix**: separate tool-use from synthesis so no single turn has to think, search, and author a long report simultaneously. A tool-free synthesizer that only reads state and emits text is the reliable shape already proven by `merge_planners` (never retry-wrapped, never flakes).

Design decision (confirmed with user): **keep the `RetryUntilKeyAgent` wrappers** as a safety net, wrapping the `[searcher → synthesizer]` pair. This preserves the exact external contract (same `*_insights` keys, same `*_resilient` names, same `*__retry_exhausted` markers WS3 consumes).

**Branch base:** off `main` once the `#70 → #71 → #72` stack merges (preferred — `main` will then carry WS1 + trend_scout hardening + WS3). If starting before the stack lands, branch off `exp/ws3-observability` (the state this plan was explored against). Do NOT tangle with the independent `fix/trend-scout-pick-3-trends` branch.

---

## Key files (from exploration)

Producers to split (each currently: `Agent(planner=BuiltInPlanner, tools=[google_search], output_key=…, after_agent_callback=collect_research_sources_callback, after_model_callback=log_empty_turn_finish_reason)`):
- `creative_agent/sub_agents/trend_researcher/agent.py` — `gs_web_searcher` (`:90-137`) → `gs_web_search_insights`; wrapper `gs_web_searcher_resilient` (`:144-149`); in `gs_sequential_planner` (`:151-155`) = `[gs_web_planner, gs_web_searcher_resilient]`. Reads `{initial_gs_queries}`.
- `creative_agent/sub_agents/campaign_researcher/agent.py` — `campaign_web_searcher` (`:96-144`) → `campaign_web_search_insights`; wrapper `:149-154`; in `ca_sequential_planner` (`:156-160`). Reads `{initial_campaign_queries}`.
- `creative_agent/agent.py` — `enhanced_combined_searcher` (`:163-199`) → `refined_web_search_insights`; wrapper `enhanced_combined_searcher_resilient` (`:206-211`); in `combined_research_pipeline` (`:304-313`) at index 2. Reads `{combined_research_evaluation}`.
- `trend_scout/agent.py` — `understand_trends_agent` (`:61-110`) → `info_gtrends`; wrapper `understand_trends_agent_resilient` (`:122-128`); exposed to the orchestrator as `AgentTool(agent=understand_trends_agent_resilient)` (`:248`), **not** inside a `SequentialAgent` parent. Reads `{raw_gtrends}`. Differs from the creative producers: **no** `collect_research_sources_callback` (trend_scout has no citation flow), and its output is a **JSON** object (`analyzed_trends`), not markdown. Consumer `pick_trends_agent` reads `{info_gtrends?}` (`:152`, already optional).

Shared primitives (reuse, do NOT modify):
- `agent_common/retry_agent.py` — `RetryUntilKeyAgent` runs `sub_agents[0].run_async(ctx)` and checks `ctx.session.state.get(output_key)`. **Wrapping a `SequentialAgent` as the sole sub_agent works unchanged** (verified): it runs the whole sequence, then reads the synthesizer's key. No change needed here.
- `creative_agent/callbacks.py` — `collect_research_sources_callback` (`:130`, harvests `google_search` grounding → `sources`/`url_to_short_id`); `citation_replacement_callback` (`:192`, stays on `combined_report_composer`).
- `agent_common/observability.py` — `log_empty_turn_finish_reason`, re-exported at `creative_agent/callbacks.py:28`.
- `build_gemini(config.worker_model)` — `gemini-3.5-flash`; existing tool-free synthesizer to mirror: `merge_planners` (`creative_agent/agent.py:35-70`).

Consumers (reads unchanged — synthesizers still write the same keys):
- `merge_planners` reads `{campaign_web_search_insights?}` / `{gs_web_search_insights?}` (`agent.py:52-53`, already optional).
- `combined_report_composer` reads `{refined_web_search_insights?}` (`agent.py:241`, already optional) + `{sources}` (`:253`).

Tests:
- `tests/test_retry_agent.py` — offline, `InMemoryRunner` + `_FlakyProducer` double; the oracle for the retry primitive.
- `tests/test_pipeline_structure.py` — creds-gated (imports `creative_agent.agent` → module-level genai client). Asserts producer wiring at `:37-52`, `:81-93`, `:96-107`, `:121-149`, `:175-213`.
- `tests/eval/evalsets/creative_agent_evalset.json` + `creative_eval_config.json` — judge only the **root** tool trajectory (`combined_research_pipeline`, …); the split is internal, so **eval trajectory is unaffected**.

---

## Tasks

### Task 1: Prove `RetryUntilKeyAgent` retries a searcher+synthesizer *pair* *(TDD, offline)*
**Files:** Test: `tests/test_retry_agent.py`.

1. **Failing test** `test_retries_sequential_pair_until_synthesizer_populates`: build a fake searcher (`_FlakyProducer`-style `BaseAgent` writing an intermediate `*_raw` key) and a fake synthesizer (`BaseAgent` that reads `*_raw` from `ctx.session.state`; leaves the final key empty for the first N runs, then writes it). Wrap `SequentialAgent(sub_agents=[fake_searcher, fake_synth])` in `RetryUntilKeyAgent(sub_agents=[that_seq], output_key="report", max_attempts=3)`. Drive with the existing `_run(agent)` helper. Assert: the pair re-runs until `state["report"]` is populated, and on total failure `state["report__retry_exhausted"] is True`. Mirror the existing `_FlakyProducer` / `_run` setup verbatim.
2. **Run → FAIL** (or as a lock confirming the primitive behaves): `export PATH="$HOME/.local/bin:$PATH"; cd /home/user/adk_pipe; uv run pytest tests/test_retry_agent.py -q`.
3. **Implement:** no production change — this task *locks the primitive contract* the whole plan relies on (wrap-the-pair). If the test reveals the pair isn't retried as expected, STOP and reassess before Task 2.
4. **Run → PASS.** `uvx ruff check tests/test_retry_agent.py`.

### Task 2: Split `gs_web_searcher` → searcher + synthesizer *(TDD — the template for Tasks 3–4)*
**Files:** Modify `creative_agent/sub_agents/trend_researcher/agent.py`; Test: `tests/test_pipeline_structure.py`.

1. **Failing tests** in `test_pipeline_structure.py`:
   - Update `test_trend_producer_is_retry_wrapped` (`:96-107`): `w = gs_sequential_planner.sub_agents[-1]` is still a `RetryUntilKeyAgent` with `output_key == "gs_web_search_insights"`, but now `w.sub_agents[0]` is a `SequentialAgent` whose **last** child has `output_key == "gs_web_search_insights"` and whose **first** child has `output_key == "gs_web_search_raw"` and `google_search` in `.tools`.
   - New `test_gs_synthesizer_is_tool_free`: the synthesizer has no `tools`, no `planner`, and `after_model_callback is callbacks.log_empty_turn_finish_reason`.
   - New `test_gs_searcher_keeps_source_collection`: the searcher's `after_agent_callback is callbacks.collect_research_sources_callback`.
2. **Run → FAIL.**
3. **Implement** — restructure the `gs_web_searcher` block:
   ```python
   gs_web_searcher = Agent(
       model=build_gemini(config.worker_model),
       name="gs_web_searcher",
       include_contents="none",
       description="Runs google_search for the trend queries and returns raw findings.",
       planner=BuiltInPlanner(
           thinking_config=types.ThinkingConfig(include_thoughts=False)
       ),
       instruction="""Role: You are a web research operator.
       1. Read the queries in <initial_gs_queries>.
       2. Use the `google_search` tool to run EVERY query.
       3. Output the RAW findings: for each query, list the concrete facts,
          quotes, entities, dates, and sentiment you found, grouped by query.
          Do NOT write a polished report and do NOT omit specifics — the next
          agent needs the raw material. Plain text / light markdown is fine.
       <CONTEXT><initial_gs_queries>{initial_gs_queries}</initial_gs_queries></CONTEXT>
       """,
       tools=[google_search],
       output_key="gs_web_search_raw",
       after_agent_callback=callbacks.collect_research_sources_callback,
       after_model_callback=callbacks.log_empty_turn_finish_reason,
   )

   gs_web_synthesizer = Agent(
       model=build_gemini(config.worker_model),
       name="gs_web_synthesizer",
       include_contents="none",
       description="Synthesizes the raw trend findings into a structured report.",
       instruction="""Role: You are a cultural trend analyst and synthesis expert.
       Transform the raw findings in <gs_web_search_raw> into the report below.
       <CONTEXT><gs_web_search_raw>{gs_web_search_raw?}</gs_web_search_raw></CONTEXT>
       <REPORT_STRUCTURE>
       ## Trend Overview & Trajectory ...
       ## Key Entities and Cultural Narrative ...
       ## Marketing Opportunity Analysis ...  (2-3 actionable angles)
       </REPORT_STRUCTURE>
       CRITICAL: Output ONLY the synthesized report in Markdown (## headings).
       Do not include raw links or tool output.
       """,  # move the existing <REPORT_STRUCTURE> + <CONTEXT_GUIDANCE> text here verbatim
       output_key="gs_web_search_insights",
       after_model_callback=callbacks.log_empty_turn_finish_reason,
   )

   gs_search_and_synthesize = SequentialAgent(
       name="gs_search_and_synthesize",
       sub_agents=[gs_web_searcher, gs_web_synthesizer],
   )

   gs_web_searcher_resilient = RetryUntilKeyAgent(
       name="gs_web_searcher_resilient",   # keep name: outer composition unchanged
       sub_agents=[gs_search_and_synthesize],
       output_key="gs_web_search_insights",
       max_attempts=3,
   )
   ```
   `gs_sequential_planner` (`:151-155`) is unchanged (`[gs_web_planner, gs_web_searcher_resilient]`). Move the existing `<REPORT_STRUCTURE>` and `<CONTEXT_GUIDANCE>` text into the synthesizer verbatim (keep quality); the searcher's old "do not include raw output" rule is inverted — it now emits raw findings.
4. **Run → PASS.** `uvx ruff check … && uvx ruff format …`.

### Task 3: Split `campaign_web_searcher` *(TDD — mirror Task 2)*
**Files:** Modify `creative_agent/sub_agents/campaign_researcher/agent.py`; Test: `tests/test_pipeline_structure.py`.
- Same pattern: `campaign_web_searcher` → `campaign_web_search_raw`; new `campaign_web_synthesizer` → `campaign_web_search_insights` (reads `{campaign_web_search_raw?}`, carries the existing `<REPORT_STRUCTURE>`: Target Audience & Behavioral Insights / Product Landscape & Competitive Context / Strategic Opportunities & Key Message Validation / Key Research Gaps); `campaign_search_and_synthesize` SequentialAgent; keep `campaign_web_searcher_resilient` name wrapping the pair. Update `test_campaign_producer_is_retry_wrapped` (`:81-93`) + add the two tool-free / source-collection asserts. `ca_sequential_planner` unchanged.

### Task 4: Split `enhanced_combined_searcher` *(TDD — mirror Task 2)*
**Files:** Modify `creative_agent/agent.py`; Test: `tests/test_pipeline_structure.py`.
- `enhanced_combined_searcher` → `refined_web_search_raw` (reads `{combined_research_evaluation}`, runs `google_search` on the `follow_up_queries`, emits raw findings; keep `include_contents="none"`, keep `after_agent_callback=collect_research_sources_callback`). New `refined_web_synthesizer` → `refined_web_search_insights` (reads `{refined_web_search_raw?}`, carries the "New Research Findings" summary text). `refined_search_and_synthesize` SequentialAgent; keep `enhanced_combined_searcher_resilient` name wrapping the pair; `combined_research_pipeline` (`:304-313`) unchanged.
- Update `test_combined_research_pipeline_sub_agent_order` (`:37-52`): index-2 name stays `enhanced_combined_searcher_resilient`; `w.sub_agents[0]` is now a `SequentialAgent`, last child `output_key == "refined_web_search_insights"`, first child `output_key == "refined_web_search_raw"`.
- Update `test_output_keys_are_set_correctly` (`:121-149`): `enhanced_combined_searcher.output_key` is now `refined_web_search_raw`; add the synthesizer's `refined_web_search_insights`.

### Task 5: Split `trend_scout`'s `understand_trends_agent` *(TDD — mirror Task 2, with trend_scout differences)*
**Files:** Modify `trend_scout/agent.py`; Test: `tests/test_pipeline_structure.py`.
- `understand_trends_searcher` → `info_gtrends_raw` (reads `{raw_gtrends}`; filter to the top 5–8 narrative-driven terms, run `google_search`, emit raw findings; keep `include_contents="none"`, keep the `BuiltInPlanner`, carry the existing `generate_content_config` labels; **NO** source-collection callback — trend_scout has none). New `understand_trends_synthesizer` → `info_gtrends` (reads `{info_gtrends_raw?}`, tool-free/planner-free, emits the existing **JSON** `analyzed_trends` structure verbatim). `understand_trends_search_and_synthesize` SequentialAgent; keep the `understand_trends_agent_resilient` **name and `description`** (AgentTool builds the orchestrator's tool declaration from them) wrapping the pair. The orchestrator `AgentTool(agent=understand_trends_agent_resilient)` wiring (`:248`) is unchanged.
- Update `test_trend_scout_sub_agent_output_keys` (`:258-265`): the searcher writes `info_gtrends_raw`; add the synthesizer's `info_gtrends` (update the import from `understand_trends_agent` to the new searcher/synthesizer names).
- Update `test_understand_trends_is_retry_wrapped` (`:268-284`): `matching[0].sub_agents[0]` is now a `SequentialAgent`; assert its **last** child `output_key == "info_gtrends"` and **first** child `output_key == "info_gtrends_raw"`.
- `test_pick_trends_info_gtrends_optional` (`:287-294`) and `test_trend_scout_orchestrator_thinking_budget_is_bounded_nonzero` (`:297-310`) are unchanged.

### Task 6: Restore callback-parity + confirm WS3 surfaces untouched *(TDD)*
**Files:** Test: `tests/test_pipeline_structure.py`.
- Add the three new creative synthesizers to `test_creative_model_agents_have_finish_reason_callback` (`:175-200`) and/or `test_creative_researcher_agents_have_finish_reason_callback` (`:202-213`) so every model agent still asserts `after_model_callback is callbacks.log_empty_turn_finish_reason` (searchers + synthesizers). Ensure the new trend_scout searcher + synthesizer also set `after_model_callback=callbacks.log_empty_turn_finish_reason` (as the current `understand_trends_agent` does).
- Confirm `test_observability.py`, `test_creative_eval.py`, and `test_trend_scout_logging.py` still pass **unchanged** — the `*_insights`/`info_gtrends` keys and `*__retry_exhausted` markers are identical, so WS3's degradation surfaces are unaffected (the new `*_raw`/`info_gtrends_raw` keys are internal and not retry-wrapped individually, so they never produce an exhaustion marker).

### Task 7: Full offline gate *(gate)*
- `uv run pytest tests/test_retry_agent.py -q` → green (offline, no creds).
- `PYTHONPATH="$PWD" uv run pytest tests/ -q` → green (note `test_pipeline_structure.py` is creds-gated — run with ADC on the deploy host / this session).
- `uvx ruff check . && uvx ruff format --check .` → clean.

### Task 8: Live smoke on an isolated tagged revision *(gate before PR ready)*
- Deploy the branch as a no-traffic tagged revision (auto-mode-allowed — private service, no auth change): `gcloud run deploy trend-trawler-api --source . --region us-central1 --project hybrid-vertex --no-traffic --tag ws2-split`. Verify clean boot + `/list-apps` returns all 3 agents.
- **creative_agent** run against the tag (SA-impersonation token, audience = base service URL; `POST /apps/creative_agent/users/{uid}/sessions` then `POST /run_sse`). **Verify:** research completes with a non-empty `combined_final_cited_report`; logs show **two** turns per producer (`*_searcher` then `*_synthesizer`) instead of one; **zero** `empty/abnormal model turn` warnings on the happy path; citations still render (`sources` populated, `<cite …/>` replaced); run finishes through eval → GCS → BQ.
- **trend_scout** run against the tag (`/apps/trend_scout/...`). **Verify:** `info_gtrends` is produced across two turns (`understand_trends_searcher` → `understand_trends_synthesizer`); `pick_trends_agent` selects trends; `write_trends_to_bq` succeeds; zero empty-turn warnings; `trend_scout final state […]` summary shows `info_gtrends` present.
- ADK eval trajectory is unchanged for both agents (splits are internal) — an `adk eval` run is optional confirmation, not required. Delete the tagged revision when done.

### Task 9: Finalize *(only when the user asks to commit)*
- Mirror this plan to `docs/plans/2026-07-14-ws2-split-producers.md`.
- Commit per-task (conventional `feat(creative_agent):` / `test:`; **no `Co-Authored-By`**). Open a PR based on `main` (if `#70/#71/#72` merged) or stacked on `exp/ws3-observability`.
- Update memory: `creative-agent-research-pipeline-brittleness` (mitigation #2 SHIPPED — the durable fix), `adk-retry-until-populated-research` (wrappers now guard searcher+synth pairs), `trend-scout-info-gtrends-landmine` (understand_trends_agent split resolves it), `adk-pipe-work-status`.

---

## Verification (end-to-end)

- **Offline (CI-safe):** `test_retry_agent.py` proves `RetryUntilKeyAgent` retries a `SequentialAgent[searcher, synthesizer]` pair until the synthesizer's key populates, and records the exhaustion marker on total failure.
- **Structure (creds-gated, deploy host / this session):** for each creative producer — the searcher has `google_search` + `collect_research_sources_callback` + writes `*_raw`; the synthesizer is tool-free/planner-free + reads `{*_raw?}` + writes `*_insights`; the `*_resilient` wrapper watches `*_insights` and wraps a `SequentialAgent[searcher, synthesizer]`; every model agent keeps `log_empty_turn_finish_reason`. For **trend_scout**: `understand_trends_searcher` (google_search, writes `info_gtrends_raw`, no source callback) + `understand_trends_synthesizer` (tool-free, reads `{info_gtrends_raw?}`, writes JSON `info_gtrends`), wrapped by `understand_trends_agent_resilient` (name+description preserved) inside the orchestrator's `AgentTool`.
- **Degradation path unchanged:** `*_insights` keys + `*__retry_exhausted` markers identical → WS3 warnings / `research_gaps` / HTML banner still fire on exhaustion (`test_observability.py` + `test_creative_eval.py` unchanged).
- **Live (isolated tag, prod untouched):** a full creative_agent run completes clean, logs two turns per producer, no empty-turn warnings, citations intact, eval + GCS + BQ succeed; a trend_scout run produces `info_gtrends` across two turns, picks trends, and writes to BQ.

## Risks / call-outs

- **`RetryUntilKeyAgent` only runs `sub_agents[0]`.** The split relies on wrapping the pair in a `SequentialAgent` (Task 1 locks this). Do NOT pass `sub_agents=[searcher, synthesizer]` to the wrapper directly — the synthesizer would never run.
- **Synthesizer must read `{*_raw?}` (optional).** If the searcher empty-turns, `*_raw` is unset; a required `{*_raw}` would raise `KeyError` *inside* the retried sequence and propagate as an exception (crashing the run) instead of leaving `*_insights` empty for a clean retry. The `?` makes an empty searcher degrade to an empty synthesis → the wrapper retries the pair.
- **Source collection must stay on the searcher.** `collect_research_sources_callback` reads `google_search` grounding metadata; the tool-free synthesizer has none. Keep it as the searcher's `after_agent_callback`; `citation_replacement_callback` stays on `combined_report_composer`. The `{sources}` state flow and `<cite source="src-N"/>` contract are unchanged.
- **Retrying the pair re-runs `google_search`** (quota cost) on the rare retry — acceptable: post-split retries should be near-zero, and re-running the searcher is the only way to recover a searcher that itself emptied.
- **Extra model turn per producer** (searcher + synthesizer vs. one combined) — a modest latency/token increase, offset by removing the retry storms and the KeyError crashes.
- **trend_scout's producer sits in an `AgentTool`, not a `SequentialAgent` parent.** Keep the `understand_trends_agent_resilient` **name + `description`** so `AgentTool` builds the identical tool declaration the orchestrator already calls; the retry runs inside AgentTool's isolated sub-Runner (state-delta timing verified equivalent — see `[[trend-scout-info-gtrends-landmine]]`). Its synthesizer emits JSON (`analyzed_trends`), not markdown, and it has no source-collection callback.
- **Creds-gating:** `test_pipeline_structure.py` imports build a module-level genai client; run structural tests with ADC. `test_retry_agent.py` is creds-free.

## Out of scope (later)
- Bounding the searcher's `thinking_budget` — the split is the fix; budget-tuning is a separate lever (and `thinking_budget=0` is a known MALFORMED_FUNCTION_CALL landmine when calling tools).
- Splitting the non-search producers (`merge_planners`, `combined_web_evaluator`) — they are tool-free and reliable; their required-`{var}` consumers assume they never no-op.
- Lowering the searcher/synthesizer `temperature` (e.g. trend_scout's `understand_trends_agent` carries `temperature=1.5`) — a separate tuning lever like the `pick_trends_agent` fix, not part of WS2's structural split.
