# Frontend UI Screenshots

Reference captures of the Next.js frontend (`frontend/`), used in the README and PRs.

| File | Page | Shows |
|---|---|---|
| `01-home-form.png` | `/` | Campaign input form (agent selector + brand/audience/product/selling-points/trend) |
| `02-run-creative.png` | `/run/[sessionId]` | Live run view for `creative_agent`: campaign sidebar, event-stream timeline, pipeline widgets, Cloud Storage + research-report links, completion state |
| `03-results-creative.png` | `/results/[sessionId]` | Results gallery: creative-eval summary + per-concept **real generated image** with ad-copy/visual-concept scores across the LLM-as-judge dimensions |
| `04-run-interactive-review.png` | `/run/[sessionId]` | `interactive_creative` paused at checkpoint 2, "Review ad copies" |
| `05-runs.png` | `/runs` | Run history with **Duplicate brief** |
| `06-run-trend-pick.png` | `/run/[sessionId]` | `trend_scout` paused at the opt-in trend pick |
| `07-run-research-review.png` | `/run/[sessionId]` | `interactive_creative` paused at checkpoint 1, the cited research report |
| `08-run-stopped-early.png` | `/run/[sessionId]` | An interactive run that stopped early, with **Continue run** and partial outputs |
| `09-results-proof-detail.png` | `/results/[sessionId]` | Proof-detail dialog for creative 1 (viewport capture) |
| `user-journey.gif` | all | Annotated walkthrough of an interactive run (brief → research → three reviews → results → proof detail → research report → history): amber spotlight + callout on the key UI per step, a phase progress strip, and a step title + one-line explanation |

All captures reflect one consistent campaign — **Paul Reed Smith (PRS) / SE CE24 Electric
Guitar / Powerball trend** — harvested from a single real `creative_agent` run.

## Regenerating

These are captured with Playwright against a local Next server with **all backend
calls route-mocked** — no live Agent Engine, GCP creds, or model quota needed at
capture time. The mocks are hydrated from committed fixtures in
`frontend/scripts/screenshot-fixtures/`, which were harvested from ONE real
`creative_agent` run (session state, curated event log, eval report, and the four
concept images). So `03-results-creative.png` shows the **actual
`gemini-3.1-flash-image` renders** (downscaled to lightweight JPEG fixtures), not
placeholders.

To refresh (against a production build, so no Next dev badge):

```bash
cd frontend
npm run build
cp -r .next/static .next/standalone/.next/static && cp -r public .next/standalone/public
PORT=3600 node .next/standalone/server.js                                  # terminal 1
SCREENSHOT_BASE_URL=http://localhost:3600 npm run screenshots              # terminal 2 → 01–09 PNGs
JOURNEY=1 SCREENSHOT_BASE_URL=http://localhost:3600 npm run screenshots    # journey frames + journey-frames.json → /tmp/tt-journey-frames
uv run --no-project --with pillow python scripts/build-journey-gif.py      # → user-journey.gif
```

The capture script (`frontend/scripts/capture-screenshots.mjs`) route-mocks
`/api/adk/**` and `/api/gcs?**` from the fixtures, seeds `sessionStorage`, and writes
the PNGs (viewport 1440×900 @2×, full page except 09).

In journey mode each frame's entry in `journey-frames.json` is
`{file, step, phase, title, caption, hold_ms, highlights: [{x, y, w, h, label, place?}]}`:
the highlight boxes are Playwright `boundingBox()`es of the key element(s) for that step
(1440-wide viewport CSS px, max 2 per frame; a locator that isn't found is logged and
skipped). `build-journey-gif.py` scales them to the GIF width and draws the spotlight
(dimmed surroundings, amber `#F59E0B` outline), a callout pill per box (numbered when
there are two; `place` is an optional above/below/right/left hint), the phase strip
(Brief → Research → Reviews → Images & eval → Results → History) and the caption bar.
Each annotated frame is preceded by a 0.6 s un-annotated beat (`GIF_TRANSITION_MS=0`
disables it); `GIF_EXPORT_DIR=/tmp/x` also writes the annotated frames as PNGs for review. To harvest a fresh run's fixtures (new campaign
or trend), re-run `creative_agent` headless and dump `session.state` + curated
`session.events` + the eval report + the concept images into `screenshot-fixtures/`.
