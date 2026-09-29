# Architecture Diagrams

Reference figures for the Trend Trawler pipeline. Generated with the PaperBanana
MCP figure pipeline. Kept here for easy viewing — only the accurate ones are
embedded in the top-level `README.md`.

| File | Status | Notes |
|------|--------|-------|
| `system-architecture.png` | ✅ Accurate — embedded in `README.md` + `deployment/README.md` | End-to-end system (regenerated 2026-09-29): IAP → `trend-trawler-web` proxy (IAP JWT + `hd`, `X-TT-User`) → private `trend-trawler-api` (`async_app` + `/runs`, `UserAuthzMiddleware`, agents in-process) → `trend-trawler-sessions` Agent Engine; the three ADK agents; the CRF Pub/Sub batch fan-out to the `creative_agent` Agent Engine; shared Vertex AI Gemini (all 6 models @ `global`), Google Search grounding, BigQuery, Cloud Storage. Replaces the retired `agent-engine-pipeline.png` (pre-Cloud-Run, partial model list). |
| `crf-fanout-orchestration.png` | ⚠️ Draft — **superseded, not embedded** (known inaccuracies) | CRF orchestrator→worker fan-out. Baked-in text errors the image model wouldn't fix on refine: garbled SQL (`processed_status IS status IS NULL` → should read `processed_status IS NULL`), and the status-tracking BigQuery table is mislabeled `trend_creatives` (status SQL actually runs on `target_trends_crf`). Also should name the Stage-3 worker topic `creative-worker-queue-topic`. **The accurate replacement now embedded in `README.md` / `deployment/README.md` is [`docs/diagrams/crf_fanout_system_architecture.png`](../diagrams/crf_fanout_system_architecture.png)** (see [`docs/diagrams/README.md`](../diagrams/README.md)); this draft is retained only for history. |
