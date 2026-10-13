# Frontend Review: Consider + Nit Follow-ups Implementation Plan (6 parallel PRs)

**Goal:** fix the worthwhile Consider and Nit findings still open in `frontend/code_review.md`, with minimal wall-clock time.

**Architecture:** six PRs, each owning one area of the codebase so they can all run at once:
- **A:** run-page state machine extracted into a tested hook;
- **B:** results, personalisation and People correctness;
- **C:** experiments correctness and hygiene;
- **D:** a11y, tests and config hygiene;
- **E:** API client hygiene;
- **F:** the backend poll payload.

Each PR is test-first, with mutation checks on new logic. Refactors are separated from behaviour changes by commit (code-review-and-quality: "separate refactoring from feature work"). One web + share deploy, plus one api deploy, at the end.

**Tech Stack:** Next.js 16 / React 19 / Vitest + RTL; Python 3.13 / FastAPI / pytest for F.

---

## Context

The frontend review's Critical, Required and perf items are all done (#365–#371, #379–#381). The Consider and Nit list remains. I triaged each open item by value against effort and conflict risk, and assessed them with the code-review-and-quality axes.

**Selected (worth doing now):**
- **Correctness bugs and races:** personalise late lookup, eval-report shape, experiments "held" status, metrics polling, people/share-dialog races, all-zero chart axis, double submit.
- **Missing tests on important logic:** the run state machine, `ghostGap`, IAP expiry and algorithm, shared helpers.
- **A structural fix that addresses the root cause of four earlier bugs:** extracting `useRunFollower`.
- **Cheap a11y and design-system fixes:** a skip link, the contrast of the Explain switch, entrance animations.
- **Correctness of user-facing error text.**
- **A real perf saving:** the poll payload, currently tens of MB per open run tab.
- **Dead code:** listed explicitly, so approving this plan is the approval to delete it.

**Deliberately not selected (low value or high churn for now):**
- splitting the 1,000-line experiments modules (big diff, no behaviour gain);
- `noUncheckedIndexedAccess` (repo-wide churn);
- unifying the five date formatters (cosmetic, touches many files);
- the possibly-dead `stopArrows` (needs a real-browser check first);
- pinning base images by digest (maintenance cost).

These stay listed in `code_review.md`.

**Facts confirmed during planning:**
- `state` in the `/runs` poll payload is only read from `since=0` calls: the page's seed (`run/[sessionId]/page.tsx:252`) and `resumeOutcome` (`lib/api.ts:353`). The stale-tab watcher (`:324`) and `pollRun` use only events and the cursor. So **F can drop `state` for `since>0` server-side with no frontend change.**
- The review's file:line references predate #365–#381; implementers re-locate against current main.

**Constraints (every PR):**
- `cd frontend && npm ci && npm run lint && npm test && npm run build` (F: `uv run ruff format --check . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 8`).
- Read `frontend/AGENTS.md` (Next 16 docs in `node_modules/next/dist/docs/`).
- TDD, plus mutation checks on new or changed logic, restored with `git diff --exit-code`.
- No Co-Authored-By or AI attribution. Open a PR; I merge after CI is green.
- Only the listed files.

## File ownership

| PR | Branch | Files (exclusive) |
|---|---|---|
| **A** | `refactor/run-follower` | `src/app/run/[sessionId]/page.tsx`, new `src/app/run/[sessionId]/use-run-follower.ts`, `src/app/run/[sessionId]/review-drafts.ts`, new `src/__tests__/use-run-follower.test.tsx`, run-page tests that need updating |
| **B** | `fix/results-area` | `src/app/results/[sessionId]/page.tsx`, `results/[sessionId]/proof-grid.tsx` (only to move `ProofImage` out), new `src/components/proof-image.tsx`, `src/components/personalise-panel.tsx`, `src/components/variant-compare.tsx` (import only), `src/lib/eval-report.ts`, `src/app/people/page.tsx`, `src/components/share-dialog.tsx`, `src/components/person-select.tsx`, `src/components/gcs-widget.tsx`, new `src/lib/results-paths.ts`, tests |
| **C** | `fix/experiments-area` | `src/lib/experiment-insights.ts`, `src/app/experiments/[experimentId]/page.tsx`, `src/lib/chart.ts`, `src/lib/experiment-view.ts`, `src/lib/shifts.ts`, `src/lib/scenario-preview.ts`, `src/lib/creative-detail.ts`, new `src/lib/num.ts`, tests |
| **D** | `chore/frontend-hygiene` | `src/__tests__/iap-identity.test.ts`, `src/lib/gcs.ts`, `src/lib/utils.ts`, `src/app/layout.tsx`, `src/components/explain.tsx`, `src/app/globals.css`, `src/components/ui/dialog.tsx`, `src/components/ui/select.tsx`, `src/lib/share-snapshot.ts`, `next.config.ts`, `package.json` (eslint-config-next range only) + lockfile, `src/lib/person-paths.ts`, `src/app/page.tsx` (submit fix only), `frontend/Dockerfile` (telemetry env only), tests, plan copy |
| **E** | `fix/api-client` | `src/lib/api.ts`, `src/__tests__/` api/poll tests (e.g. `poll-run.test.ts`, a new `api-errors.test.ts`) |
| **F** | `perf/poll-payload` | `runserver/async_runs.py`, `tests/test_async_runs.py` |

**Shared-file notes:**
- B changes one import line in `experiments/[experimentId]/page.tsx` (C's file). Git merges disjoint lines cleanly; if it conflicts, rebase whichever lands second.
- D's `package.json` edit is limited to the `eslint-config-next` range.

---

## PR A — `useRunFollower` (refactor, then fixes)

**Why:** the run page's poll, dedupe, status machine, watchdogs and resume/continue logic lives inside the 700-line `RunPage` and is barely tested. That is the root cause of four of the five Required bugs. The stale-tab watcher still has **no tests**; a review mutation survived.

**Commit 1, refactor (no behaviour change):**
- Move the follower logic into `use-run-follower.ts`: `useRunFollower(appName, userId, sessionId, opts)` returns `{events, status, sessionState, pauseContext, stepErrors, notice, resume, continueRun, reload}`.
- Keep the kick-off claim (`run-kickoff.ts`) and pause detection (`lib/pause-detection.ts`) as they are, and reuse them.
- `page.tsx` becomes composition only.

**Tests (written against the hook with `renderHook` + fake timers, mocked `@/lib/api`):**
- events are consumed and deduped across a replay from `since=0`;
- a pause is detected and resume resumes;
- "stalled" sets after `stallTimeoutFor(app)` and clears on the next event;
- the stale-tab watcher clears the pause when another tab answered (**new coverage**);
- `RunConnectionLostError` → "disconnected";
- a 409 re-shows the panel.

Existing page tests stay green.

**Commit 2, fixes:**
- **Batch events per poll:** `setEvents(prev => prev.concat(batch))` instead of one `[...prev, event]` per event, which is O(n²) on replay.
- **Evict review drafts** after a successful resume (`review-drafts.ts`: a `clearDraftsFor(functionCallId)` called on success).

**Mutations:** remove the stale-tab `isPauseAnswered` check; remove the stall auto-clear; each must fail a hook test.

**Expected impact:**
- **Reliability:** the run page's state machine becomes testable and tested, so regressions like the four fixed in #366 and #369 get caught.
- **Speed:** long runs and replays stop doing quadratic event copying.
- **Memory:** the draft store no longer grows for the life of the tab.
- **No visible UI change.**

## PR B — Results, personalisation and People correctness

1. **Personalise panel late lookup.** In `personalise-panel.tsx`, the initial "earlier preview" lookup can overwrite a render that is already queued, so the UI loses track of the live render. Fix with `setVariant(prev => prev ?? latestVariant(…))`. Test: render → click Render before the GET resolves → the late GET doesn't reset the queued status.
2. **Lift personalisation data.** Each proof navigation refetches the people list (lazily sized server-side, so expensive) and the variants listing. Add a `usePersonalisationData(appName, sessionId)` hook in the results page that loads both once and refreshes after a create or a finished poll, and pass `people`, `castable[concept]` and the variants as props. Test: navigating across 3 proofs calls `listPersonRefs` once and `listVariants` once (plus refreshes).
3. **Eval-report boundary.** A malformed or older report currently throws and breaks the whole results page. Add `parseEvalReport(raw): EvalReport | null` in `lib/eval-report.ts` (arrays default to `[]`, entries without a score are dropped); on null the page shows its existing "no evaluation" state. Tests: missing arrays, partial scores, non-object → null.
4. **`useResultsData` + output dir.** Extract the results page's loading and the duplicated bucket/folder derivation into `lib/results-paths.ts` (`outputDir(state)`, `bucketFromState(state)`; also use it in `gcs-widget.tsx`, replacing its hand parse with `parseGsUri`). Memoize `JSON.stringify(state, null, 2)` (`useMemo`) or render it in a child component. Keep the existing cancellation guard. Tests for `outputDir` / `bucketFromState`.
5. **People page race.** `setPeople` replaces the list after an optimistic add. Merge the load by `consent_id` (local entries win). Test included.
6. **Share dialog busy state.** Reopening during a create resets `busy`, so a late result lands in the "fresh" dialog. Use a request-id ref and ignore stale results. Test included.
7. **Person-select help text.** "Photographic concepts only" is wrong since 2026-10-10. Change it to "person-safe styles (photographic, 3D, comic, flat, collage)" or derive it from the list. The test asserts the new copy.
8. **Move `ProofImage`** to `src/components/proof-image.tsx`, and update the imports in `proof-grid`, the personalise panel, `variant-compare` and the experiments page. The experiments route then stops importing results-route code.

**Expected impact:**
- **Fewer requests:** proof navigation stops refetching per creative (8–12 requests across 4 proofs become 2).
- **No more silently wrong UI states:** a live render that vanished, a "Rated" list wiped on load, a share link landing in a fresh dialog.
- **Robustness:** one malformed report can't break the results page.
- **Accurate consent text:** the person picker now says which styles it can actually be used in.

## PR C — Experiments correctness and hygiene

1. **"Held" status bug.** In `experiment-insights.ts`, `cardStatus` returns "held" while some episodes never recovered. Require `recovered >= n` and `rec !== null`, else fall through to "partly". Tests: rec above the floor; partial recovery with after ≥ 80%. This kills the surviving `recoveryFloor` mutation.
2. **`ghostGap` fallback tests.** One exact-value test (ghost 120±30 vs linear_ts 100±40, n=25 → diff 20, half 19.6), plus the "cost" sentence for diff > 0 and the "helped" sentence for diff < 0. This kills the surviving √n and sign mutations. The code is unchanged; the math is correct.
3. **Metrics polling.** The experiment page's metrics poll is a fixed `setInterval` with no timeout, no single-flight guard and no ordering. Replace it with a self-scheduling loop: `await loadMetrics(); await wait(...)`, a per-request timeout (reuse the `getExperimentWithTimeout` pattern from `lib/experiments.ts`) and a sequence guard that drops stale responses. Tests with fake timers: no overlap when a response takes longer than the interval; a stale response is ignored.
4. **Stable derived state.** Each 5 s status poll rebuilds insights and the shift preview. Keep the previous `exp` object when `updatedAt`, `status` and `progress` are unchanged, so the memos hold. Test: identical polls don't change the `exp` identity.
5. **All-zero chart axis.** `niceDomain` gives an all-zero click-rate series a negative axis (−100%). Return `[0, 1]` for that case. Test included.
6. **Leader sort.** `headlineFor` relies on the API ordering creatives by `finalShare` (contracts §8). Sort locally so the headline can't name the wrong creative. Test with unordered input.
7. **Shared helpers:** new `lib/num.ts` (`finite`, `round4`, `joinList`), used by `shifts`, `scenario-preview`, `creative-detail`, `experiment-insights` and the page's `joinCounts`.
8. **Dead exports:** remove `downsample` (`chart.ts`; tests-only, so update those tests to `downsampleIndices`), `VIEWS` (`experiment-view.ts`) and `fracText` (`shifts.ts`), after a fresh `grep` confirms zero production references.

**Expected impact:**
- **Correct conclusions:** a shift card can no longer claim "held" when some episodes never recovered.
- **Guarded statistics:** a regression in the shift-cost math would now fail CI instead of telling users the opposite conclusion.
- **Steady metrics:** no stacked or out-of-order metric requests while BigQuery is slow (curves no longer jump backwards).
- **Smaller fixes:** less CPU per poll, no −100% axis, and less duplicated code.

## PR D — a11y, tests and config hygiene

1. **IAP token tests:** expired token outside the 30 s tolerance → rejected; 20 s inside it → accepted; ES384- or HS256-signed → rejected. Kills the surviving `clockTolerance` and algorithm-pin mutations. Tests only.
2. **Helper bugs and tests:**
   - `lib/gcs.ts` `parseGsUri("gs://bucket")` returns garbage; make it return `null`, and test it;
   - add a `formatStateValue(3) === "3"` test (`lib/utils.ts`; fix it if it's wrong).
3. **Skip link:** in `app/layout.tsx`, add `<a href="#main" className="sr-only focus:not-sr-only …">Skip to content</a>` and `<main id="main" tabIndex={-1}>`. Test: the link exists and targets `#main`.
4. **Explain switch contrast:** `explain.tsx` uses `bg-[#b9c2ca]` (1.81:1) → a new `--control-off` token in `globals.css` at 3:1 or better (WCAG 1.4.11). Check whether the shared `--input` border (1.4:1) can also move to 3:1 without breaking the "proof room" look; adjust it only if it stays within the design rules, and note the decision.
5. **Entrance animations:** drop the shadcn `animate-in fade-in zoom-in-95` classes from `ui/dialog.tsx` and `ui/select.tsx`, per the design rule "no entrance animations".
6. **Snapshot size cap:** `share-snapshot.ts` checks size after buffering the whole body, and counts characters rather than bytes. Check `content-length` first and cap the bytes read. Test with an oversize body → null.
7. **Config:**
   - `next.config.ts` `allowedDevOrigins` comes from an env var (`ALLOWED_DEV_ORIGINS`, comma-separated) instead of a committed personal workstation hostname;
   - align the `eslint-config-next` range with `next` (`^16.4.0`);
   - add `ENV NEXT_TELEMETRY_DISABLED=1` in the `frontend/Dockerfile` runner.
8. **Double submit:** `app/page.tsx` re-enables the submit button before navigation, so a double submit creates an orphan "Not started" session. Reset `loading` only in `catch`. Test: a second click while navigating doesn't call `createSession` twice.
9. **Dead export:** remove `personRefsPrefix` (`lib/person-paths.ts`; tests-only, so drop those tests) after confirming zero production references.
10. **Plan copy.**

**Expected impact:**
- **Accessibility:** keyboard users skip the header, and the Explain switch is visible to low-vision users (WCAG 1.4.11).
- **Design system:** dialogs follow the "no entrance animation" rule.
- **Security guard:** a regression in JWT expiry or algorithm checking now fails CI.
- **Config:** no personal hostname in the config, and no Next telemetry from production.
- **No orphan sessions** from double submits.

## PR E — API client hygiene

1. **Readable error messages.** `res.statusText` is empty over HTTP/2/3, so users see "Failed to get session: ". Add one `httpError(res, what)` helper (status + JSON `detail` when present) and use it in the session and artifact functions, matching the existing typed error classes. Test: an h2-style response with `statusText: ""` and `{detail: "Session not found"}` → "Failed to get session (404): Session not found".
2. **Encoded path segments:** wrap `appName`, `userId` and `sessionId` in `encodeURIComponent` on every path-building call. Test: an `app` containing `../` is encoded.
3. **429 detection:** anchor the regex as `\b429\b`. Tests: "429" inside an ID → not a quota error; a 429-only message → a quota error.
4. **Dead code:** remove `listApps` (no callers) and `getArtifact` (unused since #368), after a fresh `grep`.

**Expected impact:** error banners tell users what went wrong, path-building is defence-in-depth against crafted `?app=` values, quota detection no longer misfires on IDs containing 429, and there are two fewer dead exports.

## PR F — Smaller poll payloads (backend)

**Fix:** in `runserver/async_runs.py`, the poll handler includes `"state": dict(session.state)` only when `since == 0`. For `since > 0` it omits the key, or sends `{}` if the response model requires it. Keep `not_found` as is.

**Tests (first, `tests/test_async_runs.py`):**
- `since=0` includes the full state;
- `since=5` omits it;
- existing tests that read state at `since > 0` (if any) are adjusted to `since=0`.

Then check the frontend: the hook in PR A and `pollRun` read `state` only from `since=0`, which was confirmed during planning.

**Expected impact:** every 1.5 s poll stops re-sending the whole session state (research report, sources, brief, concepts), about tens of MB per open run tab over a run. That means less api CPU (JSON serialisation per poll), less egress, and snappier polls on slow links. No UI change.

---

## Execution and deploy

1. **t=0:** dispatch A–F in parallel (worktrees). A and B are the largest, at about 60–75 min.
2. **Review:** review each PR with code-review-and-quality: tests first, mutation evidence, five axes, severity-labelled feedback. Merge as CI goes green. Rebase B or C if the one-line import collides.
3. **Deploy:**
   - **web + share:** one `cloudbuild.yaml` build, then `gcloud run deploy trend-trawler-web --image <digest> --no-traffic`, pin it (`prev` = the serving revision; `GCS_ALLOWED_BUCKETS` carries over), and redeploy share with the same digest.
   - **api, for F:** deploy the build's api digest `--no-traffic`, pin only after the idle check (no "run start" in the last 15 min). Revision changes kill in-flight detached runs.
   - **Verify:** web 302 unauthenticated; share `robots.txt` 200 and `/` 404; api 403 unauthenticated with clean startup logs.
4. Mark the addressed items in `frontend/code_review.md` (a local file) and update memory.

**Estimated total:** about 75–90 min for the parallel PRs, plus about 15 min for the deploys.

## Verification

- **All:** frontend lint, about 1,370+ tests and the build are green; the Python gates are green for F; the listed mutations each fail a test.
- **A:** the new hook tests pass. **Live:** follow a creative run end to end: the stage spine advances, nothing stalls falsely through rendering, and the results link appears.
- **B:** **live:** arrow through 4 proofs with the Network panel open: one `person-refs` and one `variants` request, not 4 each. Personalise still works (render, poll, compare).
- **C:** **live:** an experiment page polls metrics without overlapping requests (Network panel).
- **D:** Tab on any page: the first stop is "Skip to content". The Explain switch's off state is visibly outlined. Dialogs open without a zoom animation.
- **E:** **live:** opening a deleted session shows a readable error.
- **F:** **live:** during a run, poll responses after the first omit `state` (DevTools: smaller response sizes), and the run page still shows outputs and stages correctly.
