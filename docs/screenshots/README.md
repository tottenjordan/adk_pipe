# Frontend UI Screenshots

Reference captures of the Next.js frontend (`frontend/`), used in the README and PRs.

| File | Page | Shows |
|---|---|---|
| `01-home-form.png` | `/` | Campaign input form (agent selector + brand/audience/product/selling-points/trend) |
| `02-run-creative.png` | `/run/[sessionId]` | Live run view for `creative_agent`: campaign sidebar, event-stream timeline, pipeline widgets, Cloud Storage + research-report links, completion state |
| `03-results-creative.png` | `/results/[sessionId]` | Results gallery: creative-eval summary, per-concept image + ad-copy/visual-concept scores across the LLM-as-judge dimensions |
| `04-run-interactive-review.png` | `/run/[sessionId]` | `interactive_creative` paused at the "Review Ad Copies" human-in-the-loop checkpoint |

## Regenerating

These are captured with Playwright against the local dev server with **all backend
calls route-mocked** (canned session/poll/artifact responses + placeholder concept
images) — no live Agent Engine, GCP creds, or model quota needed. The mock data is
illustrative (a fictional "Nimbus Athletics / CloudStride" campaign); concept images
are SVG placeholders, not real `gemini-3.1-flash-image` renders.

To refresh: start the frontend (`cd frontend && npm run dev`), then run the Playwright
capture script that mocks `/api/adk/*` and `/api/gcs` (see PR #91 for the script used).
