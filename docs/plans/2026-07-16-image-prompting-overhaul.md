# Image-Generation Prompting Overhaul Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Produce dramatically better, style-diverse ad images by (1) giving the visual
agents a real image-prompt "grammar" grounded in Nano Banana best practices, (2) removing
the baked-in photoreal bias, (3) enriching the visual pipeline's context, and (4) wiring the
image model's real config knobs (aspect ratio / size) plus optional reference images.

**Architecture:** All work is inside `creative_agent/` (bundled into both `creative_agent`
and `interactive_creative` engines) plus a tiny frontend field. New prompt constants in
`prompts.py`; additive, defaulted schema fields; one new in-pipeline "art director" agent;
config-knob + reference-image changes in `image_tools.py`. No root-orchestration or tool-name
changes (keeps ADK evals + root-tool tests intact).

**Tech Stack:** Python 3.13, Google ADK, `google-genai==2.11.0` (confirmed supports
`types.ImageConfig(aspect_ratio, image_size)` + multimodal `contents`), `uv`/`ruff`/`ty`/
`pytest`; Next.js 16 + TypeScript frontend.

**Delivery:** Single branch off `main` (e.g. `feat/image-prompting-overhaul`); do NOT commit
to `main`; open the PR only when asked. Tasks sequenced Phase 1 → 4; run the gate after each
phase. No `Co-Authored-By` / no "Generated with Claude Code" trailers.

**Task 0 (first step):** copy this plan to `docs/plans/2026-07-16-image-prompting-overhaul.md`
and work from there.

---

## Context (why)

Today the image model (`gemini-3.1-flash-image`) is under-driven and biased:
- **Thin guidance.** `VISUAL_CONCEPT_DRAFTER_INSTR` has a 3-bullet `<PROMPT_ENGINEERING_GUIDANCE>`
  and `VISUAL_CONCEPT_CRITIC_INSTR` says "as per prompt best practices" — which are **never
  supplied**. There is no VEO3-style taxonomy for stills.
- **Photoreal bias.** The drafter's example keywords are `"photorealistic," "8k"` and the
  critic **mandates** "photorealistic, award-winning studio lighting, 8k, 100+ words". This
  fights the user's explicit goal of many styles (cartoon, meme, sticker, minimalist…).
- **Style isn't first-class.** No `style` field in any visual schema → can't enforce
  diversity, log it, or judge it; the style is buried in prose.
- **Context-starved drafter.** `visual_concept_drafter` receives ONLY `ad_copy_critique` +
  `target_product` + `target_search_trends` — **not** the research report or brand identity.
- **Config ignored.** `generate_image` passes only `response_modalities=["IMAGE"]`; no
  `aspect_ratio` → the model defaults to **1:1 square**, wrong for IG/TikTok (want 9:16).
- **No reference image.** In-image product/brand fidelity is left to text description only.

Grounding facts (Nano Banana docs): be hyper-specific; describe the scene + **name the
style**; positive/"semantic negative" framing; state the ad intent; the model renders
**legible in-image text** (great for headlines/logos); style is specified descriptively (no
enum); aspect ratio/size are **config params** (`ImageConfig`); reference images are extra
`contents` parts.

---

## Invariants — the safety net (verified against tests/consumers; DO NOT break)

1. **Root orchestration unchanged.** Keep the root tool set + order and the tool name
   `visual_production_pipeline` (guards: `test_pipeline_structure.py:4-23`, ADK evalset
   `tests/eval/evalsets/creative_agent_evalset.json`, `creative_eval_config.json`). The new
   art-director agent lives **inside** `visual_generation_pipeline`, never as a root tool.
2. **`visual_production_pipeline` shape unchanged**: `[visual_generation_pipeline,
   visual_generator_resilient]`, last is `RetryUntilKeyAgent(output_key="_images_generated")`,
   `sub_agents[0] is visual_generator` (`test_pipeline_structure.py:151-165`).
3. **New schema fields must be OPTIONAL/defaulted** (`test_schemas.py:97-128` builds models
   without them): `visual_style: str = ""`, `aspect_ratio: str = ""`.
4. **`image_tools.generate_image` must use `.get()`** for every new per-concept key
   (`test_tools_retry.py:127/157/204` pass concepts with only `image_generation_prompt` +
   `concept_name`). Never `entry["visual_style"]`.
5. **Preserve substrings** `"ad_copy_critique"` and `"ad_copy_id"` in the finalizer
   instruction (`test_pipeline_structure.py:665-671`).
6. **`visual_concept_critic` stays on `config.worker_model`** (`:343-356`).
7. **Don't remove existing `VisualConceptFinal` fields** — `creative_eval` does
   `str.format(**visual_concept)` over its template (`evaluate.py:170-173`,
   `creative_eval/prompts.py:84-94`); a missing key → `KeyError`. Adding fields is safe.
8. **Don't remove `entry["concept_name"]` / `entry["image_generation_prompt"]`** — gallery
   builder uses bracket access (`tools.py:196-200`).
9. Deployment is automatic (whole `creative_agent/` package is bundled); `interactive_creative`
   imports `creative_agent` so it inherits everything. Only the reference-image **frontend**
   field needs a `trend-trawler-web` redeploy.

---

## Phase 1 — Wire the image model's config knobs (small, safe, immediate quality win)

Independent of all prompt work. Fixes framing (1:1→9:16) and resolution now.

### Task 1: Add image-config defaults to config
**Files:** `agent_common/config.py` (next to `image_gen_model`, ~line 59).
- Add env-overridable fields: `image_size: str = "2K"`, `image_aspect_ratio_default: str =
  "9:16"`, and `image_aspect_ratios_allowed: tuple = ("9:16", "1:1", "3:4")` (the social-safe
  set the LLM may choose from; all in the SDK's supported list).
- Optional: `image_thinking_level: str | None = None` (leave model default) for a future knob.

### Task 2: Pass `ImageConfig` (aspect ratio + size) in `generate_image`
**Files:** `creative_agent/image_tools.py` (the `GenerateContentConfig` at ~line 117).
- Per concept: `ar = entry.get("aspect_ratio") or config.image_aspect_ratio_default`; if `ar
  not in config.image_aspect_ratios_allowed`, fall back to the default (log a warning). Then:
  ```python
  config=types.GenerateContentConfig(
      response_modalities=["IMAGE"],
      image_config=types.ImageConfig(aspect_ratio=ar, image_size=config.image_size),
  )
  ```
- Verify: `uv run pytest tests/test_tools_retry.py -q` still green (concepts without
  `aspect_ratio` must fall back — that's the `.get() or default` path).
- Commit: `feat(creative_agent): render images at 9:16/2K via ImageConfig`.

---

## Phase 2 — `IMAGE_PROMPT_GUIDE` + de-bias drafter/critic + `visual_style` field

The core of "many styles". All prompt literals live in `prompts.py` (project convention).

### Task 3: Author `IMAGE_PROMPT_GUIDE` in `prompts.py`
**Files:** `creative_agent/prompts.py` (new UPPER_SNAKE constant; may draw on the existing
`<VISUAL_STYLE_AND_AESTHETICS>` block at `:65-87`).
Structure (the VEO3 analog, but **style-first + template-driven**, not a flat menu):
- **Core principles**: hyper-specific; describe the scene as a coherent noun-phrase; **name
  the style explicitly**; positive/"semantic negative" framing; state it's a social ad;
  render legible in-image text/CTA/brand when it strengthens the ad.
- **Style palette** — a menu of families, each with a one-line *when-to-use* + a fill-in
  template: photoreal/editorial, cinematic film still, 3D character (Pixar-ish), 2D
  flat/vector cartoon, anime/manga, comic panel, **meme aesthetic** (bold caption / screenshot
  / sticker), diecut sticker, watercolor/gouache, collage/mixed-media, retro/vaporwave,
  isometric, minimalist negative-space.
- **Tone→style mapping** (drives selection): Meme-based→meme/sticker/flat cartoon;
  Humorous→3D character/comic; Aspirational→cinematic photoreal; Emotional/Authentic→candid
  35mm; Educational→minimalist + legible text/isometric; Problem-Solution→studio product.
- **Building blocks**: subject, composition/framing, setting, lighting, color, mood, rendering
  cues, + an **in-image text & branding** block.
- **Aspect-ratio note**: choose per concept from `9:16` (default, vertical reel), `1:1` (feed),
  `3:4` (portrait); output the choice in the `aspect_ratio` field.
- **Length rule** replacing the old ">100 words": *as detailed as the style needs* (a
  minimalist/sticker prompt should be short; a photoreal scene long).

### Task 4: Add defaulted `visual_style` + `aspect_ratio` fields to visual schemas
**Files:** `creative_agent/schemas.py` (`VisualConcept`, `VisualConceptCritique`,
`VisualConceptFinal`). Add `visual_style: str = ""` and `aspect_ratio: str = ""` with
`Field(description=...)` to each (defaulted → invariant #3). No new list wrappers.

### Task 5: Rewrite drafter + critic instructions (de-bias + inject the guide)
**Files:** `creative_agent/prompts.py`.
- `VISUAL_CONCEPT_DRAFTER_INSTR`: replace `<PROMPT_ENGINEERING_GUIDANCE>` with the
  `IMAGE_PROMPT_GUIDE` content (embed the constant via f-string/concatenation at author time —
  these are plain literals today, so keep the ADK `{state}` tokens intact and only splice the
  guide text). Instruct: pick the `visual_style` from the ad-copy tone/audience/trend using the
  mapping; DO NOT default to photoreal; emit `visual_style` + `aspect_ratio` per concept.
- `VISUAL_CONCEPT_CRITIC_INSTR`: drop the "photorealistic/8k/100+ words" mandate; instead
  "refine the prompt **within its chosen `visual_style`**, applying the guide; length
  appropriate to the style; preserve the style unless it's clearly wrong for the tone." Keep
  `visual_concept_critic` on `worker_model` (invariant #6).
- `VISUAL_CONCEPT_FINALIZER_INSTR`: add "**enforce visual_style diversity** across the final
  4" and carry `visual_style`/`aspect_ratio` through. **Keep** the `ad_copy_critique` +
  `ad_copy_id` substrings (invariant #5).

### Task 6: Update tests for the additive fields
**Files:** `tests/test_schemas.py` (add `visual_style`/`aspect_ratio` assertions to the
visual-concept construction tests — optional since defaulted, but good coverage).
- Verify: `uv run pytest tests/test_schemas.py tests/test_pipeline_structure.py -q` green.
- Commit: `feat(creative_agent): add IMAGE_PROMPT_GUIDE, de-bias visual prompts, add visual_style`.

### Task 7 (optional surfacing): show `visual_style` in gallery + judge it in eval
**Files:** `creative_agent/tools.py` (gallery `<dd>` for `entry.get("visual_style")`),
`creative_eval/prompts.py` (add a `{visual_style}` line to `VISUAL_CONCEPT_EVAL_USER` so the
judge sees the intended style). Both purely additive. Commit separately.

---

## Phase 3 — Context enrichment + art-director step

### Task 8: Add `art_director` agent + enrich drafter context
**Files:** `creative_agent/prompts.py` (new `ART_DIRECTOR_INSTR`), `creative_agent/agent.py`.
- New agent `art_director` = `Agent(model=build_gemini(config.worker_model),
  output_key="visual_direction", ...)` — **plain text output, no output_schema** (keeps it
  simple). Instruction consumes `{combined_final_cited_report?}`, `{brand}`,
  `{target_audience}`, `{target_search_trends}`, `{ad_copy_critique}` and produces a concise
  **Visual Direction Brief**: mood, palette, recurring visual motifs from the trend, brand
  visual cues, and a recommended style family per selected ad-copy tone.
- Insert `art_director` as the **FIRST** sub-agent of `visual_generation_pipeline`
  (`agent.py:415-423`) → `[art_director, visual_concept_drafter, visual_concept_critic,
  visual_concept_finalizer]`.
- Rewrite `VISUAL_CONCEPT_DRAFTER_INSTR`'s `<CONTEXT>` to also consume `{visual_direction}` +
  `{combined_final_cited_report?}` + `{brand}` + `{target_audience}` (was ad_copy only).
- `from . import prompts` is already module-style (`agent.py:16`) — reference
  `prompts.ART_DIRECTOR_INSTR`; the `IMAGE_PROMPT_GUIDE` is embedded inside the drafter/critic
  constants at author time (Task 5), so no new import needed.

### Task 9: Update structure tests for the new sub-agent
**Files:** `tests/test_pipeline_structure.py`.
- `:140-148` expected `visual_generation_pipeline` names → add `"art_director"` first.
- `:287-316` add `art_director → "visual_direction"` output-key assertion.
- `:358-385` finish-reason-callback list → add `art_director` (attach
  `log_empty_turn_finish_reason` in `agent.py` for parity).
- Optional: `experiments/creative_latency/parse_run.py` `_EXACT_PHASES` → map `art_director`
  to `"visual_concepts"` (+ `tests/test_experiment_parse.py`).
- Verify: `uv run pytest tests/test_pipeline_structure.py -q` green.
- Commit: `feat(creative_agent): add art_director step + enrich visual drafter context`.

---

## Phase 4 — Reference-image stretch (URI/URL field)

Chosen input method: a **URI/URL text field** (no upload endpoint). Applies one product/brand
reference image to all concepts for fidelity/consistency.

### Task 10: Backend plumbing — state key + fetch + multimodal contents
**Files:** `creative_agent/callbacks.py`, `creative_agent/image_tools.py`,
`creative_agent/gcs_tools.py` (reuse `_download_blob`).
- `callbacks.py load_session_state`: add `"reference_image_uri": ""` to the init dict.
- `image_tools.generate_image`: `ref = tool_context.state.get("reference_image_uri")`. If set:
  - `gs://…` → parse bucket/object, `bytes = _download_blob(bucket, name)` (add to the
    `.gcs_tools` import).
  - `http(s)://…` → fetch via stdlib `urllib.request` with a timeout (no new dep).
  - mime from extension (`.png`→image/png, `.jpg/.jpeg`→image/jpeg, `.webp`→image/webp),
    default image/png. Build `types.Part.from_bytes(data=bytes, mime_type=mime)`.
  - `contents = [entry["image_generation_prompt"], ref_part]`; else the bare prompt string.
  - Wrap fetch in try/except → on failure log a warning and **fall back to text-only** (never
    fail the run). Fetch the reference bytes **once** before the concept loop.
- Verify: `uv run pytest tests/test_tools_retry.py -q` green (no `reference_image_uri` set →
  text-only path unchanged).

### Task 11: Frontend field
**Files:** `frontend/src/lib/types.ts` (`CampaignInput` → add `referenceImageUri?: string`),
`frontend/src/app/page.tsx` (a labeled optional `Input`; on submit for creative agents pass it
via `createSession` `initialState` as `{ reference_image_uri: form.referenceImageUri }` —
mirrors the existing `{ interactive_trend_pick: true }` precedent at page.tsx:80-84). No
`/runs` or `_StartRunBody` change needed (state is set at `createSession`).
- Verify: `cd frontend && npm run build` + `npm test`.

### Task 12: Tests for reference-image assembly
**Files:** `tests/test_tools_retry.py` or a new `tests/test_image_reference.py`.
- Unit test: with `reference_image_uri` a `gs://` URI, mock `_download_blob` + the genai client
  and assert `contents` is a 2-item `[prompt, Part]` list; without it, assert bare-string
  contents. Assert an invalid `aspect_ratio` falls back to `9:16`.
- Commit: `feat(creative_agent): optional product reference image for image generation`.

---

## Convention guard

Add one line to `CLAUDE.md` (Agent Definition Pattern section): *"Image-generation prompt
guidance lives in `creative_agent/prompts.py` as `IMAGE_PROMPT_GUIDE`; visual agents must
select a `visual_style` rather than defaulting to photorealism."* Include in the final commit.

---

## Verification (run after each phase; full gate before PR)

**Python:**
```bash
uv run python -c "import creative_agent.agent, interactive_creative.agent; print('imports ok')"
uv run pytest tests/ -q            # requires GCP creds (module-level clients)
uvx ruff format --check . && uvx ruff check . && uvx ty check
```
Expected: imports ok; all green — especially `test_pipeline_structure.py` (new sub-agent
order + output keys), `test_schemas.py` (defaulted fields), `test_tools_retry.py` (`.get()` +
aspect-ratio fallback + reference-image path).

**Frontend:** `cd frontend && npm test && npm run build` — green.

**Live smoke (gated per deploy rules; only if the user asks):** run `creative_agent` locally
via `deployment/async_app.py` + frontend (or `deployment/test_deployment.py`), confirm: images
render at 9:16, the 4 concepts show **diverse `visual_style`s** (not all photoreal), the
gallery HTML shows style, and — with a `gs://` product image in `reference_image_uri` — the
product's likeness is preserved. Quantitatively, re-run the creative_eval judge and check
`prompt_technical_quality` / `trend_visual_connection` / `stopping_power` did not regress.

## Result

| Concern | Before | After |
|---|---|---|
| Prompt guidance | 3 bullets + undefined "best practices" | full `IMAGE_PROMPT_GUIDE` grammar |
| Style range | photoreal-biased | LLM-selected from ~13 families, diversity enforced |
| Style visibility | buried in prose | first-class `visual_style` field (logged/judged) |
| Drafter context | ad copy only | + research report, brand, audience, art-director brief |
| Framing/res | 1:1 default | per-concept 9:16/1:1/3:4 @ 2K |
| Product fidelity | text description only | optional real reference image |
