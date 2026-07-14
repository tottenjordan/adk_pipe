# Research Pipeline Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate the class of crashes where a `creative_agent` research producer finishes without writing its `output_key`, making the next agent's `{var}` instruction template raise `KeyError: Context variable not found` and abort the whole run.

**Architecture:** Three layered defenses, shipped in order of leverage-per-risk. (1) A small custom `BaseAgent` wrapper (`RetryUntilKeyAgent`) re-runs a flaky producer until its `output_key` is populated — immediate relief, low blast radius. (3) Observable guardrails make any residual empty-output degrade visibly (`{var?}` + a state marker + logs) instead of crashing or silently degrading. (2) The durable structural fix: split each "search **and** synthesize in one agent" producer into a tool-using searcher + a tool-free synthesizer, so the text-emitting step has no thinking/tool pressure to blow its output budget. Layered: #2 makes each attempt reliable, #1 covers the residual, #3 makes anything left observable.

**Tech Stack:** google-adk (`BaseAgent`, `SequentialAgent`, `InMemoryRunner`), pytest (offline, `asyncio.run` — no pytest-asyncio), `uv`/`uvx ruff`, the isolated tagged-Cloud-Run-revision test harness (see memory `creative-agent-research-pipeline-brittleness`).

---

## Context (do not re-derive)

The prototype for mitigation #1 is **already built and passing** on branch `exp/retry-until-key-prototype` (off `main`):
- `creative_agent/retry_agent.py` — `RetryUntilKeyAgent` (standalone; no `genai` import so it stays offline-testable).
- `tests/test_retry_agent.py` — 3 tests GREEN via a real `InMemoryRunner` + a deterministic `_FlakyProducer` double: recovers after 2 empty attempts, does not retry a healthy producer, and stays bounded + observable when the producer never populates.

**Why the offline test is the real proof:** the empty-output flake is nondeterministic, so a live run can pass without ever triggering it — proving nothing. The `_FlakyProducer` triggers the exact failure deterministically and drives it through the genuine ADK state-delta path (the runner merges each event's `state_delta` into the same `session` object the wrapper reads, before the generator resumes — verified in `runners.py:~1412` → `base_session_service._update_session_state`).

**The three landmines** (required `{var}`; producer → consumer). All three producers share the identical anti-pattern — `google_search` + `BuiltInPlanner` **and** final-text synthesis in one `Agent`:

| # | `output_key` | Producer (file) | Consumer | Status |
|---|---|---|---|---|
| 1 | `campaign_web_search_insights` | `campaign_web_searcher` (`creative_agent/sub_agents/campaign_researcher/agent.py:95`) | `merge_planners` (`creative_agent/agent.py:51`) | UNGUARDED |
| 2 | `gs_web_search_insights` | `gs_web_searcher` (`creative_agent/sub_agents/trend_researcher/agent.py:91`) | `merge_planners` (`creative_agent/agent.py:52`) | UNGUARDED |
| 3 | `refined_web_search_insights` | `enhanced_combined_searcher` (`creative_agent/agent.py:160`) | `combined_report_composer` (`creative_agent/agent.py:232`) | GUARDED by `{var?}` (#69) |

**Deprecation note (out of scope, flag only):** `SequentialAgent`/`ParallelAgent`/`LoopAgent` are all `@deprecated` in this ADK in favor of `Workflow`, which "cannot yet be used as an LlmAgent sub-agent" — migration is currently blocked. The whole pipeline sits on these. `RetryUntilKeyAgent` (custom `BaseAgent`) is unaffected. Do **not** attempt a `Workflow` migration in this plan.

## Standing constraints (apply throughout)

- `export PATH="$HOME/.local/bin:$PATH"` before any `uv`/`uvx`. Use `uv`/`uvx ruff`, never bare pip/python. Lint+format with `uvx ruff`.
- **Only commit/push when the user explicitly asks.** Branch off `main` first; never commit to `main`. Do NOT commit `.python-version`; commit `uv.lock` normally. No `Co-Authored-By` trailers. PR bodies end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- Models pinned to `global` in code via `agent_common` (env `GOOGLE_CLOUD_LOCATION` stays UNSET); regional resources use `GCP_REGION=us-central1`. BigQuery dataset `trend_trawler` is the data store — preserve.
- Live deploys use the **isolated tagged no-traffic revision** harness (never touch prod traffic): `gcloud run deploy trend-trawler-api --source . --region us-central1 --project hybrid-vertex --no-traffic --tag <tag>`; auth by impersonating `tt-web-sa` with audience = the **base** service URL (see memory note for the full recipe). Plain private-service deploys are auto-mode-allowed; auth-surface/IAM changes are NOT (user runs via `!`).

---

# Workstream 1 — Productionize `RetryUntilKeyAgent` (validated #1)

The prototype code is done; this workstream promotes it into the live pipeline around the two **unguarded** producers, plus landmine #3's producer (belt-and-suspenders with its existing downstream `{var?}`).

**Wiring rule (critical):** an ADK agent can have exactly one parent. To wrap an existing producer you must **remove it from its current parent's `sub_agents` and put the wrapper there instead**, with the wrapper holding the producer as its sole `sub_agents` entry.

### Task 1.1: Confirm the prototype is green on this branch

**Files:** none (verification only).

**Step 1:** Run `export PATH="$HOME/.local/bin:$PATH"; cd /home/user/adk_pipe; uv run pytest tests/test_retry_agent.py -q`
Expected: `3 passed`. If red, stop — the prototype must be green before wiring.

**Step 2:** `uvx ruff check creative_agent/retry_agent.py tests/test_retry_agent.py` → `All checks passed!`

### Task 1.2: Wrap the two unguarded producers  *(TDD)*

**Files:**
- Modify: `creative_agent/sub_agents/campaign_researcher/agent.py` (wrap `campaign_web_searcher` inside `ca_sequential_planner`)
- Modify: `creative_agent/sub_agents/trend_researcher/agent.py` (wrap `gs_web_searcher` inside `gs_sequential_planner`)
- Test: `tests/test_pipeline_structure.py`

**Step 1: Write the failing structural test.** Add to `tests/test_pipeline_structure.py`:

```python
def test_research_producers_are_retry_wrapped():
    """The two unguarded research producers must be wrapped for retry-on-empty."""
    from creative_agent.retry_agent import RetryUntilKeyAgent
    from creative_agent.sub_agents.campaign_researcher.agent import ca_sequential_planner
    from creative_agent.sub_agents.trend_researcher.agent import gs_sequential_planner

    ca_last = ca_sequential_planner.sub_agents[-1]
    gs_last = gs_sequential_planner.sub_agents[-1]

    assert isinstance(ca_last, RetryUntilKeyAgent)
    assert ca_last.output_key == "campaign_web_search_insights"
    assert ca_last.sub_agents[0].output_key == "campaign_web_search_insights"

    assert isinstance(gs_last, RetryUntilKeyAgent)
    assert gs_last.output_key == "gs_web_search_insights"
    assert gs_last.sub_agents[0].output_key == "gs_web_search_insights"
```

**Step 2: Run it to verify it fails.**
Run: `PYTHONPATH="$PWD" uv run pytest tests/test_pipeline_structure.py::test_research_producers_are_retry_wrapped -v`
Expected: FAIL (last sub_agent is the raw `Agent`, not `RetryUntilKeyAgent`). *(Requires GCP creds — module-level `genai.Client`. If unavailable locally, this test runs in CI / on the deploy host; note it and proceed to implement.)*

**Step 3: Implement — campaign_researcher.** In `creative_agent/sub_agents/campaign_researcher/agent.py`, after `campaign_web_searcher` is defined and before `ca_sequential_planner`, add:

```python
from ...retry_agent import RetryUntilKeyAgent

# Retry-on-empty: campaign_web_searcher (google_search + thinking) intermittently
# returns no final text, leaving `campaign_web_search_insights` unset and crashing
# merge_planners' `{campaign_web_search_insights}`. Re-run until populated (bounded).
campaign_web_searcher_resilient = RetryUntilKeyAgent(
    name="campaign_web_searcher_resilient",
    sub_agents=[campaign_web_searcher],
    output_key="campaign_web_search_insights",
    max_attempts=3,
)
```

Then change `ca_sequential_planner`'s `sub_agents` from `[campaign_web_planner, campaign_web_searcher]` to `[campaign_web_planner, campaign_web_searcher_resilient]`.

**Step 4: Implement — trend_researcher.** Mirror it in `creative_agent/sub_agents/trend_researcher/agent.py`: add `gs_web_searcher_resilient` wrapping `gs_web_searcher` (`output_key="gs_web_search_insights"`), and swap it into `gs_sequential_planner.sub_agents`.

**Step 5: Run tests to verify they pass.**
Run: `PYTHONPATH="$PWD" uv run pytest tests/test_pipeline_structure.py -v`
Expected: PASS (new test + all existing structure tests still green — check none asserted the old last-sub-agent name).

**Step 6:** `uvx ruff check … && uvx ruff format …` on the two modified files. Do not commit yet.

### Task 1.3: Wrap the third producer (`enhanced_combined_searcher`)  *(TDD)*

**Files:**
- Modify: `creative_agent/agent.py` (wrap `enhanced_combined_searcher` inside `combined_research_pipeline`)
- Test: `tests/test_pipeline_structure.py`

**Step 1: Update the existing order test.** `test_combined_research_pipeline_sub_agent_order` currently asserts the 3rd entry is `enhanced_combined_searcher`. Change that entry to the wrapper name `enhanced_combined_searcher_resilient`, and add an assertion that it is a `RetryUntilKeyAgent` with `output_key == "refined_web_search_insights"`.

**Step 2: Run to verify fail.** `PYTHONPATH="$PWD" uv run pytest tests/test_pipeline_structure.py -v` → FAIL.

**Step 3: Implement.** In `creative_agent/agent.py`, import `RetryUntilKeyAgent` (`from .retry_agent import RetryUntilKeyAgent`), and after `enhanced_combined_searcher` add a `RetryUntilKeyAgent` wrapping it (`output_key="refined_web_search_insights"`, `max_attempts=3`). Replace it in `combined_research_pipeline.sub_agents`. Keep the existing `{refined_web_search_insights?}` guard in `combined_report_composer` — retry reduces the chance of degrade; the guard + `__retry_exhausted` marker cover the residual.

**Step 4: Verify.** `PYTHONPATH="$PWD" uv run pytest tests/test_pipeline_structure.py -v` → PASS. Ruff clean.

### Task 1.4: Full offline test sweep + lint gate

**Step 1:** `uv run pytest tests/test_retry_agent.py -q` → 3 passed.
**Step 2:** `PYTHONPATH="$PWD" uv run pytest tests/ -q` → all green (or, for creds-gated files, run on the deploy host / CI; record which were skipped).
**Step 3:** `uvx ruff check .` → clean.

### Task 1.5: Live isolated smoke (gate before merge)

**Step 1:** Deploy the branch to a no-traffic tagged revision (auto-mode-allowed — private service, no auth change):
`gcloud run deploy trend-trawler-api --source . --region us-central1 --project hybrid-vertex --no-traffic --tag retry-harden`

**Step 2:** Drive a full `creative_agent` run against the tag URL using the SA-impersonation + base-URL-audience recipe (memory note). Pick a campaign likely to exercise research.

**Step 3:** Verify in logs: run completes through `combined_report_composer`; if any `*_resilient` retried, its `WARNING ... left '<key>' empty ... retrying` and (on recovery) `INFO ... populated ... on attempt N` appear. No `KeyError: Context variable not found`. Record the revision + outcome.

**Step 4:** Clean up the tagged revision when done (leftover `exp-harden`/`retry-harden` no-traffic revisions).

---

# Workstream 3 — Observable guardrails (do before #2)

Cheap, and it makes the bigger #2 refactor safe to land. Ensure every consumer of a research `output_key` degrades **observably** rather than crashing, and that retry-exhaustion is surfaced.

### Task 3.1: Make `merge_planners` tolerate a missing input observably  *(TDD)*

**Files:**
- Modify: `creative_agent/agent.py` (`merge_planners`, lines ~35-68)
- Test: `tests/test_pipeline_structure.py` (or a new `tests/test_research_guardrails.py`)

**Design:** `merge_planners` reads `{campaign_web_search_insights}` and `{gs_web_search_insights}` — both required. Even with retry (#1), exhaustion can leave one unset. Two options; **prefer (a)**:
- **(a)** Make both optional in the instruction (`{campaign_web_search_insights?}` / `{gs_web_search_insights?}`) AND add an instruction clause: "If either report is empty, note the gap explicitly in the brief and synthesize from what is present." This keeps the run alive and makes the gap visible in the output.
- (b) A `before_agent_callback` that backfills a sentinel string ("_(campaign research unavailable this run)_") into any missing key. More moving parts; only if (a) underperforms.

**Step 1:** Write a test that inspects `merge_planners.instruction` for the trailing-`?` optional markers on both keys (mirror how existing tests assert instruction content, or assert substring presence). RED.
**Step 2:** Implement (a). **Step 3:** GREEN. **Step 4:** Ruff.

### Task 3.2: Surface `__retry_exhausted` in run output / eval

**Files:**
- Modify: whichever persistence/logging path is most visible — e.g. include any `*__retry_exhausted` state keys in the run's final summary log, or add them to the eval report metadata (`creative_eval`), so an exhausted producer is queryable, not just buried in logs.

**Step 1:** Decide the surfacing point (log line in a persistence tool vs. eval metadata). **Step 2:** TDD a small unit test asserting the marker is included when present. **Step 3:** Implement + GREEN + ruff.

---

# Workstream 2 — Split tool-use from synthesis (durable #2)

The root cause: each producer does `google_search` (tool use) **and** final-text synthesis in one `Agent` under a thinking budget — the model can spend its whole output budget on tool calls/thinking and never emit the summary. Splitting removes the pressure: a **tool-free** synthesizer has nothing competing for its output budget, so it reliably emits text.

Do this **last**, with #1 (retry) and #3 (guardrails) already protecting the pipeline, and land it **one producer at a time** behind the isolated-revision harness.

**Pattern (apply per producer):** replace the single searcher `Agent` with a 2-step `SequentialAgent`:
1. **Searcher** — `tools=[google_search]`, `BuiltInPlanner`, keeps `after_agent_callback=collect_research_sources_callback` (grounding capture). Its job is only to run the queries; it writes the **raw** findings to a new intermediate key, e.g. `campaign_search_raw`.
2. **Synthesizer** — **no tools**, `include_contents="none"`, reads `{campaign_search_raw}`, writes the original `output_key` (`campaign_web_search_insights`) in the required report structure. Tool-free → reliable final text.

The `RetryUntilKeyAgent` from Workstream 1 then wraps the **synthesizer** (or the whole 2-step sequence) — retry stays cheap because a tool-free re-run is fast and doesn't re-spend search calls if it wraps only the synthesizer.

### Task 2.1: Prove the split reduces empty-output — spike + decision

**Step 1:** Spike the split on **one** producer (`campaign_web_searcher` → `campaign_web_searcher` + `campaign_web_synthesizer`) on a scratch branch. Deploy to a tagged revision; run N campaigns; compare empty-`output_key` / retry-attempt rates vs. the un-split (Workstream-1-only) baseline from Task 1.5.
**Step 2:** If the split materially lowers retries, proceed to roll out to all three; if not, stop at Workstream 1 + 3 and record why.

### Task 2.2–2.4: Roll out the split per producer  *(TDD each)*

For `campaign_researcher`, `trend_researcher`, and `enhanced_combined_searcher` respectively:
- **Files:** the producer's `agent.py`; `tests/test_pipeline_structure.py`.
- **Step 1:** Test that the searcher sequence exposes the original `output_key` via the synthesizer, the searcher has no `output_schema`/writes the raw key, and the pipeline sub-agent order/names are updated. RED.
- **Step 2:** Implement the searcher/synthesizer split; re-point `RetryUntilKeyAgent` at the synthesizer; update `collect_research_sources_callback` placement (stays on the searcher). GREEN.
- **Step 3:** Ruff; then one isolated-revision smoke per producer before moving to the next.

---

## Verification (whole plan)

- **Offline gate (no creds / CI-safe):** `uv run pytest tests/test_retry_agent.py -q` (3 passed) + `PYTHONPATH="$PWD" uv run pytest tests/ -q` green (creds-gated files run on deploy host/CI); `uvx ruff check .` clean.
- **Structure:** the two unguarded producers and `enhanced_combined_searcher` are `RetryUntilKeyAgent`-wrapped with matching `output_key`s; pipeline order tests updated and green.
- **Live (isolated tagged revision, prod untouched):** a full `creative_agent` run completes through `combined_report_composer` with **no** `KeyError: Context variable not found`; retry WARN/INFO lines appear when a producer flakes; any exhaustion is visible via `__retry_exhausted`. After #2, retry-attempt rate drops vs. the Workstream-1 baseline.

## Risks / call-outs

- **One-parent constraint:** wrapping requires removing the producer from its old parent's `sub_agents`; forgetting leaves a dangling/duplicate parent — the structure tests catch this.
- **`collect_research_sources_callback` on retry:** re-runs each attempt; it dedups by URL (`url_to_short_id`) so re-collection is idempotent. In #2 it stays on the searcher (which still calls `google_search`).
- **`max_attempts` cost:** each retry is a full model turn (quota: pro 5 RPM / image 2 RPM — see memory). Keep `max_attempts=3`; retries are rare (only on the flake). #2 further lowers the rate.
- **Existing structure tests:** several assert exact sub-agent names/order — update them in lockstep or they'll fail.
- **ADK deprecation:** `SequentialAgent`/`ParallelAgent` are deprecated; this plan keeps using them (migration to `Workflow` is blocked). Track separately.

## Out of scope (deferred)

`Workflow` migration off deprecated `SequentialAgent`/`ParallelAgent`; changing the research model/thinking-budget knobs (unproven band-aid — the bounded `thinking_budget=512` on `enhanced_combined_searcher` from `exp/harden-enhanced-combined-searcher` is neither validated nor part of this plan); reworking the citation/grounding pipeline.

## Execution

Subagent-driven in this session (fresh subagent per task, review between tasks), OR a parallel session with `superpowers:executing-plans`. Live steps (1.5, 2.x smokes) use the isolated tagged-revision harness and pause for the user where auth/IAM is involved. A single PR opened when the user asks. Ordering: **Workstream 1 → 3 → 2.**
