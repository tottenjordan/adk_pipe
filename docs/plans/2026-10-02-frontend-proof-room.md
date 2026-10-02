# Frontend Redesign Plan: "Proof Room" for daily creative teams

**Status:** complete 2026-10-02 (P0 #203, P1 #204, P2 #205, P3 #206, P4 this PR)

## Context
The Trend Trawler frontend (`frontend/`, Next.js 16 App Router, Tailwind 4, shadcn/ui on @base-ui) works, but it reads as a generic SaaS template, and it hides the product's real output.

**Generic tells:**
- 37 `text-[10px] font-bold uppercase tracking-wider` eyebrow labels.
- 29 `.glass` cards with identical radius and shadow.
- Monospace used for labels.
- Sora for everything, with a single blue.

**Usability problems:**
- **Home form:** every field is duplicated as a "Select a preset…" dropdown plus an input. The agent picker shows raw ids.
- **Run page:** the main column is a raw tool-call log, and there is no notion of a current stage. The interactive review panel renders below the event stream.
- **Results page:** each creative is followed by 12 dense rationale paragraphs in about 9px text.

**User decisions (2026-10-02):**
- Primary audience: **daily creative-team users**, so efficiency, density, history and keyboard flow matter.
- Direction: the **"proof room" redesign**.
- Scope: **all phases P0–P4**, one PR each.

**Intended outcome:** creatives are the hero, run progress is legible at a glance, past runs are one click away, and the UI has a distinct identity grounded in creative review (light table, contact sheets, grease-pencil marks).

## Design system (applies to all phases)
**Palette** (Tailwind theme tokens in `src/app/globals.css`, replacing the oklch blue set). The light theme stays; the declared dark variant is dropped unless it is actually used.

| Token | Hex | Role |
|---|---|---|
| `--background` light table | `#F2F5F7` | page |
| `--foreground` ink | `#1A1D21` | text, rules |
| `--primary` proof cyan | `#0077A8` | the only action colour: buttons, links, focus ring |
| `--mark-fail` grease pencil | `#C8102E` | fail scores and "needs revision" only |
| `--mark-pass` approve green | `#2E7D4F` | pass marks and approved checkpoints only |
| `--rule` | `#D5DBE0` | borders and dividers |

**Type:**
- **Archivo**, one variable family with the `wdth` axis, via `next/font/google` in `src/app/layout.tsx`, replacing Sora. Check Next 16's `next/font` axes API in `node_modules/next/dist/docs/` first (per `frontend/AGENTS.md`).
- Condensed heavy weights for creative headlines and scores. Normal width for body, with tabular numerals.
- Keep JetBrains Mono **only** for real code-like data: tool calls, GCS URIs, session ids in the technical log.
- Labels are sentence case, with no uppercase eyebrows.

**Shape and motion:**
- Three radii by hierarchy: 4px for controls, 8px for panels, 0 for images on the contact sheet.
- Remove `.glass`/`.glass-strong` and the staggered `fadeInUp` entrances. Keep `processing-dots`.
- Add a global `@media (prefers-reduced-motion: reduce)` rule.
- Add a visible 2px `--primary` focus ring on every interactive element.

**Copy:**
- Plain names: "Trend scout", "Creative run", "Creative run with reviews".
- Buttons say what happens: "Find trends", "Generate creatives".
- Errors give directions rather than apologies.

## Phases (branch + PR each; squash-merge after green CI; redeploy web per the recorded recipe)

### P0: Foundations (`feat/fe-proof-room-tokens`)
- `src/app/globals.css`: the new tokens, radii and focus ring; reduced-motion rule; delete `.glass*` and the decorative keyframes.
- `src/app/layout.tsx`: Archivo. A quiet header with the logo, "New run" and a "Runs" link (target added in P1).
- `src/components/ui/*` (button, card, input, select, badge, tabs): token-driven variants and the hierarchical radii.
- Replace the uppercase-label pattern everywhere with one `FieldLabel` component, e.g. `src/components/field-label.tsx`. Remove `glass` usages across the 8 files.
- Done when there is no `uppercase tracking-wider` or `glass` left in `src/` (grep), and every interactive element passes a keyboard tab-through.

### P1: Home + run history (`feat/fe-home-runs`)
- **Agent choice:** replace the hardcoded SelectItems (`src/app/page.tsx:160-180`) with three described choice tiles from a constant `AGENTS` (what it does, typical duration, whether it pauses for review), in a new `src/lib/agents.ts`. Keep the trend_scout "pick trends myself" option (183-207).
- **Presets:** remove the duplicate preset Selects (213/239/270/298). Each field becomes an input with suggestions from `src/lib/presets.ts` (native `<datalist>`, which is accessible and needs no new dependency).
- **Validation:** extract the inline `isValid` (113-118) to `src/lib/form-validation.ts`. Rewrite `src/__tests__/form-validation.test.ts` to import it, so it covers `interactive_creative` too, which the current copy misses.
- **Recent runs:** add a `/runs` page (`src/app/runs/page.tsx`), linked from the header and shown as a short list on home.
  - Uses the existing `listSessions` (`src/lib/api.ts:66`) for each app; the proxy already allows the user-scoped GET.
  - Rows show brand, trend, agent, time and status, sorted newest first, and link to run/results.
  - "Duplicate brief" pre-fills the form from a past session's state, reusing `buildInitialState` (`src/lib/initial-state.ts`) in reverse via a small tested helper.
- **Keyboard:** Cmd/Ctrl+Enter submits.
- Keep the visual-direction block (376-470), collapsed by default.

### P2: Run page stage spine (`feat/fe-run-stages`)
- New pure helper `src/lib/run-stages.ts`: `deriveStages(appName, state, pause, status)` returns an ordered list of `{id, label, state: done|active|needs_review|pending|degraded}`. It uses state keys the agents already write:
  - **trend_scout:** `raw_gtrends` → (`review_trends` pause) → `info_gtrends` → `selected_gtrends` → `select_trends_markdown_gcs_uri`.
  - **creative_agent:** `combined_final_cited_report` → `research_report_gcs_uri` → `ad_copy_critique` → `final_visual_concepts` → `_images_generated` → `eval_report_gcs_uri`.
  - **interactive_creative:** the same, with `review_research` / `review_ad_copies` / `review_visual_concepts` stages; the active pause's `functionName` marks `needs_review`.
  - `*__retry_exhausted` markers mark a stage `degraded`. Reuse the `imagesRetryExhausted` / `nonImageWarnings` logic in `src/lib/utils.ts`.
  - Tests go in a new `src/__tests__/run-stages.test.ts`.
- **Layout:** in `src/app/run/[sessionId]/page.tsx`, a numbered stage spine on the left (the numbers are justified because it is a real sequence) and a current-stage panel in the main area.
  - When paused, `ReviewPanel` (currently at page.tsx:556-564, below the stream) **takes over the main area**.
  - The event log (`src/components/event-log.tsx`) moves into a collapsed "Technical log" disclosure.
  - Campaign metadata becomes a one-line brief summary, expandable.
- `run-widgets.tsx`: replace "click to view" with a descriptive label and count ("4 ad copies").
- **ReviewPanel** keyboard: Cmd/Ctrl+Enter approves; there is a visible "Request changes" action.
- **Responsive:** the spine collapses to a horizontal step bar under `lg`.
- Pause, resume and stale-tab behaviour (`consumePollEvents`, `followRun`, the watcher) stay untouched.

### P3: Results contact sheet (`feat/fe-results-proofs`)
- Split the 880-line `src/app/results/[sessionId]/page.tsx` into components under `src/app/results/[sessionId]/`: `ResultsSummary`, `ProofGrid`, `ProofDetail`, `ArtifactsPanel`. Leave the raw session state collapsed as it is.
- **Eval matching:** extract the eval-to-creative matching (266-299) and `conceptNameToFilename` (110) into `src/lib/eval-matching.ts`, with new tests. Both are untested today.
- **Summary:** one sentence, e.g. "3 of 4 visuals pass. Weakest: trend connection." Human dimension names come from a new `src/lib/eval-dimensions.ts`, mapping the 12 snake_case dimensions.
- **ProofGrid:** a dense contact sheet of images, each with its headline and a condensed score mark (green for pass, grease-pencil for fail). It can be sorted by score.
- **ProofDetail** (dialog or inline expand, using the existing `components/ui/dialog.tsx`):
  - image on the left;
  - copy on the right;
  - a 6+6 dimension score strip;
  - rationale on demand.
  - Arrow keys move between proofs.
- Keep the existing banners (zero-image, degraded research, eval pending/refresh) and the visual-direction strip, restyled.

### P4: Polish, states and screenshots (`feat/fe-polish`)
- Rewrite error and empty states as directions: no runs yet, eval pending, artifact missing, run failed.
- Mobile pass at 390px on all four routes. Fix contrast so no text is below 12px or under 4.5:1.
- **Screenshot harness** (`frontend/scripts/capture-screenshots.mjs`):
  - Add a sessions-list fixture to `scripts/screenshot-fixtures/` and a `05-runs.png` capture.
  - Regenerate `docs/screenshots/01–05`.
  - Update the frontend sections of `README.md` and `CLAUDE.md`, including the pages list with `/runs`.

## Constraints
- Use `npm` in `frontend/`, and no new runtime dependencies unless unavoidable (Archivo comes through `next/font`).
- Read the Next 16 docs in `node_modules/next/dist/docs/` before using framework APIs.
- No `Co-Authored-By` trailers and no AI attribution in commits or PRs.
- Don't touch the backend, the proxy allowlist (`src/lib/user-scoping.ts`) or the auth code.
- Web redeploy recipe: `gcloud run deploy trend-trawler-web --source ./frontend --service-account tt-web-sa@…`, no env flags. Find the new revision by timestamp, then confirm IAP (302) and env.

## Verification (every phase)
1. In `frontend/`: `npm run lint && npm test && npm run build`. All existing suites plus the new tests must pass: `run-stages`, `eval-matching`, `form-validation`, duplicate-brief.
2. Visual check: run `npm run dev`, then `npm run screenshots`, and review the regenerated PNGs (Read tool) against the design system.
3. Grep gates after P0: no `uppercase tracking-wider`, no `glass` in `src/`, and `font-mono` only in the log, URI and id contexts.
4. Manual a11y:
   - keyboard tab-through on every route, with the focus ring visible;
   - the OS reduced-motion setting removes animation;
   - 390px viewport via Playwright (`page.setViewportSize`).
5. Live check after deploy: on the IAP URL, run one trend_scout run (interactive pick) and one creative run.
   - Confirm the stage spine advances.
   - Confirm the review takes over the main area and resume works.
   - Confirm `/runs` lists them.
   - Confirm results show the contact sheet.
