# Frontend Critical + Required Fixes Implementation Plan (5 parallel PRs)

**Goal:** fix the 1 Critical and 5 Required findings from `frontend/code_review.md` with the shortest wall-clock time, and close the security hole first.

**Architecture:** five small, independent PRs, one per finding (two Required run-page issues share one PR because they touch the same lines). Each is a minimal, test-first fix. No refactors: the `useRunFollower` extraction is a *Consider* item, kept out so these PRs stay small and conflict-free. PR J deploys on its own as soon as it merges; the rest go out in one web deploy.

**Tech Stack:** Next.js 16 route handlers, React 19, Vitest + React Testing Library, Cloud Run (web + share, same image).

---

## Context

`frontend/code_review.md` (5 parallel reviewers, each Critical/Required finding re-verified against the source) found:
- **Critical:** `/api/gcs` interpolates the unvalidated `bucket` param into the GCS URL. A crafted `bucket` rewrites the request (the route's own path ends up in the URL fragment), which bypasses the owner-only check, so any domain user can read other users' consented person photos and variants. The response content type is taken from `path`, so the same trick can also serve an uploaded file as HTML on the app origin. Live in production.
- **Required:**
  - one failed poll marks a live run "Failed" and suggests a duplicate run;
  - "stalled" never clears;
  - a 409 resume wipes edits at checkpoints 2–3, the trend pick, and the report-only checkpoint 1;
  - a brief edit drops `trend_bridge.timely_angle`;
  - the results page downloads every artifact's contents for nothing.

**Facts confirmed during planning:**
- The web service env has only `ADK_API_BASE` and `IAP_ALLOWED_HD`, so there is no bucket env yet. Every caller builds URLs through `lib/gcs.ts` `gcsProxyUrl(bucket, path)`; the bucket comes from session state (`gcs_bucket_name` / `gs://` URIs), always `trend-trawler-deploy-ae` in production.
- `share-snapshot.ts` already has a bucket-name regex (`BUCKET_RE`) to reuse.
- `review-drafts.ts` `useReviewDraft(key, init)` exists, and `ReviewPanel.tsx:493-523` already receives `functionCallId`. Only `ReviewBrief` uses it, so the fix stays inside `ReviewPanel.tsx`.
- `useReportEdit(state, draftKey?)` (`report-editor.tsx:15`) already supports a key.

**Constraints (every PR):**
- `cd frontend && npm run lint && npm test && npm run build` must be green.
- TDD: failing test first, plus 1–2 mutation checks on the new logic (mutate, run, restore, `git diff --exit-code`).
- No Co-Authored-By or AI attribution; merge only after CI passes (path-gated `frontend-tests.yml`).
- Each PR owns only its listed files.

## File ownership (disjoint, so all five run in parallel)

| PR | Branch | Finding(s) | Files |
|---|---|---|---|
| **J** | `fix/gcs-proxy-bucket` | Critical | `src/app/api/gcs/route.ts`, `src/lib/gcs.ts`, new `src/__tests__/gcs-route-bucket.test.ts`, `src/lib/share-snapshot.ts` (only to import the shared `BUCKET_RE` from `lib/gcs.ts`), deployment/README.md (web env), plan copy |
| **K** | `fix/run-poll-resilience` | R1 poll failure + R4 stall | `src/lib/api.ts` (`pollRun`, `resumeRun` only), `src/app/run/[sessionId]/page.tsx`, `src/app/run/[sessionId]/run-sections.tsx` (banner copy), `src/__tests__/poll-run.test.ts`, new `src/__tests__/run-page-connection.test.tsx` |
| **L** | `fix/review-drafts` | R2 409 wipes edits | `src/app/run/[sessionId]/ReviewPanel.tsx`, new `src/__tests__/review-drafts-409.test.tsx` |
| **M** | `fix/brief-timely-angle` | R3 timely_angle dropped | `src/lib/creative-brief.ts`, `src/lib/brief-edit.ts`, `src/app/run/[sessionId]/review-brief.tsx` (read-only display), `src/__tests__/brief-edit.test.ts`, new `src/__tests__/creative-brief-drift.test.ts` |
| **N** | `fix/results-artifacts` | R5 artifact over-fetch | `src/app/results/[sessionId]/page.tsx`, `src/app/results/[sessionId]/artifacts-panel.tsx`, new `src/__tests__/results-page-artifacts.test.tsx` |

K and L both live under `run/[sessionId]/`, but L doesn't touch `page.tsx`. N doesn't touch `api.ts` (K's file). `getArtifact` becomes unused after N; per the dead-code rule N lists it in the PR body but doesn't delete it.

---

## PR J — Critical: lock down `/api/gcs` (deploy on its own as soon as it merges)

**Fix**
1. **`lib/gcs.ts`:** move `BUCKET_RE` here (`share-snapshot.ts` imports it) and add two helpers.
   - `isAllowedBucket(bucket)`: the name must match `BUCKET_RE`; if `GCS_ALLOWED_BUCKETS` (comma-separated) is set, the name must also be in it.
   - `gcsMediaUrl(bucket, object)`: `https://storage.googleapis.com/storage/v1/b/${encodeURIComponent(bucket)}/o/${encodeURIComponent(object)}?alt=media`.
2. **`route.ts`, before any fetch:** an invalid or disallowed bucket returns 400 `{error:"Invalid bucket"}`. Build the URL only through `gcsMediaUrl`.
   - On Cloud Run (`K_SERVICE` set) with `GCS_ALLOWED_BUCKETS` unset, fail closed (400) for person paths and log a warning. Other paths still accept any `BUCKET_RE`-valid bucket, so a deploy before the env is set doesn't break images.
3. **Close the HTML escalation from the same finding:**
   - choose the content type from the object *actually fetched*: GCS's `content-type` header, with a `.pdf` path fallback;
   - always send `X-Content-Type-Options: nosniff`;
   - serve `text/html` only for non-person paths, with `Content-Security-Policy: sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'`, so the gallery renders but can't script the origin;
   - refuse `.html` under person paths (404).
   - Also use the computed `cacheControl` in the HTML branch, a one-line *Consider* item on the same lines.
4. **Gallery link rewrite (`route.ts:76`):** the rewritten links already go through the route again, so they get the same validation for free.

**Tests (written first, in `gcs-route-bucket.test.ts`)**
- buckets containing `/`, `?`, `#`, `%2F`, uppercase or a trailing `.` give 400 and `fetch` is never called;
- the documented exploit URL gives 400;
- a valid allowlisted bucket hits `fetch` with the encoded URL;
- with `GCS_ALLOWED_BUCKETS=a`, bucket `b` gives 400;
- a person path on Cloud Run without the allowlist gives 400;
- an HTML response carries the sandbox CSP and `nosniff`;
- a person-path `.html` gives 404;
- the existing `gcs-route-person.test.ts` still passes.

Mutation checks: drop the bucket check (a test must fail), and drop `encodeURIComponent(bucket)` (a test must fail).

**Deploy (right after merge, before the other PRs):**
1. `gcloud run services update trend-trawler-web --region us-central1 --update-env-vars GCS_ALLOWED_BUCKETS=trend-trawler-deploy-ae`. That creates a new revision; pin it.
2. Build the web image with the root `cloudbuild.yaml`, then `gcloud run deploy trend-trawler-web --image <digest> --no-traffic` and pin it (prev tag = the current serving revision).
3. The share service runs the same image but SHARE_MODE 404s every `/api/*`. Redeploy it with the digest so both stay on one image.

**Expected impact:** cross-user reads of person photos and variants become impossible (400 before any GCS call), and objects can no longer be served as active HTML on the app origin. Legitimate traffic is unaffected: every caller already passes the real bucket. Small side gain: sniffing protection on every proxied object.

## PR K — Required R1 + R4: runs survive poll hiccups, and "stalled" clears itself

**Fix**
1. **`pollRun` (`api.ts`):** wrap each poll request in a small retry.
   - Retry network errors (TypeError), 5xx and 429 up to `POLL_MAX_RETRIES = 5`, with backoff 1, 2, 4, 8, 16 s capped at 15 s (about 45 s total), keeping the `since` cursor.
   - A 4xx other than 429, or `status === "error"`, still throws immediately.
   - When retries run out, throw a typed `RunConnectionLostError` (new; re-exported next to `ResumeNotAppliedError`).
   - **`resumeRun`:** on a network error or 5xx, call `getRunStatus(…)` once to see whether the resume took effect (the pause answered, or the run running again) before throwing `RunConnectionLostError`.
2. **`page.tsx`:**
   - `RunConnectionLostError` → a new status `"disconnected"`, not `"error"`.
   - The banner (`run-sections.tsx`) says "Lost connection to the run. It is still running on the server. Reload to reconnect." with no Duplicate advice. The Reload button calls the existing follow path (`followRun` from `since=0`).
   - Only real run failures keep the "Failed" banner and its Duplicate hint.
3. **Stall auto-clear:** in `consumePollEvents`, on every new event, `setStatus(s => s === "stalled" ? "running" : s)`. Also raise the threshold for the creative apps:
   - `RUN_STALL_TIMEOUT_MS` = 3 min stays the default;
   - `creative_agent` / `interactive_creative` get 8 min, covering 4 sequential renders under the 2-images-per-minute quota plus QA re-renders;
   - pick the value with a tiny `stallTimeoutFor(appName)` in `run-completion.ts` (or wherever `RUN_STALL_TIMEOUT_MS` lives), unit-tested.

**Tests (first)**
- **`poll-run.test.ts`** (fake timers, mocked `fetch`):
  - 2× 503 then 200 → events continue with `since` intact;
  - 5× 503 → `RunConnectionLostError`;
  - a 404/403 throws immediately;
  - a network TypeError retries;
  - `resumeRun` with a 502 whose status check shows the pause answered → resolves.
- **`run-page-connection.test.tsx`:**
  - a poll that keeps failing shows "Lost connection…" and no "Failed" or Duplicate text;
  - a stall followed by a new event goes back to running (the "No recent activity" banner disappears);
  - creative apps don't stall at 4 min.

Mutation checks: retry counter off-by-one; remove the stall auto-clear.

**Expected impact:** a single 502, network blip or IAP refresh no longer makes a 10–20 minute run look failed, and users stop being pushed into duplicate runs (double Gemini and image quota, duplicate GCS and BigQuery rows). Healthy creative runs stop showing a stale "No recent activity" warning during rendering, and if one does stall briefly the warning clears once events resume. The documented "runs survive disconnect/IAP re-auth" behaviour becomes true for transient failures.

## PR L — Required R2: edits survive a rejected resume at every checkpoint

**Fix (`ReviewPanel.tsx` only)**
- Pass `functionCallId` from the dispatcher (`:493-523`) to `ReviewResearch`, `ReviewAdCopies`, `ReviewVisualConcepts` and `ReviewTrends` (`ReviewBrief` already gets it as `draftKey`).
- Replace their local `useState` with `useReviewDraft` from `review-drafts.ts`, with namespaced keys:
  - `${id}:feedback`
  - `${id}:concepts`
  - `${id}:trend-selected` (store an array; convert to and from a `Set`)
  - `${id}:trend-instruction`
- Call `useReportEdit(state, functionCallId)` in `ReviewResearch` (it already supports the key).
- No change to `page.tsx`: it already remounts with `key={pauseContext.functionCallId}` and passes `functionCallId`.

**Tests (first, in `review-drafts-409.test.tsx`)**, one per panel:
1. render the panel with an id;
2. type feedback, edit a concept prompt or pick trends;
3. unmount and remount with the same id;
4. assert the input is restored.

Also check that a different id starts empty, and keep a `clearReviewDrafts()` in `afterEach`. Mutation: change one key to `undefined`; that panel's test must fail.

**Expected impact:** a 409 (`prior_segment_active`) or 400 on resume no longer throws away the user's feedback, the four visual-concept edits and notes, or the trend pick. The page's existing "submit it again" message becomes true at every checkpoint. No behaviour change on the happy path.

## PR M — Required R3: brief edits keep `timely_angle`

**Fix**
- **`creative-brief.ts`:** add `timelyAngle: string` to `TrendBridge`; parse it with `parseCreativeBrief` (`str(bridge.timely_angle)`).
- **`brief-edit.ts`:** add `timely_angle: tb.timelyAngle` to `toBriefPayload`.
- **`review-brief.tsx`:** show it read-only in the Trend fit section when non-empty ("Timely angle (set by the current moment)"). It is not editable, because brief rule 16 only allows it for a validated timely moment.

**Tests (first)**
- **`brief-edit.test.ts`:** `toBriefPayload(parseCreativeBrief(raw))` round-trips `timely_angle`. An edited insight on a brief with a timely angle keeps it in the payload.
- **New `creative-brief-drift.test.ts`:** read `../creative_agent/schemas.py`, extract the field names of `class TrendBridge` and `class CreativeBrief` with a regex (`^\s{4}(\w+):` inside each class block), and assert every one appears as a key in the `toBriefPayload` output for a fully populated brief. This follows the existing cross-language drift tests (eval dimensions, rating reasons).

Mutation: drop `timely_angle` from the payload (both tests fail).

**Expected impact:**
- **Timely angle kept:** editing any brief field on a run with a validated timely moment keeps the "Timely angle" line in `creative_brief_md`, so the timely creative stays anchored to the verified moment instead of losing it silently.
- **Future-proofing:** the drift test fails CI the next time a field is added to the Python brief without updating the form payload. That stops this class of silent data loss.

## PR N — Required R5: results page stops downloading every artifact

**Fix**
- **`results/[sessionId]/page.tsx`:** replace the `getArtifact` loop with `setArtifacts(artifactNames.map((name) => ({ name })))`, inside the same `Promise.all` that already lists names. The page shows as soon as the session and the name list arrive.
- **`artifacts-panel.tsx`:** narrow `ArtifactData` to `{ name: string }`; it only reads `name`, and image previews already load from GCS through `urlFor(name)`.
- Add a `cancelled` guard to the load effect, a two-line correctness gain in the same effect.
- Leave `getArtifact` in `api.ts`, and list it in the PR body as now-unused (delete only on approval).

**Tests (first, in `results-page-artifacts.test.tsx`)**
- mock `@/lib/api`;
- render `ResultsPage` with three artifact names;
- assert `getArtifact` is never called;
- assert the artifacts panel lists all three names and the contact sheet renders.

This is also the first render-level test for the results page, which closes part of the test gap the review flagged.

**Expected impact:** opening a results page no longer pulls several MB of base64 artifact payloads through IAP, the proxy, the api and the artifact service before anything shows. First paint waits for 2 requests instead of 2 + N, which makes the most-used page noticeably faster and cuts backend and artifact-service load. Nothing visible changes in the UI.

---

## Execution and deploy

1. **t=0:** dispatch the J, K, L, M and N implementers in parallel. J's prompt marks it as security-urgent.
2. **Review:** review each PR with the code-review-and-quality checklist: tests first, mutation evidence, five axes, severity-labelled comments. Merge as CI goes green.
3. **J ships immediately:** set the env, build, deploy web (pinned, prev tag), redeploy share with the same digest.
4. **After K, L, M and N merge:** one more `cloudbuild.yaml` build, then deploy web (pinned) and share. The api and engines are untouched.
5. Update the runbook memory, and add an "Addressed" line per finding at the top of `frontend/code_review.md`. The file is untracked, so this is a local edit only.

**Estimated total:** about 1 h (parallel PRs; K is the largest) plus about 10 min per web deploy. J's fix can be live within about 30 min of starting.

## Verification

- **J:** unit tests and mutations pass. **Live:** in a browser on the IAP app, open the exploit URL `/api/gcs?bucket=trend-trawler-deploy-ae%2Fo%2Fperson-refs%252F<other-slug>%252Fx.jpg%3Falt%3Dmedia%23&path=x.png` and expect `400 Invalid bucket`. A normal results page still shows images, the gallery link still opens (now sandboxed), and the People page thumbnails still load.
- **K:**
  - Unit and page tests pass.
  - **Live:** start a creative run, then on the api momentarily block it, e.g. pin api traffic to a broken tag for a few seconds (**skip in prod**). Simpler: confirm in DevTools by setting the network offline for 20 s; the page shows "Lost connection…" only after about 45 s, and recovers on its own if the network returns sooner.
  - Watch the run page through the render stage: no "No recent activity".
- **L:** tests pass. **Live (optional):** during an interactive run at checkpoint 3, submit twice quickly; the second 409 keeps the edits.
- **M:** tests pass. **Live:** on an interactive run with a timely moment, edit the brief at checkpoint 1; the PDF/brief Markdown still shows "Timely angle".
- **N:** tests pass. **Live:** open a results page with DevTools → Network; there are no `/artifacts/<name>` content requests, only the list, and the page renders faster.
