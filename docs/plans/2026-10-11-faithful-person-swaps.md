# Faithful Person Swaps + Headshot Guidance Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (or `executing-plans`) to implement task-by-task. Save a copy as `docs/plans/2026-10-11-faithful-person-swaps.md` in PR 0.

**Goal:** personalised variants (person swaps) keep the original creative intact, with the same layout, text, arrows, props and product, and change only the person. Person registration also asks for a single high-quality headshot and enforces a minimum resolution.

**Architecture:** today a variant **re-generates the whole scene** from the concept's text prompt plus the person photo, so every detail is re-rolled. The new **edit mode** sends the original render plus the person photo with an edit instruction ("replace only the person"). That is one edit from the original, not an edit chain, so the drift warning in the research (F3) doesn't apply. Image QA gains a variant-only **composition check** against the original, which can trigger a re-render. Because the edit is derived from the original image, its URI is recorded on the new person's consent **and**, for cast originals, on the original person's consent, so revoking either deletes it. Registration enforces a short side ≥ 512 px, warns below 1024 px, and shows photo guidance.

**Tech Stack:** google-genai image editing (`gemini-nano-banana-2.1`, multi-image input), the image-QA vision model, FastAPI `runserver/`, Pillow, Next.js 16, pytest + Vitest.

---

## Context

**What the user saw (2026-10-11):** two swaps on run 1977362982702678016 had obvious errors compared with the originals. On "The Replay Booth Defense" the arrows point the wrong way, the "DEFENSIVE SHIELD" label is lost, and the brand name is duplicated. On "The Unnecessary Roughness Flag" there are 6 malformed penalty flags instead of 2, and the composition is busier. Fidelity and realism are lower in both.

**Diagnosis:**
- **Every variant is a fresh full render.** `runserver/variants.py` `render_variant` runs `creative_agent/render_concept.py` with the concept's text prompt (plus the hero and appearance lines) and the person photo. Every scene detail is sampled again; the original was simply a good sample. Nothing about a "second iteration" degrades the model; the variant just doesn't use the first image at all.
- **Image QA can't see the drift.** Image QA passed all three swaps on attempt 1 (no failures recorded). It checks product, motif, text, logos, artifacts, unsafe content and likeness, but never compares the variant with the original, so arrow direction, prop count and missing labels go unchecked.
- **The reference photo was very small.** Both bad swaps used the "Jupiter" photo, which is only 240×240 px. A low-resolution reference likely lowers realism, and registration has no resolution check (`runserver/person_refs.py` only checks the content type and ≤ 10 MB).

**User decisions (2026-10-11):**
- **Swap method:** **edit the original.** No fresh-render fallback; a blocked or failed edit is reported as rejected or failed.
- **Person photos:** **keep one headshot**, but enforce a minimum resolution and show guidance.
- **Calibration:** use the existing photos (600 px "JTott", 240 px "Jupiter").

**Answer to the user's reference-sheet question** (recorded in docs, Task 2.4):
- **Don't use character turnaround sheets as the person photo.** The models take one reference image as one picture, so a multi-view sheet risks:
  - copying the sheet's multi-figure layout or its "FRONT"/"SIDE" labels into the ad;
  - diluting the face, since each view is small;
  - confusing which figure is "the person".
- **What works better:** a single clear front headshot gives the best likeness for ad heroes. The documented multi-image character consistency works best with separate images, which we deliberately don't support in v1. So the guidance asks for one headshot and explicitly says "not a collage or character sheet".

**Code anchors:**
- `runserver/variants.py`:
  - `render_variant` (filters `person-refs/` / `variants/` references at :331-334; calls `render_concept`);
  - `variant_concept` (hero line + `VARIANT_APPEARANCE_LINE`), `variant_key` (`VARIANT_KEY_VERSION="v2"`);
  - `castability` (`mode` swap/add, `cast_consent_id`), `_create_variant`;
  - `person_refs.record_renders` called before upload.
- `creative_agent/render_concept.py`: `render_contents` (:73-99), `render_concept`; `_render_image` (`image_tools.py:395-410`) and the person `ImageConfig` with `ALLOW_ADULT` (`person_render.py`).
- `creative_agent/image_qa.py`: `inspect_image(..., person_image=)` (:423), `ImageQAResult`, `_RULES`, `correction_text`.
- `runserver/person_refs.py`: the `POST /person-refs/{user}` validation (HEAD content type/size); `frontend/src/app/people/page.tsx`.

## PR grouping

| PR | Branch | Scope | Gate / deploy |
|---|---|---|---|
| **0** | `spike/swap-edit-mode` | comparison script + note: re-generate vs edit, on the 3 existing swap concepts | **go/no-go** for edit mode |
| **1** | `feat/variants-edit-mode` | edit-mode variants, composition check, consent recorded on both consents, cache key v3 | api + web |
| **2** | `feat/person-photo-quality` | resolution check at registration, guidance, low-res badge, docs | api + web |

**Every PR:**
- **Gates:** Python `unset VIRTUAL_ENV; uv run ruff format --check . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 4`; frontend `npm run lint && npm test && npm run build`.
- **Merge only after every GitHub check passes**, then confirm `MERGED` before cleanup.
- **No Co-Authored-By trailer and no AI attribution.**
- **Deploys:** pin api and web traffic, and check for in-flight runs before an api deploy.
- **Engines:** unchanged (variants and registration run in the api).

---

## PR 0 — Spike: edit vs re-generate (gate)

### Task 0.1: Comparison script

**Files:** create `experiments/person_reference/swap_compare.py`; test `tests/test_swap_compare.py` (pure helpers only). Results go under the gitignored `experiments/person_reference/results/swap/`.

**Design:** inputs are `--session <id> --concepts <names…> --photos <gs://…> --n 2`. For each (concept, photo, mode ∈ {regenerate, edit}), render N times:
- **regenerate:** today's `render_variant` path (reuse `runserver.variants.variant_concept` + `creative_agent.render_concept.render_concept` with QA off).
- **edit:** the new `edit_contents(base_image, person_photo, concept)` from Task 1.1. The spike may inline a prototype and PR 1 then moves it.

Score each output with one vision call (image-QA model, temperature 0) given the original and the variant:

```json
{"same_layout": bool, "same_text": bool, "same_graphics": bool, "same_product": bool, "only_person_changed": bool, "differences": [str]}
```

Also record likeness with the existing person comparison. Write a CSV, then `summary.md` with rates per mode and photo, plus the go/no-go.

**Step 1: Failing tests:**
- `parse_comparison(json)` tolerates missing keys;
- `summarise(rows)` computes per-mode `fidelity_rate` (all `same_*` true) and `likeness_rate`;
- `decision(summary)` returns go when edit `fidelity_rate` ≥ regenerate + 0.3 and edit likeness ≥ 0.8.

**Steps 2–5:** run (fails), implement, run (passes), commit `feat(experiments): swap edit-vs-regenerate comparison`.

### Task 0.2: Run + record

- **Run:** session 1977362982702678016 ("The Replay Booth Defense", "The Unnecessary Roughness Flag") and session 7850757285700698112 ("Tailgate Sideline Pit Stop"), with photos JTott (600 px) and Jupiter (240 px), N=2. That's 3 × 2 × 2 × 2 = 24 renders, about 13 min at 2/min.
- **Look:** spot-check one pair per mode by eye.
- **Write:** `docs/notes/person-swap-edit-mode.md` with fidelity and likeness per mode and photo, the resolution observation (240 vs 600 px), and the decision.
- **Clean up:** delete the local renders (real people). Commit only the summary.
- **No-go** (edit isn't clearly better, or its likeness < 0.8): stop and report to the user before PR 1.

**Commit:** `docs: person swap edit-mode calibration`.

---

## PR 1 — Edit-mode variants

### Task 1.1: `edit_contents` + edit render

**Files:**
- Create `creative_agent/person_edit.py` (pure, brace-free text constants).
- Modify `creative_agent/render_concept.py`: add `render_edit(base_image, person, concept, *, aspect_ratio, strictness, qa, brand, target_product, budget, original_for_qa)`, reusing `_render_image`, the person `ImageConfig` (`ALLOW_ADULT`), `person_block_reason`, and the QA/re-render loop.
- Test: `tests/test_person_edit.py`, `tests/test_render_concept.py`.

**Contents** (order matters):
```
[text: EDIT_INSTRUCTION + concept guard lines (strictness/no-other-logos) + REFERENCE_IGNORE_TEXT_LINE,
 "Image 1 (original ad):", <base image part>,
 "Image 2 (person):",      <person photo part>]
```

**`EDIT_INSTRUCTION`:**
> Edit image 1. Replace only the main person with the person in image 2: keep their face shape, skin tone, hair, eye colour, apparent age and distinguishing features. Keep everything else in image 1 exactly as it is: the composition and framing, camera angle, lighting, colours, background, every prop and its count, all arrows and graphic elements and their direction, every word of text, logos and the product. Match image 1's art style. Do not add, remove or move anything else.

The aspect ratio is the original image's (read from the base PNG's size and mapped to the nearest allowed ratio, or the concept's `aspect_ratio`).

**Step 1: Failing tests:**
- `edit_contents` puts the base image before the person photo, each with its label;
- the instruction contains "Keep everything else" and no `{`;
- `render_edit` passes the person image config (`ALLOW_ADULT`);
- a blocked edit returns `rejected_reason` (no fallback);
- a QA re-render repeats the edit with `correction_text` appended.

Use a fake genai client like `tests/test_render_concept.py`.

**Commit:** `feat(creative_agent): person edit render (replace only the person)`.

### Task 1.2: Composition check in image QA (variants only)

**Files:** `creative_agent/image_qa.py`; tests `tests/test_image_qa.py`.

**Changes:**
- `ImageQAResult` gets `composition_changed: bool = False` and `composition_differences: list[str] = []` (old payloads still parse).
- `inspect_image(..., original_image: tuple[bytes, str] | None = None)`. When it's given, attach it as "Original ad (before the person was replaced)" and add the instruction: "Apart from the person, does the edited image keep the original's layout, text, graphic elements (count and direction), props and product? Set composition_changed and list concrete differences."
- New rule `("composition_changed", "composition changed")`, not critical. `correction_text` for it restates "keep everything except the person exactly as in image 1" plus the listed differences, capped and brace-free.

**Step 1: Failing tests:**
- the rule fires only when `original_image` is given and `composition_changed` is true;
- the instruction text appears only with `original_image`;
- old payloads parse;
- the correction includes the differences.

**Commit:** `feat(image_qa): composition check against the original for person edits`.

### Task 1.3: Variants use edit mode

**Files:** `runserver/variants.py` (`render_variant`, `variant_key`, `_create_variant` record step); tests `tests/test_variants_api.py`.

**Changes:**
- **Base image:** `render_variant` loads the base render (`generated_images[c].gcs_uri`; must be in the bucket under the run's output folder, reusing the `shares.output_prefix`-style safe check) and calls `render_edit(..., original_for_qa=base)` instead of `render_concept`. This applies to both `mode="swap"` and `mode="add"`, since an uncast original's generic hero gets replaced the same way.
- **Prompt lines:** `variant_concept` keeps the hero and appearance lines only as QA context. The edit instruction carries the intent, and the concept text is passed so QA knows the motif and product.
- **Blocks:** a safety block → `rejected` (no regenerate fallback, per the user decision).
- **Consent:** record the variant URI on the new consent **and**, when `mode == "swap"` and `cast_consent_id` is set, on that consent too (`person_refs.record_renders` for both, same metadata check). The blob metadata gets `consent_id` = new consent and `source_consent_id` = cast consent. The revoke cascade (`delete_renders`) deletes a blob when **either** metadata id matches: update the match in `person_refs.delete_renders` and `delete_session_renders`, and test it.
- **Cache:** `VARIANT_KEY_VERSION = "v3"` (the edit mode changes what's rendered).
- **Base gone:** if the base image is missing (e.g. the original consent was revoked), return 409 `concept_not_ready` with the reason "Original image is no longer available".

**Step 1: Failing tests:**
- the renderer receives the base image bytes;
- the swap records both consents;
- revoking the cast consent deletes the variant (fake GCS metadata `source_consent_id`);
- the v3 key differs from v2;
- a missing base gives 409;
- a blocked edit gives `rejected`.

**Commit:** `feat(variants): swaps edit the original image; revoke of either person deletes`.

### Task 1.4: Docs

Update:
- `CLAUDE.md` / `GEMINI.md`, Personalised variants: edit mode, composition check, two-consent recording, v3 key, no fallback;
- `docs/notes/creative-quality-gates.md`: the composition rule.

**Commit:** `docs: edit-mode person swaps`.

**After merge:** deploy the api (no traffic, verify via the tag, pin after in-flight runs finish), then web (pin).

---

## PR 2 — Headshot quality at registration

### Task 2.1: Resolution check

**Files:** `runserver/person_refs.py` (POST validation), `runserver/person_refs_store.py` + `deployment/bq_schemas/person_references.json` (new nullable `photo_min_side INT64`; ALTER migration in the README); tests `tests/test_person_refs_api.py`, `tests/test_person_refs_store.py`.

**Changes:**
- After the existing HEAD checks, download the photo (≤ 10 MB, already enforced) and open it with Pillow in a thread.
- Reject when the short side < `PERSON_PHOTO_MIN_SIDE` (env, default 512): 400 `photo_too_small` "Use a photo at least 512 px on its shorter side (1024 px or more recommended)."
- Store `photo_min_side`. The response and the GET include `photo_min_side` and `low_resolution: photo_min_side < 1024`; records without a value read `null` / `false`.

**Step 1: Failing tests:** 300×300 → 400; 800×600 → ok with `low_resolution: true`; 1500×1200 → ok with `low_resolution: false`; an unreadable image → `photo_unreadable`.

**Migration:** `ALTER TABLE …person_references ADD COLUMN IF NOT EXISTS photo_min_side INT64`, run before the api deploy.

**Commit:** `feat(person): minimum photo resolution at registration`.

### Task 2.2: Photo guidance + low-res badge on /people

**Files:** `frontend/src/app/people/page.tsx`, `frontend/src/lib/person-refs.ts` (reason message for `photo_too_small`, new fields); tests `frontend/src/__tests__/people-page.test.tsx`.

**Guidance** (above the form, a muted list):
- one person, a front-facing head-and-shoulders photo, face filling about a third of the frame;
- even light, neutral background, no sunglasses, hats or heavy filters;
- at least 1024 px on the shorter side;
- not a collage, character sheet or group photo, and no text on the image.

**List:** people with `low_resolution` show a muted "Low resolution: previews may look softer. Re-register with a 1024 px photo." Existing records (no value) show nothing.

**Step 1: Failing tests:** the guidance is rendered; the badge shows only for `low_resolution: true`; the `photo_too_small` message.

**Commit:** `feat(frontend): headshot guidance and low-resolution notice`.

### Task 2.3: Backfill notice for existing people

The api's GET computes `photo_min_side` lazily for records that lack it: a single download + size read, cached by updating the row. That way the existing 240 px "Jupiter" record shows the low-resolution badge.

**Tests:** a lazy fill on GET; a store error is non-fatal.

**Commit:** `feat(person): backfill photo size for existing people`.

### Task 2.4: Docs

Update:
- `docs/notes/person-reference-calibration.md`: a "Photo guidance" section, including why character sheets aren't recommended (see Context);
- `deployment/README.md`: the ALTER and `PERSON_PHOTO_MIN_SIDE`;
- `CLAUDE.md` / `GEMINI.md`.

**Commit:** `docs: person photo guidance`.

**After merge:** run the ALTER, then deploy the api (pin) and web (pin). Tell the user to re-register Jupiter with a ≥ 1024 px photo.

---

## Verification
1. **Unit:** each task's tests plus both full suites in CI.
2. **Spike (PR 0):** the note shows edit fidelity clearly above regenerate, with likeness ≥ 0.8.
3. **Live, after PR 1:**
   - Re-run the user's two swaps on run 1977362982702678016 (the new v3 key renders fresh).
   - Expect, by eye and per the composition check: arrows still trace the cheek and point at the bottle, the "DEFENSIVE SHIELD" label is kept, the brand appears once, and there are 2 flags, with only the person changed.
   - Revoke a throwaway consent used as the cast person and confirm the derived variant is deleted.
4. **Live, after PR 2:**
   - Registering a 300 px photo fails with the size message.
   - The Jupiter record shows the low-resolution badge.
   - The guidance shows on /people.
5. **Regression:** runs without a person are unchanged (no engine change), and older variants still display.
