# trend_scout Degradation-Surfacing + raw_gtrends Hardening — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `trend_scout` (a) surface a `RetryUntilKeyAgent` retry-exhaustion as a user-visible note instead of only a log line, and (b) degrade gracefully instead of `KeyError` if `raw_gtrends` is ever missing.

**Architecture:** Reuse the existing `agent_common.collect_degradation_warnings` (the single source of truth `creative_agent` already uses for its gallery banner / `creative_evals.research_gaps`) via a small new `record_research_gaps` tool that writes a human-readable `research_gaps` state key, surfaced in the orchestrator handoff and the GCS session JSON. Separately, make the one remaining non-optional template var (`{raw_gtrends}`) optional + guarded, mirroring the guard `pick_trends_agent` already has for `{info_gtrends?}`.

**Tech Stack:** Python 3.13, Google ADK, `uv`, `pytest`, `ruff`.

---

## Context (why)

The `info_gtrends` empty-turn landmine is **already fixed** — PR #74 (`676d565`) split `understand_trends_agent` into a searcher (`info_gtrends_raw`) + synthesizer (`info_gtrends`) pair wrapped by `understand_trends_agent_resilient = RetryUntilKeyAgent(output_key="info_gtrends", max_attempts=3)`, and `pick_trends_agent` guards `{info_gtrends?}`. (My memory note `trend-scout-info-gtrends-landmine` was stale and predated PR #74; it is corrected in Task 5.)

Exploration surfaced two real, net-new gaps PR #74 did **not** cover:

1. **Retry-exhaustion is invisible to users.** When `understand_trends_agent_resilient` exhausts all 3 attempts it emits an `info_gtrends__retry_exhausted` state marker (`agent_common/retry_agent.py:119-136`). `creative_agent` turns that marker into a user-facing note via `collect_degradation_warnings` (`creative_agent/tools.py:464`, `creative_eval/agent.py:99`), but **`trend_scout` never calls it** — the marker only lands in a log line (`make_final_state_summary`) and as a raw boolean in the GCS state dump. A trend_scout run can silently ship a degraded/empty trend set.
2. **`{raw_gtrends}` is consumed non-optionally** (`trend_scout/agent.py:79`). It's a deterministic tool write (`get_daily_gtrends` → `tool_context.state["raw_gtrends"]`, `tools.py:123`), so low-risk, but it's the one remaining hard-`KeyError` spot of the same class as the original landmine (e.g. if the orchestrator ever calls research before gather, or the gather tool errors). Making it optional + guarded lets the wrapper retry/exhaust cleanly (which Gap 1 then surfaces) instead of raising inside the searcher.

**Intended outcome:** a degraded trend_scout run reports *why* in its final handoff and in the persisted session JSON (parity with the creative gallery banner), and a missing `raw_gtrends` degrades to an observable exhaustion note rather than a crash.

## Key existing code to reuse (do NOT reinvent)

- `agent_common/collect_degradation_warnings(state)` (`observability.py:112`) — turns every `*__retry_exhausted` marker into a sorted list of step-neutral notes (`"Step 'info_gtrends' exhausted retries and produced no output."`). Exported from `agent_common` (`__init__.py:21`). Already unit-tested in `tests/test_observability.py:147-179`.
- `creative_agent/tools.py:417-466` — reference for wiring `collect_degradation_warnings` into a deliverable (`_build_research_warning_banner` + call site). Mirror the "empty ⇒ render nothing on the happy path" behavior.
- `pick_trends_agent` guard (`trend_scout/agent.py:204-206`) — the exact "if empty, don't invent, say so, stop" instruction pattern to copy for the `raw_gtrends` guard.
- `MockToolContext` / `MockState` (`tests/test_tools.py:41-49`) — the offline fake for tool tests. `SimpleNamespace(state={...})` is used in `tests/test_observability.py` for callback tests.

---

## Task 1: Harden the `{raw_gtrends}` consumer (optional var + guard)

**Files:**
- Modify: `trend_scout/agent.py` — `understand_trends_searcher.instruction` (`{raw_gtrends}` at line 79; add guard in the instructions block ~line 83-84)
- Test: `tests/test_pipeline_structure.py`

**Step 1 — Write failing test.** In `tests/test_pipeline_structure.py`:
```python
def test_understand_trends_searcher_raw_gtrends_is_optional():
    from trend_scout import agent as ts

    instr = ts.understand_trends_searcher.instruction
    # optional template var so a missing raw_gtrends degrades instead of KeyError
    assert "{raw_gtrends?}" in instr
    assert "{raw_gtrends}" not in instr  # the bare non-optional form is gone
```

**Step 2 — Run, expect FAIL** (bare `{raw_gtrends}` still present):
`PYTHONPATH="$PWD" uv run pytest tests/test_pipeline_structure.py::test_understand_trends_searcher_raw_gtrends_is_optional -q`

**Step 3 — Implement.** In `understand_trends_searcher.instruction`:
- Change `{raw_gtrends}` (line 79) to `{raw_gtrends?}`.
- Add a guard as the first instruction step (before the current "1. **Filter:**"), mirroring `pick_trends_agent` lines 204-206:
  > `0. If <raw_gtrends> is empty, the upstream trend gather did not run. Do NOT invent terms — report that no trends were available and stop.`

**Step 4 — Run, expect PASS.** Also re-run the existing trend_scout structure tests to confirm no regression:
`PYTHONPATH="$PWD" uv run pytest tests/test_pipeline_structure.py -q`

## Task 2: Add `record_research_gaps` tool

**Files:**
- Modify: `trend_scout/tools.py` — new import + new tool (add near `memorize`, ~line 39)
- Test: `tests/test_tools.py`

**Step 1 — Write failing tests.** In `tests/test_tools.py` (reuse `MockToolContext`/`MockState` already defined there):
```python
class TestRecordResearchGaps:
    def test_exhaustion_marker_becomes_note(self):
        from trend_scout.tools import record_research_gaps

        ctx = MockToolContext()
        ctx.state["info_gtrends__retry_exhausted"] = True
        result = record_research_gaps(ctx)

        assert result["status"] == "success"
        assert result["count"] == 1
        assert "info_gtrends" in ctx.state["research_gaps"]
        assert ctx.state["research_gaps"] == result["research_gaps"]

    def test_clean_state_is_empty_string(self):
        from trend_scout.tools import record_research_gaps

        ctx = MockToolContext()
        ctx.state["info_gtrends"] = "some real briefing"
        result = record_research_gaps(ctx)

        assert result["status"] == "success"
        assert result["count"] == 0
        assert ctx.state["research_gaps"] == ""  # happy path renders nothing
```

**Step 2 — Run, expect FAIL** (`record_research_gaps` undefined):
`PYTHONPATH="$PWD" uv run pytest tests/test_tools.py::TestRecordResearchGaps -q`

**Step 3 — Implement.** In `trend_scout/tools.py`:
- Add import: `from agent_common import collect_degradation_warnings` (next to `from .config import config`).
- Add the tool:
```python
def record_research_gaps(tool_context: ToolContext) -> dict:
    """Turn any RetryUntilKeyAgent exhaustion markers into a human-readable note.

    Reads the `*__retry_exhausted` markers left in state by the resilient research
    wrapper and stores a single `research_gaps` string (empty when research was
    clean). This is trend_scout's analog of the creative gallery banner — the same
    `collect_degradation_warnings` source of truth — so a degraded run surfaces WHY
    in the handoff and the persisted GCS session JSON instead of only in logs.
    """
    notes = collect_degradation_warnings(tool_context.state)
    research_gaps = "; ".join(notes)
    tool_context.state["research_gaps"] = research_gaps
    return {"status": "success", "research_gaps": research_gaps, "count": len(notes)}
```

**Step 4 — Run, expect PASS.**

## Task 3: Wire the tool + handoff into the orchestrator

**Files:**
- Modify: `trend_scout/agent.py` — import the tool (`.tools` block, lines 10-17), register it in the root `tools=[...]` list (lines 297-306), add it as the first Phase 3 step and a "Research Notes" line in Phase 4 (instruction, lines 281-296)
- Test: `tests/test_pipeline_structure.py`

**Step 1 — Write failing test:**
```python
def test_trend_scout_exposes_record_research_gaps():
    from trend_scout import agent as ts
    from trend_scout.tools import record_research_gaps

    assert record_research_gaps in ts.trend_scout.tools
    # handoff surfaces the note (optional var so happy path shows nothing)
    assert "{research_gaps?}" in ts.trend_scout.instruction
```

**Step 2 — Run, expect FAIL:**
`PYTHONPATH="$PWD" uv run pytest tests/test_pipeline_structure.py::test_trend_scout_exposes_record_research_gaps -q`

**Step 3 — Implement.** In `trend_scout/agent.py`:
- Add `record_research_gaps` to the `from .tools import (...)` block and to the root agent's `tools=[...]` list.
- In the instruction, make it the **first** Phase 3 step so `research_gaps` is set before `save_session_state_to_gcs` snapshots state:
  > `### Phase 3: Finalization & Persistence` → `1. record_research_gaps` → then `write_trends_to_bq`, `write_to_file`, `save_session_state_to_gcs`.
- In Phase 4 handoff output, append a conditional section:
  > `**Research Notes:** {research_gaps?}` — (with a note in the prompt: omit/leave blank when empty).

**Step 4 — Run, expect PASS.**

## Task 4 (OPTIONAL — user-gated, touches the BigQuery data store): BQ `research_gaps` column parity

Only do this if the user explicitly wants full `creative_evals.research_gaps` parity in `target_trends_crf`. It requires an **additive, non-destructive** schema migration that must run **before** the code deploy (an `INSERT` naming a missing column fails).

**Files:**
- Modify: `trend_scout/tools.py` — `write_trends_to_bq` INSERT (`tools.py:295-321`): add `research_gaps` to the column list and `"{tool_context.state.get("research_gaps", "")}"` to `VALUES`.
- Migration (user runs it): `ALTER TABLE \`<BQ_PROJECT_ID>.<BQ_DATASET_ID>.<BQ_TABLE_TARGETS>\` ADD COLUMN IF NOT EXISTS research_gaps STRING;`

**Test:** `write_trends_to_bq` hits live BQ, so don't unit-test the network path. If pursued, extract the row SQL into a pure `_build_trend_insert_sql(...)` helper and assert it contains the `research_gaps` column + value — otherwise skip automated testing and validate live.

**Ordering:** run the `ALTER TABLE` first (safe/additive, preserves all rows), then deploy. Deferred by default.

## Task 5 (USER-GATED — do only when the user asks): finalize + validate

Per repo conventions and standing constraints:
- Branch off `main` (never commit to `main`); conventional commits; **no `Co-Authored-By` trailers**; do **not** commit `.python-version`; commit `uv.lock` only if it changes (it won't here).
- Copy this plan to `docs/plans/2026-07-15-trend-scout-degradation-surfacing.md` for the repo record.
- Open a PR (body ends with the Claude Code trailer). Do **not** merge without human review.
- **Correct stale memory:** update `trend-scout-info-gtrends-landmine.md` — the landmine was fixed in PR #74; this PR adds degradation-surfacing + raw_gtrends hardening on top. Refresh the `MEMORY.md` pointer line.
- **Live validation** (auto-mode-allowed isolated tagged revision, prod untouched): deploy `trend-trawler-api` with `--no-cpu-throttling --min-instances 1 --no-traffic --tag`, run a `trend_scout` job, and confirm the happy path shows an **empty** `research_gaps` in the GCS `trawler_session_state.json` and no "Research Notes" content in the handoff (no false degradation). The exhaustion path is covered offline in Task 2; forcing a live model flake is not reliably reproducible, so do not gate on it. Migrate traffic + prune the tag only on explicit user go-ahead.

## Verification (end-to-end)

- **Offline (CI-safe, no creds — `build_gemini` and `trend_scout/tools.py` clients are lazy, so both modules import without ADC):**
  - `record_research_gaps` maps an `info_gtrends__retry_exhausted` marker to a `research_gaps` note and yields `""` on a clean state (`tests/test_tools.py`).
  - `understand_trends_searcher` uses `{raw_gtrends?}` (no bare `{raw_gtrends}`) and `trend_scout` exposes the tool + `{research_gaps?}` in its handoff (`tests/test_pipeline_structure.py`).
  - Existing `collect_degradation_warnings` / trend_scout structure tests still pass unchanged.
- **Gate:** `uvx ruff check . && uvx ruff format --check .`; `PYTHONPATH="$PWD" uv run pytest tests/test_tools.py tests/test_pipeline_structure.py tests/test_observability.py tests/test_retry_agent.py -q` (creds-free subset; run full `tests/` on a host with ADC per CLAUDE.md → Testing).
- **Import smoke:** `PYTHONPATH="$PWD" uv run python -c "import trend_scout.agent; print('ok')"`.
- **Live (user-gated, Task 5):** happy-path trend_scout run shows empty `research_gaps` and a clean handoff; no regression in the persisted trends.

## Risks / call-outs

- **State-delta propagation across AgentTool:** the `info_gtrends__retry_exhausted` marker is emitted inside `understand_trends_agent_resilient`, which runs via `AgentTool` (isolated sub-Runner). `info_gtrends` itself already propagates back to the parent (pick_trends reads it), so the sibling marker should too — but confirm the marker is present in parent state at Phase 3 during live validation.
- **Phase-3 ordering matters:** `record_research_gaps` must run before `save_session_state_to_gcs` (and before the optional BQ write) so `research_gaps` is in the snapshot. It's placed first in Phase 3; the orchestrator is instructed to run Phase 3 steps in order.
- **BQ column is opt-in and infra-touching:** Task 4 is deferred by default; it needs a pre-deploy `ALTER TABLE ADD COLUMN` (additive, preserves data). The code-only surface (state key + handoff + GCS JSON) delivers the user-visible win with zero data-store changes.
- **Scope:** this is orthogonal to the already-shipped PR #74 retry-wrap and to the async-job/image-fix work; no `runserver/`, frontend, or `creative_agent` changes.
