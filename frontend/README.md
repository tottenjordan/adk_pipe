# Trend Trawler frontend

The web UI for Trend Trawler: write a campaign brief, start one of the three agents
(`trend_scout`, `creative_agent`, `interactive_creative`), follow the run live, answer
the review checkpoints, and look through the finished creatives.

**Stack:** Next.js 16 (App Router), React 19, TypeScript, Tailwind CSS 4, and
shadcn/ui on `@base-ui/react`.

**Design system ("proof room"):** design tokens live in `src/app/globals.css`, where
`primary` is the only action colour and `mark-pass`/`mark-fail`/`mark-pending` are the
status marks. Type is Archivo, labels use the sentence-case `FieldLabel`
(`src/components/field-label.tsx`), and mono is only for code-like data. The rationale
is in [the proof-room plan](../docs/plans/2026-10-02-frontend-proof-room.md).

> **Next.js version note:** see [`AGENTS.md`](AGENTS.md). This Next.js release has
> breaking changes compared with older versions, so check the guides in
> `node_modules/next/dist/docs/` before you write framework code.

## Pages

| Route | What it does |
|---|---|
| `/` | Campaign brief form, agent tiles, and a recent-runs sidebar. Creative agents also get the optional **Person (optional)** select (the caller's active consents) and the per-run **Learn from past ratings for this brand** checkbox (off by default; both restored by **Duplicate brief**). A trend carried over from a trend card flagged sensitive shows an amber "Sensitive trend" alert (cleared when the trend is edited) |
| `/people` | Person references: the caller's upload prefix (`gs://<bucket>/person-refs/<slug>/`), the consent text, photo guidance, a register form (photo `gs://` URI, name, who, adult attestation, optional public-share consent), and the caller's people (thumbnail, public links yes/no, a low-resolution note) with a confirmed **Revoke** that deletes the photo and every image made with it |
| `/runs` | Run history (brand, trend, agent, status, updated). **Duplicate brief** fills in a new run from an old one. A muted "Judge agreement" line shows kappa and agreement % once 20 of your ratings pair with a current-judge verdict (else "Rate N more creatives…"), plus a learned vs not-learned kappa line |
| `/run/[sessionId]` | Live run view. It polls the async-job run (`GET /runs/...?since=N`), so a run keeps going through a reload or disconnect. Shows a stage spine, the current stage, outputs so far (incl. the rating-learning summary and a muted "Current moment: …" line for the trend's validated situation), and a collapsed technical log. Failure comes only from the poll status: an error event in the log (a step that failed and was contained) is a muted, non-blocking notice "A step reported an error and was skipped: …" (`lib/step-errors.ts`), so the run keeps being followed. Review checkpoints take over the main area (the trend pick shows each trend's one-line "why it's trending" summary and the Google Trends refresh date, and marks flagged trends with an amber "Sensitive" chip, still selectable; checkpoint 1 is an editable structured brief, checkpoint 2 accepts revision feedback once). A run that stopped early shows **Continue run** |
| `/results/[sessionId]` | Contact sheet of creatives with scores (and a "Check failed" chip when a blocking eval gate failed; a "Rated" mark once rated), and a proof-detail dialog per creative: a **Checks** section (pass/fail per gate, advisory ones labelled) above the advisory quality scores, the image check, a "Timely: …" row for the creative that references the current moment (a "Timely" chip on the contact sheet that turns "Expired" once the moment's `valid_until` has passed), the person-casting row, **Your rating** (pass/fail, optional fail-reason chips on a fail, optional 1–5 score + note for copy and visual), **Share this creative**, and a **Personalise** panel (a preview with one of your consented people; "Swap person" on a creative that already casts one). The page also shows the rating-learning summary, the "Current moment" line, a research report panel, the eval report and artifacts. The **Deploy creatives as a live experiment** panel starts a bandit experiment from 2–4 creatives (cast creatives can be picked; the api accepts them while their consent is active; an expired timely creative gets a muted warning line). **Share slate** (header) and **Share this creative** create a public, revocable link to a frozen snapshot, optionally with judge checks (the dialog notes any shared creative's failed blocking checks; expired timely creatives are left out); **Shared links** lists this run's links with Copy and Revoke |
| `/s/[token]` | Public share page (no sign-in; served by the `trend-trawler-share` service with `SHARE_MODE=1`, also reachable behind IAP as an owner preview): "<brand> × <trend>", a Card / Feed / Story view switcher (`?view=`), one article per creative, optional judge checks when the share includes them, Open Graph/Twitter metadata, noindex. Images come from `/s/<token>/img/<n>`. A missing or revoked link renders "This link is no longer available" (404) |
| `/experiments` | Bandit experiments list: scenario, creatives, status, created, endpoint lifetime |
| `/experiments/[experimentId]` | One experiment: status and TTL, **Start traffic** / **Stop**, the arms, and SVG charts (reward vs oracle, regret, % optimal, arm share, per-segment winners, total reward ± std, per-arm CTRs). Above **Start traffic**, the shift editor scripts up to four behaviour shifts for the next traffic run; traffic runs are numbered and `?run=N` selects one (newest by default). With shifts, the charts use a linear round axis with a rule per shift and a dashed ghost line, and the Overview adds one result card per shift. An arm whose person consent was revoked shows "Image removed (consent revoked)". See the [bandit guide](../docs/bandit/README.md#scripted-behaviour-shifts) |

## Source layout

```
src/
├── app/
│   ├── layout.tsx, page.tsx, globals.css, robots.ts
│   ├── people/page.tsx
│   ├── runs/page.tsx
│   ├── run/[sessionId]/page.tsx, stage-spine.tsx, run-sections.tsx, run-widgets.tsx,
│   │   ReviewPanel.tsx, review-brief.tsx, report-editor.tsx, …
│   ├── results/[sessionId]/page.tsx, proof-grid.tsx, proof-detail.tsx, eval-checks.tsx,
│   │   rating-control.tsx, deploy-panel.tsx, reader-tuning.tsx, …
│   ├── s/[token]/page.tsx, not-found.tsx, img/[n]/route.ts
│   ├── experiments/page.tsx
│   ├── experiments/[experimentId]/page.tsx, experiment-charts.tsx,
│   │   shift-timeline.tsx, run-selector.tsx, shift-results.tsx
│   └── api/
│       ├── adk/[...path]/route.ts
│       └── gcs/route.ts
├── components/          # app components, share/ (public share viewer), charts/, ui/ (shadcn primitives)
├── lib/                 # pure logic, unit-tested
├── proxy.ts             # Next 16 Proxy (renamed Middleware): SHARE_MODE lockdown + share headers
└── __tests__/           # Vitest + React Testing Library
```

### Key modules

- `lib/api.ts`: API client for session CRUD, the async-job `startRun`/`pollRun`/`resumeRun` calls, and artifact fetching
- `lib/run-history.ts`: turns the session list into run-history rows (status, trend, agent)
- `lib/run-stages.ts`: per-agent stage lists, with progress derived from session state (feeds the stage spine)
- `lib/run-completion.ts`: detects a run that stopped early and holds the **Continue run** message
- `lib/pause-detection.ts`: finds the unanswered long-running call that marks a real review pause
- `lib/run-kickoff.ts`: kick-off guard that stops a reload or StrictMode remount from starting a second run
- `lib/eval-matching.ts`: eval-report types; pairs each visual concept with its ad copy and both verdicts
- `lib/eval-dimensions.ts`: short labels for the 12 `creative_eval` dimensions and the gate labels (mirrors `creative_eval/dimensions.py`, drift-tested)
- `lib/step-errors.ts`: turns error events in the polled log into the run page's non-fatal "A step reported an error and was skipped" notice (429 → friendly quota text); only the poll status marks a run as failed
- `lib/run-warnings.ts`: `classifyWarnings` groups the eval report's run notes into research / saves / quality notices for the results page
- `lib/timely.ts` + `lib/use-wall-clock.ts`: the timely creative's expiry rule (`timelyStatus`, mirrors `runserver/timely.py`, both pinned by `tests/fixtures/timely_expiry.json`) and the minute-refreshed wall clock behind the Timely/Expired chip
- `lib/trend-flags.ts` + `components/sensitive-chip.tsx` / `lib/trend-summaries.ts`: trend_scout's sensitivity flags (`trend_flags`) and the amber "Sensitive" chip; the per-trend one-line summaries (`trend_summaries`) shown in the trend pick and on trend cards
- `lib/trend-situation.ts` + `components/trend-situation-line.tsx`: the trend's validated current moment (`state.trend_situation`) as a muted read-only line ("Texas vs Oklahoma tomorrow", "Not timely: <reason>", sources, valid until); nothing when the feature is off
- `lib/creative-brief.ts` / `lib/brief-edit.ts` + `app/run/[sessionId]/review-brief.tsx`: the structured `creative_brief` and the checkpoint-1 edit form (`toBriefPayload` back to the snake_case schema the backend validates)
- `lib/ratings.ts` / `lib/rating-reasons.ts` + `app/results/[sessionId]/rating-control.tsx`: human ratings types and helpers, the allowlisted fail-reason chips per kind (mirrors `runserver/rating_reasons.py`, drift-tested), and the proof detail's **Your rating** control
- `lib/rating-learning.ts` + `components/learning-summary.tsx` / `learn-from-ratings-toggle.tsx`: what opt-in rating learning did to a run (`describeRatingLearning`), the "Learned from N team ratings…" card, and the per-run opt-in checkbox
- `components/judge-agreement.tsx`: the `/runs` judge-agreement line (current judge version only)
- `lib/person-refs.ts` / `lib/person-consent.ts` / `lib/person-paths.ts`: person-reference types and error copy for `/people`, the consent text + version (mirrors `runserver/person_refs.py`, drift-tested), and the owner-only GCS path rules (`person-refs/<slug>/…`, `…/variants/<slug>/…`) that `/api/gcs` enforces
- `components/person-select.tsx`: the brief form's "Person (optional)" select
- `lib/variants.ts` + `components/personalise-panel.tsx`: personalised variant previews (add or swap mode, castability notes, polling every 5 s while rendering, base and preview side by side)
- `lib/shares.ts`: share types, reason → message map and the "left out" notice for creatives whose person consent doesn't cover public links
- `lib/share-mode.ts`: pure `SHARE_MODE` route rules, share headers and the per-request nonce CSP used by `src/proxy.ts`
- `lib/share-snapshot.ts` (server only): snapshot v1 validation and token-scoped Cloud Storage reads (`shares/<token>/snapshot.json` and the named `<n>.jpg`/`<n>.png` image, content type from the extension)
- `lib/share-metadata.ts` / `lib/share-views.ts`: link-preview metadata (`SHARE_BASE_URL` makes `og:image` absolute) and the Card / Feed / Story view helpers
- `lib/gcp-auth.ts`: OAuth access token for Google APIs (metadata server / ADC on Cloud Run, gcloud CLI locally)
- `components/share/*` (`share-view`, `share-card`, `feed-post`, `story-frame`, `share-eval`, `share-image`): the public share viewer
- `components/site-header.tsx`: the header, which renders nothing on `/s/*`
- `app/robots.ts`: `Disallow: /` in both modes
- `lib/research-report.ts`: citation helpers for the cited research report (display and edit)
- `lib/agents.ts`: agent catalog (labels, descriptions, durations, review pauses)
- `lib/presets.ts`: preset values for the brief form
- `lib/initial-state.ts`: builds the `createSession` initial state (`ui_app`, trend-pick opt-in, visual-intent keys)
- `lib/experiments.ts`: bandit experiments API client (`createExperiment`, `startTraffic`, `stopExperiment`, `pollExperiment`, …), status and TTL helpers, deploy-selection payload, chart series shaping
- `lib/shifts.ts`: scripted behaviour shifts ([contracts §10](../docs/bandit/contracts.md#10-scripted-behaviour-shifts-per-traffic-run-2026-10-05)): the shift editor's model (defaults, presets, validation, the `startTraffic` payload with `forget`, one plain-language sentence per shift), tolerant readers for the per-run api fields (traffic runs, shift response, regimes, paired `shiftCost`), chart markers and recovery spans, and the run selector's `?run=N` URL state. Kinds and bounds come from `scenario-presets.generated.json`, never hand-copied; on an older api every reader returns nothing and the page renders as before
- `lib/scenario-preview.ts`: the Deploy panel's scenario preview; `applyShifts` ports the simulator's shift resolution (`bandit.environment._resolve_shifts`) to give the expected click rate per creative × segment in each period between shifts (pinned by `scenario-shifts-golden.json`)
- `lib/chart.ts`: dependency-free chart math (linear/log scales, nice ticks, line and band paths, downsampling, formatters)
- `components/charts/line-chart.tsx` / `bar-chart.tsx`: hand-drawn SVG line chart (bands, log x, `markers` for vertical event rules named in the hover readout, `spans` for shaded x ranges such as a post-shift recovery) and bar chart (error bars)
- `components/experiment-status.tsx`: experiment status label
- `app/experiments/[experimentId]/shift-timeline.tsx`: the shift editor above **Start traffic**: a run timeline with one draggable pin per shift, an **Add shift** menu (four kinds plus three presets), a sentence per shift, the period-by-period preview, and the **Let the endpoint forget old evidence** toggle (on by default when there are shifts)
- `app/experiments/[experimentId]/arm-image.tsx`: an arm's image, with the "Image removed (consent revoked)" placeholder for a cast arm whose consent was revoked
- `app/experiments/[experimentId]/run-selector.tsx`: which numbered traffic run the results show (`?run=N`); a plain label with one run, a select with several
- `app/experiments/[experimentId]/shift-results.tsx`: one Overview result card per shift (best-creative rate before → after, rounds to recover, evidence; "Too early to call" under five episodes)
- `app/results/[sessionId]/deploy-panel.tsx`: the Deploy panel (creative picks, scenario, CTR/reward mode, TTL)
- `components/share-dialog.tsx`: the share dialog (include-checks toggle, Create link, Copy with a "Link copied" announcement, Open, friendly errors from `lib/shares.ts`)
- `components/shares-list.tsx`: the results page's "Shared links" section (this session's links, Copy, Revoke with a confirmation)
- `components/research-report.tsx`: renders the research report with numbered citations
- `components/main-nav.tsx`: header nav that highlights the active page
- `components/run-list.tsx`: run-history list used on `/runs` and in the home sidebar
- `app/api/adk/[...path]/route.ts`: same-origin proxy to the private backend. It verifies the IAP JWT and scopes every request to the caller (`lib/iap-identity.ts` + `lib/user-scoping.ts`). Only `since`/`version` query params are forwarded, plus `run` (a positive integer) on `GET experiments/{u}/{id}/metrics|creatives`. Allowlisted routes beyond sessions, `/runs` and `/experiments`: ratings (`GET`/`PUT ratings/{u}/{session}`, `GET ratings/{u}/calibration`), shares (`POST shares/{u}/{app}/{session}`, `GET shares/{u}`, `DELETE shares/{u}/{token}`), person references (`GET`/`POST person-refs/{u}`, `DELETE person-refs/{u}/{consent_id}`) and variants (`POST`/`GET variants/{u}/{app}/{session}`). The two DELETEs are the only ones it forwards; ids are checked against `SESSION_ID_RE`, `SHARE_TOKEN_RE` and `PERSON_REF_ID_RE`
- `app/api/gcs/route.ts`: authenticated Cloud Storage proxy for serving artifacts (`/api/gcs?bucket=...&path=...`). Run artifacts are shared across users, but `person-refs/<slug>/…` and `…/variants/<slug>/…` are served only to their owner (`lib/person-paths.ts`; `Cache-Control: private, no-store`)

## Local development

Start the backend from the repo root. Use the async-job launcher, not bare
`adk api_server`, because the run page polls the `/runs` endpoints and only the
launcher mounts them:

```bash
TRUST_CLIENT_USER_ID=1 SESSION_SERVICE_URI=memory:// ALLOW_ORIGINS=http://localhost:3000 uv run uvicorn deployment.async_app:app --port 8000
```

Then start the frontend:

```bash
cd frontend
npm install
npm run dev   # http://localhost:3000
```

Environment variables (read server-side):

| Variable | Meaning | Local default |
|---|---|---|
| `ADK_API_BASE` | Backend URL that `/api/adk/*` forwards to | `http://localhost:8000` |
| `IAP_ALLOWED_HD` | Required Google Workspace domain (`hd` claim) on the IAP JWT. If it is unset on Cloud Run, every proxied call returns 401 | unset. Locally there is no JWT and no `K_SERVICE`, so the proxy passes requests through unscoped |
| `IAP_AUDIENCE` | Overrides the IAP audience that is otherwise looked up from the metadata server | unset |
| `NEXT_PUBLIC_API_BASE` | Client-side API base used by `lib/api.ts` | `/api/adk` |
| `GCS_ALLOWED_BUCKETS` | Comma-separated bucket allowlist for `/api/gcs`; on Cloud Run, person paths are refused while it is unset | unset (any well-formed bucket) |
| `SHARE_MODE` | `1` turns the app into the public share viewer: `src/proxy.ts` serves only `/s/<token>`, `/s/<token>/img/<n>`, `/_next/static/*`, `/robots.txt` and `/favicon.ico`; everything else is a 404 | unset (normal IAP app) |
| `GOOGLE_CLOUD_STORAGE_BUCKET` | Bucket the share pages read `shares/<token>/…` from (`gs://` tolerated) | unset (share pages fail) |
| `SHARE_BASE_URL` | Public origin of the share service; makes `og:image` absolute | unset (relative) |

## Testing

```bash
npm run lint    # ESLint
npm test        # Vitest + React Testing Library (tests in src/__tests__/)
npm run build   # next build, which also type-checks everything in tsconfig's include (tests too)
```

There are 108 test files in `src/__tests__/` (plus `setup.ts`, `helpers.ts` and
`fixtures/`), named after the module or component they cover (e.g. `poll-run.test.ts`,
`user-scoping.test.ts`, `share-view.test.tsx`, `personalise-panel.test.tsx`). The
grouping by feature is in [docs/reference/testing.md](../docs/reference/testing.md).

Vitest runs in the `node` environment by default (creating a jsdom for every
file used to dominate the run). A test that needs a DOM (`window`, `document`,
`localStorage`, React Testing Library, jest-dom matchers) must opt in with a
docblock on its first line:

```ts
// @vitest-environment jsdom
```

A DOM test that forgets the docblock fails loudly (`window is not defined`),
so there is no exclude list to keep in sync.

CI runs all three on PRs that touch `frontend/**`:
[`.github/workflows/frontend-tests.yml`](../.github/workflows/frontend-tests.yml).

## Screenshots and journey GIF

`npm run screenshots` (`scripts/capture-screenshots.mjs`) uses Playwright to capture
the reference PNGs. It needs a server on `:3000`, or one set with
`SCREENSHOT_BASE_URL`. Backend calls are not live: every `**/api/**` request is mocked
from the fixtures in `scripts/screenshot-fixtures/`, so no GCP credentials or model
quota are needed. `JOURNEY=1 npm run screenshots` captures annotated journey frames
instead, and the GIF is built with:

```bash
uv run --no-project --with pillow python scripts/build-journey-gif.py
```

The full recipe (capture against a production standalone build, frame metadata,
GIF knobs) and the list of captures are in
[docs/screenshots/README.md](../docs/screenshots/README.md).

## Deployment

The image builds from the [`Dockerfile`](Dockerfile) using Next.js
`output: "standalone"` (`next.config.ts`) and runs on Cloud Run as two services:

- `trend-trawler-web`: the IAP-gated app. It reaches the private `trend-trawler-api`
  backend (the `deployment/async_app.py` launcher) with a metadata-server ID token.
  Runbook: [deployment/README.md → Frontend + api_server on Cloud Run](../deployment/README.md#frontend--api_server-on-cloud-run).
- `trend-trawler-share`: the same image with `SHARE_MODE=1`, the only public service
  (`--allow-unauthenticated`). Its service account can read only the bucket's `shares/`
  prefix. Runbook: [deployment/README.md → Shareable links](../deployment/README.md#shareable-links).
