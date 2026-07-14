# Workstream 1 — Retry-on-Empty Wrapper Rollout (Implementation Plan)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire the already-built `RetryUntilKeyAgent` around all three flaky research producers, plus a minimal downstream guard, so a producer that finishes without writing its `output_key` no longer crashes the `creative_agent` / `interactive_creative` run — it retries, and on total exhaustion degrades observably instead of raising `KeyError: Context variable not found`.

**Architecture:** Each flaky producer is an `Agent` that runs `google_search` under a `BuiltInPlanner` *and* synthesizes the final text in one turn; on gemini-3 it intermittently emits no final text, leaving its `output_key` unset. `RetryUntilKeyAgent` (a custom `BaseAgent`, `creative_agent/retry_agent.py`) re-runs a wrapped producer until its `output_key` is populated (bounded by `max_attempts`). We wrap each producer *inside its existing parent pipeline* (raw producer symbol preserved, wrapper named `*_resilient`), and additionally make the one **unguarded** consumer (`merge_planners`) tolerate a missing input. Because `interactive_creative` and the CRF fan-out worker both reuse the same shared pipeline objects, the fix propagates to every caller with no per-caller change.

**Tech Stack:** google-adk 2.4.0 (`BaseAgent`, `SequentialAgent`, `InMemoryRunner`), pytest (offline; `asyncio.run`, no pytest-asyncio), `uv` / `uvx ruff`, the isolated tagged-Cloud-Run-revision smoke harness.

---

## Context (why this change)

`creative_agent`'s research pipeline is brittle: any producer that finishes without writing its `output_key` makes the next agent's `{var}` instruction template raise `KeyError` and abort the whole run *after* the expensive research. This has caused real production crashes (PR #69 patched only the third landmine downstream). `retry_config`/`INFRA_RETRY` does **not** help — it retries only infra exceptions (transient 5xx), not an empty-but-successful model turn.

The retry primitive was researched (Task #88) and prototyped: **PR #70** (branch `exp/retry-until-key-prototype`) already contains `creative_agent/retry_agent.py` (`RetryUntilKeyAgent`) and `tests/test_retry_agent.py` (3 tests green via a real `InMemoryRunner`). A custom `BaseAgent` was chosen because `LoopAgent`/`SequentialAgent`/`ParallelAgent` are all `@deprecated` in google-adk 2.4.0 in favor of `Workflow`, which cannot yet be an `LlmAgent` sub-agent.

This plan **builds on PR #70's branch** — the wrapper module and its unit tests already exist and need no changes. WS1 adds the wiring, the guard, and the structural tests. (WS3 = richer observability, WS2 = split tool-use from synthesis; both are separate, later plans.)

**Scope decisions (confirmed with the user):**
- **Wrap all three** producers (the two unguarded landmines + `enhanced_combined_searcher`, whose downstream is already guarded — retrying it is a quality gain, not just crash-safety).
- **Wrapper + minimal guard**: also make `merge_planners`' two inputs optional so WS1 *fully* eliminates the crash (retry recovers the common case; exhaustion degrades observably).

---

## Where `RetryUntilKeyAgent` is applied — the three wrap sites

All three producers share the identical shape: `tools=[google_search]` + `BuiltInPlanner(include_thoughts=False)` + `after_agent_callback=callbacks.collect_research_sources_callback`. Each is wrapped **in place inside its existing parent** (the parent's other sub-agents and order are unchanged; only the flaky producer entry is replaced by its `*_resilient` wrapper).

| Site | Producer (raw symbol kept) | File | `output_key` | Parent pipeline (entry replaced) | Consumer of the key | Guard status |
|---|---|---|---|---|---|---|
| 1 | `campaign_web_searcher` | `creative_agent/sub_agents/campaign_researcher/agent.py` | `campaign_web_search_insights` | `ca_sequential_planner` (entry `[1]`) | `merge_planners` | **UNGUARDED** → guard added in Task 5 |
| 2 | `gs_web_searcher` | `creative_agent/sub_agents/trend_researcher/agent.py` | `gs_web_search_insights` | `gs_sequential_planner` (entry `[1]`) | `merge_planners` | **UNGUARDED** → guard added in Task 5 |
| 3 | `enhanced_combined_searcher` | `creative_agent/agent.py` | `refined_web_search_insights` | `combined_research_pipeline` (entry `[2]`) | `combined_report_composer` | already guarded by `{refined_web_search_insights?}` (#69) |

**Propagation (why this is enough):** `interactive_creative/agent.py` imports the *same* `combined_research_pipeline` object and exposes it as an `AgentTool`; `creative_agent`'s own root exposes it the same way; the CRF fan-out worker invokes `creative_agent` on Agent Engine. None of them reference the inner producers directly. So wrapping the leaves fixes all three entry points at once, provided `combined_research_pipeline`'s identity, sub-agent **order**, and asserted `output_key`s stay intact.

**Naming rule:** keep the raw producer symbol bound to the raw `Agent` (so `test_output_keys_are_set_correctly` still imports it and checks its `output_key`); name each wrapper `<producer>_resilient`. Keep `after_agent_callback` on the **inner** producer (grounding capture must run per search attempt; `collect_research_sources_callback` dedups by URL so re-runs are idempotent).

---

## Tasks (as executed)

- **Task 1 — baseline gate:** `uv run pytest tests/test_retry_agent.py -q` (3 passed) + `uvx ruff check creative_agent/retry_agent.py` clean.
- **Task 2 — wrap `campaign_web_searcher`:** TDD `test_campaign_producer_is_retry_wrapped`; add `campaign_web_searcher_resilient`, swap into `ca_sequential_planner`.
- **Task 3 — wrap `gs_web_searcher`:** TDD `test_trend_producer_is_retry_wrapped`; add `gs_web_searcher_resilient`, swap into `gs_sequential_planner`.
- **Task 4 — wrap `enhanced_combined_searcher`:** update `test_combined_research_pipeline_sub_agent_order` (entry `[2]` → `enhanced_combined_searcher_resilient` + `RetryUntilKeyAgent` assertions); add `enhanced_combined_searcher_resilient`, swap into `combined_research_pipeline`.
- **Task 5 — guard `merge_planners`:** TDD `test_merge_planners_inputs_are_optional`; change the two `<CONTEXT>` refs to `{campaign_web_search_insights?}` / `{gs_web_search_insights?}` and add a "Research Gaps" instruction clause.
- **Task 6 — full offline gate:** `tests/test_retry_agent.py` 3 passed; full suite green; `uvx ruff check .` clean.
- **Task 7 — live isolated smoke:** deploy branch as no-traffic tagged revision `retry-ws1`; drive a full `creative_agent` run; verify it reaches `combined_report_composer` with no `KeyError`, retry WARN/INFO lines on a flake.
- **Task 8 — finalize:** mirror this plan; commit wiring + tests; retitle PR #70 for WS1 + mark ready; update memory.

---

## Verification (end-to-end)

- **Offline (CI-safe):** `uv run pytest tests/test_retry_agent.py -q` (3 passed) + `PYTHONPATH="$PWD" uv run pytest tests/ -q` green; `uvx ruff check .` clean.
- **Structure:** all three producers are `*_resilient` `RetryUntilKeyAgent`s with matching `output_key`s; `combined_research_pipeline` order test updated and green; `merge_planners` inputs optional.
- **Live (isolated tagged revision, prod untouched):** a full `creative_agent` run completes through `combined_report_composer` with no `KeyError`; retry WARN/INFO lines appear on a flake; a forced/observed exhaustion leaves the run alive with a `Research Gaps` note + `<key>__retry_exhausted` marker.

## Risks / call-outs

- **Sub-agent order is load-bearing.** Replace *in place* at the same index; don't reorder. Task 4's test enforces this.
- **One-parent constraint.** Wrapping removes the raw producer from its parent's `sub_agents` and inserts the wrapper instead; leaving both would give the producer two parents. The structure tests catch it.
- **Resumability (`interactive_creative`).** `RetryUntilKeyAgent` does not emit ADK agent-state/resume events. Safe because the research pipeline runs to completion inside one pre-checkpoint `AgentTool` call (checkpoints are at the orchestrator level, after research). If a checkpoint is ever added *inside* research, the wrapper would need resumability support — flag, don't fix here.
- **Retry cost.** Each retry is a full producer turn on `worker_model` (`gemini-3.5-flash`, higher RPM than pro/image). `max_attempts=3` is a safe default; retries fire only on the flake.
- **`include_contents` on retry.** `campaign_web_searcher` / `gs_web_searcher` use the default (history included), so a retry sees the prior failed attempt's events (harmless); `enhanced_combined_searcher` uses `include_contents="none"` (clean retry).

## Out of scope (later workstreams)

- **WS3:** richer observable guardrails (surface `<key>__retry_exhausted` in the eval report / final summary; sentinel-text backfill).
- **WS2:** split each producer into a tool-using searcher + a tool-free synthesizer (the durable root-cause fix).
- **ADK deprecation:** migrating off deprecated `SequentialAgent`/`ParallelAgent` to `Workflow` (blocked — can't nest under `LlmAgent`).
- Tuning research thinking budgets (the unproven `thinking_budget=512` experiment on `exp/harden-enhanced-combined-searcher`).
