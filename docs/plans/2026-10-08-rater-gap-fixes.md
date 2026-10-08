# Rater-Gap Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` (or `subagent-driven-development`) to implement this plan task-by-task. Save a copy as `docs/plans/2026-10-08-rater-gap-fixes.md` in the first PR.

**Goal:** close the gaps the 2026-10-08 run audit found. The judge passed 16/16 creatives that the user failed 8/16 of. Seven findings, numbered 1–7 below.

**Architecture:** no new pipeline stages. The work tightens existing pieces:
- the judge's gate set and wording (`creative_eval/`);
- image QA fields (`image_qa.py`);
- deterministic cue and claim lexicons (`concept_guard.py`, `brief_check.py`, `copy_gate.py`);
- prompt rules;
- removal of a wasteful gallery upscale (`gcs_tools.py`).

Every change follows the existing pattern: keyword-only params, defaults unchanged where possible, pure functions tested first.

**Tech Stack:** Python 3.13, ADK 2.10 Workflows, google-genai judge (`gemini-3.1-pro-preview`), Pillow, pytest, Next.js mirror for gate labels, Vitest.

---

## Context

Audit of runs 2945244376936218624 and 4254314124873302016 (PRS SE CE24 × "hurricane tracker"). Raw data is in `/home/user/run_audit/`.

**What the user failed that the pipeline passed:**
- trend unclear ×5
- artifacts ×3, including a backwards guitar
- off-brief ×4

Kappa is 0 because the judge never fails anything.

**Facts from exploration** (these correct the earlier summary):
- **`trend_motif_visible` already blocks** (`creative_eval/dimensions.py:36-45`; only `brand_cue_present` is advisory). The problem is its wording, "is clearly visible" (`creative_eval/prompts.py:154`), which passes any motif that is present. It needs stricter wording, not a new gate.
- **The judge prompt never mentions artifacts or anatomy** (`creative_eval/prompts.py:150-163`, "Return all 5 gates" hard-coded at :162 and :66).
- **Image QA's `artifacts` field is "Severe anatomy or object deformities"** (`image_qa.py:101`, instruction :385). It has no product geometry or orientation field.
- **`_TEXT_CUE` lacks quote/phrase/wording/inscription/displays/painted/neon** (`concept_guard.py:117-123`, window 6), so `sits the exact quote "…"` isn't recognised as in-image text. That turns off the QA exact-text check too (`image_qa.expected_text`, :122-129).
- **The upscale:** `_get_high_res_img` (`gcs_tools.py:107`) is called only by `save_creative_gallery_html` (`tools.py:176-189`), one concept at a time. It produces a 1.5× Lanczos copy under `resized/XL_local_*` that only the gallery lightbox reads (`gallery_template.py:463`, `data-high-res-src`). Nothing else consumes it.
- **`check_brief`** (`brief_check.py:211`) never checks the product name, claims or risks.
  - `CreativeBrief.trend_bridge.risks` reaches the copy only via the brief Markdown's display row (`brief_render.py:101`).
  - The judge's brief view omits risks (`creative_eval/brief.py:76`).
  - The copy gate never reads risks (`copy_gate.py:383/437`).
- **Minor findings:**
  - `_rating_delta` records the *configured* effects (`brand_history.py:396,422`), so "styles" is listed even when no style changed. `learning_flags` (`creative_eval/agent.py:141-163`) and the UI (`rating-learning.ts`) are already conditional.
  - The brand-history note says "avoid repeating the recent styles" (`brand_history.py:338-339`), but `pick_style_shortlist` tops up from excluded families to keep the 2/3/1 quotas (`style_shortlist.py:117-126`).
  - `mentions("PRS SE CE24", "SE CE24 Electric Guitar", brand="PRS")` is False: it needs 3 of 4 tokens (`text_match.py:234`). That causes a redundant product append (`concept_guard.py:301-304`).

## PR grouping (recommended order)

| PR | Branch | Findings | Why grouped | Deploy |
|---|---|---|---|---|
| **1** | `perf/gallery-and-learning-polish` | 4 + 7 | small, low-risk, no judge change; ships the ~29 s/run win first | engines (creative + interactive) + api |
| **2** | `feat/catch-rater-failures` | 1, 2, 3 + the judge half of 6 | **every judge change in one PR**, so pass rates break comparability once (as with the gates PR); image QA + text cue catch the same failures before the judge | engines + api + web (gate labels) |
| **3** | `feat/truthful-sensitive-briefs` | 5 + the upstream half of 6 | prevention at brief/copy time (deterministic checks + prompt rules), measured by PR 2's stricter judge | engines + api |

Each PR is gated on `uv run ruff format --check . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 4`, plus `npm run lint && npm test && npm run build` for frontend changes.
- **Merge only after every GitHub check passes.**
- No Co-Authored-By trailers or AI attribution.
- Deploy with the env-unset prefix, smoke test, delete the old engines by numeric ID, and pin api/web traffic.
- No BigQuery migrations are needed: gates live in the report JSON, and `creative_evals.gates_pass_rate` is unchanged.

---

## PR 1 — Gallery without upscale + learning/style/product polish

### Task 1.1: Drop the gallery upscale (finding 4)

**Files:**
- Modify: `creative_agent/tools.py:176-189` (remove the `_get_high_res_img` loop; `HIGH_RES_URL = AUTH_GCS_URL`).
- Modify: `creative_agent/gcs_tools.py:107-160` (delete `_get_high_res_img`).
- Modify: `creative_agent/gallery_template.py:463` (the lightbox uses `image.src` when `data-high-res-src` is absent; keep the attribute equal to the src for template stability).
- Tests: `tests/test_export_concurrency.py` (delete `test_get_high_res_img_isolates_concurrent_runs`), and remove the stubs in `tests/test_image_qa.py:633` and `tests/test_brief_render.py:197`.
- Check `docs/examples/capture_examples.py` for references.

**Step 1: Failing test** (`tests/test_gallery_html.py`, or next to the existing gallery tests):
```python
def test_gallery_links_original_images_without_upscale(monkeypatch, gallery_state):
    uploads = []
    monkeypatch.setattr(
        gcs_tools, "_upload_blob", lambda *a, **k: uploads.append(a)
    )  # name per file
    html = render_gallery(gallery_state)
    assert "resized/XL_local_" not in html
    assert html.count('data-high-res-src="https://storage.mtls.cloud.google.com/') == 4
    assert not any("XL_local_" in str(u) for u in uploads)
```
Adapt the fixture names to the existing gallery tests (grep `save_creative_gallery_html` in `tests/`).

**Steps 2–5:** run it (it fails), implement, run it (passes), then `git commit -m "perf(creative_agent): link original 2K images in the gallery instead of re-uploading a 1.5x upscale"`.

### Task 1.2: Record only the effects that changed the run (finding 7a)

**Files:** `creative_agent/brand_history.py:396-422`; test `tests/test_brand_history.py`.

**Step 1: Failing test**
```python
def test_applied_effects_list_only_what_changed(...):
    # enough ratings, but no style reaches RATING_STYLE_MIN → no style lists
    d = run_delta(state={"brand": "PRS", "learn_from_ratings": True}, ratings=ROWS_NO_STYLE_SIGNAL)
    assert "styles" not in d["rating_signals_applied"]["effects"]
    assert d["rating_signals_applied"]["styles_excluded"] == []
```

**Step 3:** after computing `excluded`/`preferred`/`signals`/`strictness`, set `effects` to the subset that produced output:
- `guidance` if the note is non-empty;
- `styles` if either list is non-empty;
- `checks` if strictness is non-empty.

Keep `no_effects` only for the configured-empty case.

Commit: `fix(creative_agent): rating_signals_applied lists only effects that changed the run`.

### Task 1.3: Note wording matches the shortlist fallback (finding 7b)

**Files:** `creative_agent/brand_history.py:338-339`; test `tests/test_brand_history.py`.

**Steps:**
1. **Failing test:** the note ends with `"favour styles not used recently"` and does not contain `"avoid repeating"`.
2. **Implement:** change the tail to `" Build on what worked, fix the weaknesses, and favour styles not used recently."`
3. **Commit:** `fix(creative_agent): brand-history note no longer forbids styles the shortlist may still offer`.

### Task 1.4: Product named by brand + model tokens (finding 7c)

**Files:** `creative_agent/text_match.py:234` (`mentions`); tests in `tests/test_concept_guard.py`, next to :654-677 (and `tests/test_text_match.py` if present).

**Step 1: Failing tests**
```python
def test_brand_plus_model_tokens_count_as_mention():
    assert mentions("A PRS SE CE24 on a stage", "SE CE24 Electric Guitar", brand="PRS")


def test_model_tokens_without_brand_still_need_ratio():
    assert not mentions("A CE24 on a stage", "SE CE24 Electric Guitar", brand="PRS")
```

**Step 3:** add a distinctive-token rule. A token is distinctive if, in the original phrase, it contains a digit or is all-caps with length ≥ 2. The rule returns True when:
- the phrase has at least one distinctive token;
- every distinctive token is hit;
- a brand token is hit, if a brand is given.

The existing ratio logic stays the fallback. Run the whole `test_concept_guard.py` + `test_copy_gate.py` suite to check for regressions.

Commit: `fix(creative_agent): brand + model number counts as naming the product`.

**PR 1 gate**, then open the PR. Merge after CI, then deploy the engines + api and run the smoke test. Expect finalize persist to drop from ~34 s to ~5 s.

---

## PR 2 — Catch what raters fail (judge + image QA + text cue)

### Task 2.1: New blocking visual gate `no_visual_defects` (finding 1)

**Files:**
- `creative_eval/dimensions.py`: add to `VISUAL_GATES` and to `GATE_LABELS` ("No visual defects").
- `creative_eval/prompts.py:150-163`: the gate text, and "Return all 6 gates".
- `frontend/src/lib/eval-dimensions.ts` (label mirror).
- `docs/notes/creative-quality-gates.md`.
- Tests: `tests/test_eval_gates.py` (`TestPromptsListGates`, `TestGateDefaults`, `TestEvaluateVisualGates`), `tests/test_eval_dimensions.py` (drift), `frontend/src/__tests__/eval-checks.test.tsx`.

**Gate text:**
```
- **no_visual_defects**: The image has no visible rendering defects: the product is physically correct
  (right shape, parts in the right place and orientation, not mirrored, duplicated, melted or merged),
  and people, hands and objects have normal anatomy and geometry. Fail on a defect a viewer would notice;
  ignore deliberate stylisation (cartoon, comic, collage).
```
List it with the violation checks (:160).

**Step 1: Failing tests**
- `"no_visual_defects" in VISUAL_GATES`;
- `"**no_visual_defects**" in VISUAL_JUDGE_PROMPT`;
- `"Return all 6 gates"`;
- a visual judge output with `no_visual_defects: passed=False` → `passed is False`;
- the frontend drift test passes once the label is added.

**Steps 2–5:** run (fails), implement, run (passes), commit `feat(creative_eval): blocking no_visual_defects gate`.

### Task 2.2: Stricter `trend_motif_visible` wording (finding 1)

**Files:** `creative_eval/prompts.py:154`; tests in `tests/test_eval_gates.py` (`TestConservativeGateWording`).

**New text:**
```
- **trend_motif_visible**: The trend motif is prominent and recognisable at a glance as a reference to
  "{trend}" by someone who knows the trend, without reading any caption or copy. A motif that is tiny,
  hidden in the background, generic (e.g. any storm or any phone screen) or only implied fails.
```

Commit: `feat(creative_eval): trend motif must be recognisable at a glance`.

### Task 2.3: Judge sees trend risks; new blocking gate `trend_risks_respected` (finding 6, judge half)

**Files:**
- `creative_eval/brief.py:76` (`format_brief_for_judge`): render `Trend risks: …` from `trend_bridge.risks`, brace-escaped like the other fields.
- `creative_eval/dimensions.py`: add `trend_risks_respected` to **both** `AD_COPY_GATES` and `VISUAL_GATES`, to `BRIEF_GATES` (no brief → pass "no brief"), and to `GATE_LABELS` ("Respects trend risks").
- `creative_eval/prompts.py`: the gate text in both blocks; the counts become 6 (copy) and 7 (visual).
- `frontend/src/lib/eval-dimensions.ts`.

**Gate text:**
```
- **trend_risks_respected**: The creative does not make light of, exploit or sensationalise the harms named
  in the brief's trend risks (for a real disaster or tragedy: no jokes about its severity, victims, damage
  or official warnings). Empty trend risks → pass.
```

**Step 1: Failing tests**
- the brief text contains the risks;
- the gate is in both prompts and in `BRIEF_GATES`;
- with no brief it passes with "no brief";
- a failed gate blocks `passed`;
- `normalize_gates` treats an unreported gate as "not checked".

Commit: `feat(creative_eval): trend risks reach the judge as a blocking gate`.

### Task 2.4: Image QA flags a malformed product (finding 2)

**Files:**
- `creative_agent/image_qa.py`:
  - `ImageQAResult` gets `product_malformed: bool = False` with the description "The product's shape is wrong: mirrored or reversed, parts missing, duplicated or in the wrong place, warped or melted.";
  - a `_RULES` entry `("product_malformed", "malformed product")`;
  - an instruction line next to :385;
  - a `_TEXT_RULES`-style correction phrase "Render the product with its correct real-world shape and orientation.";
  - and soften the `artifacts` description to "Noticeable anatomy or object deformities…".
- Tests: `tests/test_image_qa.py` (rule table :74-78, correction text, schema default for old payloads).

**Step 1: Failing tests**
- `qa_failed_rules(ImageQAResult(..., product_malformed=True))` contains `"malformed product"`;
- the instruction mentions "mirrored";
- `correction_text` includes the orientation phrase;
- a payload without the field parses with the field False.

Not critical: it counts toward the total only, so `_keep_retry` ranking is unchanged.

Commit: `feat(creative_agent): image QA re-renders malformed or mirrored products`.

### Task 2.5: More in-image text cues (finding 3)

**Files:** `creative_agent/concept_guard.py:117-123`; `tests/test_concept_guard.py` (next to :616 `test_wider_cue_window_and_new_cue_words`).

**Step 1: Failing test**
```python
def test_quote_style_cues_detect_in_image_text():
    p = 'A neon sign above which sits the exact quote "No Setlist, No Problem".'
    assert in_image_quotes(p) == ["No Setlist, No Problem"]
    assert in_image_quotes('a mural bearing the phrase "Ride It Out"') == [
        "Ride It Out"
    ]
```

**Step 3:** add `quote|quoted|quotes|phrase|wording|inscription|inscribed|displays|displaying|bearing|painted|neon` to `_TEXT_CUE`.

**Step 4:** run the full concept_guard, image_qa and copy_gate suites. Watch for false positives on prompts that merely quote style descriptors; fix by adjusting the test fixtures only if a hit is genuinely text.

Commit: `fix(creative_agent): recognise quote/phrase/inscription cues for in-image text`.

### Task 2.6: Docs + comparability note

Update:
- `CLAUDE.md` / `GEMINI.md` gate lists (+ image QA rule list);
- `docs/notes/creative-quality-gates.md`;
- `docs/notes/judge-calibration.md`: "judge gates changed 2026-10-08; compare calibration only within a judge version".
- `tests/README.md` if test files were added.

Commit: `docs: stricter judge gates and image QA`.

**PR 2 gate** (Python + frontend). Merge after CI, then deploy the engines + api + web.

**Live check:** re-run the PRS × hurricane tracker brief. Expect the judge to fail at least some of the creatives the user failed, and the run to show the image-QA malformed rule on a mirrored product if one occurs.

---

## PR 3 — Truthful, sensitive briefs and copy

### Task 3.1: Brief names the product exactly (finding 5)

**Files:** `creative_agent/brief_check.py:211` (`check_brief`, which already receives `target_product`); `tests/test_brief_check.py`.

**Step 1: Failing tests**
```python
def test_brief_must_name_product_exactly_in_mandatories():
    b = brief_with(mandatories=["Feature the SE CE 24 Standard Satin"])
    assert any(
        "exactly as" in i
        for i in check_brief(b, target_product="SE CE24 Electric Guitar", **KW)
    )


def test_exact_product_in_mandatories_passes():
    b = brief_with(mandatories=["Name the SE CE24 Electric Guitar"])
    assert not any(
        "exactly as" in i
        for i in check_brief(b, target_product="SE CE24 Electric Guitar", **KW)
    )
```

**Step 3:** pass when some mandatory `contains_phrase(m, target_product)`, or when `text_match.mentions(m, target_product, brand=brand)` holds after Task 1.4. Otherwise add the issue `Name the product exactly as "{target_product}" in mandatories; do not add a variant, finish or model the user did not give.` The existing gate routes it to the reviser (`agent.py brief_gate_decision`).

Commit: `feat(creative_agent): brief gate requires the exact product name`.

### Task 3.2: Absolute-claim lexicon in brief + copy gates (finding 5)

**Files:**
- Create `creative_agent/claims.py`:
  - `ABSOLUTE_CLAIM_TERMS = ("guarantee", "guaranteed", "indestructible", "unbreakable", "bulletproof", "risk-free", "100%", "100 percent", "never fails", "never goes out of tune", "lifetime")`;
  - `unsupported_claims(text, *, allowed_text: str) -> list[str]`: a term counts only if it isn't in `allowed_text`, i.e. the user's key selling points + mandatories.
- Use it in `brief_check.check_brief` (proposition + RTB claims; new kwarg `key_selling_points=""`, wired from state in `agent.py` where `check_brief` is called).
- Use it in `copy_gate._deterministic_issues` (headline, body, caption, CTA; new kwarg `allowed_claims_text=""`, wired in `agent._copy_issues`).
- Tests: `tests/test_claims.py`, `tests/test_brief_check.py`, `tests/test_copy_gate.py`.

**Step 1: Failing tests**
- `unsupported_claims("Guaranteed to stay in tune", allowed_text="")` returns `["guaranteed"]`;
- with `allowed_text="lifetime warranty"` the term "lifetime" is allowed;
- the copy gate flags "truly indestructible" deterministically;
- the brief gate flags the RTB claim.

Word-boundary matching uses the `contains_phrase` helper from `copy_gate`/`text_match`.

Commit: `feat(creative_agent): flag unsupported absolute claims in briefs and copy`.

### Task 3.3: Prompt rules (findings 5 + 6, upstream)

**Files:** `creative_agent/prompts.py`:
- **Brief writer (`CREATIVE_BRIEF_WRITER_INSTR` ~:320):** "Use the product name exactly as given ({target_product}); never add a variant, finish, colour or model the user did not give. No guarantees or absolute claims unless a cited source or the user's selling points state them. If the trend is a real disaster, tragedy or crisis, choose fit_mode light_touch, list its harms in trend_bridge.risks, and never build the idea on the harm itself."
- **Ad copy drafter + critic:** "Respect the brief's trend risks: never joke about a real disaster's severity, victims, damage or warnings (e.g. storm categories, evacuation orders)." Add the critic `brief_checks` item `risks`, gating in `GATING_BRIEF_CHECKS` (`copy_gate.py`).
- **Art director:** same risks rule for visuals.

Keep `test_every_brace_is_a_state_token` passing.

**Step 1: Failing tests** (`tests/test_creative_brief_prompts.py`, `tests/test_ad_copy_prompts.py`, `tests/test_copy_gate.py`): the rules are present, and a failed `risks` self-check yields a self-reported gating issue.

Commit: `feat(creative_agent): brief, copy and art direction respect product name, claims and trend risks`.

### Task 3.4: Docs

Update `CLAUDE.md` / `GEMINI.md` (brief gate + copy gate check lists, `claims.py`) and `docs/notes/creative-quality-gates.md`. Commit `docs: truthful-brief and trend-risk checks`.

**PR 3 gate.** Merge after CI, then deploy the engines + api.

---

## Verification

1. **Unit:** each task's failing-then-passing tests, plus the full suite (`pytest tests/ -q -n 4`) and the frontend suite for PR 2.
2. **Regressions to watch:**
   - `test_concept_guard.py` (cue words, mentions);
   - `test_copy_gate.py` (claim false positives, e.g. "never miss" must not flag; only the listed terms);
   - `test_eval_gates.py` (gate counts, advisory set unchanged = `{"brand_cue_present"}`).
3. **Live, after each deploy:** `integration_test.py --check smoke --agent creative_agent` (asserts finalize outputs).
   - PR 1: persist ≈ 5 s (logs), and the gallery lightbox opens the 2K original.
   - PR 2: re-run the PRS × "hurricane tracker" brief and compare judge verdicts with the user's ratings. Expect pass rate < 100% and the `trend_risks_respected` / `no_visual_defects` notes to be sensible, not over-strict. If the judge fails > 60% of creatives, soften the wording before moving on.
   - PR 3: same brief. Expect `mandatories` to use "SE CE24 Electric Guitar", no "guaranteed/indestructible" in the brief or copy, fit_mode light_touch, and no storm-category jokes.
4. **Calibration:** after PR 2, rate ~20 new creatives and read the `/runs` judge-agreement line (kappa > 0 expected).
