# Frontend UI Screenshots

Reference captures of the Next.js frontend (`frontend/`), used in the README and PRs.

| File | Page | Shows |
|---|---|---|
| `01-home-form.png` | `/` | Campaign input form (agent selector + brand/audience/product/selling-points/trend) |
| `02-run-creative.png` | `/run/[sessionId]` | Live run view for `creative_agent`: campaign sidebar, event-stream timeline, pipeline widgets, Cloud Storage + research-report links, completion state |
| `03-results-creative.png` | `/results/[sessionId]` | Results gallery: creative-eval summary + per-concept **real generated image** with ad-copy/visual-concept scores across the LLM-as-judge dimensions |
| `04-run-interactive-review.png` | `/run/[sessionId]` | `interactive_creative` paused at the "Review Ad Copies" human-in-the-loop checkpoint |

All four reflect one consistent campaign — **Paul Reed Smith (PRS) / SE CE24 Electric
Guitar / Powerball trend** — harvested from a single real `creative_agent` run.

## Regenerating

These are captured with Playwright against the local dev server with **all backend
calls route-mocked** — no live Agent Engine, GCP creds, or model quota needed at
capture time. The mocks are hydrated from committed fixtures in
`frontend/scripts/screenshot-fixtures/`, which were harvested from ONE real
`creative_agent` run (session state, curated event log, eval report, and the four
concept images). So `03-results-creative.png` shows the **actual
`gemini-3.1-flash-image` renders** (downscaled to lightweight JPEG fixtures), not
placeholders.

To refresh:

```bash
cd frontend
npm run dev          # terminal 1 — serves http://localhost:3000
npm run screenshots  # terminal 2 — runs scripts/capture-screenshots.mjs → docs/screenshots/*.png
```

The capture script (`frontend/scripts/capture-screenshots.mjs`) route-mocks
`/api/adk/**` and `/api/gcs?**` from the fixtures, seeds `sessionStorage`, and writes
all four PNGs (viewport 1440×900 @2×). To harvest a fresh run's fixtures (new campaign
or trend), re-run `creative_agent` headless and dump `session.state` + curated
`session.events` + the eval report + the concept images into `screenshot-fixtures/`.
