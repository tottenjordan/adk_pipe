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
2. **trend_trawler orchestrator thinking runaway** — gemini-3.5-flash burned its whole
   output budget "thinking" and hit `MAX_TOKENS` before emitting tool calls. Fixed with
   `BuiltInPlanner(thinking_config=ThinkingConfig(thinking_budget=0))` on the root agent.
3. **Image generation** — `gemini-3.1-flash-image` needs the `generate_content` API on
   `global`, not the Imagen `generate_images` API. Migrated `generate_image`.
4. **Frontend** — Cloud Workstations same-origin proxy (avoids the port-auth redirect);
   fixed a React "objects are not valid as a child" crash on the `target_search_trends`
   nested-object session-state value.

## trend_trawler

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
