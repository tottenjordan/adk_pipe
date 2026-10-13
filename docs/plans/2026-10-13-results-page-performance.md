# Results Page Performance Implementation Plan (3 parallel PRs)

**Goal:** make the web app, the results page in particular, load faster by fixing the four "Performance and bundle" items in `frontend/code_review.md`.

**Architecture:**
- **PR V:** replace the 989 KB header banner with a correctly sized asset, keeping the original in the repo.
- **PR W:** load the markdown renderer and the bandit deploy panel only when they're shown, using `next/dynamic` code splitting on the results page and the run page.
- **PR X:** move the CSS-only build tools (`shadcn`, `tw-animate-css`) to devDependencies so `npm audit` is meaningful again.

One web + share deploy at the end.

**Tech Stack:** Next.js 16 (App Router, `next/dynamic`), React 19, Vitest + RTL, Pillow (via `uv run`) for the image, npm.

---

## Context

From the frontend review's cross-cutting reviewer, re-checked during planning:

| # | Problem | Evidence |
|---|---|---|
| 1 | Banner far too large | `frontend/public/trend_trawler_banner.png` is 988,670 bytes, 1248×832, rendered by `src/components/site-header.tsx:22-26` as `<img className="h-9 w-auto">` (about 54×36 px) on every page except `/s/*` |
| 2 | Markdown renderer bundled eagerly | `ResearchReport` (react-markdown + remark-gfm, about 47 KB gzip) is imported statically by `app/results/[sessionId]/page.tsx:33` (rendered at `:368` inside a collapsed `QuietDisclosure`) and `app/run/[sessionId]/report-editor.tsx:5` (checkpoint 1 only). There is no `next/dynamic` anywhere in `src`. |
| 3 | Deploy panel bundled eagerly | `DeployPanel` (`results/[sessionId]/page.tsx:36`, rendered at `:346`) pulls in `reader-tuning.tsx` plus bandit/scenario code, a chunk of about 78 KB raw, on every results visit |
| 4 | CLI tool as a runtime dependency | `package.json` `dependencies` lists `shadcn` and `tw-animate-css`, used only as CSS `@import`s in `src/app/globals.css:2-3` (build time). `shadcn` causes 9 of the 10 `npm audit --omit=dev` findings; none ship (`.next/standalone` traces only next/react/sharp and friends). |

The baseline from the review is the results route's own JS: **183 KB gzip**.

**Constraints (every PR):**
- `cd frontend && npm ci && npm run lint && npm test && npm run build` must be green.
- Follow `frontend/AGENTS.md`: read `node_modules/next/dist/docs/` before using `next/dynamic` or `next/image` (Next 16 differs from training data).
- **Never delete displayed media** (standing user rule): move the original banner with `git mv`, don't delete it.
- No Co-Authored-By or AI attribution. Push and open a PR; I merge after CI is green. `frontend-tests.yml` is path-gated, so dispatch it for any PR whose diff is only `public/` files.

## File ownership (disjoint, so all three run in parallel)

| PR | Branch | Item | Files |
|---|---|---|---|
| **V** | `perf/banner-asset` | 1 | `frontend/public/trend_trawler_banner.png` (replaced), `frontend/design/trend_trawler_banner_full.png` (the `git mv`'d original; outside `public/`, so not served), `frontend/src/components/site-header.tsx`, new `frontend/src/__tests__/site-header.test.tsx` |
| **W** | `perf/lazy-panels` | 2 + 3 | `frontend/src/app/results/[sessionId]/page.tsx`, `frontend/src/app/run/[sessionId]/report-editor.tsx`, tests that render those (e.g. `results-page-artifacts.test.tsx`, review-panel tests), new `frontend/src/__tests__/lazy-panels.test.tsx` |
| **X** | `chore/frontend-dev-deps` | 4 | `frontend/package.json`, `frontend/package-lock.json`, plan copy |

---

## PR V — Banner asset (item 1)

**Task V1: generate the right-sized banner and keep the original**
1. `git mv frontend/public/trend_trawler_banner.png frontend/design/trend_trawler_banner_full.png`.
2. Make the served asset from the original at **4× the displayed size (216×144)**, covering 2–3× DPR with headroom:
   ```bash
   uv run python -c "from PIL import Image; im=Image.open('frontend/design/trend_trawler_banner_full.png'); im.thumbnail((216,144), Image.LANCZOS); im.save('frontend/public/trend_trawler_banner.png', optimize=True)"
   ```
   Expect well under 30 KB. Check that its aspect ratio matches the original, since the header uses `w-auto` and `object-cover`.
3. **Filename:** keep the same name, so `share-mode.test.ts:111` (which lists the path as a URL share mode must refuse) stays valid and no other reference changes.

**Task V2: explicit dimensions in the header (no layout shift)**
- **Test first** (new `site-header.test.tsx`, following an existing component test for setup and mocking `usePathname`): the banner `<img>` has `src="/trend_trawler_banner.png"` and intrinsic `width`/`height` attributes, `alt=""` (decorative, as today), and renders nothing on `/s/<token>` (existing chromeless behaviour).
- **Implement:** add `width={54} height={36}` plus `decoding="async"` to the existing `<img>`. Keep the raw `<img>` and its eslint-disable, because `next/image` would route through `/_next/image`, which share mode blocks. The header isn't rendered on share routes, but staying static avoids that coupling. Keep the classes.

**Verify:**
- `ls -la frontend/public/trend_trawler_banner.png` is under 30 KB;
- `npm run build` passes;
- a visual check in the PR body: `npm run build && node .next/standalone/server.js` (cp static/public as in the screenshot README), then `curl -sI http://127.0.0.1:3000/trend_trawler_banner.png`, recording content-length before and after.

**Expected impact:** about 960 KB less on every first visit and every cache miss. It was the largest asset the app served. The header looks identical.

## PR W — Code-split the markdown renderer and the deploy panel (items 2 + 3)

**Task W1: lazy `ResearchReport`**
1. Read `node_modules/next/dist/docs/` for `next/dynamic` in client components (the `ssr` option and the loading fallback in Next 16).
2. **Results page:** replace the static import with
   ```ts
   const ResearchReport = dynamic(
     () => import("@/components/research-report").then((m) => m.ResearchReport),
     { loading: () => <p className="text-sm text-muted-foreground">Loading report…</p> }
   );
   ```
   It renders inside the `QuietDisclosure`. Check the disclosure unmounts closed content (Base UI `Collapsible.Panel` does, per the review), so the chunk loads only when opened.
3. **`run/[sessionId]/report-editor.tsx`:** same pattern.
4. Keep the `ReportSources` *type* import static. Types are erased, so this costs nothing.

**Task W2: lazy `DeployPanel`**
- **Results page:** `const DeployPanel = dynamic(() => import("./deploy-panel").then((m) => m.DeployPanel), { loading: … })`. The panel is visible on the page (not inside a disclosure), so the chunk loads after first paint instead of blocking it. That's still a win, because it's off the critical path. Note this in the PR.
- If `DeployPanel` is already behind a user toggle (check the JSX around `:346`), keep the dynamic import behind that toggle so the chunk loads only on demand.

**Tests (first)**
- **New `lazy-panels.test.tsx`:**
  - the results page module has no static import of `react-markdown`, `@/components/research-report` or `./deploy-panel`. Check the source text with a simple regex, the same approach as the existing source-level tests.
  - Opening the research report disclosure eventually renders report content (`findBy…`).
- **Existing tests that render `ResultsPage`, `ReportEditor` or the checkpoint-1 review** (e.g. `results-page-artifacts.test.tsx`, `review-brief.test.tsx`, `interactive-mode.test.ts`): switch synchronous `getBy` on report or deploy content to `await findBy…`. Don't weaken assertions.

**Bundle check (in the PR body):** before and after own-JS for `/results/[sessionId]` and `/run/[sessionId]`, gzip. Method: after `npm run build`, read `.next/server/app/<route>/page_client-reference-manifest.js`, collect the route's entry chunk paths, and sum `gzip -c | wc -c` per file. Use the same method for both builds. The baseline is `/results/[sessionId]` at 183 KB gzip.

**Mutation:** restore a static `ResearchReport` import; the source-level test must fail.

**Expected impact:**
- **Results page:** its initial JS drops by roughly 30% (react-markdown at about 47 KB gzip plus the deploy/reader-tuning chunk), so it becomes interactive sooner, most noticeably on mobile and slow links. The research report and deploy panel still load the moment they're needed.
- **Run page:** sheds the markdown renderer until checkpoint 1.

## PR X — Build-only packages to devDependencies (item 4)

**Task X1**
1. Move `shadcn` and `tw-animate-css` from `dependencies` to `devDependencies`: `npm install --save-dev shadcn tw-animate-css`, which also updates the lockfile consistently.
2. Check `frontend/Dockerfile`: the builder stage must install dev dependencies (`npm ci` without `--omit=dev`) before `next build`, because the CSS `@import`s resolve at build time. It does today, per the review; confirm and quote the line in the PR.
3. `npm run build` passes, and `.next/standalone/node_modules` still contains neither package (it didn't before).
4. Run `npm audit fix` (non-breaking only) for the build-only `source-map-js` advisory if it applies cleanly. Skip it if it touches anything beyond the lockfile.
5. **Evidence:** `npm audit --omit=dev` before and after, in the PR body (expect 10 findings to drop to about 0–1).
6. Add the plan copy as `docs/plans/2026-10-13-results-page-performance.md`.

There's nothing to unit-test here; the gates are the build, the lint and the audit output.

**Expected impact:** no runtime or bundle change. `npm audit --omit=dev` then reports what actually ships, so a real future vulnerability isn't lost among false alarms. The dependency split matches reality.

---

## Execution

1. **t=0:** dispatch V, W and X in parallel (worktrees). W is the largest, at about 45 min.
2. **Review:** review each PR with code-review-and-quality (tests first, mutation evidence for W, five axes). Merge as CI goes green. Dispatch `frontend-tests.yml` where path gating could skip it.
3. **One deploy:**
   - build `cloudbuild.yaml` (the web image changes; api and the batch functions are cache hits and aren't deployed);
   - `gcloud run deploy trend-trawler-web --image <digest> --no-traffic`, then pin it, with the `prev` tag on the currently serving revision. `GCS_ALLOWED_BUCKETS` carries over with `--image`;
   - redeploy `trend-trawler-share` with the same digest;
   - verify:
     - web returns 302 unauthenticated;
     - share `robots.txt` returns 200 and `/` returns 404;
     - `curl -sI <web>/trend_trawler_banner.png` via the IAP session, or check the image's size in the build output.
4. Update memory, and mark the four items done in `frontend/code_review.md` (local file).

**Estimated total:** about 45–60 min, plus about 10 min for the deploy.

## Verification

- **V:** the served banner is under 30 KB with the same look; the header test passes; the original is preserved at `frontend/design/trend_trawler_banner_full.png`.
- **W:** the before/after gzip table shows `/results/[sessionId]` well below 183 KB. On the live results page, opening "Research report" loads it (a brief "Loading report…" then content), and the Deploy panel still works.
- **X:** `npm audit --omit=dev` is down to about 0–1 findings, and the build and image are unchanged in behaviour.
- **All:** lint, about 1,340 frontend tests and the build are green.
