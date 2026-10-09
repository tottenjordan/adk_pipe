# Shareable Creative Links Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (or `executing-plans`) to implement task-by-task. Save a copy as `docs/plans/2026-10-09-shareable-links.md` in PR 1.

**Goal:** an owner can share one creative or a whole slate (4 visuals + paired ad copy) as a public link that **anyone** can open without signing in. The link shows a simple, accessible presentation as a proof card, a social feed post or a story. Judge checks and scores are an option. Links are frozen snapshots the owner can revoke.

**Architecture:**
- **Freeze.** The owner clicks Share. The api checks they own the session, builds a **frozen snapshot** of an allowlist of fields, and server-side copies the images into `gs://<bucket>/shares/<token>/`. It writes `snapshot.json` there and records the share in BigQuery `creative_shares`.
- **Serve.** A **new, separate public Cloud Run service `trend-trawler-share`** serves the snapshot. It runs the same Next.js image in `SHARE_MODE`, where only `/s/*`, `/_next/*` and `robots.txt` exist. Its SA can read only `shares/` and it has no api or session access.
- **Keep the app private.** The IAP-gated web and api are untouched: no allUsers on them, IAP stays on.
- **Revoke.** Revoking deletes the snapshot objects, so the link returns 410.

**Tech Stack:** FastAPI router in `runserver/` (ratings pattern), BigQuery + GCS (google-cloud-storage rewrite), Next.js 16 App Router (server components + route handler), shadcn/ui Tabs, Vitest + RTL, pytest.

---

## Context

The user wants a creative, or the whole slate, viewable by people outside the tool (clients, stakeholders) via a link. It should be presented as the ad would appear in a social feed or a story, and be accessible.

**User decisions (2026-10-09):**
- **Audience:** **anyone with the link**, with no sign-in.
- **Views:** **proof card, feed mockup, story mockup**, plus an **optional eval section**.
- **Link model:** **frozen snapshot, revocable**, with no expiry.

**Facts from exploration:**
- **No public route exists today.** The web service is entirely IAP-gated: there is no middleware and no public path. IAP on Cloud Run can't exempt a single path.
- **`/api/gcs` must never become public.** `/api/gcs` (`frontend/src/app/api/gcs/route.ts`) proxies **any** object in the bucket with the web SA, so it would expose everything. Hence a separate service whose SA is limited to `shares/`, and whose `SHARE_MODE` middleware 404s `/api/*`.
- **Org policy allows public access:** `iam.allowedPolicyMemberDomains` allows all values and `storage.publicAccessPrevention` isn't enforced. That allows `--allow-unauthenticated` on the **new** service only.
- **Sessions are user-scoped:** another user gets a 404 (`runserver/authz.py` `decide`, ownership handler). So the viewer can't read the live session; it must be a snapshot.
- **Patterns to reuse:**
  - `runserver/ratings.py` gives the router + `configure()`, `_get_session` ownership, `allowed_report_uri`, `_error`.
  - `runserver/ratings_store.py` gives the Protocol + InMemory/BigQuery store + `build_store_from_env`.
  - `frontend/src/lib/user-scoping.ts` `ROUTES` gives the allowlist entries, the way ratings added theirs.
  - `frontend/src/lib/eval-matching.ts` `buildProofs` does the visual↔copy pairing.
  - `proof-grid.tsx` `ProofImage` and the `ui/tabs` + `ui/dialog` components.
  - `lib/eval-dimensions.ts` `GATE_LABELS` labels the eval section.
- **The image source of truth** is `generated_images[concept].gcs_uri`, not the rebuilt `<name>.png`.
- **Ad copy fields:** `headline, body_text, social_caption, call_to_action, tone_style`; there is no hashtags field. **Alt text:** `concept_summary`.

## Why a second deployment instead of making the web app public

The user asked about this on 2026-10-09; these are the reasons.

**Option A: make `trend-trawler-web` public by removing IAP.**
- **Sign-in breaks.** The proxy's identity is the IAP JWT (`lib/iap-identity.ts`). Without IAP, every user-scoped route returns 401 on Cloud Run, so the app stops working for us too. We'd have to build replacement sign-in (Google OAuth / Identity Platform, sessions, CSRF) and re-test the P3 authz model.
- **The bucket leaks.** `/api/gcs` would become a public proxy for the **whole bucket**: every creative, research PDF, eval report and session-state JSON from every user. It would need its own auth and path locks.
- **It undoes a deliberate decision.** IAP was re-enabled on 2026-09-28 after turning it off broke browsers and in-flight resumes.
- **Net:** much more work, and a large attack surface.

**Option B: one service behind an HTTPS load balancer**, with IAP on the default backend and an un-IAP'd `/s/*` path to the same Cloud Run service.
- Single deployment, but it needs a domain + certificate and a load balancer (~$18+/month).
- It migrates off Cloud Run direct IAP.
- The service would need allUsers invoker with ingress restricted to the load balancer, and one URL-map mistake exposes `/api/*`.
- **Net:** more infrastructure and more fragile than A's alternative.

**Recommended: a second Cloud Run service from the *same image*.**
- **No new codebase:** `SHARE_MODE=1` makes it serve only `/s/*`, and its SA can read only `shares/`.
- **The extra cost is small:** one `gcloud run deploy` command and one SA, scaling to zero (≈ $0 when idle).
- **The app is untouched:** web, IAP and the api stay exactly as they are.

## Snapshot contract (`snapshot.json`, version 1)

```json
{
  "version": 1, "token": "…", "created_at": "…", "scope": "slate|creative",
  "brand": "…", "product": "…", "trend": "…",
  "include_eval": false,
  "creatives": [
    {"index": 0, "image": "0.png", "aspect_ratio": "4:5", "alt": "<concept_summary>",
     "visual_style": "…", "headline": "…", "body": "…", "caption": "…", "cta": "…", "tone": "…",
     "eval": {"passed": true, "score": 0.83, "checks": [{"gate": "product_visible", "label": "Product visible", "passed": true, "advisory": false}]}}
  ]
}
```

**Allowlist only:**
- **Excluded:** prompts, rationales, rating notes, judge notes, emails, session id, user id.
- **`eval`** is present only when `include_eval`: per creative the copy and visual verdicts, gate labels and pass/fail, overall score.
- **Human ratings** are never included, because they're personal.
- **The owner and session** are recorded only in BigQuery.

## PR grouping

| PR | Branch | Contents | Deploy |
|---|---|---|---|
| **1** | `feat/shares-api` | snapshot builder, store, `/shares` router, image copy, authz, BigQuery table, docs | BigQuery table creation → api |
| **2** | `feat/share-viewer` | `SHARE_MODE` middleware, `/s/[token]` pages (card / feed / story / eval), image route, OG metadata, a11y tests | new `trend-trawler-share` service (+ SA, IAM condition) |
| **3** | `feat/share-owner-ui` | Share dialog on results (slate + per creative), manage/revoke list, proxy allowlist | web |

**Rules for every PR:**
- **Gates:** Python `uv run ruff format --check . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 4`; frontend `npm run lint && npm test && npm run build`.
- **Merge only after every GitHub check passes**, and confirm `MERGED` before cleanup.
- **No Co-Authored-By trailers and no AI attribution.**
- **Deploys:** pin api and web traffic.
- **IAP and allUsers:** never change IAP or allUsers on `trend-trawler-web` / `trend-trawler-api`. Only the new share service is public.

---

## PR 1 — Shares API (`feat/shares-api`)

### Task 1.1: Pure snapshot builder

**Files:** create `runserver/share_snapshot.py`; test `tests/test_share_snapshot.py`.

**Step 1: Failing tests**
```python
from runserver.share_snapshot import build_snapshot


def test_slate_snapshot_allowlists_fields(state, report):
    snap = build_snapshot(
        state,
        report,
        token="t",
        concept_names=None,
        include_eval=False,
        now="2026-10-09T00:00:00Z",
    )
    assert snap["scope"] == "slate" and len(snap["creatives"]) == 4
    c = snap["creatives"][0]
    assert set(c) == {
        "index",
        "image",
        "aspect_ratio",
        "alt",
        "visual_style",
        "headline",
        "body",
        "caption",
        "cta",
        "tone",
    }
    blob = json.dumps(snap)
    for secret in ("image_generation_prompt", "rationale", "session", "@"):
        assert secret not in blob


def test_single_creative_and_eval(state, report):
    snap = build_snapshot(
        state,
        report,
        token="t",
        concept_names=["Stage Left"],
        include_eval=True,
        now="…",
    )
    assert (
        snap["scope"] == "creative"
        and snap["creatives"][0]["eval"]["checks"][0]["label"]
    )


def test_unknown_concept_raises(state, report):
    with pytest.raises(ValueError):
        build_snapshot(
            state,
            report,
            token="t",
            concept_names=["nope"],
            include_eval=False,
            now="…",
        )
```
Fixtures: reuse the session-state and report fixtures from `tests/test_ratings_api.py`. Promote `_state()`/`_report()` into `tests/conftest.py` if needed.

**Step 3: Implement**
- Pair concepts with copy the same way `frontend/src/lib/eval-matching.ts` does: by `ad_copy_id`/`original_id`, then headline, then index. Port that pure logic and test the parity.
- Gate labels come from `creative_eval.dimensions.GATE_LABELS` + `ADVISORY_GATES`.
- `image` is `f"{i}.png"`, and the builder also returns the source `generated_images[name].gcs_uri` list for the copy step. Don't put it in the snapshot.
- Skip concepts without a rendered image, and raise when none remain.
- Strip brace-free text to plain strings.

**Commit:** `feat(shares): allowlisted share snapshot builder`.

### Task 1.2: Shares store

**Files:**
- `runserver/shares_store.py`: `SharesStore` Protocol, `InMemorySharesStore`, `BigQuerySharesStore` (MERGE on `token`), `build_store_from_env()` reading `SHARES_STORE=bigquery|memory` and `BQ_TABLE_SHARES` (default `creative_shares`). It fails at startup on Cloud Run without the BigQuery env, mirroring `ratings_store.build_store_from_env`.
- `deployment/bq_schemas/creative_shares.json`.
- Test: `tests/test_shares_store.py`.

**Columns:** `token STRING, owner_user STRING, app_name STRING, session_id STRING, scope STRING, concept_names ARRAY<STRING>, include_eval BOOL, title STRING, created_at TIMESTAMP, revoked_at TIMESTAMP`.

**Methods:** `put(row)`, `list_for(owner)` (newest first, not revoked), `get(token)`, `revoke(token, owner)`. `revoke` returns False when the token isn't the owner's.

**Tests:** in-memory behaviour; that the BigQuery SQL is parameterised; and the ARRAY param (copy `test_ratings_store` patterns).

**Commit:** `feat(shares): share records store`.

### Task 1.3: `/shares` router + image copy

**Files:**
- `runserver/shares.py`: `router`, `configure(session_service=, store=, gcs_client=)`.
- `runserver/authz.py`: add `/shares/{u}` to `_PATH_USER_RES`.
- `deployment/async_app.py`: mount it, like ratings at L148-153.
- Test: `tests/test_shares_api.py`.

**Routes** (all user-scoped):
- **`POST /shares/{user}/{app}/{session}`**, body `{concept_names?: [str], include_eval: bool}`, returns `{token, url}`. Steps:
  1. Load the session via the ratings-style `_get_session` (a foreign session gives 404).
  2. Load the report via `allowed_report_uri`, falling back to the state copy.
  3. `build_snapshot`.
  4. `token = secrets.token_urlsafe(16)`.
  5. Server-side **copy** each source image with `bucket.copy_blob`, only when the source is `gs://$GOOGLE_CLOUD_STORAGE_BUCKET/…` (else 400 `image_outside_bucket`), to `shares/<token>/<i>.png`. Set `Cache-Control: private, max-age=300`.
  6. Upload `snapshot.json` with `no-store`.
  7. `store.put`.

  `url = f"{SHARE_BASE_URL}/s/{token}"` (env `SHARE_BASE_URL`; empty gives a relative URL). Cap at 200 active shares per user (429). On a partial failure, delete the `shares/<token>/` objects and return 502.
- **`GET /shares/{user}`**: the owner's active shares, with the title (brand × trend, or the headline).
- **`DELETE /shares/{user}/{token}`**: `store.revoke`, then delete every object under `shares/<token>/`; 404 if it isn't the owner's.

**Tests:**
- 401 without a trusted user; 403 for a foreign path user; 404 for a foreign session.
- A copy goes only to `shares/<token>/`, with a fake GCS client.
- `snapshot.json` contents.
- Revoke deletes the objects.
- A cross-owner revoke gives 404.
- Rollback on a copy failure.
- The cap.

**Commit:** `feat(shares): owner-scoped /shares API with frozen image copies`.

### Task 1.4: Docs + migration

- **`deployment/README.md`:** a "Shareable links" section covering:
  - the `bq mk --table … deployment/bq_schemas/creative_shares.json` creation command;
  - the env vars `SHARES_STORE=bigquery`, `BQ_TABLE_SHARES`, `SHARE_BASE_URL`;
  - the share service runbook (PR 2).
- Also update CLAUDE.md, GEMINI.md and tests/README.md.
- **Commit:** `docs: shareable links API`.

**After merge:** create the table, then deploy the api with `--update-env-vars SHARES_STORE=bigquery,SHARE_BASE_URL=<share service URL once known>` and pin traffic. PR 2 provides the URL, so set it then, or deploy PR 1 with an empty value first.

---

## PR 2 — Public share viewer (`feat/share-viewer`)

### Task 2.1: SHARE_MODE lockdown (security first)

**Files:** create `frontend/src/middleware.ts`; test `frontend/src/__tests__/share-mode.test.ts`.

**Behaviour:**
- **`SHARE_MODE=1`:** allow only `/s/<token>`, `/s/<token>/img/<n>`, `/_next/*`, `/robots.txt`, `/favicon.ico`. Everything else, **especially `/api/*`**, returns 404.
- **Normal mode:** `/s/*` also works behind IAP (owner preview), with no other change.
- **Response headers on `/s/*`:** `X-Robots-Tag: noindex, nofollow`, `Referrer-Policy: no-referrer`, `Cache-Control: private, max-age=60`, and a strict CSP (`default-src 'self'; img-src 'self'; frame-ancestors 'none'`).
- **`robots.txt`:** in share mode it returns `Disallow: /`.

**Failing tests:** a matcher table: `/api/gcs?bucket=x&path=y` gives 404 in share mode; `/s/abc_DEF-123` is allowed; `/s/../api` gives 404; `/` gives 404 in share mode; headers are present.

**Commit:** `feat(share): SHARE_MODE middleware exposes only share routes`.

### Task 2.2: Snapshot + image loading (server only)

**Files:**
- `frontend/src/lib/share-snapshot.ts`: types for snapshot v1; `TOKEN_RE = /^[A-Za-z0-9_-]{16,64}$/`; `loadSnapshot(token)` fetches `shares/<token>/snapshot.json` from GCS with the service's ADC token (the same mechanism as `api/gcs/route.ts`, but with a **fixed** path); `isSnapshotV1` validation.
- `frontend/src/app/s/[token]/img/[n]/route.ts`: streams `shares/<token>/<n>.png` (n must be digits < creatives length). Returns 404 or 410 when missing.

**Tests:**
- An invalid token gives null without a fetch.
- A malformed JSON snapshot is rejected.
- The image route refuses non-numeric `n` or a bad token.
- The GCS URL is always `…/o/shares%2F<token>%2F…` (mock fetch).

**Commit:** `feat(share): token-scoped snapshot and image loading`.

### Task 2.3: Presentation components

**Files:**
- `frontend/src/components/share/share-card.tsx` (proof card), `feed-post.tsx`, `story-frame.tsx`, `share-eval.tsx`, `share-view.tsx` (the Tabs switcher);
- `frontend/src/app/s/[token]/page.tsx` (server component: `loadSnapshot` → 410/404 page or `ShareView`);
- `frontend/src/app/s/[token]/not-found.tsx`.

**Design (proof-room tokens, no platform trademarks):**
- **Proof card:** the image (`ProofImage`, `alt` = snapshot alt, aspect from `aspect_ratio`) next to a semantic text block: `h2` headline, body `p`, caption, CTA rendered as a non-interactive styled `span` (it's a mock).
- **Feed post:** a generic feed chrome: an avatar disc with the brand initial (`aria-hidden`), the brand name, "Sponsored", the image, the caption (truncated with "more" using a real `button` that expands), a headline + CTA bar.
- **Story:** a 9:16 phone frame with the image as a cover and the headline + CTA overlaid on a gradient scrim for contrast (WCAG AA on any image: white text on a ≥60% black scrim). The full text is repeated below the frame for screen readers.
- **Eval** (only when `include_eval`): a "Checks" list (pass/fail marks with text labels, advisory gates labelled) and the score. Reuse the `mark-pass`/`mark-fail` tokens and `GATE_LABELS` from the snapshot (they're already labelled).
- **Page:** `lang`, `h1` = "<brand> × <trend>", product subtitle, a view switcher (`ui/tabs`, keyboard navigable; the view is persisted in `?view=card|feed|story`), slate = a list of `<article aria-labelledby>` per creative, single = one article. A muted footer: "Shared from Trend Trawler · AI-generated images". `prefers-reduced-motion` is respected, and there are no autoplay animations.

**Tests** (`frontend/src/__tests__/share-view.test.tsx`, jsdom, copying `image-check.test.tsx`):
- each view renders the headline/body/CTA as text;
- every `img` has non-empty alt;
- the tabs switch with the keyboard (arrow keys);
- eval is hidden unless `include_eval`;
- no prompt/rationale fields appear;
- story text is duplicated outside the frame.

**Commit:** `feat(share): accessible card, feed and story presentations`.

### Task 2.4: Link previews

**Files:** `frontend/src/app/s/[token]/page.tsx` `generateMetadata`.

**Values:** `og:title` = first headline (slate: "<brand> × <trend> — 4 creatives"); `og:description` = the caption trimmed to 160 characters; `og:image` = absolute `/s/<token>/img/0` (base from `SHARE_BASE_URL`); `twitter:card=summary_large_image`; `robots: noindex`.

**Test:** the metadata values come from a fixture snapshot.

**Commit:** `feat(share): Open Graph metadata for link previews`.

### Task 2.5: Infra docs + deploy recipe

In `deployment/README.md` → "Shareable links":
- SA `tt-share-sa` with `roles/storage.objectViewer` on the bucket, **IAM condition** `resource.name.startsWith("projects/_/buckets/<bucket>/objects/shares/")`. It gets no other roles.
- Deploy: `gcloud run deploy trend-trawler-share --source ./frontend --service-account tt-share-sa@… --set-env-vars SHARE_MODE=1,GCS_BUCKET=<bucket>,SHARE_BASE_URL=<its URL> --allow-unauthenticated --min-instances 0 --max-instances 3`.
- **Explicit note:** this is the only public service. Never put allUsers on `trend-trawler-web`/`-api`.
- Rollback = delete the service; links break and data stays.

**Commit:** `docs: public share service runbook`.

**After merge:**
1. Create the SA and the conditional binding.
2. Deploy the share service.
3. Set `SHARE_BASE_URL` on the api (pin traffic) and on the share service.
4. Probe: `curl` the share URL `/api/gcs?...` gives 404, `/` gives 404, a valid `/s/<token>` gives 200 (with a token from a test share), and an object outside `shares/` can't be read even via a forged image path.

---

## PR 3 — Owner UI (`feat/share-owner-ui`)

### Task 3.1: Proxy allowlist + client

**Files:**
- `frontend/src/lib/user-scoping.ts` `ROUTES`: add `POST shares/{u}/{app}/{session}` (app name identifier-checked, `SESSION_ID_RE`), `GET shares/{u}`, `DELETE shares/{u}/{token}` (token `TOKEN_RE`), all `userAt: 1`.
- `frontend/src/lib/api.ts`: `createShare`, `listShares`, `revokeShare`.

**Tests:** `user-scoping.test.ts` (userId rewrite, a bad token or app refused, DELETE allowed only for the shares path); `adk-proxy-auth.test.ts` unchanged.

**Commit:** `feat(frontend): proxy routes and client for shares`.

### Task 3.2: Share dialog + manage list

**Files:**
- `frontend/src/components/share-dialog.tsx`;
- the results page header gets a "Share slate" button (`results/[sessionId]/page.tsx`);
- `proof-detail.tsx` gets a "Share this creative" button next to the rating slot;
- `frontend/src/components/shares-list.tsx` (on `/runs` under the judge-agreement line, or a "Shared links" section on the results page; pick the results page for locality).

**Dialog:**
- An "Include judge checks and scores" checkbox (off).
- A one-line notice: "Anyone with the link can view these creatives and copy. Prompts, notes and ratings are never shared."
- **Create link** → shows the URL with **Copy** (a `navigator.clipboard` button, with `aria-live` "Link copied") and **Open**.
- Errors are shown inline.

**List:** title, scope, created, eval yes/no, **Copy**, and **Revoke** (with a confirm dialog). After revoking, the row disappears.

**Tests** (`share-dialog.test.tsx`, `shares-list.test.tsx`):
- the payload (concept_names for a single creative, none for the slate, include_eval);
- the copy-confirmation live region;
- revoke calls the DELETE and removes the row;
- focus returns to the trigger.

**Commit:** `feat(frontend): share slate or creative + manage links`.

### Task 3.3: Docs

Update CLAUDE.md / GEMINI.md (pages, key files, proxy routes, snapshot contract summary) and `frontend` README. **Commit:** `docs: share owner UI`.

**After merge:** deploy web and pin traffic (prev tag).

---

## Verification

1. **Unit:** each task's tests plus the full suites in CI.
2. **Security probes**, on the live share service:
   - `/api/gcs?bucket=…&path=…`, `/api/adk/list-apps` and `/` all return 404;
   - an image path outside `shares/` can't be read (SA condition);
   - a revoked token returns 410/404;
   - `X-Robots-Tag: noindex` is present;
   - the snapshot JSON has no prompts, notes, emails or session id (grep).
3. **End-to-end:**
   - On the IAP web app, open a finished run's results, then **Share slate** with eval on, and copy the link.
   - Open it in a **private window** with no Google session: the card, feed and story tabs work by keyboard, and the eval section shows.
   - Share one creative with eval off: one article, no checks.
   - Paste the link into Slack: the preview shows the headline and image.
   - Revoke it: the link returns 410.
4. **Accessibility:**
   - an axe/Lighthouse pass on `/s/<token>` (target: no serious violations);
   - screen-reader text order is headline → body → caption → CTA;
   - story-overlay contrast checked.
5. **Isolation:** `trend-trawler-web`/`-api` IAM unchanged (`get-iam-policy`: no allUsers), and IAP still returns 302 for anonymous requests on the web.
