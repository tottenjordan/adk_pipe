# Context-Bloat File Splits — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Shrink the four biggest load-bearing source files by extracting cohesive
concerns into sibling modules, so routine edits pull far fewer lines into an agent's
(or a human's) context — without changing any runtime behavior.

**Architecture:** Pure structural refactor. Move code, preserve every public name via
re-export, keep every agent object and its final `.instruction` text byte-identical.
The existing test suite is the safety net (green-stays-green); a few frontend tests are
upgraded to import the real symbols instead of their current duplicated copies.

**Tech Stack:** Python 3.13 (Google ADK, `uv`, `ruff`, `ty`, `pytest`); Next.js 16 +
TypeScript frontend (Vitest).

**Delivery:** Single combined PR, branched off `main`. Tasks are sequenced Tier 1 → 2 → 3;
run the full verification gate after each tier so a regression is localized.

---

## Context (why)

As the codebase grows, four files have become large *and* load-bearing — every
creative-pipeline or run-view change forces a 1000+-line read:

| File | Lines | Dominated by |
|---|---|---|
| `creative_agent/tools.py` | 1553 | a ~450-line inline HTML/CSS gallery template + 5 function groups |
| `frontend/src/app/run/[sessionId]/page.tsx` | 1259 | a self-contained `Review*` component cluster + a constants block |
| `creative_agent/agent.py` | 1033 | 12 inline `instruction="""…"""` literals |
| `trend_scout/agent.py` | 382 | 5 inline `instruction="""…"""` literals |

The instructions and the HTML template are pure text/constants that rarely need reading
when editing *logic* (and vice-versa). Splitting them out is the highest-yield,
lowest-risk way to cut per-edit context. Intent: same behavior, smaller working set.

## Invariants — the safety net (DO NOT break these)

Verified against tests + consumers. The refactor is safe **iff** all of these hold:

1. **Every agent object keeps its current module-level name** in `creative_agent/agent.py`
   and `trend_scout/agent.py` (tests read `root_agent`, `combined_research_pipeline`,
   `research_refinement_block`, `_base_research_is_degraded`, `enhanced_combined_searcher`,
   `refined_web_synthesizer`, `ad_creative_pipeline`, `visual_generation_pipeline`,
   `visual_production_pipeline`, `visual_generator`, `parallel_planner_agent`,
   `merge_planners`, `combined_web_evaluator`, `visual_concept_finalizer`,
   `understand_trends_searcher`, `pick_trends_agent`, `trend_scout`, …).
2. **Every `.instruction` value stays byte-identical.** Tests assert substrings
   (e.g. `merge_planners` contains `{campaign_web_search_insights?}`; trend_scout
   `root_agent` contains `{interactive_trend_pick?}` / `review_trends` and must *not*
   contain a "Do NOT call `pick_trends_agent`" line; `understand_trends_searcher` contains
   `{raw_gtrends?}` not `{raw_gtrends}`; `visual_concept_finalizer` contains `ad_copy_id`).
   All instructions are **plain literals with ADK `{state}` tokens — no f-strings, no
   `.format()`** — so they move verbatim. Copy exactly; do not reflow or re-indent.
3. **`creative_agent.tools` must still expose** (as importable names / module attributes):
   `memorize`, `build_eval_bq_row`, `_build_research_warning_banner`, `generate_image`,
   `save_creative_gallery_html`, `save_draft_report_artifact`, `save_eval_report_to_gcs`,
   `write_trends_to_bq`, `write_eval_report_to_bq`, plus whatever `test_tools_retry.py`
   exercises. Consumers: `creative_agent/agent.py` uses `tools.generate_image`
   (agent.py:888) and `tools.save_creative_gallery_html` (agent.py:1014);
   `interactive_creative/agent.py:14` does `from creative_agent import tools, callbacks`.
4. **`trend_scout.tools` must still expose** the 7 names imported by
   `trend_scout/agent.py:11-19`: `save_search_trends_to_session_state`,
   `save_session_state_to_gcs`, `record_research_gaps`, `write_trends_to_bq`,
   `get_daily_gtrends`, `write_to_file`, `memorize` — plus `_build_trend_insert_sql`
   (test_tools.py:142).
5. **Deployment is unaffected**: `deployment/deploy_agent.py` bundles whole package
   directories (`AGENT_EXTRA_PACKAGES`, deploy_agent.py:64-70), not individual files, so
   new modules inside an existing package ship automatically.
6. **Output-schema classes stay importable from `creative_agent.agent` AND keep object
   identity** (Task 1b): `test_schemas.py` does `from creative_agent.agent import AdCopy…`;
   `test_pipeline_structure.py:335-340` asserts `<agent>.output_schema == <SchemaClass>`.
   Re-exporting via `from .schemas import (…)` in `agent.py` satisfies both — the kwarg and
   the test reference the same class object.

## Constraints

- No `Co-Authored-By` / no "Generated with Claude Code" trailers. Branch off `main`
  (e.g. `refactor/context-bloat-file-splits`); do not commit to `main`; open the PR only
  when asked. Preserve BigQuery `trend_trawler` data (untouched here).
- Dispatch file-mutating execution subagents with `isolation:"worktree"`.
- **Task 0 (first execution step):** copy this plan to
  `docs/plans/2026-07-16-context-bloat-file-splits.md` (the writing-plans home) and work
  from there.

---

## Tier 1 — Extract agent instructions → `prompts.py`

Biggest, safest reduction (~40-50% off `creative_agent/agent.py`). All literals move verbatim.

### Task 1: `creative_agent` instructions → existing `prompts.py`

**Files:**
- Modify: `creative_agent/prompts.py` (currently 98 lines; holds only the orphaned
  `VEO3_INSTR` constant — unused. Keep it; append the new constants.)
- Modify: `creative_agent/agent.py` (12 instruction literals; import them back)

**Steps:**
1. For each of the 12 agents, cut the triple-quoted literal currently assigned to
   `instruction=` and paste it into `prompts.py` as an UPPER_SNAKE constant. Naming:
   `<AGENT_VAR>_INSTR` (e.g. `MERGE_PLANNERS_INSTR`, `COMBINED_REPORT_COMPOSER_INSTR`,
   `AD_COPY_DRAFTER_INSTR`, `AD_COPY_CRITIC_INSTR`, `VISUAL_CONCEPT_DRAFTER_INSTR`,
   `VISUAL_CONCEPT_CRITIC_INSTR`, `VISUAL_CONCEPT_FINALIZER_INSTR`,
   `COMBINED_WEB_EVALUATOR_INSTR`, `ENHANCED_COMBINED_SEARCHER_INSTR`,
   `REFINED_WEB_SYNTHESIZER_INSTR`, `VISUAL_GENERATOR_INSTR`, `ROOT_AGENT_INSTR`).
   Source line ranges (approx): merge_planners 40-67; combined_web_evaluator 113-153;
   enhanced_combined_searcher 175-192; refined_web_synthesizer 209-229;
   combined_report_composer 266-332; ad_copy_drafter 444-468; ad_copy_critic 539-567;
   visual_concept_drafter 635-665; visual_concept_critic 720-741;
   visual_concept_finalizer 809-840; visual_generator 883-887; root_agent 954-1006.
   **Copy the string content exactly** (including trailing newlines/indentation inside the
   quotes) — invariant #2.
2. In `agent.py`, add `from . import prompts` (or
   `from .prompts import (MERGE_PLANNERS_INSTR, …)`), and replace each inline literal with
   `instruction=prompts.MERGE_PLANNERS_INSTR` etc.
3. **Verify byte-identity** (this IS the test for the move):
   `uv run python -c "import creative_agent.agent as a; print(a.merge_planners.instruction[:40])"`
   then run `uv run pytest tests/test_pipeline_structure.py -q` — the instruction-substring
   assertions (see invariant #2) must pass unchanged.
4. `uvx ruff format creative_agent/ && uvx ruff check creative_agent/ && uvx ty check creative_agent/`
5. Commit: `refactor(creative_agent): move agent instructions to prompts.py`

### Task 1b: `creative_agent` output schemas → new `schemas.py`

Same move-verbatim + re-export shape as Task 1, applied to the 13 Pydantic models
interleaved with the agent definitions (agent.py:57–546). Mirrors the existing
`creative_eval/schemas.py` convention; only `creative_agent/agent.py` has these (trend_scout
has none, sub-agents' one-off inline schemas stay put — nil payoff).

**Files:**
- Create: `creative_agent/schemas.py` (one-line module docstring mirroring `prompts.py`)
- Modify: `creative_agent/agent.py` (cut 13 class defs; import them back)

**Steps:**
1. Move these 13 classes verbatim (with their `Field(...)` defs and docstrings), in
   dependency order so referencing classes follow their members: `SearchQuery` (57),
   `ResearchFeedback` (65), `AdCopy` (230), `AdCopyList` (257), `FinalAdCopy` (294),
   `FinalAdCopyList` (325), `VisualConcept` (376), `VisualConceptList` (396),
   `VisualConceptCritique` (430), `VisualConceptCritiqueList` (449), `VisualConceptFinal`
   (485), `VisualConceptFinalList` (522). Add the needed `from pydantic import BaseModel,
   Field` (+ `import` for any other referenced types) to `schemas.py`.
2. In `agent.py`, add `from .schemas import (SearchQuery, ResearchFeedback, AdCopy,
   AdCopyList, FinalAdCopy, FinalAdCopyList, VisualConcept, VisualConceptList,
   VisualConceptCritique, VisualConceptCritiqueList, VisualConceptFinal,
   VisualConceptFinalList)` near the top. This both supplies the names to the
   `output_schema=` kwargs AND re-exports them as `creative_agent.agent` attributes — so
   `from creative_agent.agent import AdCopyList` (test_schemas.py, test_pipeline_structure.py)
   and the identity asserts `ad_copy_drafter.output_schema == AdCopyList`
   (test_pipeline_structure.py:335–340) both keep passing (same class object). Drop the now-unused
   `from pydantic import …` line in `agent.py` iff nothing else there needs it (ruff will flag).
3. Verify: `uv run python -c "from creative_agent.agent import AdCopyList; import creative_agent.agent as a; print(a.ad_copy_drafter.output_schema is AdCopyList)"` prints `True`,
   then `uv run pytest tests/test_schemas.py tests/test_pipeline_structure.py -q`.
4. `uvx ruff format creative_agent/ && uvx ruff check creative_agent/ && uvx ty check creative_agent/`
5. Commit: `refactor(creative_agent): move output schemas to schemas.py`

### Task 2: `trend_scout` instructions → new `prompts.py`

**Files:**
- Create: `trend_scout/prompts.py` (none exists today)
- Modify: `trend_scout/agent.py` (5 instruction literals)

**Steps:** same pattern as Task 1. Constants: `GATHER_TRENDS_INSTR` (39-46),
`UNDERSTAND_TRENDS_SEARCHER_INSTR` (77-96), `UNDERSTAND_TRENDS_SYNTHESIZER_INSTR`
(121-146), `PICK_TRENDS_INSTR` (195-247), `TREND_SCOUT_INSTR` (282-341). Add a one-line
module docstring mirroring `creative_agent/prompts.py`. Import back into `agent.py`.
Verify `uv run pytest tests/test_pipeline_structure.py -q` (asserts trend_scout
instruction substrings). Ruff+ty. Commit
`refactor(trend_scout): move agent instructions to prompts.py`.

---

## Tier 2 — Split `creative_agent/tools.py` (template + function groups)

Target: `tools.py` becomes a thin re-export/orchestration surface (~300 lines). Every name
in invariant #3 must remain importable from `creative_agent.tools` — do this by moving code
into siblings and **re-exporting** from `tools.py`.

### Task 3: Extract the static HTML gallery template → `gallery_template.py`

**Files:**
- Create: `creative_agent/gallery_template.py`
- Modify: `creative_agent/tools.py` (`save_creative_gallery_html`, ~L435-1076)

**Steps:**
1. Move ONLY the **plain (non-f-string) fragments** to the new module as constants,
   verbatim: `HTML_TEMPLATE` (L473-854, ~382 lines of `<head>`/CSS), `HTML_POST_GALLERY`
   (920-928), `HTML_PRE_VS` (934-942), `HTML_POST_VS` (961-965), `HTML_PRE_AD_COPY`
   (971-980), `HTML_POST_AD_COPY` (1001-1006), `HTML_END_JAVASCRIPT` (1008-1035).
   **Leave `HTML_END_JAVASCRIPT` a plain string** — it contains literal JS `{...}` braces;
   never convert to an f-string.
2. **Do NOT move** the dynamic f-string fragments (`HTML_BODY` L856-870,
   `GALLERY_IMAGE_BLOCK` L899-917, `VISUAL_CONCEPT_BLOCK` L947-958, `AD_COPY_BLOCK`
   L985-998) — they interpolate Python locals from `tool_context.state`. They stay inside
   `save_creative_gallery_html`.
3. In `tools.py`, `from . import gallery_template as gt` and reference the moved constants
   (e.g. `gt.HTML_TEMPLATE`). The final `FINAL_HTML = (...)` concatenation (L1037) is
   unchanged except for the `gt.` prefixes.
4. Verify: `uv run pytest tests/test_tools.py -q` and a render smoke —
   `uv run python -c "import creative_agent.tools"` imports clean. Ruff+ty.
5. Commit: `refactor(creative_agent): extract static gallery HTML to gallery_template.py`

### Task 4: Split remaining functions into concern modules + re-export

**Files:**
- Create: `creative_agent/image_tools.py` — image gen: `generate_image`,
  `_generate_image_with_backoff`, `_is_retryable_genai_error`, `_IMAGE_GEN_*` constants,
  and a new lazy `_get_genai_client()` (mirror `_get_gcs_client`, invariant/pattern at
  tools.py:37-44) replacing the module-level `client = genai.Client(...)` (L50) whose only
  call site is L182. This removes the one import-time genai side effect.
- Create: `creative_agent/bq_tools.py` — `build_eval_bq_row`, `write_trends_to_bq`,
  `write_eval_report_to_bq`, `_get_bigquery_client`.
- Create: `creative_agent/gcs_tools.py` — `_download_blob`, `_save_to_gcs`,
  `_upload_blob_to_gcs`, `_get_high_res_img`, `_get_gcs_client`, plus report exports
  `save_draft_report_artifact`, `save_eval_report_to_gcs`.
- Keep in `tools.py`: `memorize`, `_build_research_warning_banner`,
  `save_creative_gallery_html` (uses gallery_template + gcs helpers), and
  **re-export everything** so the public surface is unchanged.

**Steps:**
1. Move each group to its module (functions unchanged). Watch shared helpers: gallery/
   report functions call `_get_gcs_client`/`_upload_blob_to_gcs` — import them from
   `gcs_tools` where needed to avoid circular imports (config-only deps make this safe).
2. At the bottom of `tools.py`, re-export for backward compat:
   ```python
   from .image_tools import generate_image  # noqa: F401  (public API)
   from .bq_tools import build_eval_bq_row, write_trends_to_bq, write_eval_report_to_bq  # noqa: F401
   from .gcs_tools import save_draft_report_artifact, save_eval_report_to_gcs  # noqa: F401
   ```
   (Add any private names the tests reach for, e.g. keep `_build_research_warning_banner`
   defined in `tools.py`.) Confirm invariant #3 names all still resolve.
3. Verify the import surface explicitly:
   `uv run python -c "from creative_agent.tools import memorize, build_eval_bq_row, _build_research_warning_banner, generate_image, save_creative_gallery_html; import creative_agent.agent; import interactive_creative.agent; print('ok')"`
4. `uv run pytest tests/test_tools.py tests/test_tools_retry.py tests/test_pipeline_structure.py -q`. Ruff+ty on the package.
5. Commit: `refactor(creative_agent): split tools.py into image/bq/gcs modules with re-exports`

> Note: `trend_scout/tools.py` (431 lines) is intentionally left as-is — it already uses
> lazy clients and its payoff is small (see the assessment). Not in scope.

---

## Tier 3 — Split `frontend/src/app/run/[sessionId]/page.tsx`

Extract two clean seams; the container `RunPage` stays. Bonus: the tests currently
**duplicate** the moved logic (they can't import it today) — point them at the real source.

### Task 5: Extract constants + pure helpers

**Files:**
- Create: `frontend/src/app/run/[sessionId]/run-config.ts` — the constants block
  (L33-129): `RUN_STALL_TIMEOUT_MS`, `PIPELINE_STATE_KEYS`, `FIELD_LABELS`, `FIELD_COLORS`,
  `HIDDEN_FIELDS`, `WIDGET_LAYOUTS`, `DEFAULT_LAYOUT`. `export` each.
- Create: `frontend/src/app/run/[sessionId]/run-helpers.ts` — pure helpers `widgetAccent`
  (L132), `extractItems` (L145), `parseRawGtrends` (L586). `export` each.
- Modify: `page.tsx` to import from both.

**Steps:** move + export; update `page.tsx` imports; `cd frontend && npm run build` (or
`npx tsc --noEmit`) to confirm types resolve.

### Task 6: Extract the Review component cluster

**Files:**
- Create: `frontend/src/app/run/[sessionId]/ReviewPanel.tsx` — `ReviewField` (L438),
  `ReviewResearch` (L361), `ReviewAdCopies` (L448), `ReviewVisualConcepts` (L533),
  `ReviewTrends` (L594), `ReviewPanel` (L699). `export` the ones `page.tsx` uses
  (`ReviewPanel`, and any panel referenced directly). Also `FieldCell`/`ItemCard`/
  `PipelineWidget` (L156/174/264) may move here or to a `run-widgets.tsx` sibling if the
  Review components depend on them — keep coupled components together.
- Modify: `page.tsx` to import the panels.

**Steps:** move components; thread props (they already take props — no shared closure
state); update imports; `npm run build`.

### Task 7: De-duplicate the tests against the real source

**Files:**
- Modify: `frontend/src/__tests__/parse-raw-gtrends.test.ts` — delete the copied
  `parseRawGtrends` (lines ~5-12) and `import { parseRawGtrends } from "@/app/run/[sessionId]/run-helpers"`.
- Modify: `frontend/src/__tests__/extract-items.test.ts` — same for `extractItems`.
- Modify: `frontend/src/__tests__/widget-layouts.test.ts` — delete the copied
  `WIDGET_LAYOUTS` and import it from `run-config`.
- Check `interactive-mode.test.ts` / `poll-run.test.ts` for any other duplicated symbols
  now importable; prefer importing over duplicating.

**Steps:** update imports, delete duplicates, `cd frontend && npm test` — all suites green.
Commit: `refactor(frontend): split run page into config/helpers/ReviewPanel; tests import real symbols`

---

## Convention guard (prevent regrowth)

Add one line to `CLAUDE.md` under the Agent Definition Pattern section: *"Agent
`instruction=` strings live in the package's `prompts.py`, not inline in `agent.py`."*
Include in the final commit.

---

## Verification (run after EACH tier; full gate before PR)

**Python:**
```bash
uv run python -c "import creative_agent.agent, trend_scout.agent, interactive_creative.agent; print('imports ok')"
uv run pytest tests/ -q          # requires GCP creds (module-level clients) — see CLAUDE.md
uvx ruff format --check . && uvx ruff check . && uvx ty check
```
Expected: import smoke prints `imports ok`; pytest all green (esp. `test_pipeline_structure.py`,
`test_tools.py`, `test_tools_retry.py`); ruff/ty clean.

**Frontend:**
```bash
cd frontend && npm test && npm run build
```
Expected: all Vitest suites pass (including the 3 de-duplicated tests now importing real
symbols); production build compiles.

**Behavior parity (the whole point — nothing should change at runtime):**
- `git diff` shows only moved code + import lines; no `.instruction` text altered
  (grep the diff for changed string content inside instruction literals — there should be
  none).
- Optional live smoke (only if the user wants it, gated per deploy constraints): run
  `interactive_creative` locally via `deployment/async_app.py` + frontend, confirm the
  review panels render and the gallery HTML still exports identically.

## Result

| File | Before | After (target) |
|---|---|---|
| `creative_agent/agent.py` | 1033 | ~300 (+ prompts.py / schemas.py) |
| `creative_agent/tools.py` | 1553 | ~300 (+ gallery_template/image_tools/bq_tools/gcs_tools) |
| `trend_scout/agent.py` | 382 | ~180 |
| `run/[sessionId]/page.tsx` | 1259 | ~600 (+ run-config/run-helpers/ReviewPanel) |

Same behavior, ~2400 fewer lines in the four hottest files, and prompts/templates no longer
loaded when editing logic.
