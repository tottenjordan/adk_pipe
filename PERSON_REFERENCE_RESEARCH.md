# Person reference images in creatives: research and recommendations

**Status:** research only, nothing implemented. **Date:** 2026-10-09.
**Method:** a deep-research run (5 search angles, 23 sources fetched, 96 claims extracted, 25 checked by 3-vote adversarial verification: 19 confirmed, 6 refuted), then a map of our current reference-image code path.

**Labels used below:**
- **Verified:** a sourced finding that survived verification.
- **Recommendation:** our design, inferred from the findings and the code. It is not a sourced fact.

## Feature request

1. **Cast a person:** a user supplies a GCS URI of a photo of a person. When a concept has a human subject or hero, the agents *decide* whether to cast that person. They don't have to.
2. **Personalised variants:** show the *same* creative re-rendered with a *different* person photo (e.g. one variant per user who has provided a photo). This is for display in the UI only and must never reach bandit traffic.

---

## 1. Executive summary

- **Feasible on our current model path.** Google documents the Gemini / Nano Banana image models we already call (`generate_content` with image parts) as supporting **multi-image fusion and character consistency**, including our deployed Nano Banana 2.1. So a person reference can be one more role-tagged image part next to today's product / logo / style parts. **Verified, high confidence.**
- **Likeness is a known weak point.** Google's own docs say:
  - the models "struggle with small faces";
  - consistency "may vary";
  - features drift over chained edits, and the fix is to start fresh.

  That points to four design rules: cast the person only as a **large, prominent hero**; render every variant **fresh from the stable concept prompt**, re-sending the original photo and never editing a previous render; add a **likeness check** to the existing image-QA re-render loop; and keep person casting to 1–2 of the 4 concepts. **Verified, high confidence.**
- **Don't use Imagen subject customization.** Imagen 3 subject customization (`REFERENCE_TYPE_SUBJECT`, person) is deprecated, both shutdown dates have passed, and Google maps it to `gemini-3.1-flash-image`. **Verified, high confidence.**
- **Safety filters will block some photos.** Vertex runs input and output filters for photorealistic celebrities and children, and for people when the request disallows them. `ImageConfig.person_generation = ALLOW_ADULT` blocks generated children. So photos of minors, or of people the filter reads as celebrities, will be rejected, and this needs a typed, user-visible outcome. **Verified, high confidence.**
- **Consent is a precondition.** Google's Generative AI Prohibited Use Policy forbids "using personal data or biometrics without legally-required consent". Consent capture, revocation and retention controls have to ship *before* the feature, not after. **Verified, medium confidence**; the legal detail is unverified.
- **Variants must be structurally separate from the original creative.** Bandit arms and the results page find images by recomputing `<concept_name>.png`. A variant stored under the normal key would silently replace the original in both places. Variants therefore need their own state key and GCS prefix, never touching `final_visual_concepts`, `generated_images` or `_generated_artifact_keys`. **Code fact; the design is a recommendation.**

---

## 2. Capability findings (verified)

| # | Finding | Confidence | Sources |
|---|---|---|---|
| F1 | Current Gemini image models document **multi-image fusion + character consistency**, pitched for marketing/advertising. The Vertex models page lists it for 3.1 Flash Image, 2.5 Flash Image and **Nano Banana 2.1**. Google's prompt formula is `[Reference images] + [Relationship instruction] + [New scenario]`. Google gives no recipe for keeping a likeness *while also* mixing in product and style references; that is our inference. | High | [Vertex models](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models), [Nano Banana prompting guide](https://cloud.google.com/blog/products/ai-machine-learning/ultimate-prompting-guide-for-nano-banana), [2.5 Flash Image on Vertex](https://cloud.google.com/blog/products/ai-machine-learning/gemini-2-5-flash-image-on-vertex-ai), [Nano Banana Pro tips](https://blog.google/products-and-platforms/products/gemini/prompting-tips-nano-banana-pro/) |
| F2 | **Reference limits:** up to 14 input images (6–14 depending on surface) on Pro / 3.1 Flash Image, but only **about 5 characters** kept recognisable. Vertex lists a maximum of 3 for 2.5 Flash Image. Our `MAX_REFERENCE_IMAGES = 3` is our own cap. **The limit for `gemini-nano-banana-2.1` is unconfirmed.** | Medium | [DeepMind Pro](https://deepmind.google/models/gemini-image/pro/), [Pro tips](https://blog.google/products-and-platforms/products/gemini/prompting-tips-nano-banana-pro/) |
| F3 | **Identity preservation is weak in specific ways:** small faces, consistency that "may vary", first renders that need refinement, and drift over chained edits, fixed by "restart … with a detailed description". | High | [DeepMind Pro](https://deepmind.google/models/gemini-image/pro/), [Developers blog](https://developers.googleblog.com/en/how-to-prompt-gemini-2-5-flash-image-generation-for-the-best-results/) |
| F4 | **Imagen 3 subject customization is not viable.** All Imagen endpoints are deprecated (Vertex: June 30 2026; Gemini API / Firebase: Aug 17 2026, both passed), and `imagen-3.0-capability-001` maps to `gemini-3.1-flash-image`. Gemini image models also differ from Imagen: one image per request, always PNG with SynthID, no negative prompts. | High | [Vertex edit-controlled](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/image/edit-controlled), [Firebase subject customization](https://firebase.google.com/docs/ai-logic/edit-images-imagen-subject-customization) |

**Refuted** (excluded from the design):
- "Character consistency holds across up to 14 reference inputs" (0–3).
- "2.5 Flash Image keeps the same subject across generations without fine-tuning" (0–3). There is no reliability guarantee, so the likeness check is needed.
- The ArcFace/Arc2Face claims, both the likeness-measurement one and the identity-conditioning one (1–2, 0–3).

## 3. Policy, safety and legal constraints

| # | Constraint | Confidence | Source |
|---|---|---|---|
| P1 | **Safety filters apply to the input** (reference image parts) **and the output**: photorealistic celebrity (codes 29310472 / 15236754), person or face disallowed by request settings (39322892), child content (58061214 / 17301594), celebrity-or-child policy (64151117). | High | [Gemini image responsible AI](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/multimodal/gemini-image-responsible-ai) |
| P2 | `ImageConfig.person_generation` (`ALLOW_ALL` / `ALLOW_ADULT` / `ALLOW_NONE`) exists in google-genai and aiplatform v1, next to the `aspect_ratio` / `image_size` we already set. `ALLOW_ADULT` limits **output** only and does not age-check the uploaded photo. Whether Nano Banana 2.1 honours it is **unconfirmed**. | High | [ImageConfig.PersonGeneration](https://docs.cloud.google.com/ruby/docs/reference/google-cloud-ai_platform-v1/latest/Google-Cloud-AIPlatform-V1-ImageConfig-PersonGeneration) |
| P3 | The Prohibited Use Policy forbids "using personal data or biometrics without legally-required consent". | Medium | [GenAI Prohibited Use Policy](https://policies.google.com/terms/generative-ai/use-policy) |
| P4 | Every output carries **SynthID**: renders are always watermarked PNGs. | High | F4 sources |

**Not verified (get legal review):**
- right of publicity by jurisdiction;
- GDPR Article 9 / BIPA treatment;
- rules for minors beyond Google's filters.

The verifier noted that a plain photo is generally not "biometric data" under the GDPR **unless processed for identification**, for example by face-embedding similarity. That argues against embedding-based likeness checks (§4.5). The claim that the policy *mandates disclosure* for every cast person was refuted (0–3). Disclosure is still good practice for ads.

---

## 4. Recommended architecture for our pipeline

How the code works today (from the code map):
- `creative_agent/references.py`: roles `product|logo|style`, max 3, resolved to `[(uri, role)]`.
- `image_tools.generate_image`: renders the whole batch from a `ToolContext`; there is no single-concept entry point.
- Role text lives in `_REFERENCE_ROLE_INSTRUCTIONS` (image_tools.py:298-311). It is appended by `_reference_prompt`, and parts are assembled in `contents_for`.
- `VisualConceptFinal` (schemas.py:263-318) has no field for whether a concept has a human subject.
- `IMAGE_PROMPT_GUIDE` already says "never a likeness of the person" for real-person *trends* (prompts.py:23). Keep that rule.
- Bandit arm images come from `experiments._image_uri` → `artifact_key_for(concept_name)`, gated on `_generated_artifact_keys` (experiments.py:262-301). The results page uses the same `<name>.png` lookup (`results/[sessionId]/page.tsx:217`).

### 4.1 Inputs and consent (prerequisite)

- **State key:** a new optional key `person_reference = {uri, consent_id}`, seeded via `createSession` initialState like the other visual-intent keys and `setdefault`ed in callbacks.
  - Keep it **out of** `reference_images`/`REFERENCE_ROLES`. Consent and stricter validation apply only to person references, and the generic list accepts http(s) URLs.
  - Allow only `gs://` inside our bucket, under a dedicated prefix such as `person-refs/<user>/…`.
- **Consent record:** a `person_references` store (BigQuery or Firestore), written by a new api route before the photo can be used. Fields:
  - `consent_id`, `owner_user` (the IAP user), `subject` (`self` | `third_party_with_consent`);
  - `adult_attested: true` (required);
  - `scope` ("Trend Trawler creative previews"), `consent_text_version`, `created_at`, `revoked_at`.
- **Authorisation:** the api checks that `consent_id.owner_user == X-TT-User` and that consent isn't revoked, both at kick-off and on every variant render. Reuse the P3 authz pattern from `runserver/authz.py`.
- **Retention:** a GCS lifecycle rule on `person-refs/` and on variant renders (e.g. 30 days).
  - **Revocation** deletes the photo, every derived variant, and the matching `person_variants` state entries.
  - Rating learning never reads person data, and logs never print person URIs.
- **Open risk:** GCS artifacts are shared across users by design (P3b was declined). The `/api/gcs` proxy must refuse `person-refs/` and person-variant paths for anyone except their owner. Otherwise any user could fetch another user's photo.

### 4.2 Agent judgment (whether to cast)

- **Schema:** add two fields to `VisualConceptFinal`: `casts_person_reference: bool = False` and `person_casting_reason: str = ""`. The drafter, critic and finalizer read a new `{person_reference_available?}` flag, but never the URI.
- **Prompt rules** (brace-free, in `VISUAL_CONCEPT_RULES` / the art director). Cast the person only when **all** of these hold:
  1. the concept's hero is a single human subject;
  2. the face is large and visible, roughly the top third of the frame or bigger, not a crowd or far shot (F3);
  3. the style is one that keeps a likeness, such as photoreal, cinematic, editorial, or candid film. Never meme, comic, isometric, pictogram, or product-only stills;
  4. it fits the brief and its `trend_bridge.risks`. Never for real-person or tragedy trends, which keep the existing "never a likeness" rule;
  5. at most **2 of the 4** concepts, to keep set diversity and limit likeness risk.

  The `person_casting_reason` records why.
- **Deterministic guard:** a `concept_guard` check alongside `concept_issues`. It clears `casts_person_reference` (with a warning) when:
  - the style isn't in a person-safe allowlist;
  - the prompt lacks a human-subject cue;
  - the cap is exceeded;
  - no person reference exists.

  This is the same pattern as `ensure_trend_and_product`.

### 4.3 Render path

- **Refactor first:** pull a pure `render_concept(prompt, references, aspect_ratio, *, strictness, person_ref=None)` out of `generate_image`, reusing `_fetch_references`, `_reference_prompt`, `_render_image` and `_inspect_and_rerender`. It must not need a `ToolContext`. Batch generation keeps calling it per concept, and the variant endpoint (§4.6) calls it too.
- **Which concepts get the photo:** only concepts with `casts_person_reference` get the person part, as `Reference image N (person)`.
- **Proposed role text:** "Cast this exact person as the hero: keep their face shape, skin tone, hair, eye colour, apparent age and distinguishing features; change only pose, clothing, expression and lighting as described. Do not beautify, slim or alter their body." Follow F1's formula (references → relationship → scenario).
- **Request settings for person renders:**
  - set `ImageConfig.person_generation = ALLOW_ADULT` (P2);
  - keep any product reference too: 1 person plus up to 3 others stays within F2's limits, pending our calibration.
- **Safety blocks are a typed outcome:** when a person render is blocked by the filters in P1 (detect the block reason or code), record `person_reference_rejected = {concept, reason}`. Then re-render the concept **without** the person (a generic hero) and surface a plain note in the run's quality notices. Never fail the run.

### 4.4 Image QA

- **New fields:** add `person_cast: bool | None`, `person_likeness: bool | None` and `person_distorted: bool` to `ImageQAResult`.
  - For cast concepts, the QA call also receives the person reference image and is asked: "Is the hero clearly the same person as the reference?"
- **New rules:** "person likeness lost" and "person distorted". Neither is critical. They feed the existing bounded re-render, with correction text that restates the likeness instruction, and the better attempt is kept.
- **Why a vision-LLM comparison and not face embeddings:** it lives in the existing QA call, and it avoids biometric processing (§3). The embedding claims were refuted, and embeddings would add GDPR/BIPA obligations.

### 4.5 Eval

- Add a visual judge gate `person_depicted_respectfully`, checked only when `casts_person_reference`; for every other concept it passes with "no person cast".
- Likeness itself stays in image QA: the judge doesn't need the private photo.
- Bump `JUDGE_VERSION` when this gate lands.
- `creative_evals` / ratings stay as they are for the **base** run.

### 4.6 Personalised variants (UI only)

- **Endpoint:** `POST /variants/{app}/{user}/{session}` with body `{concept_name, consent_id}`. It runs as a detached task, like `/runs`; the status is polled with `GET`.
  - It reuses the concept's stored `image_generation_prompt`, `aspect_ratio`, the run's product/logo/style references and its strictness, then calls `render_concept` + QA.
  - Every variant is a **fresh render** (F3), never an edit of the base image.
- **Storage, deliberately separate:**
  - **GCS:** `{gcs_folder}/{agent_output_dir}/variants/{concept_slug}/{sha256(person_uri + prompt + model)[:12]}.png`. That is the cache key: an identical request returns the cached render.
  - **State:** `person_variants[concept_name][hash] = {gcs_uri, status, qa, consent_id, created_at}`.
  - It never writes `final_visual_concepts`, `generated_images`, `_generated_artifact_keys` or the `<name>.png` key.
- **Bandit exclusion:** structural, since `snapshot_arms` only reads `final_visual_concepts` plus `<name>.png`, and explicit on top of that:
  - a test asserting `snapshot_arms` output is unchanged by `person_variants`;
  - the experiments API rejects any `variants/` URI.
- **Quota (about 2 images/min, shared with live runs):** variants are on demand only.
  - A process-wide render semaphore in the api caps them, with a per-user cap (e.g. 10/day).
  - A variant shows "queued" while a run is rendering.
  - Variants are **not** judged by the full eval and **not** ratable, so judge calibration isn't polluted.
- **UI:** a "Personalise" section in the results proof dialog. The base image pane is at `proof-detail.tsx:249`, and the panel sits next to `ratingSlot`.
  - The user picks one of their consented photos (or adds one, with the consent form), clicks **Render preview**, then sees base and variant side by side, plus its image-check result.
  - A muted label: "Personalised preview, not used in experiments".
  - The Deploy panel doesn't change.
- **Base runs:** in the campaign form, an optional "Person reference" field (gs:// only) with the consent checkboxes. The proof detail shows "Cast: yes — <reason>" for cast concepts.

---

## 5. Phased plan

| Phase | Scope | Exit criterion |
|---|---|---|
| **0. Calibration spike** (about 1 day, no product code) | A script renders about 12 concepts (3 styles × 2 framings × 2 consented test photos of adult team members) on `gemini-nano-banana-2.1`. | Confirmed for our model: likeness rate in each style and framing, whether `ALLOW_ADULT` is honoured, how many total references work, and the filter false-positive rate on ordinary photos. Go/no-go and the style allowlist come from this. |
| **1. Consent + storage** | The `person_references` store and api routes (create, list, revoke), GCS prefix + lifecycle, `/api/gcs` owner scoping, proxy allowlist entries. | Revocation deletes the photo and its variants; another user gets 404. |
| **2. Casting in base runs** | Schema fields, prompt rules, deterministic guard, `render_concept` refactor, person part + `ALLOW_ADULT`, typed safety fallback, QA likeness rules, judge gate (bump `JUDGE_VERSION`), form field, "Cast" display. | Smoke run with a person reference: 1–2 concepts cast with reasons, QA likeness passes, an uncast run is unchanged. |
| **3. Personalised variants** | `/variants` endpoint + cache, quota semaphore and per-user caps, proof-dialog Personalise panel, bandit-exclusion tests. | Same concept rendered with two photos shown side by side; the deploy snapshot is unchanged. |
| **4. Polish** | Interactive checkpoint 3: toggle casting per concept; usage metrics; extend the docs (`creative-quality-gates.md`, CLAUDE.md). | none |

## 6. Open questions and risks

1. **Model behaviour on Nano Banana 2.1:** does it honour `person_generation = ALLOW_ADULT`, and how many references does it keep recognisable? Nearly all the figures above come from Pro / 3.1 / 2.5 pages (phase 0).
2. **Filter false positives:** do the celebrity and child input filters fire on ordinary user photos or on lookalikes, and how often? That drives how much the fallback is needed and what UX it needs.
3. **Likeness QA method:** a vision-LLM comparison (no biometrics, less precise) or face embeddings (more precise, likely biometric processing, more obligations). The recommendation is vision-LLM; revisit only if phase 0 shows it misses obvious drift.
4. **Legal:** which consent model and retention policy satisfy right-of-publicity and data-protection law for a user, or a third party, in an ad creative? Does UI-only display change those obligations? Get legal review before phase 2 ships.
5. **Cross-user access:** person photos and variants must be owner-scoped in the GCS proxy, unlike today's shared artifacts.
6. **Quality trade-off:** casting a real photo can fight the style shortlist's diversity, which is why there's a cap of 2 casts and a style allowlist. Watch whether cast concepts lower judge scores or ratings.
7. **Quota contention:** variants share the 2/min image quota with live runs. A burst of previews slows real runs unless the semaphore gives runs priority.
