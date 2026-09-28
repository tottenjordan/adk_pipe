# Baseline — `main` branch (local end-to-end)

Record of testing the agent workflows and output artifacts on `main` before any
refactor. Captured 2026-07-11.

## Important caveats

This baseline was run against the **local working tree**, not the pristine committed
`main`. Differences that matter:

- **Models:** switched from the committed `gemini-2.5-*` to **`gemini-3.x`**
  (`gemini-3.5-flash` worker, `gemini-3.1-pro-preview` critic,
  `gemini-3.1-flash-lite` planner, `gemini-3.1-flash-image` image gen).
- **Dependencies:** `uv.lock` was re-resolved from public PyPI (the committed lock
  points at an inaccessible private Google mirror), so versions are newer than the
  branch shipped (e.g. `google-adk` 1.35.2). `uv.lock` / `.python-version` are **not**
  committed.
- **Locations:** `GOOGLE_CLOUD_LOCATION=global` (gemini-3 models are only served from
  `global`), `GCP_REGION=us-central1` for other resources.
- **Runtime:** local `adk api_server` (not deployed Agent Engine). Frontend via the
  same-origin Next.js proxy on Cloud Workstations.

## Fixes required to reach a working baseline

1. **Dep re-resolution** from PyPI (private mirror inaccessible).
2. **trend_scout orchestrator thinking runaway** — gemini-3.5-flash burned its whole
   output budget "thinking" and hit `MAX_TOKENS` before emitting tool calls. Fixed with
   `BuiltInPlanner(thinking_config=ThinkingConfig(thinking_budget=0))` on the root agent.
3. **Image generation** — `gemini-3.1-flash-image` needs the `generate_content` API on
   `global`, not the Imagen `generate_images` API. Migrated `generate_image`.
4. **Frontend** — Cloud Workstations same-origin proxy (avoids the port-auth redirect);
   fixed a React "objects are not valid as a child" crash on the `target_search_trends`
   nested-object session-state value.

## trend_scout

- **Status:** ✅ completed end-to-end (~67s after the thinking fix).
- **Pipeline:** memorize → gather_trends_agent → understand_trends_agent →
  pick_trends_agent → save_search_trends_to_session_state → write_trends_to_bq →
  write_to_file → save_session_state_to_gcs.
- **Artifacts:**
  - BigQuery: 3 selected trends written to `target_trends_crf`.
  - GCS: 2 artifacts (selected-trends file + session-state JSON) under
    `gs://trend-trawler-deploy-ae/2026_07_11_20_24_5bb4/`.

## creative_agent

- **Status:** ✅ completed end-to-end.
- **Pipeline:** combined_research_pipeline (parallel campaign + trend research → merge →
  evaluate → report) → ad_creative_pipeline → visual_generator (`generate_image`) →
  save_creative_gallery_html.
- **Artifacts** under `gs://trend-trawler-deploy-ae/2026_07_11_21_34_5d4c/`:
  - Generated image creatives (PNG) via `gemini-3.1-flash-image`, plus high-res (1.5×)
    resized copies under `.../resized/`.
  - HTML creative gallery.
  - Research report PDF.
  - Session-state JSON.
  - BigQuery: creative results to `trend_creatives`.

## Known gaps / follow-ups

- Not validated against **deployed Agent Engine** or the **Cloud Run Functions**
  orchestration — local runtime only.
- Video generation (`veo-3.1-generate-001`) is configured but **not wired up** (no
  `generate_videos` call). Would need a regional client.
- `ruff` / `ty` not yet configured in `pyproject.toml` (see CODE_STANDARDS.md gaps).
- Baseline uses gemini-3 + newer deps, so it is not byte-identical to the deployed env.

## 2026-09-28 model-lineup refresh

Validation of branch `feat/model-lineup-2026-09` (retire gemini-2.5 before Vertex
blocks it ~Oct 20 2026). New lineup, all served @ `global`:

| Role | Before | After |
|---|---|---|
| `worker_model` (trend research searcher/synth, trend_scout searcher) | gemini-3.5-flash | **gemini-3.8-flash** |
| `lite_planner_model` (trend planner, trend_scout synthesizer) | gemini-3.1-flash-lite | **gemini-3.5-flash-lite** |
| creative_agent campaign research (default arm) | gemini-2.5-flash / -lite @ us-central1 (`regional_25`) | **gemini-3.5-flash** (`global_altbucket`) |
| trend_scout `gather_model` | gemini-2.5-flash-lite @ us-central1 | **gemini-3.1-flash-lite** |
| trend_scout `picker_model` | gemini-2.5-pro @ us-central1 | **gemini-3.5-flash** |
| critic / creative_eval judge / image | gemini-3.1-pro-preview / gemini-3.1-flash-image | unchanged |

All runs local (`InMemorySessionService`, ADC, project `hybrid-vertex`), run
sequentially except where noted. **Every figure below is N=1 (or N=2)** — single
runs on shared project-wide quota are noisy (the 2026-07 DoE saw N=1 totals range
371–742 s for one arm), so treat deltas as sanity checks, not measurements.

| Run | Wall | Result | 429 / 404 | `*__retry_exhausted` |
|---|---|---|---|---|
| trend_scout (gather/pick still on 2.5) | 116 s | ✅ full pipeline, 3 trends to BQ | 0 / 0 | none |
| trend_scout (final, all 3.x) | 103 s | ✅ full pipeline, 3 trends to BQ | 0 / 0 | none |
| creative_agent `headless_run.py` (PRS / "tswift wedding") | 500 s | ✅ 4 images, eval report, gallery, BQ | 0 / 0 | none (report `warnings: []`) |
| `adk eval creative_agent` (2 cases, run **concurrently** by adk eval) | 965 s | ✅ 2/2 PASSED, all 4 rubrics 1.0 | 0 / 0 | none |

In-pipeline `creative_eval` (unchanged gemini-3.1-pro-preview judge):

| Run | Ad copy pass / avg | Visual pass / avg | Overall pass |
|---|---|---|---|
| headless PRS | 3/4 · 0.758 | 4/4 · 0.867 | 0.875 |
| eval — Estée Lauder | 4/4 · 0.842 | 4/4 · 0.900 | 1.00 |
| eval — PRS | 4/4 · 0.771 | 2/4 · 0.733 | 0.75 |
| **Pooled (3 runs)** | 11/12 · 0.790 | 10/12 · 0.833 | **0.875** |

**Comparison to prior references** (different runtime — Cloud Run — and older code,
so directional only):

- trend_scout: 103 s vs 67 s on 2026-07-11, but that predates the WS2
  searcher/synthesizer split + retry wrapper (extra model turns); no regression signal.
- creative_agent latency: 500 s local vs the 2026-07-17 DoE `regional_25` N=1
  Cloud Run median 449 s (range 371–742) — inside the historical N=1 range.
- creative_agent quality: pooled pass 0.875 / mean ≈0.81 vs DoE `regional_25` N=1
  0.969 / 0.839 and `global_3x` N=1 0.906 / 0.815. Slightly lower but within the
  run-to-run spread seen then; one eval-case image concept was dropped because the
  (unchanged) image model returned `finish_reason=OTHER`, which also pulled the PRS
  visual score down. Worth re-checking with more runs before reading anything into it.
- Observed: the final trend_scout run picked "unabomber" among its 3 trends for a
  guitar brand. `PICK_TRENDS_INSTR` has no brand-safety guidance, and one run
  can't say whether gemini-3.5-flash (vs the old gemini-2.5-pro) is to blame —
  flagged as a follow-up.
