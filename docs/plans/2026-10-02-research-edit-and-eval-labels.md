# Research-Edit Checkpoint + Readable Eval Dimensions Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `subagent-driven-development` to implement this plan task-by-task.
> On approval, the first commit copies this file to `docs/plans/2026-10-02-research-edit-and-eval-labels.md`.

**Goal:** Close two follow-ups from the frontend redesign.
1. An edited research report at interactive checkpoint 1 should actually change what the rest of the run sees, including the research PDF.
2. BigQuery `creative_evals` rows should carry human-readable weakest-dimension names alongside the snake_case keys.

**Architecture:**
- **Part A (research edit):** mirror the working checkpoint-3 path.
  - The frontend sends the edited report in the resume `edits` field.
  - `runserver.start_resume` branches on `function_name` and writes a `state_delta` event (`combined_final_cited_report` and a re-rendered `final_report_with_citations`) before relaunching.
  - The root prompt tells the agent to call `save_draft_report_artifact` again when `report_edited` is true.
- **Part B (labels):** a Python twin of the frontend label map lives in `creative_eval/dimensions.py`.
  - A drift-guard test parses `frontend/src/lib/eval-dimensions.ts`.
  - `build_eval_bq_row` adds a `weakest_dimension_labels` STRING column.
  - ALTERs on the prod and eval datasets run before deploy, and a one-time UPDATE backfills old rows.

**Tech Stack:** Python 3.13 + uv + pytest + ruff + ty; ADK 2.10 (`Event`/`EventActions` state_delta); FastAPI runserver; Next.js 16 + Vitest; BigQuery DML.

**User decisions (2026-10-02):** a new column **plus backfill**; roll out to the **api + creative engine** (the interactive engine stays dormant).

---

## Context (why)
- **Part A:** in `ReviewPanel.tsx` `ReviewResearch` (lines ~58-140), `editedReport` lives only in React state. Neither the Approve nor the Request changes payload includes it, and Preview renders the original report.
  - The backend `start_resume` (`runserver/async_runs.py:618-621`) applies any `edits` as **visual-concept** edits regardless of `function_name`.
  - Downstream agents read `{combined_final_cited_report?}`: the ad copy drafter and critic, the art director and the visual drafter (`creative_agent/prompts.py:267,315,368,460`).
  - The PDF (`save_draft_report_artifact`, `creative_agent/gcs_tools.py:166-225`) is saved before checkpoint 1 from `final_report_with_citations`.
- **Part B:** `creative_agent/bq_tools.py:53` writes `"weakest_dimensions": ",".join(weakest)`, e.g. `trend_visual_connection,copy_quality`.
  - Nothing reads that column back, so a new column is purely additive.
  - The precedent is the `research_gaps` column (an ALTER before deploy; `deployment/README.md:240-252`).

## Execution strategy (fast path; minimizes wall-clock time)
The tasks below are the spec. They run as **two parallel tracks**, each with one implementer, rather than one subagent per task.

```
t=0   ┌─ Track A implementer (worktree, A1→A5 sequential, TDD, ~20-25 min)
      ├─ Track B implementer (worktree, B1→B3 sequential, TDD, ~12-15 min)
      └─ Me: R1 step 1 ALTERs now (additive, safe before code) and prep the deploy commands
B done → my review (single combined pass) → PR/CI → merge
       → kick off in background: creative engine v9 deploy + smoke (~20 min, only needs Part B)
       → backfill dry-run → show count → --execute
A done → my review → PR/CI → merge
       → ONE api deploy (both parts, one revision change) ∥ web deploy (concurrent)
       → ONE live interactive run (you): checks Part A edit + Part B labels together,
         while the engine smoke finishes in the background
```

**Speed-ups compared with a sequential run:**
- Parts A and B are built in parallel.
- One implementer per part, with a single combined spec+quality review per PR instead of two review stages per task.
- The ALTERs run up front, since they're additive.
- The engine deploy starts as soon as Part B merges and overlaps everything after it.
- The api, web and engine deploys run concurrently.
- One api revision change (so in-flight runs are interrupted only once).
- One live run verifies both parts.

**Estimate:** about 40-50 minutes end to end, versus about 90 sequentially. The live run itself takes about 10 minutes of that.

## Conventions (restate in every subagent dispatch)
- Two branches/PRs: `feat/research-edit-checkpoint` (Part A) and `feat/eval-dimension-labels` (Part B). They are independent and can be built in parallel; Part B lands first because of the migration ordering. Squash-merge after green CI.
- **Never add `Co-Authored-By` trailers or any AI attribution to commits or PRs.** Don't stage the untracked repo-root `.agents/`, `.claude/` or `skills-lock.json`.
- Python loop: `GOOGLE_CLOUD_PROJECT=test-project uv run ruff format --check . && uv run ruff check . && uv run ty check && uv run pytest tests/ -q -n 4`.
- Frontend loop: `cd frontend && npm run lint && npm test && npm run build`. Revert `frontend/AGENTS.md` if the dev server rewrote it.
- TDD: failing test first, one commit per task. No prod BigQuery writes outside the gated rollout task.

---

## Part A: research edit at checkpoint 1 (`feat/research-edit-checkpoint`)

### Task A1: Shared citation renderer
**Files:** Create `creative_agent/citations.py`. Modify `creative_agent/callbacks.py:200-235`. Test `tests/test_citations.py`.

1. Write a failing test, `tests/test_citations.py`:
```python
from creative_agent.citations import render_citations

SOURCES = {"src-1": {"title": "Vogue", "url": "https://v.example"}}


def test_replaces_known_tag_with_markdown_link():
    out = render_citations('Glow up <cite source="src-1"/>.', SOURCES)
    assert out == "Glow up [Vogue](https://v.example)."


def test_drops_unknown_tag_and_fixes_punctuation_spacing():
    assert render_citations('Hi <cite source="src-9"/> .', SOURCES) == "Hi."


def test_falls_back_to_domain_then_id():
    s = {"src-2": {"domain": "d.example", "url": "u"}}
    assert render_citations('x <cite source="src-2"/>', s) == "x [d.example](u)"


def test_braces_and_empty_input_safe():
    assert render_citations("{not_a_key}", {}) == "{not_a_key}"
    assert render_citations("", SOURCES) == ""
```
2. Run `uv run pytest tests/test_citations.py -v`. Expect a FAIL with ModuleNotFoundError.
3. Implement `creative_agent/citations.py`: move `tag_replacer`, the cite regex and the punctuation regex out of `citation_replacement_callback` verbatim, as `render_citations(report: str, sources: dict) -> str`. Keep the `logging.warning` for invalid tags.
4. In `callbacks.py`, have `citation_replacement_callback` call `render_citations(final_report, sources)`. Its behaviour must not change; the existing callback tests in `tests/` stay green.
5. Run the full Python loop, then commit: `refactor(creative_agent): extract render_citations for reuse`.

### Task A2: Pure `merge_research_edit`
**Files:** Modify `runserver/async_runs.py`, next to `merge_visual_concept_edits` (~line 477). Test: add to `tests/test_async_runs.py`.

1. Write failing tests:
```python
from runserver.async_runs import merge_research_edit, RESEARCH_EDIT_MAX_CHARS

SRC = {"src-1": {"title": "T", "url": "u"}}


def test_research_edit_writes_raw_and_rendered_report():
    delta = merge_research_edit(
        {"combined_final_cited_report": "old", "sources": SRC},
        [
            {
                "field": "combined_final_cited_report",
                "value": 'new <cite source="src-1"/>',
            }
        ],
    )
    assert delta == {
        "combined_final_cited_report": 'new <cite source="src-1"/>',
        "final_report_with_citations": "new [T](u)",
        "research_report_edited": True,
    }


def test_research_edit_ignores_unchanged_blank_wrong_field_and_bad_types():
    st = {"combined_final_cited_report": "same", "sources": {}}
    assert (
        merge_research_edit(
            st, [{"field": "combined_final_cited_report", "value": " same "}]
        )
        == {}
    )
    assert (
        merge_research_edit(
            st, [{"field": "combined_final_cited_report", "value": "   "}]
        )
        == {}
    )
    assert merge_research_edit(st, [{"field": "other", "value": "x"}]) == {}
    assert (
        merge_research_edit(st, [{"field": "combined_final_cited_report", "value": 5}])
        == {}
    )
    assert merge_research_edit(st, None) == {}


def test_research_edit_rejects_oversized_value():
    st = {"combined_final_cited_report": "a", "sources": {}}
    big = "x" * (RESEARCH_EDIT_MAX_CHARS + 1)
    assert (
        merge_research_edit(
            st, [{"field": "combined_final_cited_report", "value": big}]
        )
        == {}
    )
```
2. Run them and confirm they FAIL (ImportError).
3. Implement:
```python
RESEARCH_EDIT_FIELD = "combined_final_cited_report"
RESEARCH_EDIT_MAX_CHARS = 200_000


def merge_research_edit(state: dict | None, edits: list | None) -> dict:
    """Pure: turn a checkpoint-1 report edit into a state delta ({} = no-op).

    Writes the raw report (read by the creative prompts) and re-renders
    ``final_report_with_citations`` (read by the PDF tool) with the same
    citation renderer the composer callback uses. Unchanged, blank, oversized
    or non-string values are ignored."""
    state = state if isinstance(state, dict) else {}
    for edit in edits or []:
        if not isinstance(edit, dict) or edit.get("field") != RESEARCH_EDIT_FIELD:
            continue
        value = edit.get("value")
        if not isinstance(value, str) or not value.strip():
            return {}
        if len(value) > RESEARCH_EDIT_MAX_CHARS:
            return {}
        if value.strip() == str(state.get(RESEARCH_EDIT_FIELD) or "").strip():
            return {}
        return {
            RESEARCH_EDIT_FIELD: value,
            "final_report_with_citations": render_citations(
                value, state.get("sources") or {}
            ),
            "research_report_edited": True,
        }
    return {}
```
   Import with `from creative_agent.citations import render_citations`. runserver already runs alongside the agent packages in the api image; confirm the import doesn't create a cycle.
4. Run the tests and confirm they pass. Commit: `feat(runserver): pure merge_research_edit for checkpoint-1 report edits`.

### Task A3: Apply edits by checkpoint in `start_resume`
**Files:** Modify `runserver/async_runs.py` (`_apply_visual_concept_edits` at ~524; the `if edits:` block at ~618-621). Test `tests/test_async_runs.py`.

1. Copy `test_resume_with_edits_appends_state_delta_before_relaunch` (~:712) for `function_name="review_research"`. Assert:
   - the appended `RUNSERVER_AUTHOR` event's `state_delta` has the new `combined_final_cited_report` and `research_report_edited: True`;
   - it does **not** touch `final_visual_concepts`;
   - it is appended before the relaunch.

   Add a test that `review_ad_copies` + `edits` appends no state event, and keep the existing visual test green.
2. Run them and confirm the new tests FAIL.
3. Implement `_apply_research_edit(session_service, app_name, user_id, session_id, edits)`, mirroring `_apply_visual_concept_edits`:
   - a missing session is a no-op;
   - `delta = merge_research_edit(session.state, edits)`;
   - append the event only if `delta` is non-empty.

   Replace the `if edits:` block with:
```python
_EDIT_APPLIERS = {
    "review_visual_concepts": _apply_visual_concept_edits,
    "review_research": _apply_research_edit,
}
...
if edits and (applier := _EDIT_APPLIERS.get(function_name)):
    await applier(session_service, app_name, user_id, session_id, edits)
elif edits:
    logger.warning("resume edits ignored for %s", function_name)
```
   Only these two checkpoints send edits, so behaviour for existing clients doesn't change.
4. Run the Python loop. Commit: `feat(runserver): route resume edits by checkpoint (research + visuals)`.

### Task A4: Agent regenerates the PDF after an edit
**Files:** Modify `interactive_creative/review_tools.py:5-8` (docstring) and `interactive_creative/prompts.py:77` (checkpoint 1). Test: `tests/test_pipeline_structure.py`, or a new `tests/test_interactive_prompts.py`.

1. Failing test: `ROOT_AGENT_INSTR` mentions `report_edited` and `save_draft_report_artifact` in the checkpoint-1 step, and the `review_research` docstring mentions `report_edited`.
2. Edit the prompt (step 3, after the feedback clause): "If the response has `report_edited: true`, the user edited the research report: call `save_draft_report_artifact` again so the PDF matches the edited report, then proceed to step 4. Do NOT re-run the research pipeline." Update the docstring to list `report_edited` (boolean, optional).
3. Run the tests. Commit: `feat(interactive_creative): re-save research PDF when the report was edited`.

### Task A5: Frontend sends the edit
**Files:** Modify `frontend/src/app/run/[sessionId]/run-helpers.ts` (add the helper next to `buildConceptEdits`) and `ReviewPanel.tsx` `ReviewResearch`. Test: add to `frontend/src/__tests__/concept-edits.test.ts`, or a new `research-edit.test.ts`.

1. Failing Vitest:
```ts
import { buildResearchEdit } from "@/app/run/[sessionId]/run-helpers";
it("returns an edit only when the report changed", () => {
  expect(buildResearchEdit("a", "a ")).toBeNull();
  expect(buildResearchEdit("a", "   ")).toBeNull();
  expect(buildResearchEdit("a", "b")).toEqual([{ field: "combined_final_cited_report", value: "b" }]);
});
```
2. Implement `buildResearchEdit(original: string, edited: string)`. It returns `null` when `edited.trim()` is empty or equals `original.trim()`; otherwise it returns the one-element edits array.
3. In `ReviewResearch`:
   - Both actions build `const edits = buildResearchEdit(report ?? "", editedReport)`, then call `onResume({ status, feedback, instruction, ...(edits ? { edits, report_edited: true } : {}) })`.
   - `handleResume` already lifts `edits` to the top-level resume field; `report_edited` stays in the functionResponse for the LLM.
   - Preview renders `editedReport`.
   - "Request changes" is enabled when feedback **or** an edit exists.
   - Copy: "Edit the report to change what ad copy and visuals are based on; the PDF is regenerated. Feedback is passed on as guidance."
4. Run the frontend loop. Commit: `feat(frontend): send edited research report at checkpoint 1`.

**Part A PR:** open, run CI, review the whole diff, merge.

---

## Part B: readable dimension names (`feat/eval-dimension-labels`)

### Task B1: Python label map with a drift guard
**Files:** Create `creative_eval/dimensions.py`. Test `tests/test_eval_dimensions.py`.

1. Failing tests:
```python
import re, pathlib
from creative_eval.dimensions import (
    DIMENSION_LABELS,
    dimension_label,
    dimension_labels_csv,
)


def test_known_and_fallback_labels():
    assert dimension_label("trend_visual_connection") == "Trend connection"
    assert dimension_label("brand_product_representation") == "Brand & product"
    assert dimension_label("  weird__New_dim ") == "Weird new dim"


def test_csv_join():
    assert (
        dimension_labels_csv(["copy_quality", "stopping_power"])
        == "Copy quality, Stopping power"
    )
    assert dimension_labels_csv([]) == ""


def test_matches_frontend_map():
    ts = pathlib.Path("frontend/src/lib/eval-dimensions.ts").read_text()
    pairs = dict(re.findall(r'^\s*(\w+):\s*"([^"]+)"', ts, re.M))
    assert pairs == DIMENSION_LABELS
```
2. Implement the 12-entry `DIMENSION_LABELS` (verbatim from the TS map):
   - strategic_alignment → "Strategy fit"
   - trend_authenticity → "Trend authenticity"
   - platform_viability → "Platform fit"
   - copy_quality → "Copy quality"
   - audience_fit → "Audience fit"
   - call_to_action_strength → "Call to action"
   - trend_visual_connection → "Trend connection"
   - brand_product_representation → "Brand & product"
   - audience_appeal → "Audience appeal"
   - prompt_technical_quality → "Prompt quality"
   - stopping_power → "Stopping power"
   - concept_coherence → "Coherence"

   The `dimension_label` fallback mirrors the TS one: collapse `_`+ to spaces, trim, collapse whitespace, lowercase, capitalize the first char. `dimension_labels_csv` joins with `", "` (labels never contain commas).

   Check the drift regex against the real TS file's formatting; adjust the regex, not the map.
3. Run the tests. Commit: `feat(creative_eval): readable dimension labels mirrored from the frontend`.

### Task B2: New BigQuery column in the row builder
**Files:** Modify `creative_agent/bq_tools.py:38-81` and `tests/test_tools.py` (`TestBuildEvalBqRow` ~316, plus the schema-guard tests ~392/469/486).

1. Failing tests:
   - `test_weakest_dimension_labels_human_readable`: the row has `"weakest_dimension_labels": "Trend connection, Copy quality"` for `["trend_visual_connection", "copy_quality"]`, and `""` when the list is empty.
   - Update `test_row_keys_match_table_schema` to include the new column.
2. Implement: add `"weakest_dimension_labels": dimension_labels_csv(weakest)` after `weakest_dimensions`, and `"weakest_dimension_labels": "STRING"` to `EVAL_COLUMN_TYPES`. Import from `creative_eval.dimensions`; creative_agent already bundles creative_eval via `AGENT_EXTRA_PACKAGES`.
3. Run the Python loop. Commit: `feat(creative_agent): write weakest_dimension_labels to creative_evals`.

### Task B3: Backfill script and docs
**Files:** Create `deployment/backfill_eval_dimension_labels.py`. Test `tests/test_backfill_eval_labels.py`. Docs: `README.md:205-209` (`bq mk` schema line) and `deployment/README.md` (the migration note next to research_gaps at ~240-252; the Eval CI table-clone section at ~1045).

1. Failing test: `build_backfill_sql("proj.ds.creative_evals")` returns SQL that:
   - contains one `WHEN '<key>' THEN '<label>'` per `DIMENSION_LABELS` entry, with labels SQL-escaped;
   - has a fallback `CONCAT(UPPER(SUBSTR(x,1,1)), LOWER(SUBSTR(x,2)))` over `TRIM(REGEXP_REPLACE(d, r'_+', ' '))`;
   - keeps the original order (`WITH OFFSET … ORDER BY off`);
   - joins with `', '`;
   - has `WHERE weakest_dimension_labels IS NULL`.
2. Implement the pure `build_backfill_sql(table)` plus an absl CLI: `--table` (required), `--execute` (default False: print the SQL and run a dry-run job showing bytes and the count of affected rows; only `--execute` runs the UPDATE). Use `agent_common.clients.get_bigquery_client`.
3. Docs:
   - Add `weakest_dimension_labels:STRING` to the README `bq mk` line.
   - In deployment/README, add a "weakest_dimension_labels migration" note:
     - `ALTER TABLE \`$PROJECT.trend_trawler.creative_evals\` ADD COLUMN IF NOT EXISTS weakest_dimension_labels STRING;` and the same for `trend_trawler_eval`;
     - run **before** deploying the code;
     - then the backfill command.
4. Run the Python loop. Commit: `feat(deployment): weakest_dimension_labels backfill + migration docs`.

**Part B PR:** open, run CI, review, merge (merge before running Task R1's deploy steps).

---

## Task R1: Gated rollout (from a clean `main`, or a clean worktree of `origin/main` while the other track is still in progress). The steps overlap as shown in "Execution strategy": step 1 at t=0; steps 2 and 4 right after Part B merges; steps 3 and 5 together after Part A merges.
1. **Migration, before any deploy:** run the two `ALTER TABLE … ADD COLUMN IF NOT EXISTS weakest_dimension_labels STRING` statements (prod `trend_trawler` and `trend_trawler_eval`). Verify with `bq show --schema`.
2. **Backfill:** `uv run python deployment/backfill_eval_dimension_labels.py --table hybrid-vertex.trend_trawler.creative_evals` (a dry run: show the SQL and the row count to the user). Then rerun with `--execute`, and spot-check 5 rows with `SELECT weakest_dimensions, weakest_dimension_labels … LIMIT 5`.
3. **Api deploy** (the safe-redeploy recipe in memory `trend-trawler-api-traffic-pin`):
   - First check the logs for in-flight runs.
   - `env -u SCOUT_AGENT_ENGINE_ID -u CREATIVE_AGENT_ENGINE_ID -u INTERACTIVE_AGENT_ENGINE_ID -u VIRTUAL_ENV gcloud run deploy trend-trawler-api --source . … --no-traffic --tag verify`, with no env flags.
   - Find the newest revision by timestamp, verify authed `/list-apps` on the tag, pin `=100`, move `prev`, remove the `verify` tag.
4. **Creative engine** (batch-sync runbook memory):
   - `deploy_agent.py --version=v9 --agent=creative_agent --create --enable_tracing`, with the env-unset prefix;
   - `integration_test.py --check smoke --agent creative_agent`;
   - update the CRF trigger message and drift check to the v9 ID;
   - delete v8 only after the smoke passes.
5. **Web deploy:** `gcloud run deploy trend-trawler-web --source ./frontend --service-account tt-web-sa@…`, with no env flags. Check the revision by timestamp, IAP (302) and env.

## Verification (end to end)
- **CI:** both PRs green. The local Python and frontend loops pass, including the new tests: citations, merge_research_edit, resume routing, prompt, buildResearchEdit, labels + drift guard, row builder, backfill SQL.
- **Live, Part A:** run a "Creative run with reviews" from the web UI and edit the report at checkpoint 1 (e.g. add the sentence "Lead with the nostalgia angle.").
  - The api logs show the resume, and the session state has `research_report_edited: true` with the new text in `combined_final_cited_report`.
  - `save_draft_report_artifact` runs a second time, and the PDF at `research_report_gcs_uri` contains the edit (`gcloud storage cat` + `pdftotext`, or open it).
  - The ad copy reflects the angle.
- **Live, Part B:**
  - The new run's `creative_evals` row has `weakest_dimension_labels` like `Trend connection, …`.
  - Backfilled old rows are populated.
  - The CRF smoke (creative engine v9) writes a row with labels.
  - The eval-dataset schema includes the column; the next nightly eval CI run passes.
- **Memory:** update the work-status, batch-sync runbook (v9 ID) and api/web revision notes.
