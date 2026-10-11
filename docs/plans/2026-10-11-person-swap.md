# Person Swap for Cast Creatives + Share Feed Layout Implementation Plan

Two independent PRs:
- **Part A** (`feat/person-swap`, Tasks 1–5): person swap.
- **Part B** (`fix/share-feed-layout`, Tasks 6–7): the share page's Feed tab layout.

> **For Claude:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (or `executing-plans`) to implement this plan task-by-task. Save a copy as `docs/plans/2026-10-11-person-swap.md` in the PR.

**Goal:** let the Personalise panel re-render a creative that already features a cast person with a *different* consented person (a "person swap"). The same person is refused, and every variant render is told to follow the reference photo over any appearance the prompt describes.

**Architecture:** api and web only; the engines are unchanged, because variants render inside the api through `runserver/variants.py` + `creative_agent.render_concept`.
- **`castability`:** stops refusing cast concepts and reports `mode: "swap"` plus the base's cast consent id.
- **The POST:** refuses the cast consent itself.
- **`variant_concept`:** adds an appearance-override line, and the cache key gets a version salt.
- **The panel:** reads "Swap person" for cast creatives and leaves the already-cast person out of the picker.

**Tech Stack:** FastAPI (`runserver/`), pytest, Next.js 16 + Vitest/RTL.

---

## Context

Today `castability` (`runserver/variants.py:302-310`) refuses any concept with `generated_images[c].cast is True` (400 `concept_already_cast`), and the frontend hides the panel for cast proofs (`lib/variants.ts:76-82` `canPersonalise`).

The user asked why, and agreed to lift the restriction. The analysis behind that:
- **No likeness of the original person carries over.** A variant is a fresh render from the concept's text prompt plus only the newly chosen consent's photo. `render_variant` filters any `person-refs/` / `variants/` URI out of the references (`variants.py:331-334`), and `resolve_references` never reads `state["person_reference"]`. The casting rules also forbid describing the person's face, age, ethnicity or body.
- **Consent stays clean:**
  - A variant is tied only to the new person's consent, and revoking that consent deletes it (existing cascade).
  - Revoking the cast person deletes only the base image; the panel's "Original" then fails to load.
  - Shares and experiments stay unchanged: variants are never shared or deployed.
- **Main quality risk:** prompt descriptors such as "a man in his 30s" may conflict with the new person. That already affects non-cast variants, hence the override line for **all** variants.
- **Cache:** the override line changes what is rendered, so the cache key needs a version salt. Otherwise an old cached variant would be returned for the new behaviour.

**Code anchors:**
- `variants.py`:
  - `castability` :268-328 (check order: concept → finished + base image → `is_cast` → `castable_reason`);
  - `is_cast` :165-169;
  - `variant_concept` :210-221 (idempotent hero line via `PERSON_HERO_PHRASE`);
  - `variant_key` :115-118 (called at :752 with the original prompt);
  - POST `_create_variant` :715-813 (consent resolved :735-742; `castability` after `_run_is_live`);
  - GET castable :827-832 (`public()` :258-265 → `{ok, reason, code, style}`).
- `generated_images[c]` carries `consent_id` only when the image was actually cast (`image_tools.py:919-924`).
- `creative_agent/concept_guard.py`: `PERSON_HERO_PHRASE` / `PERSON_HERO_LINE` :343-344; `enforce_person_casting` doesn't duplicate the line.
- Frontend:
  - `lib/variants.ts`: `canPersonalise` :76-82, `castabilityNote` :142-149, reason message for `concept_already_cast` :213;
  - `components/personalise-panel.tsx`: heading :171-173, button :209-216, base/variant pair :244-262;
  - `results/[sessionId]/page.tsx:327-328`: `personaliseSlot` gating.
- Tests:
  - `tests/test_variants_api.py`: `test_concept_checks` :412 (asserts `concept_already_cast` at :423), `test_castability_helper_matches_the_post_checks` :732, `test_get_reports_castability_for_every_concept` :762, `test_variant_concept_casts_and_points_at_the_photo` :208;
  - `frontend/src/__tests__/personalise-panel.test.tsx`: "offers Personalise only for rendered, uncast creatives" :312.

**Branch:** `feat/person-swap`.

**Every task:**
- Python gate: `unset VIRTUAL_ENV; uv run ruff format --check . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 4`.
- Frontend gate: `cd frontend && npm run lint && npm test && npm run build`.
- Commits are conventional, with **no Co-Authored-By and no AI attribution**.
- Merge only after every GitHub check passes, and confirm `MERGED` before cleanup.

---

### Task 1: Backend castability allows cast concepts as "swap"

**Files:** modify `runserver/variants.py` (`castability`, `public()`, the dataclass it returns); test `tests/test_variants_api.py`.

**Step 1: Failing tests** (replace the `concept_already_cast` expectations):
```python
def test_castability_allows_a_cast_concept_as_swap(finished_state):
    state = with_cast(finished_state, "Stage Left", consent_id="consentA")
    c = castability(state, "Stage Left")
    assert c.ok and c.mode == "swap" and c.cast_consent_id == "consentA"
    assert c.public()["mode"] == "swap" and c.public()["cast_consent_id"] == "consentA"


def test_castability_uncast_concept_is_add(finished_state):
    c = castability(finished_state, "Stage Left")
    assert c.ok and c.mode == "add" and c.cast_consent_id == ""
```
Update `test_concept_checks` (:412), `test_castability_helper_matches_the_post_checks` (:732) and `test_get_reports_castability_for_every_concept` (:762): a cast concept is now `ok` with `mode == "swap"`. Reuse the existing fixtures; add a small `with_cast(state, name, consent_id)` helper if one doesn't exist.

**Step 2:** run `GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/test_variants_api.py -q`. Expected: FAIL.

**Step 3: Implement:**
- Remove the `is_cast` refusal from `castability`.
- Set `mode = "swap" if is_cast(state, name) else "add"` and `cast_consent_id = (generated_images[name].get("consent_id") or "") if swap else ""`.
- Still run `castable_reason`: the style/human checks apply, and a cast concept normally passes them.
- `public()` adds `mode` and `cast_consent_id`. These ids are the caller's own (the run owner passed the consent check at kick-off) and GET is owner-scoped.
- Keep the `already_cast` code constant only if something else uses it; otherwise delete it, and its frontend message too (Task 4).

**Step 4:** tests pass. **Step 5:** commit `feat(variants): personalise cast creatives as a person swap`.

### Task 2: Refuse swapping in the same person

**Files:** `runserver/variants.py` `_create_variant` (after `castability`, :715-813); test `tests/test_variants_api.py`.

**Step 1: Failing test**
```python
def test_post_refuses_the_person_already_cast(
    client, finished_state_cast_with_consentA
):
    r = post_variant(concept_name="Stage Left", consent_id="consentA")
    assert (
        r.status_code == 400 and r.json()["detail"]["reason"] == "same_person_as_cast"
    )


def test_post_allows_a_different_person_on_a_cast_concept(
    client, finished_state_cast_with_consentA
):
    r = post_variant(concept_name="Stage Left", consent_id="consentB")
    assert r.status_code == 200
```

**Step 3: Implement:** if `castability.mode == "swap"` and `consent_id == castability.cast_consent_id`, return 400 `same_person_as_cast` ("This creative already features this person."). The check runs before the cache lookup and the daily-cap count.

**Commit:** `feat(variants): refuse swapping in the person already cast`.

### Task 3: Appearance-override line for every variant + cache version salt

**Files:** `runserver/variants.py` (`variant_concept` :210-221, `variant_key` :115-118); tests `tests/test_variants_api.py` (`test_variant_concept_casts_and_points_at_the_photo` :208).

**Step 1: Failing tests**
```python
def test_variant_prompt_tells_the_model_the_photo_overrides_described_appearance():
    v = variant_concept({"image_generation_prompt": "A man in his 30s plays guitar."})
    assert VARIANT_APPEARANCE_LINE.strip() in v["image_generation_prompt"]
    # idempotent
    assert (
        variant_concept(v)["image_generation_prompt"].count(
            VARIANT_APPEARANCE_LINE.strip()
        )
        == 1
    )


def test_variant_key_is_versioned():
    assert variant_key("gs://b/p.jpg", "prompt", "m") != _unsalted_key(
        "gs://b/p.jpg", "prompt", "m"
    )
```

**Step 3: Implement:**
- Add `VARIANT_APPEARANCE_LINE = " Where this prompt describes the hero's appearance (gender, age, hair, build, skin tone), follow the person in the person reference image instead."`, brace-free and variants-only, so engine renders and `tests/fixtures/render_parity.json` are untouched.
- `variant_concept` appends it after the hero line when missing (case-insensitive check, like the hero phrase).
- `VARIANT_KEY_VERSION = "v2"`, mixed into `variant_key` (`sha256(f"{VARIANT_KEY_VERSION}|{photo_uri}|{prompt}|{model}")[:12]`).
- Old `person_variants` records keep their old keys and stay displayed. A new request renders fresh under the new key.

**Commit:** `feat(variants): photo overrides described appearance; versioned cache key`.

### Task 4: Frontend — swap mode in the panel

**Files:**
- `frontend/src/lib/variants.ts` (`canPersonalise` :76-82, the castability parse, reason messages :213);
- `frontend/src/components/personalise-panel.tsx`;
- `frontend/src/app/results/[sessionId]/page.tsx:327-328` (the gating still calls `canPersonalise`, now without the cast exclusion);
- tests `frontend/src/__tests__/personalise-panel.test.tsx`.

**Step 1: Failing tests:**
- `canPersonalise` is true for a rendered cast proof; replace the :312 test with "offers Personalise for rendered creatives, cast or not".
- With `castable[c] = {ok: true, mode: "swap", cast_consent_id: "A"}`, the heading reads "Swap person", the person picker doesn't list consent A, and the side-by-side labels read "Original" / "With <new label>".
- If consent A is the only registered person, the button is disabled with a note: "Register another person on the People page to swap."
- `same_person_as_cast` maps to "This creative already features this person."
- `mode: "add"` (or a missing `mode`, for an older api) keeps today's "Personalise" wording.

**Step 3: Implement:**
- Parse `mode` / `cast_consent_id`, tolerating their absence.
- Heading: `mode === "swap" ? "Swap person" : "Personalise"`.
- Filter the picker options.
- The muted label stays "Personalised preview — not used in experiments or share links".
- Drop the `concept_already_cast` message if Task 1 removed the code.
- Use the proof-room design system and keep the `aria-describedby` note pattern.

**Step 4:** `npx vitest run src/__tests__/personalise-panel.test.tsx`, then the full frontend gate.

**Commit:** `feat(frontend): swap the person in a cast creative`.

### Task 5: Docs

Update:
- `CLAUDE.md` and `GEMINI.md`, Personalised variants paragraph: cast concepts are now allowed as a swap, `same_person_as_cast`, `castable` `mode` / `cast_consent_id`, the appearance-override line, cache key version.
- The plan copy `docs/plans/2026-10-11-person-swap.md` (run `uv run ruff format` on it).
- `tests/README.md` if test names changed.

**Commit:** `docs: person swap for cast creatives`.

---

## Part B — Share page Feed layout (separate PR `fix/share-feed-layout`)

**Why:** the user reported that the public share page's **Feed** tab wastes the space under the creative.

**Today** (`frontend/src/components/share/feed-post.tsx`), under the image there is one muted bar that squeezes three things into a single row: the headline, the body (`text-xs` muted), and the CTA chip on the right. The caption only follows after that bar. Long body text wraps in a narrow column next to the CTA.

**Chosen layout** (user, 2026-10-11): **caption above, link bar below**, like Facebook/LinkedIn ads.

### Task 6: Feed layout — caption above, full-width link bar below

**Files:** modify `frontend/src/components/share/feed-post.tsx` (reuse `captionPreview` from `lib/share-views.ts` and `ShareImage`); tests in `frontend/src/__tests__/share-view.test.tsx`.

**Layout, top to bottom:**
1. **Header:** unchanged (avatar disc, brand, "Sponsored").
2. **Primary text:** `creative.caption`, `text-sm leading-relaxed`, truncated via `captionPreview` with the existing real "more" `button` (`aria-expanded`). No brand prefix: the header already names the brand.
3. **Image:** `ShareImage`, full width.
4. **Link bar** (full width, `border-t`, `bg-muted`, `px-3 py-3`), a two-column grid `grid-cols-[1fr_auto] items-center gap-3` that stacks to one column below ~360 px (`max-[360px]:grid-cols-1`):
   - **Left:** brand line (`text-xs text-muted-foreground`, e.g. the brand name); headline `h2` (`text-[15px] font-semibold leading-snug`, `line-clamp-2`); body as a description (`text-sm text-muted-foreground leading-snug`, `line-clamp-2`).
   - **Right:** CTA as a non-interactive styled `span` (`rounded-sm bg-card border px-4 py-2 text-sm font-semibold whitespace-nowrap`). When stacked, it goes full width and centred.
   - The `line-clamp` truncation is visual only: the full text stays in the DOM, and the Card view also shows it.
5. **Accessibility:** the heading id stays on the headline (`aria-labelledby` on the article). Reading order is header → primary text → image → headline → body → CTA. The image alt is unchanged.
6. **Design:** proof-room tokens, no new colours, `max-w-[470px]` kept.

**Step 1: Failing tests** (`share-view.test.tsx`, jsdom):
- In the feed view the caption text comes **before** the image in document order (`compareDocumentPosition`), and the headline/body/CTA come **after** it.
- The headline has the heading id.
- The body is no longer inside the same flex row as the CTA. Assert the link bar container has `data-slot="feed-link-bar"`, with the CTA in its own `data-slot="feed-cta"` cell.
- The "more" button still expands the caption; update the existing tests at :110/:125 to the new position.
- Card and Story views are unchanged; keep their existing tests green.

**Step 2:** `cd frontend && npx vitest run src/__tests__/share-view.test.tsx`. Expected: FAIL.

**Step 3:** implement the layout above.

**Step 4:** the tests, then the full frontend gate.

**Step 5: Visual check:** the `playwright-skill` against a local `SHARE_MODE=1 next start` with the share fixtures, or the live share service after deploy. Screenshot the feed view at 470 px and at 340 px, and check that the body uses the full width and the CTA doesn't squeeze the text.

**Commit:** `fix(share): feed view puts the caption above the image and a full-width link bar below`.

### Task 7: Docs

Update the `CLAUDE.md` / `GEMINI.md` public share page description (Feed: caption above the image, link bar with brand, headline, body and CTA below). **Commit:** `docs: share feed layout`.

---

## Deploy
- **Part A (person swap):** no engine change, so: `trend-trawler-api` (`--no-traffic`, verify via a tag, pin), then `trend-trawler-web` (pin; prev tag). Check for in-flight runs before the api deploy.
- **Part B (feed layout):** redeploy **`trend-trawler-share`**, the public viewer, from `./frontend` with its service account (`gcloud run deploy trend-trawler-share --source ./frontend --region us-central1 --service-account tt-share-sa@hybrid-vertex.iam.gserviceaccount.com`; env and public access preserved). Then web (the owner preview of `/s/*`; pin). Re-probe the lockdown afterwards: `/` and `/api/gcs` return 404, and an existing share returns 200.

## Verification
1. **Unit:** the task tests plus both full suites in CI.
2. **Live api check** (impersonated proxy token, as in earlier verifications):
   - `GET /variants/...` on a run that cast `jtott` returns `castable[c] = {ok: true, mode: "swap", cast_consent_id: <jtott consent>}` for the cast concept and `mode: "add"` for the others;
   - `POST` with the jtott consent → 400 `same_person_as_cast`;
   - `POST` with the jupe_dawg consent → 200.
3. **Live render spot-check** (user, in the UI):
   - On a cast creative, choose **Swap person** → the other person. Expect the variant to show the new person, with the scene and composition kept.
   - Personalise an uncast creative whose prompt describes the hero's age or build. Expect the photo's appearance to win.
   - A cross-gender check needs a consented photo of someone of another gender; skip it if none is registered (both current photos are adult men).
4. **Regression:** runs without a person are unchanged (no engine change), and existing variants still display.
5. **Feed layout (Part B):** open the existing PRS × wrestling share link (`/s/twLtiDmyz_qkRLrBp_migw?view=feed`) after the share-service deploy. The caption sits above the image, and under the image the full-width link bar shows the headline, a 2-line body and the CTA on the right. At a narrow width the CTA stacks below. Card and Story views are unchanged.
