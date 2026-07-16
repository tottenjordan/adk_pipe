# Architecture Diagrams

Reference figures for the Trend Trawler pipeline. Generated with the PaperBanana
MCP figure pipeline. Kept here for easy viewing — only the accurate ones are
embedded in the top-level `README.md`.

| File | Status | Notes |
|------|--------|-------|
| `agent-engine-pipeline.png` | ✅ Accurate — embedded in `README.md` | Two-phase Agent Engine pipeline (trend_scout → creative_agent). Simplifies the model-serving row to 3 of the 5 models (omits the `gemini-3.1-flash-lite` planner and `veo-3.1` video). |
| `crf-fanout-orchestration.png` | ⚠️ Draft — **superseded, not embedded** (known inaccuracies) | CRF orchestrator→worker fan-out. Baked-in text errors the image model wouldn't fix on refine: garbled SQL (`processed_status IS status IS NULL` → should read `processed_status IS NULL`), and the status-tracking BigQuery table is mislabeled `trend_creatives` (status SQL actually runs on `target_trends_crf`). Also should name the Stage-3 worker topic `creative-worker-queue-topic`. **The accurate replacement now embedded in `README.md` / `deployment/README.md` is [`docs/diagrams/crf_fanout_system_architecture.png`](../diagrams/crf_fanout_system_architecture.png)** (see [`docs/diagrams/README.md`](../diagrams/README.md)); this draft is retained only for history. |
