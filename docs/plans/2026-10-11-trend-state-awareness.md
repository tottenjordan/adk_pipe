# Trend State Awareness Implementation Plan (Ideas 1–4)

> **For Claude:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (or `executing-plans`) to implement this plan task-by-task. Save a copy as `docs/plans/2026-10-11-trend-state-awareness.md` in PR 1.

**Goal:** creatives become aware of a trend's current state, and one concept per slate (configurable 0–2) may reference it with phase-appropriate tone. Examples: "game tomorrow" → anticipation; "lost yesterday" → empathy; unknown or sensitive → no timely concept, which is exactly today's behaviour.

**Architecture** (`docs/research/TREND_STATE_RESEARCH.md` Ideas 1–4):
1. **Date awareness:** every run seeds `current_date` / `current_datetime` / `current_timezone`, and prompts read them. trend_scout's detection date and research context are forwarded to creative runs through BigQuery, the CRF and the UI.
2. **Situation:** a grounded, date-aware **trend situation** branch in `combined_research_pipeline`'s START fan-out produces a draft. A **deterministic validator** turns it into `trend_situation`, abstaining to `unknown` unless the dates and outcome are source-backed and inside the window. It also derives `timely_ok`, `valid_until` and a `tone_rule`.
3. **Timely slot:** a capped timely slot reuses the person-casting pattern: rules in state only when relevant, a per-concept flag plus reason, and deterministic cap enforcement. A temporal-word lexicon keeps evergreen concepts free of dated or outcome claims.
4. **Tone:** a phase-conditioned tone and safety playbook, generated in code.

**Tech Stack:** Google ADK 2.10 graph Workflows (function nodes, `RetryUntilKeyNode`, `FailSoftNode`, `google_search`), Pydantic, BigQuery, Cloud Run Functions (CRF), Next.js 16, pytest + Vitest.

---

## Context

**Why:** the user wants 1–2 creatives per slate to reflect a trend's current state (e.g. "Texas Football": lost yesterday, or the game is tomorrow).

**What `docs/research/TREND_STATE_RESEARCH.md` found:**
- **No agent knows today's date.**
- **Only the trend term reaches the creative run:** trend_scout's research and `refresh_date` are dropped by the CRF (`cloud_functions/creative_fanout/main.py:680-688`, `:342-352`) and by the frontend trend cards (`frontend/src/components/trend-cards.tsx:66-77`).
- **The brief has no state:** `TrendBridge` (`creative_agent/schemas.py:364-383`) has no state field.

**What the research recommends:**
- grounded retrieval plus a current date (F1);
- citation-backed validation with abstention in code (F2/F3);
- tone that follows the state, and silence on tragedies (F5);
- timely creatives kept a minority, held to the same craft gates (F7).

**User decisions (2026-10-11):**
- **Activation:** **on by default**, with the kill switch `TREND_STATE_ENABLED`; `MAX_TIMELY_CONCEPTS=0` also disables the slot.
- **Cap:** **default 1** timely concept (knob 0–2).
- **Timezone:** **`America/New_York` default** (`TREND_TIMEZONE`).
- **Scope:** Ideas 1–4 only. The judge gate, expiry chips and share/experiment guards (Ideas 5–6) come later, but `valid_until` is computed now.

**Code anchors** (verified):
- `creative_agent/agent.py`:
  - START fan-out `("START", (gs_sequential_planner, ca_sequential_planner, load_brand_history), research_join, research_barrier, merge_planners, refinement_gate)` at :583-611;
  - `load_brand_history` function node (writes `ctx.state[...]`, returns None) at :457-484;
  - `refinement_gate` route pattern at :487;
  - `_copy_issues` → `gate_copies` at :751-764.
- `creative_agent/sub_agents/trend_researcher/agent.py`: the `RetryUntilKeyNode` pattern (`gs_search_and_synthesize` :181, wrapped :194-199); planner, searcher and synthesizer prompts at :43-77 / :100-122 / :144-171.
- `creative_agent/callbacks.py`:
  - `collect_research_sources_callback` (:395) is used as the searcher's `after_agent_callback` and fills `state["sources"]` / `url_to_short_id`;
  - `_set_initial_states` at :53-142, with the conditional seed of `person_casting_rules` at :127-132;
  - `ensure_trend_and_product_callback` runs `enforce_person_casting` at :305-312.
- `creative_agent/concept_guard.py`: `enforce_person_casting` (:363-420), the pattern for `enforce_timely_cap`.
- `creative_agent/copy_gate.py`: `_deterministic_issues` (:386-445; claims check :432-437), `CopyIssue` (:126), `gate_copies` (:492).
- `creative_agent/claims.py`: the lexicon pattern (`_pattern` / `fold` / idiom exclusions, :23-60).
- `creative_agent/prompts.py`:
  - `{person_casting_rules?}` appended at :618 / :787 / :906 / :991 / :1075, and `interactive_creative/prompts.py:21`;
  - `build_person_casting_rules` at :726;
  - `TREND_RISKS_RULE` at :328 (used :430 / :497 / :615);
  - brief writer rule 13 at :354.
- `creative_agent/schemas.py`: `casts_person_reference` / `person_casting_reason` on `VisualConcept` :199, `VisualConceptCritique` :253, `VisualConceptFinal` :331; `FinalAdCopy` :109; `TrendBridge` :364.
- `brief_render.py` Trend fit block :94-106; `brief_check.check_brief` :212.
- trend_scout:
  - `trend_scout/prompts.py:36-61` (synthesizer, `info_gtrends` JSON `{term, category, context, cultural_angle}`);
  - `agent.py:121`, `:181` (`selected_gtrends` markdown);
  - `tools.py:238-325` (`_build_trend_insert_sql`, MERGE incl. `refresh_date`), `write_trends_to_bq` :330.
- `frontend/src/lib/initial-state.ts:42-53` (key mapping).
- interactive_creative reuses `combined_research_pipeline`, the schemas and the callbacks, so everything applies there too, plus its reviser prompt token.
- **Tests:**
  - callbacks and guards: `test_callbacks.py`, `test_concept_guard.py`, `test_copy_gate.py`, `test_claims.py`;
  - prompts: `test_visual_concept_prompts.py` (brace tokens :81, optional tokens :90, rules-from-state :125-135), `test_ad_copy_prompts.py`, `test_creative_brief_prompts.py`, `test_interactive_prompts.py`;
  - schemas and briefs: `test_schemas.py`, `test_brief_render.py`, `test_brief_check.py`;
  - trend_scout: `test_tools.py` :143-159;
  - CRF: `test_crf_logic.py` :140-160, `test_crf_sql_params.py`;
  - graph: `test_pipeline_structure.py`, `test_creative_agent_graph.py`.

## PR grouping

| PR | Branch | Ideas | Deploy |
|---|---|---|---|
| **1** | `feat/trend-date-context` | 1: date awareness + trend context plumbing | BigQuery ALTER, then engines (scout, creative, interactive), api, web, **CRF worker** |
| **2** | `feat/trend-situation` | 2: situation branch + validator + read-only display | engines (creative, interactive), api, web |
| **3** | `feat/timely-slot` | 3 + 4: timely cap, lexicon, tone playbook | engines (creative, interactive), api, web |

**Every PR:**
- **Gates:** Python `unset VIRTUAL_ENV; uv run ruff format --check . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 4`; frontend `npm run lint && npm test && npm run build`.
- **CRF changes:** the CRF CI workflow runs on PRs that touch `cloud_functions/**`.
- **Merge only after every GitHub check passes**, and confirm `MERGED` before cleanup.
- **No Co-Authored-By trailer and no AI attribution.**
- **Deploys:**
  - env-unset prefix for engine deploys, plus a smoke test;
  - pin api and web traffic, and check for in-flight runs before switching the api.
- **Prompt hygiene:** new prompt tokens are optional (`{x?}`), and runs where a feature is off get byte-identical instructions where specified.

---

## PR 1 — Date awareness + trend context plumbing (Idea 1)

### Task 1.1: Shared date helper + config
**Files:**
- Create `agent_common/clock.py`: `now_in_tz(tz_name) -> datetime` (zoneinfo; an invalid name falls back to `America/New_York` with a warning), and `date_state(tz_name, *, now=None) -> dict` returning `{current_date: "YYYY-MM-DD", current_datetime: ISO with offset, current_timezone, current_weekday}`.
- Modify `agent_common/config.py` `BaseAgentConfiguration`: `trend_timezone` (env `TREND_TIMEZONE`, default `America/New_York`).
- Add `TREND_TIMEZONE` to `deployment/deploy_agent.py` `ENV_VAR_DICT`.
- Tests: `tests/test_clock.py`, `tests/test_deploy_utils.py`.

**Step 1: Failing tests**
```python
def test_date_state_uses_timezone():
    now = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)  # 23:00 on the 11th in New York
    s = date_state("America/New_York", now=now)
    assert (
        s["current_date"] == "2026-10-11"
        and s["current_timezone"] == "America/New_York"
    )


def test_invalid_timezone_falls_back():
    assert (
        date_state("Mars/Base", now=datetime(2026, 1, 1, tzinfo=UTC))[
            "current_timezone"
        ]
        == "America/New_York"
    )
```

**Commit:** `feat(agent_common): per-run date state helper`.

### Task 1.2: Seed the date in every agent's state
**Files:**
- `agent_common/state.py` `seed_initial_state`: `setdefault` the `date_state(config.trend_timezone)` keys. They are computed per run at state init, **never at import**, because Agent Engine pickles the agent at deploy time.
- `creative_agent/callbacks.py` and `trend_scout/callbacks.py`, if they don't go through `seed_initial_state`.
- Tests: `tests/test_callbacks.py`, `tests/test_trend_scout_graph.py` (or the callbacks test).

**Tests:**
- a fresh state gets `current_date` and friends;
- an existing value is not overwritten (resume keeps the original run date);
- two runs on different days get different dates (freeze `now`).

**Commit:** `feat(state): seed current date/time/timezone per run`.

### Task 1.3: Prompts read the date
**Files:**
- `trend_scout/prompts.py`: searcher + synthesizer.
- `creative_agent/sub_agents/trend_researcher/agent.py`: planner, searcher, synthesizer.
- `creative_agent/prompts.py`: brief writer; ad copy drafter and critic; art director.
- Tests: the prompt test files above (brace tokens must be optional).

**Line to add** (brace-safe, optional tokens):
> Today is {current_date?} ({current_weekday?}, {current_timezone?}). Interpret "yesterday", "tonight", "tomorrow" and "this weekend" relative to this date, and never assume an event's timing or result without a dated source.

- **trend_scout synthesizer:** extend each JSON item with `"as_of": "<current_date>"` and optional `"event_hint": "<one factual line about the next or last notable event with its date, or empty>"` (prompt text only; no schema enforcement exists today).
- **Trend synthesizer report:** add a section "Current moment & timeline: dated recent and upcoming events with sources, or 'no notable dated event'".

**Tests:** each prompt contains the date line; the brace test stays green.

**Commit:** `feat(prompts): date-aware trend research and briefs`.

### Task 1.4: Persist trend context in BigQuery
**Files:**
- `trend_scout/tools.py` `_build_trend_insert_sql` / `write_trends_to_bq`: a new `trend_context` STRING column holding compact JSON `{context, cultural_angle, event_hint, hook}`. It is pulled from `info_gtrends` (parsed JSON) and `selected_gtrends` (the `### term` section's Hook/Context) for that term; empty on a parse failure.
- `deployment/create_bq_tables.sh` and its test; `deployment/README.md` ALTER: `ALTER TABLE …target_trends_crf ADD COLUMN IF NOT EXISTS trend_context STRING`.
- Tests: `tests/test_tools.py` (:143-159 pattern), `tests/test_create_bq_tables.py`.

**Tests:** the MERGE binds `trend_context`; the extractor handles both JSON and markdown; malformed input gives `""`.

**Commit:** `feat(trend_scout): persist per-trend context for creative runs`.

### Task 1.5: Forward detection date + context to creative runs
**Files:**
- `cloud_functions/creative_fanout/main.py`: add `refresh_date` → state `trend_detected_on`, and `trend_context` → state `trend_scout_context`, to `row_dict` (:680-688) and the `campaign_state` map (:342-352). Blanks are skipped as today.
- `creative_agent/callbacks.py`: `setdefault` both to `""`.
- Prompts: the trend planner and brief writer read `{trend_scout_context?}` and `{trend_detected_on?}` as **hints to re-verify**.
- Frontend: `trend-cards.tsx` passes `trendContext` (hook + context) and `trendDetectedOn`; `initial-state.ts` maps them to `trend_scout_context` / `trend_detected_on`; Duplicate-brief restore.
- Tests: `tests/test_crf_logic.py`, `test_crf_sql_params.py`, `tests/test_callbacks.py`, `frontend/src/__tests__/initial-state.test.ts` (+ a trend-cards test if present).

**Commit:** `feat: forward trend detection date and context to creative runs`.

### Task 1.6: Docs
Update CLAUDE.md and GEMINI.md (state keys, `TREND_TIMEZONE`, `trend_context` column) and deployment/README.md (ALTER + CRF redeploy). Commit the plan copy too.

**Deploy order:**
1. The ALTER.
2. trend_scout, creative and interactive engines, then the smoke test.
3. The CRF worker (from `cloud_functions/creative_fanout`, with no env flags so the env is preserved).
4. api, then web (pin both).

---

## PR 2 — Trend situation + validator (Idea 2, observe-only display)

### Task 2.1: Schemas + config
**Files:**
- `creative_agent/schemas.py`:
  - `TrendSituationDraft` (synthesizer `output_schema`): `phase: Literal["upcoming","live","just_happened","ongoing","evergreen","sensitive","unknown"]`, `event_name: str = ""`, `event_start: str = ""` (ISO date or datetime), `event_end: str = ""`, `outcome: Literal["win","loss","draw","other",""] = ""`, `outcome_summary: str = ""` (≤ 1 sentence), `audience_sentiment: Literal["positive","negative","mixed","neutral"] = "neutral"`, `source_ids: list[str] = []` (`src-N` ids from `state["sources"]`), `confidence: Literal["high","medium","low"] = "low"`.
  - `TrendSituation` = the validated object: the draft fields plus `as_of`, `timely_ok: bool`, `valid_until: str`, `tone_rule: str`, `abstain_reason: str`.
- `creative_agent/config.py`: `TREND_STATE_ENABLED` (default true; `parse_brand_history_enabled` style), `TREND_EVENT_WINDOW_DAYS` (7, 1–30), `TIMELY_VALID_HOURS_AFTER` (48, 6–168); `ENV_VAR_DICT`.
- Tests: `test_schemas.py`, `test_config.py` / `test_person_config.py` pattern.

**Commit:** `feat(creative_agent): trend situation schemas and knobs`.

### Task 2.2: Situation branch (grounded searcher → synthesizer)
**Files:**
- New `creative_agent/sub_agents/trend_situation/agent.py`:
  - `trend_situation_searcher`: `google_search`, `after_agent_callback=collect_research_sources_callback`, so its sources join `state["sources"]` with `src-N` ids. Output `trend_situation_raw`.
  - `trend_situation_synthesizer`: `output_schema=TrendSituationDraft`, output `trend_situation_draft`.
  - Wrapped as `Workflow("trend_situation_search", START→searcher→synthesizer)` → `RetryUntilKeyNode(output_key="trend_situation_draft", max_attempts=2)` → `FailSoftNode` (on error: `trend_situation_draft = None`, a logged warning, and the run continues).
- **Prompts** (`creative_agent/prompts.py`, brace-safe, with the date line from Task 1.3):
  - **Searcher:** "Find the trend's most recent and next notable dated events relative to today. For sports: last game result with date and score, next game date. For other trends: the dated event driving interest. Prefer sources published in the last 7 days; quote dates."
  - **Synthesizer:** "Choose `unknown` unless sources state the event date (and outcome, if any); cite only `src-N` ids present in the sources; `sensitive` for tragedies, injuries, deaths, scandals, disasters."
- **Gating:** when `TREND_STATE_ENABLED` is false, the branch is skipped. A `trend_situation_gate` function node routes `run`/`skip` (refinement-gate pattern), and both routes converge on the validator (Task 2.3).
- **Wiring:** in the START fan-out tuple (`agent.py:583-611`), add the situation branch so `research_join` waits for it too.
- Tests: `tests/test_pipeline_structure.py` (node present, inside the fan-out), `tests/test_creative_agent_graph.py` (a fake model run populates the draft; failure → None; disabled → skipped, no model call).

**Commit:** `feat(creative_agent): grounded trend situation branch`.

### Task 2.3: Deterministic validator
**Files:**
- New `creative_agent/trend_situation.py` (pure): `validate_situation(draft, *, sources, now, window_days, valid_hours_after) -> TrendSituation`, plus `tone_rule_for(phase, outcome)` (the Idea 4 table; also used by PR 3).
- The function node `validate_trend_situation(ctx)` writes `trend_situation` (a dict) and `trend_situation_md` (a one-line "Current moment: … (sources: src-N; valid until …)" or "").
- Tests: `tests/test_trend_situation.py`.

**Rules** (abstain → `phase="unknown"`, `timely_ok=False`, with `abstain_reason`):
- a missing or None draft, or disabled → unknown;
- `phase in {upcoming, just_happened}` requires a parseable event date with |date − today| ≤ window;
- `upcoming` must be in the future (or today);
- `just_happened` must be ≤ today and needs a non-empty `outcome`;
- every `source_id` must exist in `state["sources"]`, and ≥1 is required for upcoming / just_happened. When a source has a URL date (`/2026/10/11/` pattern), it must be ≥ `event_start − 1 day` for just_happened.
- `live`, `sensitive`, `evergreen`, `ongoing`, `unknown` → `timely_ok=False` (live is too volatile by default);
- `confidence == "low"` → `timely_ok=False`;
- `valid_until`: upcoming → `event_start` (end of that day in the TZ); just_happened → `event_end or event_start` + `valid_hours_after`.

**Failing tests:** a table of drafts → expected phase and `timely_ok`:
- tomorrow's game with a source → upcoming, ok;
- yesterday's loss with a source → just_happened, ok, `valid_until` = +48 h;
- an outcome with no source → unknown;
- a date 20 days out → unknown;
- an unknown `src` id → unknown;
- sensitive → not ok;
- live → not ok;
- low confidence → not ok;
- None → unknown.

**Commit:** `feat(creative_agent): deterministic trend situation validator`.

### Task 2.4: Brief sees the moment (read-only use)
**Files:**
- `creative_agent/prompts.py` brief writer: "Current moment: {trend_situation_md?}". Rule: "Use the current moment only if it is present; never state an event date or result that is not in it."
- `brief_render.py`: render the "Current moment" line in the Trend fit block when the state has it. Pass `trend_situation_md` in where `creative_brief_md` is built.
- Tests: `test_creative_brief_prompts.py`, `test_brief_render.py`.

**Commit:** `feat(brief): carry the validated current moment`.

### Task 2.5: Show it (observe)
**Files:**
- Frontend results and run pages: a muted "Current moment" line from `state.trend_situation`: the phase label ("Game tomorrow" / "Lost yesterday" / "Ongoing" / "Not timely: <abstain_reason>"), the sources count and valid until. New `frontend/src/lib/trend-situation.ts` + component.
- Tests: `trend-situation.test.ts(x)`.

**Commit:** `feat(frontend): show the current moment of the trend`.

### Task 2.6: Docs
Update CLAUDE.md and GEMINI.md (the situation branch, validator rules, state keys `trend_situation` / `trend_situation_md`, knobs) and `docs/notes/creative-quality-gates.md`. **Deploy:** engines + smoke, api, web.

---

## PR 3 — Timely slot + tone playbook (Ideas 3–4)

### Task 3.1: Config + rules builder
**Files:**
- `creative_agent/config.py`: `MAX_TIMELY_CONCEPTS` (default 1, 0–2, `_parse_bounded` pattern; `ENV_VAR_DICT`).
- `creative_agent/prompts.py`: `build_timely_rules(situation, max_timely)` (brace-free text). When `timely_ok`:
  > At most N of the 4 concepts (and their paired ad copies) may reference the current moment: <phase line from situation, e.g. "Texas plays Oklahoma tomorrow (Oct 12)" or "Texas lost to Oklahoma yesterday (Oct 10)">. Mark them uses_trend_state and give timely_reason. Tone: <tone_rule>. Speak from the target audience's side; if the audience may include both sides, use neutral "big game" framing. Never use team or league logos, wordmarks, mascots, player names or likenesses, and never imply sponsorship or endorsement. Every other concept stays evergreen: no dates, scores, results or "tonight/tomorrow/yesterday".
- **`tone_rule_for`** (in `trend_situation.py`):
  - upcoming → anticipation, rituals, countdown energy, no score predictions;
  - win → celebration and pride, no gloating at rivals;
  - loss → empathy and loyalty, comfort, no urgency/discount pushes, no jokes at the team's expense, no "lose/lost" in the headline;
  - other → acknowledge the moment factually.
- **Seeding:** `validate_trend_situation` sets `timely_rules = build_timely_rules(...)` when `timely_ok` and `MAX_TIMELY_CONCEPTS > 0`, else `""`. `callbacks._set_initial_states` `setdefault`s `timely_rules=""`.
- **Prompts:** `{timely_rules?}` is appended at the end of the last instruction line wherever `{person_casting_rules?}` is: art director, drafter, critic, finalizer, fixer, interactive reviser. Also the ad copy drafter and critic, plus the ad copy reviser.
- Tests:
  - `test_visual_concept_prompts.py` and `test_ad_copy_prompts.py`: the rules come only from state (copy `test_casting_rules_come_only_from_state`), and a run with `timely_rules=""` gets byte-identical instructions to main;
  - `tests/test_trend_situation.py`: the tone table, and the rules text per phase (incl. the loss wording).

**Commit:** `feat(creative_agent): timely rules and tone playbook from the validated moment`.

### Task 3.2: Schemas — per-concept flag
**Files:**
- `creative_agent/schemas.py`: `uses_trend_state: bool = False` + `timely_reason: str = ""` on `AdCopy` (drafter), `FinalAdCopy`, `VisualConcept`, `VisualConceptCritique` and `VisualConceptFinal`, next to the casting fields.
- `TrendBridge.timely_angle: str = ""` (the allowed reference, from the brief writer only when the moment is timely).
- Tests: `test_schemas.py` (old payloads parse).

**Commit:** `feat(schemas): timely flag on copies and concepts`.

### Task 3.3: Temporal lexicon
**Files:**
- New `creative_agent/temporal_terms.py`, mirroring `claims.py`: `temporal_terms(text) -> list[str]`.
- **Terms** (whole-word, fold-insensitive): won, win, wins, victory, lost, loss, lose, defeat, beat, beaten, upset, comeback, score, final score, tonight, tomorrow, yesterday, this weekend, game day, gameday, kickoff, rematch, playoffs, championship, plus explicit dates (month-day patterns, weekday names).
- **Idiom exclusions:** "win-win", "lose weight", "lost and found", "never miss a beat", "skin in the game"; extend the list as false positives appear in tests.
- Tests: `tests/test_temporal_terms.py`, with idioms not flagged.

**Commit:** `feat(creative_agent): temporal/outcome term lexicon`.

### Task 3.4: Deterministic enforcement
**Files:**
- `creative_agent/concept_guard.py`: `enforce_timely_cap(concepts, *, timely_ok: bool, max_timely: int) -> (list, warnings)`, mirroring `enforce_person_casting`:
  - clear flags beyond the cap (keep the first N in concept order) with a "(Not timely: cap reached.)" reason suffix;
  - clear all flags when `timely_ok` is false ("no validated current moment").
- `concept_guard.concept_issues`: a new issue for an **evergreen** concept (`uses_trend_state` false) whose `image_generation_prompt` or headline contains `temporal_terms` ("evergreen concept mentions a dated or outcome term (…): remove it or make this the timely concept"). Also: a timely concept whose paired copy (`ad_copy_id`) isn't timely → an issue.
- `creative_agent/copy_gate.py`:
  - `_deterministic_issues` gains `timely_ok` / `max_timely` kwargs, threaded via `gate_copies` and `agent._copy_issues`;
  - an evergreen copy with temporal terms → a deterministic issue;
  - more timely copies than the cap, or any timely copy when not `timely_ok` → flags cleared in a post-step (`enforce_timely_copy_cap`) plus a warning.
- `creative_agent/callbacks.py` `ensure_trend_and_product_callback`: run `enforce_timely_cap` right after `enforce_person_casting`. The interactive reviser uses this callback too.
- Tests: `test_concept_guard.py`, `test_copy_gate.py`:
  - over-cap cleared;
  - not ok → all cleared;
  - an evergreen "won tonight" flagged;
  - a timely concept with temporal terms allowed;
  - idioms pass;
  - the pairing check;
  - existing tests unchanged when there's no timely state.

**Commit:** `feat(creative_agent): enforce the timely cap and keep evergreen concepts timeless`.

### Task 3.5: Brief rule + check
**Files:**
- `creative_agent/prompts.py` brief writer: "If the current moment is present and timely, set trend_bridge.timely_angle to one line describing how a timely concept may reference it (tone per the playbook); otherwise leave it empty." Extend rule 13 so that sensitive moments are never referenced.
- `creative_agent/brief_check.py`:
  - when `trend_situation.timely_ok` is false and `timely_angle` is set, issue "timely angle without a validated current moment: clear it";
  - when `timely_angle` contains a temporal term that contradicts the situation (e.g. "won" while `outcome == "loss"`), issue it (simple keyword check).
  - Pass `trend_situation` into `check_brief` via `agent.py`'s `brief_gate`.
- `brief_render.py`: render `timely_angle`.
- Tests: `test_brief_check.py`, `test_brief_render.py`, `test_creative_brief_prompts.py`.

**Commit:** `feat(brief): timely angle tied to the validated moment`.

### Task 3.6: Show timely creatives
**Files:**
- Frontend proof detail: a "Timely: <timely_reason> · valid until …" row when the concept has `uses_trend_state` (from `final_visual_concepts` and `trend_situation.valid_until`).
- The HTML gallery (`creative_agent/tools.py` gallery body) shows a small "Timely" label.
- Tests: a frontend component test; a gallery HTML test.

**Commit:** `feat: label timely creatives`.

### Task 3.7: Docs
Update:
- CLAUDE.md, GEMINI.md: the timely slot, lexicon, tone playbook, knobs, state keys `timely_rules`, flags;
- `docs/notes/creative-quality-gates.md`;
- the `docs/research/TREND_STATE_RESEARCH.md` status line ("Ideas 1–4 implemented in #…").

**Deploy:** engines + smoke, api, web.

---

## Verification
1. **Unit:** each task's tests plus both full suites and the CRF CI.
2. **Prompt hygiene:** with `TREND_STATE_ENABLED=false` (or no validated moment), the visual and copy agent instructions are byte-identical to main, apart from the date line added in PR 1.
3. **Live, after PR 2** (observe): pick 3 live trends, one each with a game or event in the next few days, a result in the last 2 days, and no dated event. Run creative runs and check that "Current moment" on the results page matches reality, with sources, and that unclear cases show "Not timely: <reason>". Target: no wrong outcome; unknown when unsure.
4. **Live, after PR 3:**
   - **An upcoming-event trend:** at most 1 concept is marked Timely, with an anticipation tone and no score prediction, while the other 3 contain no temporal terms.
   - **A just-happened loss:** the timely concept is empathetic, with no discount or urgency language.
   - **Sensitive or unknown:** no timely concept at all.
   - **Kill switch:** redeploying the engines with `MAX_TIMELY_CONCEPTS=0` gives no timely concepts.
5. **Batch:** trigger the CRF for 1 row. The worker's session state contains `trend_detected_on` and `trend_scout_context`, and the run re-derives the situation at run time.
6. **Regression:** the smoke test passes 5/5 after each engine deploy, and the nightly eval gate stays green. That gate checks the root tool trajectory and the efficiency baselines; the situation branch adds 2 model calls and about +1 search, so refresh `docs/baselines/eval_efficiency.json` in PR 2 if call counts exceed the tolerance.
