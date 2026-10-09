# Person Reference Casting + Personalised Variants Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (or `executing-plans`) to implement this plan task-by-task. Save a copy as `docs/plans/2026-10-09-person-reference.md` in PR 0.

**Goal:** a user can register a consented photo of a person (by `gs://` URI) and use it in a creative run. The agents then *decide* per concept whether to cast that person as the hero. Any finished concept can also be re-rendered with a different consented photo, as a UI-only personalised variant that never reaches bandit traffic. Cast creatives can be shared publicly only when the person's consent covers it.

**Architecture:** this implements `PERSON_REFERENCE_RESEARCH.md` §4, and is gated on a calibration spike.
- **Consent:** a `person_references` consent registry in BigQuery, plus owner-scoped api routes, guards every use of a photo. Revoking cascades.
- **Casting:** the visual agents mark `casts_person_reference` with a reason, and a deterministic guard enforces the rules.
- **Render path:** person renders attach the photo as an extra reference part with `person_generation=ALLOW_ADULT`, using a typed fallback when a safety filter blocks the photo. Image QA gets a likeness check, and the judge gets a respectful-depiction gate.
- **Variants:** a new `/variants` api renders through a refactored pure `render_concept()`. Results go to a separate state key and GCS prefix, so the bandit and the results page never see them.

**Tech Stack:** Google ADK 2.10 graph Workflows, google-genai `generate_content` (Nano Banana 2.1 image model, image-QA vision model), FastAPI `runserver/`, BigQuery + GCS, Next.js 16 frontend, pytest + Vitest.

---

## Context

The user wants creatives to be able to feature a real person from their photo when a concept has a human hero, at the agent's discretion and not always. They also want to preview the same creative personalised with different people's photos, in the UI only and never in bandit traffic. The research (`PERSON_REFERENCE_RESEARCH.md`, verified findings F1–F4 and P1–P4) found this feasible on our current model path, with known likeness weaknesses and hard consent/safety constraints.

**User decisions (2026-10-09):**
- **Calibration:** spike first, as a **gate**. The user provides 2 or more consented adult test photos in GCS.
- **Consent:** a registry with revoke cascade.
- **Photo input:** a pasted `gs://` URI under our bucket's `person-refs/<user>/` prefix; no upload UI in v1.
- **Sharing:** cast creatives are shareable **only if the consent record has the public-share scope**. Personalised variants are never shareable.

**Facts from the code** (verified anchors):
- **Rendering** (`creative_agent/image_tools.py`):
  - `_render_image(contents, aspect_ratio)` at :395 builds `GenerateContentConfig(response_modalities=["IMAGE"], image_config=ImageConfig(aspect_ratio, image_size))` at :404-410. It only checks for an image part, never `finish_reason` / `prompt_feedback` (so blocks are invisible today).
  - `generate_image(tool_context)` at :623: references at :648-652, `contents_for` at :675-685, the render lock at :701-705, the concept loop at :733-750, results at :752-774. `_inspect_and_rerender` is at :515-620 and `_RerenderBudget` at :505.
- **Image QA:** `image_qa.inspect_image(image_bytes, mime, concept, *, brand, target_product, client, model, has_logo_reference=False, strictness=())` at :423 builds `[image Part, instruction Part]` at :442-453. `ImageQAResult` is at :59-126, `_RULES` at :167, `CRITICAL_RULES` at :196, `correction_text` at :261.
- **References:** `references.REFERENCE_ROLES = ("product","logo","style")` at :20. `reference_roles` is derived at `callbacks.py:115`, and `_set_initial_states` (:50) seeds optional keys with `setdefault` (:89-120).
- **Prompts** (`creative_agent/prompts.py`): `ART_DIRECTOR_INSTR` at :593-683, `VISUAL_CONCEPT_RULES` at :696-703, and the drafter, critic and finalizer at :705 / :824 / :913 (each has `{reference_roles?}`). The fixer at :1000 doesn't. `IMAGE_PROMPT_GUIDE` `<REFERENCE_IMAGES>` is at :68-74 and `<STYLE_PALETTE>` at :29-45.
- **Concept guard:** `concept_guard.ensure_trend_and_product(...)` at :266 and `concept_issues(...)` at :497. They're called from `agent._concept_issues` (:1065), `callbacks.ensure_trend_and_product_callback` (:256) and `recheck_concept_issues_callback` (:305). `style_shortlist.STYLE_GROUPS` (photographic / illustrated / graphic) is at :16-37, with `canonical_style` at :53.
- **Visual schema:** `schemas.VisualConceptFinal` at :263-318 has no person fields.
- **Runs:** `runserver/async_runs.start_run` (:664) creates the session with `state={}`, and the browser seeds `initialState` through the ADK proxy. **The consent check therefore belongs in `start_run` (read the existing session state) and again at render time in the tool.**
- **Sharing:** `share_snapshot.build_snapshot` filters concepts at :216-222.
- **Bandit and results lookups:** the bandit `experiments._image_uri` (:262-277) and the results page (`page.tsx:217`) both look images up as `<concept_name>.png`. Variants must never use that key.
- **Image proxy:** `frontend/src/app/api/gcs/route.ts` has no caller identity and proxies any object. `resolveUser` (`lib/iap-identity.ts:69`) is used by `app/api/adk/[...path]/route.ts:60-64`.
- **Patterns to copy:** `runserver/shares.py` + `shares_store.py` (router + store, created after the ratings ones), and `user-scoping.ts` `ROUTES`.

## PR grouping

| PR | Branch | Scope | Gate / deploy |
|---|---|---|---|
| **0** | `spike/person-reference` | calibration script + findings note (no product code) | **Go/no-go**; sets the style allowlist and the cast cap |
| **1** | `feat/person-consent` | registry store + `/person-refs` api + proxy routes + `/api/gcs` owner scoping + consent UI (`/people` page) | BigQuery table → api + web |
| **2** | `feat/person-casting` | state key + start-run check + schema + prompts + guard + render path + QA likeness + judge gate + form field + "Cast" display | engines + api + web; bump `JUDGE_VERSION` |
| **3** | `feat/person-variants` | `render_concept()` refactor + `/variants` api + Personalise panel + bandit exclusion | api + web |
| **4** | `feat/person-share-scope` | share filter by consent scope + revoke cascade into shares, cast images and variants | api + web + share service |

**Every PR:**
- **Gates:** Python `uv run ruff format --check . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 4`; frontend `npm run lint && npm test && npm run build`.
- **Merge only after every GitHub check passes**, then confirm `MERGED` before cleanup.
- **No Co-Authored-By trailer or AI attribution.**
- **Deploys:** pin api and web traffic; check for in-flight runs before an api deploy.

---

## PR 0 — Calibration spike (gate)

### Task 0.1: Calibration script

**Files:**
- Create: `experiments/person_reference/calibrate.py`, `experiments/person_reference/README.md`.
- Test: `tests/test_person_calibration.py` (pure helpers only).

**Design:** a CLI that takes `--photos gs://…/a.jpg gs://…/b.jpg` (consented adult teammates) and renders a 3 × 2 × 2 grid of concept prompts:
- **Styles:** candid 35mm film photo, editorial portrait, cinematic still.
- **Framing:** hero close-up versus mid shot.
- **Photos:** each of the provided photos.

Each concept is rendered twice: with `ImageConfig(person_generation="ALLOW_ADULT")` and without it.
- **What it records per render:** blocked? (`finish_reason`, `prompt_feedback.block_reason`, missing image), plus a vision-LLM likeness verdict (same person? yes/no plus a reason, the same model as image QA).
- **Output:** a CSV and a summary under `experiments/person_reference/results/` (gitignored, except the summary).
- **Pure helpers to unit-test:** `build_grid(photos)`, `block_reason(response)` and `summarise(rows)`.

**Step 1: Failing tests**
```python
from experiments.person_reference.calibrate import block_reason, build_grid, summarise


def test_build_grid_covers_styles_framings_photos():
    grid = build_grid(["gs://b/person-refs/u/a.jpg", "gs://b/person-refs/u/b.jpg"])
    assert len(grid) == 12 and {g.framing for g in grid} == {"close", "mid"}


def test_block_reason_reads_prompt_feedback_and_finish_reason():
    assert (
        block_reason(_resp(prompt_block="PROHIBITED_CONTENT"))
        == "prompt:PROHIBITED_CONTENT"
    )
    assert block_reason(_resp(finish="IMAGE_SAFETY")) == "finish:IMAGE_SAFETY"
    assert block_reason(_resp(image=True)) is None


def test_summarise_likeness_rate_by_style():
    s = summarise(
        [
            {"style": "editorial", "likeness": True},
            {"style": "editorial", "likeness": False},
        ]
    )
    assert s["likeness_by_style"]["editorial"] == 0.5
```

**Steps 2–5:**
1. Run: `GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/test_person_calibration.py -v`. Expected: FAIL (module missing).
2. Implement.
3. Run again. Expected: PASS.
4. Commit: `feat(experiments): person reference calibration script`.

### Task 0.2: Run and record

- Run the script on the live project (about 24 renders, about 12 min at 2 images/min).
- Write `docs/notes/person-reference-calibration.md` with:
  - the likeness rate per style and framing;
  - whether `ALLOW_ADULT` is accepted by Nano Banana 2.1 (no API error, no change in block rate);
  - the filter false-positive count;
  - the **decisions**:
    - **`PERSON_SAFE_STYLES`:** the families with ≥ 70% likeness;
    - **`MAX_CAST_CONCEPTS`** (default 2);
    - whether the close-up framing is required.
- **Go/no-go rule:** no-go if likeness is under 60% in every style, or ordinary photos are blocked more than 20% of the time. Then stop and report.
- **Commit:** `docs: person reference calibration results`.

**PR 0 gate**, then merge after CI. PRs 1–4 use the recorded allowlist and cap.

---

## PR 1 — Consent registry, storage and access control

### Task 1.1: Registry store

**Files:**
- Create: `runserver/person_refs_store.py`, a copy of the `shares_store.py` pattern: a Protocol, InMemory, BigQuery (parameterised MERGE on `consent_id`), and `build_store_from_env` reading `PERSON_REFS_STORE=bigquery|memory` and `BQ_TABLE_PERSON_REFS` (default `person_references`).
- Create: `deployment/bq_schemas/person_references.json`.
- Test: `tests/test_person_refs_store.py`.

**Columns:** `consent_id STRING, owner_user STRING, photo_uri STRING, label STRING, subject STRING ('self'|'third_party_with_consent'), adult_attested BOOL, allow_public_share BOOL, consent_text_version STRING, created_at TIMESTAMP, revoked_at TIMESTAMP`.

**Methods:**
- `put`;
- `get(consent_id)`;
- `list_for(owner)` (active only);
- `revoke(consent_id, owner) -> bool`;
- `active_for(consent_id, owner) -> row | None` (exists, is owned by `owner`, not revoked).

**Step 1: failing tests** (copy `tests/test_shares_store.py`):
- `active_for` is None after a revoke and None for another owner;
- the SQL is parameterised;
- the MERGE has no string interpolation of values.

**Steps 2–5:** run (fails), implement, run (passes), commit `feat(person): consent registry store`.

### Task 1.2: `/person-refs` api

**Files:**
- Create: `runserver/person_refs.py` (router + `configure`).
- Modify: `runserver/authz.py` `_PATH_USER_RES` (add `/person-refs/{u}`), `deployment/async_app.py` (mount).
- Test: `tests/test_person_refs_api.py`.

**Routes:**
- **`POST /person-refs/{user}`**, body `{photo_uri, label, subject, adult_attested, allow_public_share, consent_text_version}`:
  - `photo_uri` must match `gs://<GOOGLE_CLOUD_STORAGE_BUCKET>/person-refs/<slug(user)>/<file>.(jpg|jpeg|png|webp)`, where `slug` is the lower-case email with `@` and `.` replaced by `_`. Otherwise 400 `invalid_photo_uri`.
  - The object must exist and be ≤ 10 MB with an `image/*` content type: HEAD via `agent_common.clients.get_gcs_client`. Otherwise 400 `photo_unreadable`.
  - `adult_attested` must be true (400 `adult_attestation_required`).
  - `consent_text_version` must equal the current `CONSENT_TEXT_VERSION` constant (400 `stale_consent_text`).
  - Returns the record with a `consent_id` (`secrets.token_urlsafe(12)`).
- **`GET /person-refs/{user}`:** active records.
- **`DELETE /person-refs/{user}/{consent_id}`:** revokes the record and deletes the photo object, returning 204, or 404 if it isn't the caller's. The cascade comes in PR 4; leave a `revoke_hooks` list it can register into.

**Tests:**
- 401/403 via authz;
- the URI must sit under the caller's own prefix (another user's prefix gives 400);
- the attestation is required;
- revoke deletes the object (fake GCS);
- a cross-owner revoke gives 404.

**Commit:** `feat(person): owner-scoped consent registry api`.

### Task 1.3: Owner-scoped person paths in `/api/gcs`

**Files:** `frontend/src/app/api/gcs/route.ts`, new `frontend/src/lib/person-paths.ts`, test `frontend/src/__tests__/gcs-route-person.test.ts`.

**Behaviour:**
- `isPersonPath(path)` is true for `person-refs/…` and `…/variants/…`. `personOwnerSlug(path)` takes the owner from `person-refs/<slug>/…`.
- For a person-ref path, call `resolveUser` as `app/api/adk/[...path]/route.ts:60-64` does:
  - `reject` gives 401;
  - `user` is allowed only when `slug(user.email) === owner`, otherwise 404;
  - `local` (dev) passes.
- **Variants:** owner-checked against the path segment `variants/<slug>/…`, the layout PR 3 uses. Other paths are unchanged.

**Tests:** owner allowed, other user 404, reject 401, non-person paths unchanged.

**Commit:** `feat(frontend): owner-only access to person photos and variants`.

### Task 1.4: Consent UI

**Files:**
- `frontend/src/lib/user-scoping.ts` `ROUTES`: `GET/POST person-refs/{u}`, `DELETE person-refs/{u}/{id}`.
- `frontend/src/lib/api.ts`: `listPersonRefs`, `createPersonRef`, `revokePersonRef`.
- New page `frontend/src/app/people/page.tsx` and nav link `components/main-nav.tsx`.
- `frontend/src/lib/person-consent.ts`: `CONSENT_TEXT`, `CONSENT_TEXT_VERSION` (mirrored in `runserver/person_refs.py`, drift-tested in `tests/test_person_consent_drift.py`).
- Tests: `people-page.test.tsx`, `user-scoping.test.ts`.

**Page:**
- Shows the expected prefix `gs://<bucket>/person-refs/<your slug>/` as copyable text.
- Form fields: photo URI, label, subject (self / someone who consented), and the checkboxes "I confirm this person is an adult and agreed to appear in AI-generated ad previews" and "They also agreed to appear in public share links" (off by default).
- The full consent text is shown above the form.
- Lists your people (thumbnail via `/api/gcs`, label, public-share yes/no, created) with **Revoke** (confirm dialog: "Deletes the photo and every image made with it").

**Commit:** `feat(frontend): people page to register and revoke consented photos`.

### Task 1.5: Docs, table and lifecycle

- **`deployment/README.md`** gets a "Person references" section:
  - `bq mk --table …person_references…`;
  - the env vars;
  - a GCS lifecycle rule (delete `person-refs/` objects older than 365 days, plus `*/variants/` after 30 days) with the `gcloud storage buckets update --lifecycle-file` JSON.
- **Retention note:** revoke is the primary deletion path.
- Also update CLAUDE.md, GEMINI.md and tests/README.md.
- **Commit:** `docs: person reference consent and storage`.

**After merge:** create the table, apply the lifecycle rule, deploy the api (`PERSON_REFS_STORE=bigquery`, pin) and web (pin).

---

## PR 2 — Casting in base runs

### Task 2.1: State key + start-run consent check

**Files:**
- `creative_agent/callbacks.py` `_set_initial_states` (:89-120): `setdefault("person_reference", {})` and `setdefault("person_reference_available", "")`, derived as `"yes"` when `person_reference.uri` is set.
- `runserver/async_runs.py` `start_run` (:664).
- Tests: `tests/test_callbacks.py`, `tests/test_async_runs.py`.

**Shape:** `person_reference = {uri, consent_id}`. In `start_run`, for creative apps, read the existing session state:
- if `person_reference` is non-empty, require `person_refs_store.active_for(consent_id, owner)`, with `photo_uri == uri`;
- otherwise 400 `person_reference_invalid`, raised before anything is claimed.

The engine-side tool re-checks only that the URI is under `person-refs/`. Batch (CRF) runs never set it.

**Step 1: Failing tests:** an invalid or revoked consent gives 400; a valid one starts; an empty one is unaffected.

**Commit:** `feat(person): person reference state key + kick-off consent check`.

### Task 2.2: Schema fields

**Files:** `creative_agent/schemas.py` `VisualConceptFinal` (:263-318); tests `tests/test_schemas.py`.

**Fields:**
```python
casts_person_reference: bool = Field(
    default=False,
    description="True when this concept's single human hero is the user's person reference.",
)
person_casting_reason: str = Field(
    default="",
    description="One sentence: why this concept casts (or would not cast) the person.",
)
```

**Test:** old payloads parse with the defaults.

**Commit:** `feat(person): visual concept casting fields`.

### Task 2.3: Prompt rules

**Files:** `creative_agent/prompts.py`:
- a new brace-free constant `PERSON_CASTING_RULES`, appended to `VISUAL_CONCEPT_RULES` (:696);
- `IMAGE_PROMPT_GUIDE` `<REFERENCE_IMAGES>` (:68-74) gets a person bullet;
- the art director, drafter, critic and finalizer read `{person_reference_available?}`.

Tests: `tests/test_visual_concept_prompts.py`, `tests/test_image_prompt_guide.py`, and the brace-token test.

**Rules text** (no braces):

> When person_reference_available is yes, you may cast the user's person as the hero of at most MAX concepts. Cast only when the hero is one person, the face is large and clearly visible, the style is one of: [PERSON_SAFE_STYLES from the spike], and it fits the brief and its trend risks. Never cast for real-person, tragedy or crisis trends, never in meme, comic, isometric or product-only concepts. Describe the hero generically as "the person in the person reference image"; never describe their face, age, ethnicity or body. Set casts_person_reference and give person_casting_reason either way.

**Commit:** `feat(person): casting rules for the visual agents`.

### Task 2.4: Deterministic casting guard

**Files:**
- `creative_agent/concept_guard.py`: new `enforce_person_casting(concepts, *, available: bool, max_cast: int, safe_styles: frozenset[str]) -> tuple[list, list[str]]`.
- Called from `callbacks.ensure_trend_and_product_callback` (:256) before the existing guard. Interactive's reviser shares the callback.
- Config: `PERSON_SAFE_STYLES` / `MAX_CAST_CONCEPTS` in `creative_agent/config.py` with env overrides, added to `deploy_agent.py` `ENV_VAR_DICT`.
- Tests: `tests/test_concept_guard.py`.

**Behaviour:** the guard clears `casts_person_reference`, with a warning and a reason suffix, when:
- no reference is available;
- `canonical_style(visual_style)` isn't in the allowlist;
- the prompt has no human-subject cue (regex: person|man|woman|hero|model|athlete|musician|player|…);
- the cast count exceeds the cap (it keeps the first N in concept order).

**Step 1: failing tests**, one per rule plus the cap order.

**Commit:** `feat(person): deterministic casting guard`.

### Task 2.5: Render path + typed safety fallback

**Files:** `creative_agent/image_tools.py`, plus a new `creative_agent/person_render.py` (pure helpers); tests `tests/test_person_render.py` and `tests/test_image_reference.py`.

**`person_render.py`:**
- `PERSON_ROLE_INSTRUCTION` (text from the research §4.3).
- `person_block_reason(response) -> str | None`, which reads `prompt_feedback.block_reason`, `candidates[0].finish_reason` in {`IMAGE_SAFETY`, `PROHIBITED_CONTENT`, `SAFETY`} and a missing image.
- `person_image_config(base: ImageConfig) -> ImageConfig`, which adds `person_generation="ALLOW_ADULT"` (only if the spike showed it's accepted; otherwise a no-op flag).

**`image_tools`:**
- Fetch the person photo once alongside the references (:648-652), reusing `_fetch_reference_image`, with the URI restricted to `person-refs/`.
- For concepts with `casts_person_reference`, `contents_for` appends `Reference image N (person): …` plus the part, and `_render_image` gets the person image config. That means threading an optional `image_config` override into `_render_image` (:395-410).
- On `person_block_reason`: record `person_reference_rejected[concept] = reason`, set the concept's cast flag false in `generated_images[concept]["cast"]`, and re-render **without** the person part. The run never fails.
- Record `generated_images[concept]["cast"] = True|False`.

**Tests** (fake genai client):
- the cast concept gets the person part and `ALLOW_ADULT`;
- an uncast concept gets neither;
- a blocked person render falls back and records the reason;
- the photo URI outside `person-refs/` is ignored with a warning.

**Commit:** `feat(person): cast the person reference at render time with a safe fallback`.

### Task 2.6: Image QA likeness

**Files:**
- `creative_agent/image_qa.py`:
  - `ImageQAResult` gets `person_cast: bool | None = None`, `person_likeness: bool | None = None`, `person_distorted: bool = False`;
  - `_RULES` gets `("person_likeness", "person likeness lost")` and `("person_distorted", "person distorted")`, neither critical;
  - `inspect_image(..., person_image: tuple[bytes, str] | None = None)` adds the reference Part plus the instruction "Is the hero clearly the same person as the reference image?";
  - `correction_text` restates the likeness instruction.
- `image_tools._inspect` (:452-479) passes the person image for cast concepts.
- Tests: `tests/test_image_qa.py`.

**Step 1: failing tests:** rules fire only for cast concepts; old payloads parse; the instruction includes the comparison text only with `person_image`.

**Commit:** `feat(person): image QA checks likeness for cast concepts`.

### Task 2.7: Judge gate

**Files:**
- `creative_eval/dimensions.py`: `VISUAL_GATES` + `GATE_LABELS` gain `person_depicted_respectfully` ("Person depicted respectfully"), and `JUDGE_VERSION` becomes the merge date.
- `creative_eval/prompts.py`: the gate text, "Return all 8 gates".
- `creative_eval/evaluate.py`: the gate passes with "no person cast" when the concept isn't cast, read from `generated_images[concept].cast`.
- `frontend/src/lib/eval-dimensions.ts`.
- Tests: `tests/test_eval_gates.py`, `tests/test_eval_dimensions.py`, `frontend/src/__tests__/eval-checks.test.tsx`.

**Gate text:**
> **person_depicted_respectfully**: The cast person is shown respectfully: no mockery, sexualisation, injury, or demeaning situations, and no altered body or added attributes beyond the brief. Fail on any such depiction.

**Commit:** `feat(creative_eval): person_depicted_respectfully gate`.

### Task 2.8: Form + display

**Files:**
- `frontend/src/app/page.tsx` (creative agents only, next to the references at :482-520): a "Person (optional)" select of your active consents from `listPersonRefs`, with a link to /people.
- `lib/initial-state.ts`: `person_reference: {uri, consent_id}` only when chosen.
- `lib/run-history.ts`: Duplicate brief restores it.
- `results/[sessionId]/proof-detail.tsx`: a "Cast: yes — <reason>" row from the concept fields, plus a "Person photo rejected by safety filter" note from `person_reference_rejected`.
- Tests: `initial-state.test.ts`, `run-history.test.ts`, a new `person-select.test.tsx`, `results-summary.test.tsx`.

**Commit:** `feat(frontend): pick a consented person for a run; show casting`.

### Task 2.9: Docs

Update CLAUDE.md, GEMINI.md, `docs/notes/creative-quality-gates.md` (casting guard, likeness QA, judge gate) and `docs/notes/judge-calibration.md` (version bump). **Commit:** `docs: person casting`.

**After merge:** deploy the engines (creative + interactive; the smoke test without a person must be unchanged), the api and web; pin traffic.

---

## PR 3 — Personalised variants (UI only)

### Task 3.1: `render_concept()` refactor

**Files:**
- New `creative_agent/render_concept.py`: `async def render_concept(prompt, *, aspect_ratio, references, person=None, strictness=(), qa=True, brand, target_product) -> RenderResult(image_bytes, mime, attempts, qa, cast, rejected_reason)`. It composes `_fetch_references` / `_reference_prompt` / `_render_image` / `_inspect_and_rerender` and the person helpers.
- `generate_image` calls it per concept.
- Tests: `tests/test_render_concept.py`, plus the existing image tests unchanged (behaviour parity).

**Step 1:** a parity test, where the old and new paths give the same contents and calls on a fake client.

**Commit:** `refactor(creative_agent): render_concept for single-concept renders`.

### Task 3.2: `/variants` api

**Files:**
- New `runserver/variants.py`: `POST /variants/{user}/{app}/{session}` with body `{concept_name, consent_id}`, and `GET /variants/{user}/{app}/{session}`.
- `authz.py` pattern; mounting in `async_app.py`.
- Test: `tests/test_variants_api.py`.

**Behaviour:**
- Check session ownership, the consent with `active_for`, and that the concept exists in `final_visual_concepts`.
- `key = sha256(photo_uri | prompt | model)[:12]`. If `person_variants[concept][key]` is done, return it (cache).
- Otherwise start a detached task, as `/runs` does, under a process-wide `asyncio.Semaphore(1)` (`VARIANT_RENDER_CONCURRENCY`) and a per-user daily cap (`VARIANT_DAILY_CAP`, default 10, counted from state plus the store; return 429).
- Render via `render_concept(…, person=photo)` with QA. Upload to `{gcs_folder}/{agent_output_dir}/variants/<owner_slug>/<concept_slug>/<key>.png`.
- Append a state delta event (as `async_runs` appends `__run_status`):
  - **`person_variants[concept][key] = {gcs_uri, status, qa, consent_id, created_at}`;**
  - statuses `queued|rendering|done|failed|rejected`.
- **Never** touch `final_visual_concepts`, `generated_images` or `_generated_artifact_keys`.

**Tests:**
- ownership; revoked consent gives 400;
- the cache hit;
- the cap;
- the upload path is under `variants/<slug>/`;
- state keys untouched (assert the deltas only contain `person_variants`).

**Commit:** `feat(person): personalised variant renders (UI only)`.

### Task 3.3: Bandit exclusion

**Files:**
- `runserver/experiments.py` `snapshot_arms` (:301) and `_image_uri` (:262): refuse any `variants/` URI, and refuse concepts with `generated_images[c].cast` true. Cast creatives are excluded from experiments for likeness safety.
- `frontend/src/lib/deploy-selection.ts`: disable cast proofs, with the reason shown.
- Tests: `tests/test_experiments_api.py` (adding `person_variants` to state leaves `snapshot_arms` unchanged; a cast concept is refused), `deploy-selection.test.ts`.

**Commit:** `feat(person): keep variants and cast creatives out of bandit experiments`.

### Task 3.4: Personalise panel

**Files:**
- New `frontend/src/components/personalise-panel.tsx`, in `proof-detail.tsx` next to the rating and share slots.
- `user-scoping.ts` routes for `variants`; `lib/api.ts` `createVariant` / `listVariants`.
- Tests: `personalise-panel.test.tsx`, `user-scoping.test.ts`.

**UI:**
- Pick one of your consented people, then **Render preview**.
- Status polling: queued → rendering → done.
- The base image and the variant side by side, both with alt text, plus its image-check result.
- A muted label: "Personalised preview — not used in experiments or share links".
- It shows only for concepts that are not cast and have a human hero (`person_casting_reason` present), or for all concepts if the spike allows it.

**Commit:** `feat(frontend): personalised preview panel`.

### Task 3.5: Docs

Update CLAUDE.md, GEMINI.md and deployment/README (the variant env vars and the quota note). **Commit:** `docs: personalised variants`.

**After merge:** deploy the api and web; pin traffic.

---

## PR 4 — Share consent scope + revoke cascade

### Task 4.1: Share filter

**Files:**
- `runserver/share_snapshot.py` filter at :216-222: a cast concept is included only when its consent record has `allow_public_share` and is active. `build_snapshot` gets a `consent_lookup` callable.
- `runserver/shares.py`: wire `consent_lookup` from the store, and record `person_consent_ids ARRAY<STRING>` on the share row (`shares_store.py` + schema JSON; ALTER migration in the README).
- Variants never enter snapshots: they aren't in `final_visual_concepts`.
- A slate share skips excluded creatives and returns `skipped: [{concept, reason: "person_not_shareable"}]`. The dialog shows "1 creative with a person was left out (their consent doesn't cover public links)".
- Tests: `tests/test_share_snapshot.py`, `tests/test_shares_api.py`, `share-dialog.test.tsx`.

**Commit:** `feat(shares): share cast creatives only with public-share consent`.

### Task 4.2: Revoke cascade

**Files:** `runserver/person_refs.py` revoke hooks; tests `tests/test_person_refs_api.py`.

**Revoking a consent:**
1. Revoke every share whose `person_consent_ids` contains it, using the existing share revoke, which deletes `shares/<token>/`.
2. Delete every variant object with that `consent_id`, found by listing `*/variants/<owner_slug>/` objects with metadata `consent_id` (written at upload in 3.2).
3. Delete cast base images: the session state records `generated_images[c] = {cast: true, consent_id}`. A BigQuery lookup via `trend_creatives`/session isn't available, so record cast renders in a small `person_renders` list on the consent row (`ARRAY<STRING>` of `gs://` URIs, appended at render time by the api run path) and delete those objects.
4. Delete the photo.

The results page then shows "Image removed (consent revoked)" for a missing cast image: a `generated_images[c].cast` true plus a 404 from `/api/gcs`.

**Tests:** every cascade step with fakes; idempotent repeat.

**Commit:** `feat(person): revoking consent removes the photo, variants, cast images and shares`.

**After merge:** run the ALTER (`creative_shares.person_consent_ids`, `person_references.person_renders`), then deploy the api, web and the share service.

---

## Verification

1. **Unit:** each task's tests plus the full suites in CI.
2. **Spike gate (PR 0):** a calibration note with numbers, and an explicit go/no-go.
3. **Live, after PR 2:**
   - Register a consented teammate photo on /people, then start a run with that person.
   - Expect 1–2 concepts marked "Cast" with reasons, in allowlisted styles. Image QA likeness passes or re-renders, and the judge's new gate passes.
   - A run without a person is unchanged (smoke test 5/5).
   - A photo of a public figure or minor gets a typed rejection note, and the run still finishes.
4. **After PR 3:** Personalise a concept with two different consented photos.
   - Both appear side by side.
   - The second request with the same photo is instant (cached).
   - The Deploy panel disables cast creatives, and an experiment snapshot never contains `variants/` URIs.
   - Another user can't fetch the variant URL (404).
5. **After PR 4:**
   - A slate share skips a cast creative without public-share consent and includes one with it.
   - Revoking the consent makes the share link 404 and removes the variants and cast images.
   - The photo object is gone.
6. **Isolation:** web and api IAM are unchanged; `/api/gcs` refuses another user's `person-refs/` paths.
