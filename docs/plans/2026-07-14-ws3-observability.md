# Workstream 3 — Richer Observability (Implementation Plan)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give `creative_agent` / `interactive_creative` the same debugging observability `trend_scout` got (run-start correlation, end-of-run state summary, empty-turn finish_reason warnings) by extracting those callbacks into a shared `agent_common` module, AND surface retry-exhaustion degradation (`*__retry_exhausted` markers) into three user-visible places: the eval report JSON, the `creative_evals` BigQuery row, and the HTML portfolio gallery.

**Architecture:** The three trend_scout callbacks are agent-agnostic except for one hard-coded state-key tuple. Extract them to `agent_common/observability.py` (a pure-logic module — imports `google.adk`/`google.genai` but builds no genai client, so it stays non-creds-gated and unit-testable), parameterizing the final-state summary by key tuple via a factory. Re-export thin shims from each package's `callbacks.py` so agent wiring stays terse. Add a single pure helper `collect_degradation_warnings(state)` that scans state for `*__retry_exhausted` markers and returns human-readable strings; the eval report, BQ row, and HTML gallery all derive their degradation notes from it (one source of truth).

**Tech Stack:** google-adk 2.4.0 (`CallbackContext`, `LlmResponse`, `State`), pydantic v2, pytest (offline; `caplog`, `SimpleNamespace` stubs + real `State`), `uv` / `uvx ruff`, BigQuery (`ALTER TABLE`), the isolated tagged-Cloud-Run-revision smoke harness.

---

## Context (why this change)

WS1 (PR #70) wrapped the three flaky research producers in `RetryUntilKeyAgent`, and the trend_scout hardening (PR #71) added the same wrapper + three debugging callbacks to `trend_scout`. That work proved its worth immediately: the 2026-07-14 UI run showed the retry recovering `info_gtrends` first-try, and the new `run start:` correlation line let us trace the session. But two gaps remain:

1. **Log parity:** `creative_agent` and `interactive_creative` still have **zero** `after_model_callback`s, no run-start correlation line, and no end-of-run state summary. When a creative run stalls we can't see *where* from the logs. (Noted as an open follow-up: "port the finish_reason-on-empty callback + run-start correlation line to creative_agent's producers … fold into WS3 observability.")
2. **Degradation is logs-only:** when a producer exhausts its retries, `RetryUntilKeyAgent` leaves a `<key>__retry_exhausted` marker and `merge_planners` writes a free-text "Research Gaps" note — but neither reaches a structured, consumable surface. The eval report, the `creative_evals` BQ table, and the HTML deliverable all look identical whether research succeeded or silently degraded.

WS3 closes both. **Scope confirmed with the user:** extract the callbacks to `agent_common` (refactoring trend_scout to share, matching the `RetryUntilKeyAgent` relocation precedent) **and** surface degradation in **all three** places (eval report field, HTML banner, BigQuery column).

**Branch base:** stack on `exp/harden-trend-scout` (PR #71 head, itself stacked on PR #70) — WS3 depends on both the creative_agent wrappers/markers (WS1) and the trend_scout callbacks it refactors. Merge order: #70 → #71 → WS3. (Alternatively branch off `main` once #70+#71 land.)

---

## Key files (from exploration)

- `trend_scout/callbacks.py:119-197` — the three callbacks to extract: `_describe_state_value`, `log_final_state_summary`, `log_empty_turn_finish_reason`; run-start line at `:56-62` inside `load_session_state`.
- `agent_common/__init__.py:9-15` — exports; add the new observability names here.
- `creative_agent/callbacks.py:45-66` — `load_session_state` (no run-start line today); `config.state_init` sentinel.
- `creative_agent/agent.py` — model agents needing `after_model_callback` (merge_planners `:35`, combined_web_evaluator `:107`, enhanced_combined_searcher `:161`, combined_report_composer `:217`, ad_copy_drafter `:350`, ad_copy_critic `:438`, visual_concept_drafter `:530`, visual_concept_critic `:612`, visual_concept_finalizer `:703`, visual_generator `:759`) + `root_agent` `:821` (also needs `after_agent_callback`; wiring at `:899-900`).
- `creative_agent/sub_agents/{trend_researcher,campaign_researcher}/agent.py` — planner + searcher agents (`gs_web_planner:44`, `gs_web_searcher:89`, `campaign_web_planner:44`, `campaign_web_searcher:95`) need `after_model_callback`.
- `creative_eval/agent.py:27-115` (`evaluate_all_creatives`, has `tool_context.state`), `:119-137` (`creative_eval_agent`, no callbacks today).
- `interactive_creative/agent.py:14` (imports `creative_agent.callbacks`), root at `:23`, wiring `:106-107`.
- `creative_eval/schemas.py:78-94` — `CreativeEvaluationReport`.
- `creative_agent/tools.py:1213-1247` (`build_eval_bq_row`), `:1320-1364` (`write_eval_report_to_bq`), `:416-1029` (`save_creative_gallery_html`; header block `:811-825`, CSS `:469-511`/`:717-805`).
- `tests/test_trend_scout_logging.py` — the reference test structure (SimpleNamespace ctx, `_resp` builder, **real `State`** for state-summary tests, caplog substring asserts).

**Reused primitives:** `RetryUntilKeyAgent` exhaustion marker `EventActions(state_delta={f"{output_key}__retry_exhausted": True})` (`agent_common/retry_agent.py:120-127`); the three creative markers are `campaign_web_search_insights__retry_exhausted`, `gs_web_search_insights__retry_exhausted`, `refined_web_search_insights__retry_exhausted`. State-iteration gotcha: an ADK `State` isn't directly iterable (`for k in state` → `KeyError: 0`) — always snapshot `state.to_dict()` first (the exact bug fixed in commit `9ec1c92`).

---

## Tasks

### Task 1: Create the shared `agent_common/observability.py` *(TDD)*
**Files:** Create `agent_common/observability.py`; modify `agent_common/__init__.py`; Test: create `tests/test_observability.py`.

1. **Failing tests** — new `tests/test_observability.py`, mirroring `tests/test_trend_scout_logging.py` (import from `agent_common.observability`, `SimpleNamespace` ctx, `_resp` helper, real `State`). Cover:
   - `log_empty_turn_finish_reason`: normal text turn silent; tool-call turn silent; MAX_TOKENS empty turn warns (message contains agent_name + `thoughts_tokens`); STOP-but-empty warns; partial chunk ignored.
   - `make_final_state_summary("x", ("a","b"))(ctx)`: with a real `State`, logs presence of `a`/`b` and `retry_exhausted=[...]` markers; does NOT crash on a `State` (regression lock for the `KeyError: 0` bug).
   - `collect_degradation_warnings(state)`: returns `[]` when clean; returns one human string per `*__retry_exhausted` marker; snapshots `State` (no iteration crash).
2. **Run → FAIL** (`ModuleNotFoundError`): `export PATH="$HOME/.local/bin:$PATH"; cd /home/user/adk_pipe; uv run pytest tests/test_observability.py -q`.
3. **Implement** `agent_common/observability.py`:
   - Move `_describe_state_value` and `log_empty_turn_finish_reason` verbatim from `trend_scout/callbacks.py:119-127,156-197` (they're already agent-agnostic — `log_empty_turn_finish_reason` reads only `agent_name`/`invocation_id`).
   - `log_run_start(callback_context)` — the run-start correlation `logging.info` from `trend_scout/callbacks.py:56-62`, agent-agnostic (reads `agent_name`/`invocation_id`/`session.id`/`user_id`).
   - `make_final_state_summary(agent_label: str, keys: tuple[str, ...])` — returns an `after_agent_callback` closure that snapshots `state.to_dict()`, describes `keys`, collects `*__retry_exhausted`, and logs `"%s final state [invocation=%s]: %s%s"` with `agent_label`. (Generalizes `trend_scout/callbacks.py:130-153`, whose key tuple was hard-coded.)
   - `collect_degradation_warnings(state) -> list[str]` — snapshot `state.to_dict() if isinstance(state, State) else dict(state)`; for each key ending `__retry_exhausted` whose value is truthy, append e.g. `f"Research step '{key[:-len('__retry_exhausted')]}' exhausted retries and produced no output; downstream synthesis used partial data."`; return sorted list.
   - Use lazy `%s` logging (the new-callback convention) and the module logging.basicConfig block matching sibling modules.
   - Add all five names to `agent_common/__init__.py` `__all__` + imports.
4. **Run → PASS.** `uvx ruff check agent_common/observability.py tests/test_observability.py && uvx ruff format …`.

### Task 2: Refactor `trend_scout` to consume the shared module *(refactor, tests already exist)*
**Files:** Modify `trend_scout/callbacks.py`; Test: `tests/test_trend_scout_logging.py` (unchanged — it's the oracle).

1. In `trend_scout/callbacks.py`: delete the local `_describe_state_value`, `log_final_state_summary`, `log_empty_turn_finish_reason`; replace the inline run-start block in `load_session_state` with `observability.log_run_start(callback_context)`. Add re-export shims so `trend_scout/agent.py` needs no change:
   ```python
   from agent_common import observability
   log_empty_turn_finish_reason = observability.log_empty_turn_finish_reason
   log_final_state_summary = observability.make_final_state_summary(
       "trend_scout", ("raw_gtrends", "info_gtrends", "selected_gtrends")
   )
   ```
   (Drop the now-unused `types`/`LlmResponse`/`State` imports if nothing else uses them.)
2. **Run → PASS unchanged:** `uv run pytest tests/test_trend_scout_logging.py -q` → 6 passed (behavior identical; the test file already uses a real `State`). ruff clean.

### Task 3: Wire the callbacks across the creative pipeline *(TDD — structural)*
**Files:** Modify `creative_agent/callbacks.py`, `creative_agent/agent.py`, `creative_agent/sub_agents/{trend_researcher,campaign_researcher}/agent.py`, `creative_eval/agent.py`, `interactive_creative/agent.py`; Test: `tests/test_pipeline_structure.py` (creds-gated — run on deploy host/CI if no local ADC).

1. **Failing tests** in `tests/test_pipeline_structure.py` (import inside the test fn, per the file's convention):
   - `test_creative_model_agents_have_finish_reason_callback`: for the ~11 creative model agents + root, assert `after_model_callback is callbacks.log_empty_turn_finish_reason`.
   - `test_creative_root_has_final_state_summary`: `root_agent.after_agent_callback` is the summary closure (assert it's callable + not None; optionally that calling it on a real `State` logs).
   - `test_interactive_root_has_observability_callbacks`: same two for `interactive_creative`'s root.
2. **Run → FAIL.**
3. **Implement:**
   - `creative_agent/callbacks.py`: add `observability.log_run_start(callback_context)` at the top of `load_session_state`; add re-export shims `log_empty_turn_finish_reason = observability.log_empty_turn_finish_reason` and `log_final_state_summary = observability.make_final_state_summary("creative_agent", ("combined_final_cited_report", "ad_copy_critique", "final_visual_concepts", "creative_evaluation_report"))`.
   - `creative_agent/agent.py`: add `after_model_callback=callbacks.log_empty_turn_finish_reason` to each of the 11 model agents listed in Key files + `root_agent`; add `after_agent_callback=callbacks.log_final_state_summary` to `root_agent`. (These are the `after_model`/`after_agent` slots — all currently unset; the existing `after_agent_callback`s on searcher/composer are on *sub-agents* and untouched.)
   - `creative_agent/sub_agents/*/agent.py`: add `after_model_callback=callbacks.log_empty_turn_finish_reason` to the planner + searcher agents (they already `from ... import callbacks`).
   - `creative_eval/agent.py`: `from agent_common import log_empty_turn_finish_reason`; add it as `creative_eval_agent`'s `after_model_callback`.
   - `interactive_creative/agent.py`: add `after_model_callback=callbacks.log_empty_turn_finish_reason` + `after_agent_callback=callbacks.log_final_state_summary` to its root (sub-agents inherit the wiring from creative_agent).
4. **Run → PASS.** ruff.

### Task 4: Eval report `warnings` field *(TDD)*
**Files:** Modify `creative_eval/schemas.py`, `creative_eval/agent.py`; Test: `tests/test_creative_eval.py` (pure-logic).

1. **Failing tests:** `test_report_has_warnings_default` (a `CreativeEvaluationReport` built without `warnings` has `warnings == []`); `test_evaluate_populates_warnings_from_markers` (call `evaluate_all_creatives` with a stubbed `tool_context.state` containing a `*__retry_exhausted` marker + minimal creatives → stored report's `warnings` is non-empty). Mirror the existing eval-tool test mocks.
2. **Run → FAIL.**
3. **Implement:** add `warnings: list[str] = Field(default_factory=list, description="Human-readable notes about degraded/incomplete pipeline steps (e.g. research retries exhausted).")` to `CreativeEvaluationReport` (`schemas.py:78-94`). In `evaluate_all_creatives` (`creative_eval/agent.py`), before constructing the report, `warnings = collect_degradation_warnings(state)` (import from `agent_common`) and pass `warnings=warnings` to the constructor. Default factory means existing `CreativeEvaluationReport(...)` call sites and tests don't break.
4. **Run → PASS.** ruff. (Flows to GCS automatically — `save_eval_report_to_gcs` dumps the whole dict.)

### Task 5: `research_gaps` BigQuery column *(TDD + manual ALTER)*
**Files:** Modify `creative_agent/tools.py` (`build_eval_bq_row`); Test: `tests/test_tools.py` (or wherever `build_eval_bq_row` is tested). Docs: `README.md` DDL.

1. **Failing test:** `test_build_eval_bq_row_includes_research_gaps` — a report dict with `warnings: ["…","…"]` produces a row whose `research_gaps` is the `" | "`-joined string; empty `warnings` → `""`.
2. **Run → FAIL.**
3. **Implement:** in `build_eval_bq_row`, read `report.get("warnings") or []` and add `"research_gaps": " | ".join(warnings)` to the returned row. `write_eval_report_to_bq` needs no change (derives from the report). Update the `creative_evals` DDL in `README.md` to add `research_gaps STRING`.
4. **Run → PASS.** ruff.
5. **Manual (with-creds, Task 8):** `ALTER TABLE hybrid-vertex.trend_trawler.creative_evals ADD COLUMN research_gaps STRING;` **must run before** deploying the new `build_eval_bq_row` (else `insert_rows_json` rejects the unknown field). Also update `test_deploy_utils` only if a new env var were added (none is).

### Task 6: HTML gallery degradation banner *(TDD-light)*
**Files:** Modify `creative_agent/tools.py` (`save_creative_gallery_html`); Test: `tests/test_tools.py` (creds-gated) or a targeted pure test if the HTML-assembly can be exercised without the client.

1. **Failing test:** if a lightweight path exists, assert the produced HTML contains a `research-warning` banner when `state["creative_evaluation_report"]["warnings"]` (or `collect_degradation_warnings(state)`) is non-empty, and does NOT when clean. If the function is only exercisable creds-gated, add the assertion there and note it runs on the deploy host/CI.
2. **Run → FAIL.**
3. **Implement:** in `save_creative_gallery_html`, compute `warnings = collect_degradation_warnings(state)` (single source of truth — do not re-scan markers ad hoc); if non-empty, inject a warning `<div class="research-warning">…</div>` into the `HTML_BODY` header block (`tools.py:811-825`) listing the notes, and add a small CSS rule near the existing `.sub-header-container` styles. When clean, render nothing (no empty banner).
4. **Run → PASS.** ruff.

### Task 7: Full offline gate *(gate)*
- `uv run pytest tests/test_observability.py tests/test_trend_scout_logging.py tests/test_creative_eval.py -q` → all green.
- `PYTHONPATH="$PWD" uv run pytest tests/ -q` → green (note any creds-gated files that must run on the deploy host / CI — `test_pipeline_structure.py`, `test_tools.py`).
- `uvx ruff check . && uvx ruff format --check .` → clean.

### Task 8: Live smoke + manual BQ ALTER + docs *(gate before PR ready)*
- **BQ first:** run the `ALTER TABLE … ADD COLUMN research_gaps STRING` (Task 5.5) via `bq query` — before any deploy that writes eval rows.
- Deploy the branch as a no-traffic tagged revision (auto-mode-allowed — private service, no auth change): `gcloud run deploy trend-trawler-api --source . --region us-central1 --project hybrid-vertex --no-traffic --tag ws3-obs`. Verify clean boot + `/list-apps` returns all 3 agents.
- Drive a full `creative_agent` run against the tag (SA-impersonation + base-URL-audience recipe). **Verify:** the new `run start: agent=creative_agent …` correlation line and `creative_agent final state [invocation=…]: {…}` summary appear; no `empty/abnormal model turn` warnings on the happy path; run completes through eval → GCS → BQ; the `creative_evals` row has `research_gaps=""`; the report JSON has `warnings: []`. (Exhaustion is a nondeterministic flake — the **degraded path is covered by the offline tests**, which are the reliable oracle; don't try to force it live.)
- Update `README.md` / `CLAUDE.md` observability notes as needed; delete the tagged revision when done.

### Task 9: Finalize *(only when the user asks to commit)*
- Mirror this plan to `docs/plans/2026-07-14-ws3-observability.md`.
- Commit per-task (conventional `feat(agent_common):` / `feat(creative_agent):` / `test:`; **no `Co-Authored-By`**). Open a PR stacked on PR #71 (or off main if #70/#71 have merged).
- Update memory: `adk-pipe-work-status`, `adk-retry-until-populated-research`, `creative-agent-research-pipeline-brittleness`.

---

## Verification (end-to-end)

- **Offline (CI-safe):** `tests/test_observability.py` (finish_reason predicate, final-state factory, `collect_degradation_warnings`) + `tests/test_creative_eval.py` (warnings field + marker population) + the `build_eval_bq_row` `research_gaps` test all green; `tests/test_trend_scout_logging.py` still 6 passed unchanged (refactor is behavior-preserving); `uvx ruff check .` clean.
- **Structure (creds-gated, deploy host/CI):** `test_pipeline_structure.py` asserts every creative model agent has `after_model_callback=log_empty_turn_finish_reason` and both roots have the final-state summary.
- **Degraded path (offline oracle):** a `*__retry_exhausted` marker in state → eval report `warnings` non-empty → `research_gaps` BQ cell populated → HTML gallery renders the banner.
- **Live (isolated tag, prod untouched):** a full creative_agent run logs the run-start + final-state lines, completes clean, writes `research_gaps=""` + `warnings: []` on the happy path.

## Risks / call-outs

- **Refactor touches PR #71 code.** Task 2 rewrites `trend_scout/callbacks.py` (just added on #71). The re-export shims keep `trend_scout/agent.py` unchanged and `tests/test_trend_scout_logging.py` is the behavior oracle — if it stays green, the refactor is safe. This is why WS3 stacks on #71.
- **State is not directly iterable.** Every marker scan MUST snapshot `state.to_dict()` first (the `KeyError: 0` bug, fixed in `9ec1c92`). `collect_degradation_warnings` and `make_final_state_summary` both do; the offline tests use a real `State` to lock it.
- **BQ column ordering.** The live `creative_evals` table must be `ALTER`ed to add `research_gaps` **before** the new `build_eval_bq_row` ships, or `insert_rows_json` rejects the row. Task 8 sequences this first.
- **`after_model_callback` slot is distinct** from `after_agent_callback`. Adding the finish_reason callback does not disturb the existing `collect_research_sources_callback` / `citation_replacement_callback` (those are `after_agent` on sub-agents).
- **Degradation captured at eval time.** The eval-report `warnings` reflect markers present when `evaluate_all_creatives` runs (after research/copy/visual) — the correct window for research-producer markers. If eval is skipped (no creatives), the final-state summary log still records the markers.
- **Research Gaps free-text** (from `merge_planners`) stays inline in the report body — WS3's structured warnings key off the reliable `*__retry_exhausted` markers, not substring-matching that text.

## Out of scope (later)
- **WS2:** split each producer into a tool-using searcher + a tool-free synthesizer (the durable root-cause fix for the empty-turn flake).
- **Sentinel-text backfill** (writing a placeholder into an exhausted `output_key`): superseded by WS1's `{var?}` guards + WS3's structured warnings; not needed.
- Porting `research_gaps` into the frontend results view (the BQ column + report field make it available; UI surfacing is a separate frontend task).
