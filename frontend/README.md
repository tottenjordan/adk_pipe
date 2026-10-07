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
| `/` | Campaign brief form, agent tiles, and a recent-runs sidebar |
| `/runs` | Run history (brand, trend, agent, status, updated). **Duplicate brief** fills in a new run from an old one |
| `/run/[sessionId]` | Live run view. It polls the async-job run (`GET /runs/...?since=N`), so a run keeps going through a reload or disconnect. Shows a stage spine, the current stage, and outputs so far. Review checkpoints take over the main area. A run that stopped early shows **Continue run** |
| `/results/[sessionId]` | Contact sheet of creatives with scores (and a "Check failed" chip when an eval gate failed), a proof-detail dialog for each creative (eval checks above the advisory quality scores), a research report panel, the eval report, and artifacts. The **Deploy creatives as a live experiment** panel starts a bandit experiment from 2–4 creatives |
| `/experiments` | Bandit experiments list: scenario, creatives, status, created, endpoint lifetime |
| `/experiments/[experimentId]` | One experiment: status and TTL, **Start traffic** / **Stop**, the arms, and SVG charts (reward vs oracle, regret, % optimal, arm share, per-segment winners, total reward ± std, per-arm CTRs). Above **Start traffic**, the shift editor scripts up to four behaviour shifts for the next traffic run; traffic runs are numbered and `?run=N` selects one (newest by default). With shifts, the charts use a linear round axis with a rule per shift and a dashed ghost line, and the Overview adds one result card per shift. See the [bandit guide](../docs/bandit/README.md#scripted-behaviour-shifts) |

## Source layout

```
src/
├── app/
│   ├── layout.tsx, page.tsx, globals.css
│   ├── runs/page.tsx
│   ├── run/[sessionId]/page.tsx
│   ├── results/[sessionId]/page.tsx, deploy-panel.tsx
│   ├── experiments/page.tsx
│   ├── experiments/[experimentId]/page.tsx, experiment-charts.tsx,
│   │   shift-timeline.tsx, run-selector.tsx, shift-results.tsx
│   └── api/
│       ├── adk/[...path]/route.ts
│       └── gcs/route.ts
├── components/          # app components + ui/ (shadcn primitives)
├── lib/                 # pure logic, unit-tested
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
- `lib/eval-dimensions.ts`: short labels for the 12 `creative_eval` dimensions
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
- `app/experiments/[experimentId]/run-selector.tsx`: which numbered traffic run the results show (`?run=N`); a plain label with one run, a select with several
- `app/experiments/[experimentId]/shift-results.tsx`: one Overview result card per shift (best-creative rate before → after, rounds to recover, evidence; "Too early to call" under five episodes)
- `app/results/[sessionId]/deploy-panel.tsx`: the Deploy panel (creative picks, scenario, CTR/reward mode, TTL)
- `components/research-report.tsx`: renders the research report with numbered citations
- `components/main-nav.tsx`: header nav that highlights the active page
- `components/run-list.tsx`: run-history list used on `/runs` and in the home sidebar
- `app/api/adk/[...path]/route.ts`: same-origin proxy to the private backend. It verifies the IAP JWT and scopes every request to the caller (`lib/iap-identity.ts` + `lib/user-scoping.ts`). Only `since`/`version` query params are forwarded, plus `run` (a positive integer) on `GET experiments/{u}/{id}/metrics|creatives`
- `app/api/gcs/route.ts`: authenticated Cloud Storage proxy for serving artifacts (`/api/gcs?bucket=...&path=...`)

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

Environment variables (read server-side by the proxy):

| Variable | Meaning | Local default |
|---|---|---|
| `ADK_API_BASE` | Backend URL that `/api/adk/*` forwards to | `http://localhost:8000` |
| `IAP_ALLOWED_HD` | Required Google Workspace domain (`hd` claim) on the IAP JWT. If it is unset on Cloud Run, every proxied call returns 401 | unset. Locally there is no JWT and no `K_SERVICE`, so the proxy passes requests through unscoped |
| `IAP_AUDIENCE` | Overrides the IAP audience that is otherwise looked up from the metadata server | unset |
| `NEXT_PUBLIC_API_BASE` | Client-side API base used by `lib/api.ts` | `/api/adk` |

## Testing

```bash
npm run lint    # ESLint
npm test        # Vitest + React Testing Library (tests in src/__tests__/)
npm run build   # next build, which also type-checks everything in tsconfig's include (tests too)
```

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

The frontend runs on Cloud Run as `trend-trawler-web`. It builds from the
[`Dockerfile`](Dockerfile) using Next.js `output: "standalone"`
(`next.config.ts`). The service is IAP-gated and reaches the private
`trend-trawler-api` backend with a metadata-server ID token. Runbook:
[deployment/README.md → Frontend + api_server on Cloud Run](../deployment/README.md#frontend--api_server-on-cloud-run).
