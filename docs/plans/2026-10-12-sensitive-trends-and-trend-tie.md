# Sensitive Trends + Deterministic Trend-Tie Check Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `subagent-driven-development` to implement. PRs 1–4 are built **in parallel** (one implementer per PR, in its own worktree); PR 5 follows once they merge. The plan copy goes in PR 5 as `docs/plans/2026-10-12-sensitive-trends-and-trend-tie.md`.

**Status:** Complete 2026-10-12: #341 (PR 1, trend_scout), #338 (PR 2, CRF), #340 (PR 3, frontend), #339 (PR 4, trend tie), docs in PR 5. Deviation: the CRF filter uses plain `JSON_VALUE` (BigQuery rejects `SAFE.JSON_VALUE`).

**Goal:**
- Flag trends about a death, tragedy, disaster or serious illness as **sensitive** from trend_scout through to the creative run. Never auto-pick them, warn wherever a human picks or launches one, and keep batch runs off them.
- Make the pre-judge copy check catch copies with no clear trend tie **deterministically**, before the judge's `trend_connected` gate fails them.

**Architecture:**
- **Sensitive trends:**
  - A cheap structured classifier runs on the raw trend terms right after gathering and writes `trend_flags`.
  - Auto-pick and the save tool refuse flagged terms.
  - The flag travels inside the existing `trend_context` JSON (no BigQuery schema change), where the frontend and the batch orchestrator read it.
- **Trend tie:** a new deterministic check in the copy gate matches each copy against the trend's distinctive words, the full trend phrase and the brief's motifs, and routes misses to the existing bounded copy revision.

**Tech Stack:** Google ADK 2.10, Pydantic, BigQuery JSON functions, Cloud Run Functions, Next.js 16 + Vitest, pytest.

---

## Context

**What prompted this:**
- A creative run on "rande gerber cindy crawford beach" (trend context: the couple's beach walk after their son's death) failed all 4 copies on the judge's new blocking `trend_connected` gate (#337).
- When the trend *is* a tragedy, "clearly connected" and "never reference the sensitive moment" can't both be met, so the fix belongs upstream: flag, don't auto-pick, warn.
- The copy critic self-reported its `trend_bridge` check as passed for all 4 copies, so the pre-judge half of #337 never fired. A deterministic check is needed.

**User decisions (2026-10-12):**
- Sensitive trends stay **selectable with a warning** in interactive pick; auto mode never picks them.
- Batch runs **skip** them.
- The judge stays strict.

**Key facts from exploration:**
- **The interactive pick runs before research.**
  - `ReviewTrends` (`frontend/src/app/run/[sessionId]/ReviewPanel.tsx:347-466`) lists only `state.raw_gtrends` (`run-helpers.ts:111` `parseRawGtrends`).
  - So the classifier must run right after `gather_trends_agent`.
- **Auto pick's brand-safety rule** (`trend_scout/prompts.py:122-127`) is prompt-only and omits serious illness.
- **`trend_context`** (`trend_scout/trend_context.py:112-134` `trend_context_for`) is stored in `target_trends_crf.trend_context`.
  - The CRF forwards it as `trend_scout_context`, the frontend mirrors it in `trendContextJson` (`components/trend-cards.tsx:118-128`), and `initial-state.ts` seeds it untouched.
  - New keys need no BigQuery ALTER.
- **CRF select:** `_build_select_unprocessed_sql` (`cloud_functions/creative_fanout/main.py:219-231`).
- **Copy gate:** `creative_agent/copy_gate.py` `_deterministic_issues` (:431-506).
  - It scans `("headline","body_text","social_caption","call_to_action")`.
  - **Append new checks last**, because tests index `issues["1"][0]`.
  - Deterministic issues are residual warnings; misses route to the reviser via `copy_gate_decision` (`agent.py:848`).
- **Matching:** `text_match.mentions` (0.6 ratio) misses "Lions" for "lions vs cardinals". So the rule is: any distinctive trend token, OR the full phrase, OR any brief motif (`CreativeBrief.trend_bridge.motifs`, `schemas.py:404-427`).
- **Sensitive moment in a creative run:** `state["trend_situation"]["phase"] == "sensitive"`.

**Constraints (every PR):**
- Worktree with `.python-version` copied (3.13); `unset VIRTUAL_ENV`.
- Gates: `uv run ruff format --check . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 8`; frontend `npm run lint && npm test && npm run build`; CRF CI runs on `cloud_functions/**`.
- TDD; one commit per task; brace-free prompts (`{x?}` for new tokens); no Co-Authored-By or AI attribution; merge only after CI passes.
- **Each PR leaves `CLAUDE.md`, `docs/notes/*` and `tests/README.md` untouched.** All docs land in PR 5, which removes the only cross-PR conflict.

---

## Shared contract (fixed now so PRs 1–4 can run in parallel)

1. **Session state `trend_flags`** (trend_scout, written by the classifier's `output_key`):
   ```json
   {"flags": [{"term": "<raw term, verbatim>", "sensitive": true, "reason": "<short neutral reason>"}]}
   ```
   - May arrive as a JSON string (ADK output_key), so readers must accept a dict or a JSON string, possibly fenced (like `info_gtrends`).
   - Terms are matched case- and space-insensitively (normalised like `trend_context._norm_term`: lowercase, collapsed whitespace).
2. **`trend_context` JSON** (BigQuery `target_trends_crf.trend_context`, state `trend_scout_context`) gains two keys, **present only when sensitive**:
   ```json
   {"context": "...", "cultural_angle": "...", "event_hint": "...", "hook": "...", "sensitive": true, "sensitive_reason": "..."}
   ```
   Non-sensitive JSON stays byte-identical to today's.
3. **Creative-run sensitivity:** readers use `trend_situation.phase === "sensitive"`, which already exists.

---

## The four parallel PRs (disjoint files)

| PR | Branch | Scope | Files (exclusive) |
|---|---|---|---|
| **1** | `feat/scout-sensitive-flags` | Classifier, auto-pick/save guard, flag in `trend_context` | `trend_scout/**`, `tests/test_trend_scout_sensitive.py` (new), `tests/test_trend_context.py`, `tests/test_tools.py` (trend_scout parts), trend_scout sections of `tests/test_pipeline_structure.py` |
| **2** | `feat/crf-skip-sensitive` | Orchestrator never dispatches sensitive rows | `cloud_functions/creative_fanout/**`, `tests/test_crf_*.py` |
| **3** | `feat/frontend-sensitive-warnings` | Pick-panel chip + notice, trend cards, campaign form, Current-moment line | `frontend/**` |
| **4** | `feat/copy-trend-tie` | Trend-tie matcher + copy gate + reviser rule | `creative_agent/trend_tie.py` (new), `creative_agent/copy_gate.py`, `creative_agent/agent.py`, `creative_agent/prompts.py` (reviser only), `tests/test_trend_tie.py` (new), `tests/test_copy_gate.py`, `tests/test_creative_agent_graph.py`, copy sections of `tests/test_pipeline_structure.py`, `tests/test_timely_rules.py` |

The only shared file is `tests/test_pipeline_structure.py`, and PR 1 and PR 4 touch different sections (trend_scout tests ~:1140-1200 vs copy-gate fixtures ~:1922-2170), so expect at most a trivial rebase.

### PR 1 — trend_scout flags

**Task 1.1: Classifier schema + agent**
- **Failing tests** (`tests/test_trend_scout_sensitive.py`):
  ```python
  from trend_scout.schemas import TrendFlags


  def test_trend_flags_schema():
      f = TrendFlags.model_validate(
          {
              "flags": [
                  {
                      "term": "x",
                      "sensitive": True,
                      "reason": "death of a public figure's child",
                  }
              ]
          }
      )
      assert f.flags[0].sensitive is True


  def test_root_has_classify_tool():
      from trend_scout.agent import root_agent

      assert "classify_trends_agent" in {getattr(t, "name", "") for t in root_agent.tools}
  ```
- **Implement:**
  - Schemas `TrendFlag(term, sensitive=False, reason="")` and `TrendFlags(flags=[])`, in `trend_scout/schemas.py` (new).
  - `classify_trends_agent`: the gather model, `include_contents="none"`, `output_schema=TrendFlags`, `output_key="trend_flags"`, the siblings' retry, rate-limit and scrub callbacks.
  - `CLASSIFY_TRENDS_INSTR` reads `{raw_gtrends?}`. A term is sensitive only when interest centres on a death, tragedy, disaster, mass-casualty event, violence/crime, serious illness or injury of a real person, or a similar harmful event. Celebrity, sports, politics or entertainment news alone isn't sensitive. One entry per raw term, verbatim; a short neutral reason.
  - Root gets `AgentTool(classify_trends_agent)`.
- Commit `feat(trend_scout): sensitive-trend classifier on the raw trend list`.

**Task 1.2: Root flow + auto-pick + save guard**
- **Failing tests:**
  - TREND_SCOUT_INSTR calls `classify_trends_agent` right after `gather_trends_agent` in both branches.
  - PICK_TRENDS_INSTR reads `{trend_flags?}`, says "Never select a term flagged sensitive", and adds serious illness to the brand-safety list.
  - `save_search_trends_to_session_state(term)` refuses (returns a message, saves nothing) when the term is flagged sensitive **and** `interactive_trend_pick` is False; interactive picks save normally.
- Implement, including the `<AVAILABLE_TOOLS>` list if present.
- Commit `feat(trend_scout): never auto-pick sensitive trends`.

**Task 1.3: Flag in `trend_context`**
- **Failing tests:** `trend_context_for(..., flags=)` adds `sensitive` / `sensitive_reason` per the contract (reason capped at `MAX_FIELD_CHARS`); unflagged output is byte-identical; `write_trends_to_bq` passes `state.get("trend_flags")`.
- Implement. `_clean` drops non-strings, so handle the boolean separately.
- Commit `feat(trend_scout): carry the sensitive flag in trend_context`.

### PR 2 — CRF skips sensitive rows

**Task 2.1**
- **Failing test** (`tests/test_crf_sql_params.py`): the select SQL contains `COALESCE(SAFE.JSON_VALUE(trend_context, '$.sensitive'), 'false') != 'true'`. Existing params are unchanged; `build_row_dict` is unaffected.
- **Implement** in `_build_select_unprocessed_sql`. The column exists since the 2026-10-11 migration on both datasets. Add a debug log of the filter; no extra query.
- Commit `feat(crf): never dispatch trends flagged sensitive`.
- Verify the CRF CI venv run passes.

### PR 3 — Frontend warnings

**Task 3.1: Shared parsing helpers**
- `lib/trend-flags.ts` (new): `parseTrendFlags(state.trend_flags) → Map<normTerm, reason>` (dict, JSON string or fenced) and `contextSensitivity(trendContextJson) → {sensitive, reason}`.
- Tests: `__tests__/trend-flags.test.ts`.
- Commit.

**Task 3.2: Pick panel**
- `ReviewTrends` shows a "Sensitive" chip on flagged candidates (eval-checks chip pattern with `mark-pending` amber; reason via `title` + `aria-describedby`). Candidates stay selectable.
- Selecting one shows an inline `role="note"`: "This trend is about a sensitive event (<reason>). Creatives will never reference the event, so they may connect only loosely; many brands avoid trends like this."
- No flags → unchanged markup.
- Tests: new `__tests__/review-trends.test.tsx` (modelled on `review-brief.test.tsx`).
- Commit.

**Task 3.3: Trend cards + campaign form + Current-moment line**
- `trend-cards.tsx`: `trendContextJson` copies `sensitive` / `sensitive_reason` from `state.trend_flags` (matched like `researchItem`), and the card shows the chip next to "Click to run".
- `app/page.tsx`: when the prefilled `trendContext` is sensitive, show an amber `role="alert"` notice under the prefill line (`deploy-panel.tsx:331-334` style). Clear it when the trend field is edited.
- `trend-situation-line.tsx` + `lib/trend-situation.ts`: the chip prefix when `phase === "sensitive"`.
- Tests: `parse-trends.test.ts`, a new `campaign-form-sensitive.test.tsx`, `trend-situation.test.tsx`.
- Commit.

### PR 4 — Deterministic trend tie

**Task 4.1: Matcher** (`creative_agent/trend_tie.py`, `tests/test_trend_tie.py`)
- **Failing tests:**
  ```python
  from creative_agent.trend_tie import has_trend_tie, tie_terms


  def test_named_token_counts():
      assert has_trend_tie("Lions fans, rejoice", trend="lions vs cardinals", motifs=[])


  def test_generic_tokens_alone_dont():
      assert not has_trend_tie(
          "Live results and today's score", trend="chicago marathon results", motifs=[]
      )


  def test_one_distinctive_token_is_enough():
      assert has_trend_tie(
          "Your marathon, your results", trend="chicago marathon results", motifs=[]
      )


  def test_full_phrase():
      assert has_trend_tie(
          "Chicago Marathon finishers", trend="chicago marathon results", motifs=[]
      )


  def test_motif_counts():
      assert has_trend_tie(
          "Earned at the finish line",
          trend="chicago marathon results",
          motifs=["finish line foil blanket"],
      )


  def test_no_tie():
      assert not has_trend_tie(
          "Skincare for guys who hate skincare",
          trend="lions vs cardinals",
          motifs=["stadium lights"],
      )


  def test_avoid_terms_never_count():
      assert not has_trend_tie(
          "Shooting stars", trend="shooting", motifs=[], avoid=["shooting"]
      )


  def test_empty_trend_passes():
      assert has_trend_tie("anything", trend="", motifs=[])
  ```
- **Rule:**
  - `GENERIC_TREND_TOKENS` = vs, versus, results, result, score, scores, live, news, update(s), highlights, today, tonight, game(s), match, where, watch, stream, near, me, year tokens, plus `text_match.STOPWORDS`.
  - **Distinctive** = the trend's `content_tokens` minus generic, ≥ 3 characters.
  - **A tie** = any distinctive token (`same_word` over `words(text)`), OR `mentions(text, trend)`, OR any motif `mentions(text, motif)`.
  - Avoid-list terms never count; an empty trend passes.
  - `tie_terms` = up to 3 distinctive tokens (Title-cased) plus up to 2 motifs, for the issue text.
- Commit `feat(creative_agent): trend-tie matcher for ad copy`.

**Task 4.2: Copy-gate wiring + reviser**
- `copy_gate._deterministic_issues` and `gate_copies` get keyword args `trend_terms=""`, `motifs=()`, `trend_sensitive=False`, defaulted so existing tests are unaffected. A miss **appends last**: "not clearly tied to the trend: name it or one of its motifs (e.g. Lions, Cardinals, stadium lights)".
- `agent._copy_issues` passes `trend_terms` (`target_search_trends`), `motifs` (a new `brief_motifs(brief)` helper reading `trend_bridge.motifs`) and `trend_sensitive` (`trend_situation.phase == "sensitive"`); the check is **skipped** when sensitive. Fix the stale comment at `agent.py:752-754`.
- `AD_COPY_REVISER_INSTR` rule 2: "for a trend-tie issue, name the trend or one of the listed motifs naturally". Recompute its sha pin in `tests/test_timely_rules.py`.
- **Tests:**
  - New gate tests: a miss puts the issue last; sensitive → none; empty trend → none; avoid-term ties don't count.
  - **Fixture updates:** `tests/test_creative_agent_graph.py` `_final_ad` (:919-944, add "roadrunner" to a scanned field; tests at :989/:1003/:1048/:1068/:1078); `tests/test_pipeline_structure.py` copy fixtures (:1922-1947, :2128); interactive resume tests whose copies lack the trend.
- Commit `feat(creative_agent): copy gate requires a trend tie (sensitive moments exempt)`.

---

## PR 5 — Docs (after PRs 1–4 merge; small, sequential)
- CLAUDE.md:
  - trend_scout composition (`classify_trends_agent`, `trend_flags`);
  - trend_context keys;
  - CRF filter;
  - Pages (pick panel and form warnings);
  - copy_gate list (trend tie).
- `docs/notes/creative-quality-gates.md`: the sensitive-trends policy and trend-tie rule.
- `tests/README.md`.
- The plan copy.

---

## Execution and merge order
1. **In parallel:** dispatch 4 implementers (PRs 1–4), each in its own worktree with `.python-version` copied. Each opens its PR without merging. Review each PR (spec, then quality) as it lands.
2. **Merge as CI passes**, in any order. Suggested: **4 → 2 → 1 → 3**. Rebase each on main before merging and re-run CI; only PR 1 vs PR 4 may need a trivial rebase in `tests/test_pipeline_structure.py`.
3. **PR 5** (docs), then merge.
4. **One deploy round:**
   1. trend_scout, creative and interactive engines, then the creative smoke test.
   2. CRF orchestrator (from `cloud_functions/creative_fanout`, no env flags, so the env is preserved).
   3. api + web, pinned only when no run started in the last 15 minutes.

   Delete the superseded engines after a clean smoke test, then update the runbook memory.

## Verification
1. **Unit:** each PR's tests plus its full gates; the CRF CI venv.
2. **Trend tie on today's evidence:** feed the 4 "rande gerber…" copies and an NFL copy set through `gate_copies`.
   - The beach copies are exempt when the moment is sensitive.
   - Without the flag they're flagged as no tie.
   - "Lions fans…" passes.
3. **Live:**
   - **trend_scout interactive:** tragedy terms show the Sensitive chip (today's list had one); `trend_flags` is in state.
   - **trend_scout auto:** never saves a flagged term.
   - **A human-picked sensitive trend:** `trend_context` carries `sensitive`; the campaign form shows the amber notice; the run's Current-moment line shows the chip.
   - **An NFL creative run:** untied copies get revised before the judge, and the judge's `trend_connected` passes more often.
   - **CRF:** a dry-run of the select SQL excludes a row flagged sensitive.
4. **Regression:** creative smoke 5/5. trend_scout gains a root tool: check the eval `tool_use_check` and refresh the nightly baseline deliberately if needed.
