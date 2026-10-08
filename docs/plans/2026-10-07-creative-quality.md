# Creative quality, brief relevance & brief development — Implementation Plan

**Status: complete (2026-10-08).** All 11 PRs merged; research: [docs/research/2026-10-07-creative-quality.md](../research/2026-10-07-creative-quality.md).

| PR | Section | Merged |
|---|---|---|
| 1 | Deterministic inputs + context plumbing | #263 |
| 2 | Structured brief + brand–trend fit + brief check | #264 |
| 3 | Angle-based copy + checklist critic + copy gate | #265 |
| 4 | Visual concept grounding + concept gate | #267 |
| 5 | Image prompting + multi-reference | #273 |
| 6 | Pixel-level image QA + re-render | #274 |
| 7 | Brief-aware eval, gates vs scores, judging pixels | #275 |
| 8 | Human rating UI + judge calibration | #277 |
| 9 | Cross-run brand memory | #278 |
| 10 | Interactive brief review + checkpoint-2 revision | #280 |
| 11 | Docs | this PR (`docs/creative-quality`); the eval-baseline refresh is done separately |

Shipped alongside: #266 (save the final image part), #268 (false-positive fixes in the deterministic checks), #269/#271/#272 (root empty-turn fixes, `finalize_pipeline`, single `creative_pipeline` call — [finalize plan](2026-10-07-finalize-pipeline.md)), #276/#279 (eval tool-use rubric).

**Deviations from the plan as written:** the root tool list *did* change (#271/#272: `creative_pipeline` + `memorize`), so the eval tool-use rubric was updated (#276/#279); brand history does not join `creative_ratings` (no clean run key); the visual `brand_cue_present` eval gate is advisory; the checkpoint-2 reviser runs as a small `ad_copy_user_reviser` Workflow, not an `AgentTool`.

**Goal:** Implement every enhancement in [the research doc](../research/2026-10-07-creative-quality.md) (F1–F12): a structured, fit-tested creative brief; creatives grounded in it; check → revise-on-fail loops; pixel-level image QA with a targeted re-render; a brief-aware eval that separates pass/fail gates from quality scores; a human-rating UI for calibrating the judge; cross-run memory; and a brief-review checkpoint in interactive mode.

## Context

**Why:** the research and code audit (2026-10-07) found three structural weaknesses:

1. The "strategic brief" is free Markdown that nothing can check against.
2. `{brand}` / `{target_audience}` never reach research, the ad-copy drafter or critic, or the visual critic.
3. Nothing feeds back: the eval judges *prompts*, never sees the brief, and is report-only, and rendered images are never inspected.

The user wants to fix creative quality, relevance to the brief, and how the brief itself is built.

**User decisions (2026-10-07):**
- **Loops:** revise-on-fail and image QA are **on by default, tightly bounded**: 1 revision round for copy, concepts and the brief; ≤1 re-render per image. Each has an env kill switch, and the eval baselines get updated.
- **Interactive checkpoint 1:** reviews the **structured brief** (editable fields), with the full report collapsed underneath.
- **Calibration:** a **rating UI on the results page** (stored in BigQuery), plus an agreement report.

**Design principles:**
- **The eval stays the independent final measurement.** It never triggers regeneration, because judging creatives against its own feedback is circular (research F5/F11). The regeneration points are the *pre-eval* gates: the brief check, the copy gate, the concept gate and image QA.
- **New logic goes inside the existing pipeline graphs.** New LLM work becomes graph nodes, and new deterministic checks become function nodes (the `refinement_gate` routing pattern, `creative_agent/agent.py:190-230`). **The root tool list and order stay the same**, so `tests/eval/creative_eval_config.json`'s tool-use rubric is unaffected.
- **New LLM nodes run on `worker_model`** (gemini-3.8-flash). Pro is capped at 5 RPM project-wide (memory: vertex-model-quotas).
- **Degrade, never kill a run.** Every new state key is read with `{key?}`. A missing or failed brief, gate or QA step records a `<key>__…` marker that surfaces through `agent_common/observability.collect_degradation_warnings`.

**Conventions:**
- **PRs:** one PR per section below, in order. TDD with a commit per task.
- **Gate:** `uv run ruff format . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 4`; frontend `npm run lint && npm test && npm run build`.
- **Commits and PRs:** **never** add Co-Authored-By or "Generated with Claude Code" (user rule overrides the harness default). Don't stage `.agents/`, `.claude/`, `skills-lock.json` or `creative-quality-research.md`, unless the last is moved into `docs/research/` in PR 11.
- **Templates:**
  - Prompt constants live in `prompts.py`. Pydantic models live in `schemas.py`, re-exported through `agent.py`; anything interactive needs goes through the `creative_agent/__init__.py` facade `__all__`.
  - No `{}` inside `IMAGE_PROMPT_GUIDE`.
  - `creative_eval/prompts.py` uses `.format`, so literal braces must be doubled.
- **New env knobs:** use `field(default_factory=lambda: os.getenv(...))` on `BaseAgentConfiguration` (`agent_common/config.py`), and add them to `ENV_VAR_DICT` in `deployment/deploy_agent.py:35-53` so they reach Agent Engine.

---

## PR 1: Deterministic inputs + context plumbing (F4, the quick wins of F6, risk sections)

**Status: merged #263.**
Branch `feat/creative-inputs-context`.

**Task 1.1: Seed the core campaign fields through state.**
- **Callback** (`creative_agent/callbacks.py:87-106`): `load_session_state` currently puts brand/target_* (blank) in `source`, and `seed_initial_state` then `update`s them, wiping any seeded value. Move these five keys to the `setdefault("")` path used by the visual keys (lines 59-79), and keep the "never clobber" comment style.
- **Frontend:** `frontend/src/lib/initial-state.ts` `buildInitialState` adds `brand`, `target_audience`, `target_product`, `key_selling_points` and (creative only) `target_search_trends`, trimmed and omitted when empty. Keep the kickoff message in `page.tsx:160-163` as a human-readable echo, and rename its trend line to `target_search_trends` (fixes the mismatch).
- **Root prompt** (`creative_agent/prompts.py:647-650`): "If a campaign field is already present in state (shown below), do not re-memorize it; memorize only fields missing from state." The root token set is pinned to `{gcs_bucket, gcs_folder, agent_output_dir}` by `tests/test_creative_root_prompt.py:69`, so pass the values as `{brand?}` etc. and update that test's expected set. The halt rule then checks state *or* message.
- **CRF worker** (`cloud_functions/creative_fanout/main.py:331`) and **test deployment** (`deployment/test_deployment.py:130`): pass `state=` on session create if the agentplatform `async_create_session` supports it (verify in the SDK first); otherwise leave message-only, since that path still works.
- **Tests:**
  - `tests/test_callbacks.py::TestSetInitialStates`: seeded values survive, and absent ones default to "".
  - `frontend/src/__tests__/initial-state.test.ts`.
  - `test_crf_worker_async.py`, if changed.

**Task 1.2: Get brand and audience to every agent that needs them.**
- **Campaign planner** (`sub_agents/campaign_researcher/agent.py:61-86`): add `{brand}` plus 1–2 required queries on the brand's voice, positioning, recent campaigns and **distinctive assets** (colours, logo, characters, shapes). Its synthesizer gets a new section, "Brand Voice & Distinctive Assets".
- **Prompts** (`creative_agent/prompts.py`):
  - `AD_COPY_DRAFTER` gets `{brand}`, `{target_audience}`.
  - `AD_COPY_CRITIC` gets `{brand}`.
  - `COMBINED_REPORT_COMPOSER` gets `{brand}`, `{target_product}`, `{target_audience}`.
  - `VISUAL_CONCEPT_CRITIC` gets `{brand}`, `{target_audience}`, `{ad_copy_critique?}` (its paired copy).
- **Tests:** token presence in `tests/test_visual_intent_prompts.py`; `tests/test_pipeline_structure.py` (merge-planner `?` rule unaffected).

**Task 1.3: Restore risk sections.**
- **Trend synthesizer:** uncomment and modernise the "Risk Assessment" section (`trend_researcher/agent.py:178`): controversies, real-person sensitivities, negative associations.
- **Composer:** add a 5th section, "Risks & Constraints" (≤3 bullets), to `COMBINED_REPORT_COMPOSER`, and delete the dead comment at `creative_agent/agent.py:167-169`.

---

## PR 2: Structured brief + brand–trend fit + brief check (F1, F2, F3, F7)

**Status: merged #264.**
Branch `feat/creative-brief`. Depends on PR 1.

**Task 2.1: Schema.** Add `CreativeBrief` to `creative_agent/schemas.py`:

```python
class TrendBridge(BaseModel):
    fit_score: int = Field(ge=1, le=5)
    fit_mode: Literal["direct", "cultural", "light_touch"]
    bridge: str            # which brand/product trait connects to which trend facet
    motifs: list[str]      # trend-specific, concrete (no generic phones/feeds)
    risks: list[str]
class ReasonToBelieve(BaseModel):
    claim: str
    source_id: str | None  # "src-N" from {sources?}, or "brief" for user selling points
class BrandCues(BaseModel):
    tone_of_voice: str
    distinctive_assets: list[str]
    do_not: list[str]
class CreativeAngle(BaseModel):
    angle_id: str          # "A1".."A5"
    name: str
    tension: str           # the audience tension this route dramatises
    route: str             # one-line creative route tied to the proposition
class CreativeBrief(BaseModel):
    objective: str
    audience: str
    insight: str           # "X, but Y" human tension
    single_minded_proposition: str
    reasons_to_believe: list[ReasonToBelieve]
    brand: BrandCues
    trend_bridge: TrendBridge
    mandatories: list[str]
    avoid: list[str]
    desired_response: str  # think / feel / do
    angles: list[CreativeAngle] = Field(min_length=3, max_length=5)
```

Add it to `test_schemas.py` (bounds, Literal, angle count).

**Task 2.2: `brief_writer` node.**
- **Node:** worker model, `output_schema=CreativeBrief`, `output_key="creative_brief"`, `SCHEMA_RETRY`, standard callbacks (rate limit, surrogate scrub, empty-turn log). It is wrapped in `RetryUntilKeyNode(output_key="creative_brief", max_attempts=2)` from `agent_common/retry_node.py`.
- **Prompt** (`CREATIVE_BRIEF_WRITER_INSTR`):
  - **Inputs:** `{combined_final_cited_report?}`, `{sources?}`, the 5 campaign keys, `{visual_avoid?}`, `{brand_colors?}`, `{brief_issues?}`, `{brand_history?}` (added in PR 9).
  - **Field rules, in one sentence each:**
    - the proposition is one sentence with no "and";
    - the insight is an "X, but Y" tension specific to this brand ("could this belong to any brand?" → rewrite);
    - every reason to believe cites a `src-N` or "brief";
    - `fit_score` uses a 1–5 rubric;
    - `fit_mode` rules: ≥4 means direct, 3 means cultural, ≤2 means light_touch (borrow the trend's tone or mood, don't force the product into the trend);
    - motifs are concrete and trend-specific;
    - angles are genuinely different tensions, not tone variants;
    - user selling points and `visual_avoid` map into mandatories and avoid.

**Task 2.3: `brief_check` (deterministic) and routing.**
- **Pure module:** `creative_agent/brief_check.py` with `check_brief(brief: dict) -> list[str]`. It returns an issue for each of:
  - the proposition is >1 sentence or contains " and ";
  - the insight has no tension marker ("but", "yet", "although", "even though");
  - a reason to believe has no source;
  - `fit_mode` is inconsistent with `fit_score`;
  - there are <3 distinct angles (dedupe on lowercased name);
  - motifs are empty, or generic only (reuse the generic-motif list idea from #258's prompt; keep it a small constant);
  - brand `distinctive_assets` is empty while `brand_colors` is set.
- **Function node `brief_gate`:** routes `"revise"` when there are issues *and* `brief_revision_rounds` < `config.brief_revision_rounds` (env `BRIEF_REVISION_ROUNDS`, default 1), writing `brief_issues`; otherwise `"ok"`. If the brief is still flawed after the last round, record `creative_brief__issues` (a warning, not a failure).
- **`brief_reviser`:** a second `Agent` built from the same factory as `brief_writer`, distinct name, reading `{brief_issues?}`.
- **New research graph edges:** `combined_report_composer → brief_writer_resilient → brief_gate → {ok: research_report_ready, revise: brief_reviser → research_report_ready}`.
- **`research_report_ready`** still keys on the report. Its notice mentions whether `creative_brief` exists.
- **Tests:**
  - unit tests for `check_brief` (each rule);
  - `test_pipeline_structure.py`: exact research edges, output keys, schemas, the worker-model bucket;
  - `test_creative_agent_graph.py`: recorded-LLM runs for a brief that passes, a brief that's revised once, and the writer exhausting retries (report still flows, warning recorded).
- **Observability:** add `creative_brief` to the `make_final_state_summary` keys, and `creative_brief__issues` handling to `collect_degradation_warnings`.

**Task 2.4: Downstream agents read the brief.**
- **Which prompts:** `AD_COPY_DRAFTER`, `AD_COPY_CRITIC`, `ART_DIRECTOR`, `VISUAL_CONCEPT_DRAFTER` and `VISUAL_CONCEPT_CRITIC` get a `<CREATIVE_BRIEF>{creative_brief?}</CREATIVE_BRIEF>` block, with a rule: "The brief is the contract: deliver its proposition, use its reasons to believe, honour mandatories/avoid, and use the trend *bridge* in the brief's fit_mode."
- **Keep the report:** `{combined_final_cited_report?}` stays as supporting colour, after the brief. Optional: trim the report to the copy agents only, if the token budget is tight (decide from the eval token count).
- **Visual cues:** `ART_DIRECTOR` must place at least one `brand.distinctive_assets` item per concept when any exist (F7), and pick the 4 shortlist families most compatible with `brand.tone_of_voice` (soft brand-aware selection; the shortlist stays random and stratified).

**Task 2.5: Show the brief.**
- **Research PDF / draft artifact:** `save_draft_report_artifact` prepends a rendered "Creative Brief" section, using a pure `render_brief_markdown(brief)` in `creative_agent/brief_check.py` or a new `brief_render.py`.
- **Frontend:**
  - A new `components/creative-brief.tsx` (read-only cards: proposition, insight, angles, trend fit with score and mode, reasons to believe, mandatories/avoid, brand cues). Follow the proof-room tokens: no uppercase eyebrows, `FieldLabel`, mono only for `src-N`.
  - It appears in a "Creative brief" disclosure, above "Research report", on `/results/[sessionId]` and in the run page's outputs.
  - `lib/run-stages.ts` adds a "Brief" stage keyed on `creative_brief`.
  - Vitest coverage, plus an updated screenshot fixture (the `frontend/scripts/` harness).

---

## PR 3: Angle-based copy + checklist critic + revise-on-fail (F5, F6, F8)

**Status: merged #265.**
Branch `feat/copy-gate`. Depends on PR 2.

**Task 3.1: Angles and diversity.**
- **Schema:** `AdCopy` and `FinalAdCopy` gain `angle_id: str = ""` and `typicality: float | None` (0–1, self-rated). Default values keep old sessions loading.
- **`AD_COPY_DRAFTER` rules:**
  - spread the 10 copies across the brief's angles, with ≥2 per angle when there are ≤5 angles;
  - within each angle, write ideas ranging from typical to unexpected and rate each idea's typicality (a light Verbalized-Sampling variant, F8);
  - keep the ≥4-of-6 tone rule.
- **`AD_COPY_CRITIC`:** the final 4 must cover ≥3 different angles, and at least one must have typicality < 0.5 unless that weakens the brief fit.

**Task 3.2: Checklist output.**
- **Schema:** `FinalAdCopy` gains `brief_checks: list[BriefCheck]`, where `BriefCheck{item: Literal["proposition","product","reason_to_believe","trend_bridge","tone","mandatories","avoid","cta"], passed: bool, note: str}`.
- **Critic:** fills `brief_checks` honestly, and must critique and improve each CTA (the CTA was never critiqued before).

**Task 3.3: `copy_gate` + `ad_copy_reviser`.**
- **Pure module:** `creative_agent/copy_gate.py` with `gate_copies(copies, *, target_product, avoid) -> dict[original_id, list[issue]]`. It combines:
  - deterministic checks: the product name, or its first significant token, appears in headline+body+caption (case-insensitive); the CTA is non-empty and ≤8 words; headline ≤ 60 chars; caption ≤ 2,200 chars (IG limit); no `avoid` term;
  - every `brief_checks` item with `passed=False`.
- **Function node `copy_gate`:** routes `"revise"` when any copy has issues and rounds remain (env `COPY_REVISION_ROUNDS`, default 1), writing `ad_copy_issues`; otherwise `"ok"`.
- **`ad_copy_reviser`:** worker model, `output_schema=FinalAdCopyList`, `output_key="ad_copy_critique"`. It rewrites **only** the flagged copies, with their issues quoted, and returns all 4 with unchanged ids.
- **Safety net:** an after-callback restores any unflagged copy the reviser altered, which keeps it bounded and safe.
- **Edges:** `START → ad_copy_drafter → ad_copy_critic → copy_gate → {ok: ad_copies_ready, revise: ad_copy_reviser → ad_copies_ready}`. Residual issues go to `ad_copy_critique__issues`, which becomes a warning.
- **Tests:**
  - unit tests for `gate_copies`;
  - pipeline edges/keys;
  - a graph test for "one copy revised, others untouched";
  - an `ad_copy_reviser` export in the facade (interactive needs it in PR 10).

---

## PR 4: Visual concept grounding + concept gate (F5, F7)

**Status: merged #267.**
Branch `feat/concept-gate`. Depends on PR 2 (and PR 3 for angles).

**Task 4.1: Schema.**
- `VisualConcept`, `VisualConceptCritique` and `VisualConceptFinal` gain `brand_cue: str = ""` (the distinctive asset used) and `angle_id: str = ""`.
- `test_image_prompt_guide.py`-style test that the fields exist on all three.

**Task 4.2: Prompts.**
- **Drafter/critic/finalizer:**
  - each concept carries a `brand_cue` taken from `creative_brief.brand.distinctive_assets` or `{brand_colors?}`;
  - in-image text must quote the paired final headline or CTA exactly ("text-first", F10);
  - concepts respect the brief's `avoid` and `fit_mode` (light_touch means the trend shows up as mood or motif, not forced product placement).
- **Finalizer:** carries `angle_id` through from the copy.

**Task 4.3: `concept_guard` extension + gate.**
- **Guard** (`creative_agent/concept_guard.py`):
  - `ensure_trend_and_product` also appends a missing `brand_cue` ("… featuring {brand_cue}.");
  - a pure `concept_issues(concepts, ad_copies)` flags quoted text that doesn't match the paired copy, empty `trend_motif`, >2 concepts with in-image text, and >1 centred hero (keyword heuristic: "centered"/"centred"/"symmetrical hero").
  - The existing after-callback keeps doing the deterministic repairs.
- **Function node `concept_gate`** after `visual_concept_finalizer`: routes `"revise"` when there are issues and rounds remain (env `CONCEPT_REVISION_ROUNDS`, default 1).
- **`visual_concept_fixer`:** worker, `VisualConceptFinalList`, `output_key="final_visual_concepts"`, rewrites only flagged concepts, with the guard callback attached.
- **Edges:** `… → visual_concept_finalizer → concept_gate → {ok: visual_concepts_ready, revise: visual_concept_fixer → visual_concepts_ready}`.
- **Tests:** `tests/test_concept_guard.py`, pipeline edges, and a graph test.

---

## PR 5: Image prompting to Google guidance + multi-reference (F10)

**Status: merged #273.**
Branch `feat/image-prompting-refs`.

**Task 5.1: Verify the model's capabilities.**
- **Check:** a short script/notebook (gitignored) calls `gemini-nano-banana-2.1` with 2–3 reference images (product + logo + style).
- **Confirm:** the reference limit; whether style references work, which would retire the "flash-image cannot do true style transfer" note; and whether `"exact quoted text"` renders.
- Record the results in the PR description and `docs/`.

**Task 5.2: Guide update.** Add to `IMAGE_PROMPT_GUIDE` (no braces):
- a `<REFERENCE_IMAGES>` block: "[Reference images] + [relationship instruction] + [new scenario]: name what each reference is for (product = reproduce exactly; logo = place small and legible; style = match palette/texture only)";
- Google's Subject + Action + Location/context + Composition + Style order, aligned with BUILDING_BLOCKS;
- "describe typography for quoted text".

Update `tests/test_image_prompt_guide.py`.

**Task 5.3: Multiple references.**
- **State and tools:**
  - a new state key `reference_images: list[{uri, role}]` (≤ the verified limit, default cap 3);
  - legacy `reference_image_uri` / `reference_image_role` are folded in as the first entry by a pure `resolve_references(state)` in `image_tools.py`;
  - `_fetch_reference_image` runs per reference via `asyncio.gather(asyncio.to_thread(...))`;
  - the role instructions are combined (one line per reference, numbered to match the image order).
- **Frontend:** the form adds up to 3 reference rows (URI + role); `initial-state.ts` emits `reference_images` (and keeps emitting the legacy keys for one release); `VISUAL_DIRECTION_FIELDS` displays them.
- **Tests:** `tests/test_image_reference.py` (multiple parts in order; legacy fold-in; one failed fetch skips just that reference); `initial-state.test.ts`.

---

## PR 6: Pixel-level image QA + one targeted re-render (F9)

**Status: merged #274.**
Branch `feat/image-qa`. Depends on PR 4 (`brand_cue`, quoted text).

**Task 6.1: Record per-concept URIs.** `generate_image` (`creative_agent/image_tools.py:285-340`) writes `state["generated_images"] = {concept_name: {"gcs_uri", "artifact_key", "attempts", "qa"}}`, which the eval (PR 7) and the UI need. Keep `_generated_artifact_keys`.

**Task 6.2: QA call.**
- **New module `creative_agent/image_qa.py`:**
  - `ImageQAResult(BaseModel){product_visible, motif_visible, brand_cue_visible: bool|None, text_expected: bool, text_exact: bool|None, text_legible: bool|None, gibberish_text: bool, artifacts: bool, unsafe: bool, issues: list[str]}`;
  - `qa_passed(r)`, a pure rule (product+motif visible, no gibberish/artifacts/unsafe, exact legible text when expected);
  - `async def inspect_image(image_bytes, concept, *, client, model) -> ImageQAResult`, which uses the cached genai client from `image_tools._get_genai_client` (global location, shared timeout + `build_genai_http_retry`), `config.image_qa_model` (env `IMAGE_QA_MODEL`, default `worker_model`), `response_schema=ImageQAResult`, temperature 0.
  - The call goes through `asyncio.to_thread` (the client is sync; don't block the loop).
- **Failure handling:** a QA error is fail-open: `qa=None`, plus an `image_qa__errors` marker.

**Task 6.3: Re-render loop in `generate_image`.**
- **Flow:** render, inspect, and if the image fails QA with `IMAGE_QA_MAX_RERENDERS` (default 1) remaining, re-render with `prompt + "\n\nCorrect these issues: " + "; ".join(issues)`. Keep whichever attempt has fewer failed checks, and upload only the kept image (or overwrite the same key).
- **Quota:** re-renders go through `_generate_image_with_backoff`, so they respect the 2 RPM image quota.
- **Kill switch:** `IMAGE_QA_ENABLED` (default true).
- **Env:** add all three knobs to `ENV_VAR_DICT`.
- **Warnings:** a still-failing image adds `image_qa__failed:<concept>` to the warnings (`collect_degradation_warnings`).
- **Tests** (new `tests/test_image_qa.py`): the `qa_passed` matrix; render → fail → re-render → pass; both attempts fail (keep the better one, warn); a QA exception is fail-open; disabled means a single render.
- Update `tests/test_creative_agent_graph.py`'s `_fake_generate_image` if needed.

**Task 6.4: Surface the QA results.**
- **Gallery HTML:** the gallery tool shows a QA badge and issues per image.
- **Frontend proof detail:** an "Image check" row from `state.generated_images[...].qa` (pass/fail marks; issues list).
- **Vitest coverage.**

---

## PR 7: Brief-aware eval with gates vs scores, judging pixels (F11)

**Status: merged #275.**
Branch `feat/eval-gates`. Depends on PRs 2, 6.

**Task 7.1: Schema** (`creative_eval/schemas.py`):
- `GateResult{gate: str, passed: bool, note: str}`;
- `CreativeScore` gains `gates: list[GateResult] = []` and `gates_passed: bool = True`;
- `CreativeEvaluationReport` gains `passing_threshold: float` (the frontend already reads it optionally) and `brief_used: bool`.
- **`passed` semantics:** `passed = overall_score ≥ threshold and gates_passed`. Document the change in `docs/`, since it will shift historical pass rates.

**Task 7.2: Prompts and inputs.**
- **Inputs:** `evaluate_all_creatives` (`creative_eval/agent.py:33-154`) reads `creative_brief` and `generated_images`.
- **Prompts:** both user prompts gain a `BRIEF` block (proposition, reasons to believe, mandatories, avoid, trend bridge and fit_mode, brand cues), with braces escaped. The ad-copy prompt also gains the copy's `angle_id`.
- **Gate definitions:**
  - **ad copy:** `delivers_proposition`, `product_named`, `uses_reason_to_believe`, `mandatories_met`, `avoid_respected`;
  - **visual:** `product_visible`, `trend_motif_visible`, `text_correct`, `brand_cue_present`, `avoid_respected`.
- **Missing brief:** gates that need the brief become `passed=True, note="no brief"` and `brief_used=False`.

**Task 7.3: Judge the pixels.**
- **Image input:** `evaluate_visual_concept` adds `types.Part.from_uri(file_uri=gcs_uri, mime_type="image/png")` when `generated_images[concept].gcs_uri` exists. The Vertex judge reads gs:// directly; verify the judge's service account has read access to the bucket. The rubric text changes from "judge the prompt" to "judge the rendered image, and use the prompt only for intent".
- **QA hints:** pass the image QA result as a hint, never as ground truth.
- **Clean-up:** remove the unused `max_retries` from `EvalConfig` (and the test at `tests/test_creative_eval.py:311`). Mark the gate dimensions in `creative_eval/dimensions.py` and `frontend/src/lib/eval-dimensions.ts` (drift test `tests/test_eval_dimensions.py`).
- **BigQuery:** `creative_evals` gains `gates_pass_rate FLOAT64` (row builder `bq_tools.build_eval_bq_row`, schema dict, `tests/test_tools.py:396`, create-table script + migration doc).

**Task 7.4: UI.**
- `frontend/src/lib/eval-matching.ts` types gain optional `gates`/`gates_passed`.
- The proof detail shows gates as pass/fail marks above the advisory dimension scores, labelled "Checks" vs "Quality (advisory)".
- The contact sheet sort still uses `proofScore`; a failed gate shows a `mark-fail` chip.
- Vitest coverage (old reports without gates still render).

---

## PR 8: Human rating UI + judge calibration (F11)

**Status: merged #277.**
Branch `feat/creative-ratings`. Depends on PR 7.

**Task 8.1: Storage and API.**
- **BigQuery table `creative_ratings`:** `rating_id` (`stable_row_id(session_id, creative_key, user)`), session_id, app_name, creative_key (`concept_name` or `copy:<original_id>`), kind (`visual|ad_copy`), user_id, verdict (`pass|fail`), score (1–5), note, judge_overall, judge_passed, judge_gates_passed, judge_model, created_at.
- **Writes:** `MERGE … WHEN MATCHED UPDATE / WHEN NOT MATCHED INSERT` (the user may change their rating), using `agent_common/idempotency.stable_row_id`.
- **Router `runserver/ratings.py`:** user-scoped routes `PUT /ratings/{userId}/{sessionId}` (body: creative_key, kind, verdict, score, note) and `GET /ratings/{userId}/{sessionId}`. Mount it in `deployment/async_app.py`.
- **Authz:** follow the `runserver/experiments.py` / `runserver/authz.py` pattern (the path userId must equal the trusted user; `authorize_body_user`).
- **Validation:** `creative_key` must exist in the session state; judge fields are copied from the session's eval report at write time.
- **Proxy:** allowlist the two routes in `frontend/src/lib/user-scoping.ts` (+ `user-scoping.test.ts`).
- **Local dev:** a fake store, matching `BANDIT_DEPLOY_MODE=fake`'s pattern (env `RATINGS_STORE=memory`).
- **Tests:** `tests/test_ratings_api.py` (authz 401/403, validation, upsert idempotency against the fake BigQuery in `tests/_fake_bq.py`).

**Task 8.2: UI.**
- **Rating control:** the proof-detail dialog (`app/results/[sessionId]/…proof-detail`) gets "Your rating": a pass/fail toggle, 1–5 score, optional note, saved through `lib/api.ts` `putRating`, and loaded with `getRatings`.
- **Contact sheet:** shows a small "rated" mark.
- **Design:** proof-room design.
- **Tests:** Vitest for the payload, render and optimistic update.

**Task 8.3: Calibration report.**
- **Script** `scripts/eval_calibration.py` (and `GET /ratings/{userId}/calibration`, an aggregate across the user's ratings): n, raw agreement, **Cohen's kappa** for judge `passed` vs human verdict (also for `gates_passed`), Spearman between `judge_overall` and the human score, split by kind. Pure maths lives in `runserver/calibration.py` with no numpy (like `batch_means.py`).
- **Frontend:** a small "Judge agreement" line on `/runs` (or results) once n ≥ 20; otherwise "Rate N more creatives to calibrate the judge".
- **Docs:** `docs/` explains the calibration protocol, e.g. aim for 50 rated creatives across ≥5 runs.

---

## PR 9: Cross-run brand memory (F12)

**Status: merged #278.**
Branch `feat/brand-history`. Depends on PRs 2, 7, 8.

**Task 9.1: Read helper.**
- **`creative_agent/bq_tools.py`:** `fetch_brand_history(brand: str, limit: int = 5) -> dict`. A parameterized `SELECT` on `creative_evals` for the brand, latest first, returns `weakest_dimension_labels`, `eval_report_gcs_uri` and the pass rates. It reads each report JSON from GCS (`agent_common/clients.get_gcs_client`) for: `visual_style` of the top and bottom creatives, the angle names (from `angle_id` + brief), and failed gates. It also joins `creative_ratings` human fails (PR 8).
- **Failure handling:** fail-open; returns `{}`.
- **Output:** `format_brand_history(dict) -> str` (≤ ~120 words): "Recently used styles: …; strongest angles: …; recurring weaknesses: …; avoid repeating: …".

**Task 9.2: Wiring.**
- **Function node:** `load_brand_history` (no LLM) is the first node of `combined_research_pipeline`, in parallel with the planners, or just before `brief_writer`. It writes `brand_history` (text) and, unless `visual_style_preference` is set, re-picks `style_shortlist` with a new `pick_style_shortlist(rng, exclude=recent_styles)`. That function keeps the 2/3/1 stratification and falls back to the full pool when the exclusion would empty a group.
- **Knobs:** env `BRAND_HISTORY_ENABLED` (default true) and `BRAND_HISTORY_RUNS` (5), both in `ENV_VAR_DICT`.
- **Prompts:** `{brand_history?}` goes into `CREATIVE_BRIEF_WRITER_INSTR` ("build on strong angles, fix recurring weaknesses, don't repeat") and `ART_DIRECTOR`.
- **Tests:** SQL params, formatting, exclusion-aware shortlist (`tests/test_style_shortlist.py`), BigQuery-error fail-open, graph-node presence.

---

## PR 10: Interactive mode: brief-review checkpoint + copy feedback that acts (F3)

**Status: merged #280.**
Branch `feat/interactive-brief-review`. Depends on PRs 2, 3.

**Task 10.1: Backend.**
- **Checkpoint 1:** `interactive_creative/review_tools.review_research`'s docstring documents `brief_edited` and `edits` (field `creative_brief`).
- **Server merge:** `runserver/async_runs.py` gains `merge_brief_edit` (next to `merge_research_edit`, 730-764). It validates the edited value against `CreativeBrief` (imported via the facade; `runserver` already depends on the agent packages) and writes `creative_brief` + `creative_brief_edited: True`. Invalid edits are rejected with 400.
- **Interactive prompt step 1** (`interactive_creative/prompts.py:76`): on `brief_edited`, re-run `save_draft_report_artifact`; research is still never re-run.
- **Checkpoint 2:** on `revision_requested` with feedback, call the `ad_copy_reviser` (from PR 3, an `AgentTool` via the facade) with the feedback written to `ad_copy_issues`, then re-present the copies once.
- **Tests:** `tests/test_async_runs.py`, `tests/test_interactive_resume_graph.py`, `tests/test_pipeline_structure.py` (interactive AgentTool set: add `ad_copy_reviser`), `tests/test_interactive_prompts.py` (no `{` rule).

**Task 10.2: Frontend.**
- **`ReviewResearch`** (`frontend/src/app/run/[sessionId]/ReviewPanel.tsx:63-160`) becomes **`ReviewBrief`**:
  - editable `CreativeBrief` fields: proposition, insight, angles (edit/remove, min 3), mandatories/avoid lists, `fit_mode` select with the score shown, tone;
  - the full research report collapsed underneath, still editable as today.
- **Resume payload:** `run-helpers.ts` builds `{edits:[{field:"creative_brief", value}], brief_edited:true}`.
- **Tests:** `research-edit`/new `brief-edit` tests, `interactive-mode` tests, a screenshot fixture.

---

## PR 11: Docs, baselines, rollout

**Status: this PR (docs, diagrams, migration checklist); eval baselines refreshed separately.**
Branch `docs/creative-quality`.
- **Research doc:** move `creative-quality-research.md` to `docs/research/2026-10-07-creative-quality.md`, and add this plan as `docs/plans/2026-10-07-creative-quality.md`.
- **CLAUDE.md:** update the creative_agent composition tree (brief_writer / brief_gate / copy_gate / concept_gate / image QA), the new state keys, the env knobs, the eval gates, ratings, and the `creative_ratings` table.
- **READMEs:** `tests/README.md` and `deployment/README.md` (the BigQuery migration for `creative_evals.gates_pass_rate` + `creative_ratings`).
- **Diagram:** regenerate the creative_agent architecture diagram with the `paperbanana-figures` skill (bands; no cross-band arrows).
- **Eval baselines:**
  - after PRs 1–7 merge, the nightly `adk-eval` will trip the efficiency gate (~+4–6 LLM calls per case vs `docs/baselines/eval_efficiency.json`: 25/27 calls, 315k/381k tokens);
  - dispatch `adk-eval.yml` on main, confirm the cases PASS, run `python tests/eval/efficiency_gate.py --agent creative_agent --update-baseline`, and commit the JSON plus `docs/baselines/main.md`;
  - review the latency warning.

**Rollout** (nothing in flight; follow the memory runbooks):
1. **BigQuery migrations:** add `creative_evals.gates_pass_rate`, create `creative_ratings`, **before** the code that writes them.
2. **Engines:** redeploy `creative_agent` and `interactive_creative` (and `trend_scout` only if `agent_common` changed in ways it uses), using the env-unset prefix. Update the CRF trigger message with the new creative engine ID, and run the drift check.
3. **api:** deploy, verify with `--no-traffic --tag verify`, pin traffic to the newest revision by timestamp, remove the tag (no prev tag).
4. **web:** deploy and pin.
5. **CRF worker:** redeploy only if Task 1.1 changed it.

## Verification
- **Unit/CI:** all gates pass in every PR. The new pure modules (`brief_check`, `copy_gate`, `concept_guard` additions, `image_qa.qa_passed`, `calibration`, `fetch_brand_history` formatting) have direct tests. Recorded-LLM graph tests cover revise / no-revise / exhausted paths for the brief, copy and concepts.
- **Local end-to-end:** `TRUST_CLIENT_USER_ID=1 SESSION_SERVICE_URI=memory:// … uvicorn deployment.async_app:app` + `npm run dev`. Run creative_agent on a known brief, e.g. the PRS guitars / Powerball fixture. Check:
  - `creative_brief` in state and in the UI;
  - copies span ≥3 angles with `brief_checks`;
  - the gates' routes, in the logs;
  - `generated_images[*].qa` populated, with a re-render when it fails;
  - the eval report has `gates` + `passing_threshold` and the judge saw images;
  - a rating can be saved and re-loaded;
  - the second run for the same brand gets a non-empty `brand_history` and a shortlist that avoids last run's styles.
- **Interactive:** checkpoint 1 shows the brief; an edit to the proposition flows into the copies; checkpoint 2 "revise" rewrites the copies once.
- **Evals:** `adk eval creative_agent …` (with PYTHONPATH) passes. Compare before and after on ~10 runs with human ratings through the new UI, since the judge alone isn't a valid ranker. Track the pass rate, gate failures, image QA re-render rate and latency (target ≤ +20% p50, mainly from re-renders under the 2 RPM image quota).
