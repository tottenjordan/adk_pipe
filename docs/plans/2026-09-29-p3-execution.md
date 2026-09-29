# P3 Execution Plan: Per-User Authorization for /runs + Session Routes

> **For Claude:** REQUIRED SUB-SKILL: Use `subagent-driven-development` to implement this plan task-by-task.
> On approval, the first commit copies this file to `docs/plans/2026-09-29-p3-execution.md` and sets the P3 doc's `**Status:**` to `executing 2026-09-29 via 2026-09-29-p3-execution.md`.

**Goal:** Carry out `docs/plans/2026-09-29-p3-per-user-runs-authz.md` (the "P3 doc") end to end, including the live rollout. After this, an IAP-authenticated user can only read, poll and resume their own sessions.

**Architecture:** Unchanged from the P3 doc (design option (a)).
- The Next.js `/api/adk` proxy verifies the IAP JWT with `jose` and checks `hd`. It rewrites the userId (path segment, and the `POST /runs` body) to the caller's normalized email, allowlists only the routes the UI uses, and sets `X-TT-User`.
- The backend's pure ASGI middleware trusts `X-TT-User` only when the Authorization ID token is from `tt-web-sa`. It returns 403 on a user mismatch and 401 on a missing identity, 404s the blocked canned routes, and turns the ownership `ValueError` into a 404.

**Tech Stack:** Python 3.13, uv, FastAPI/Starlette, `google-auth` (`google.auth.jwt`), google-adk 2.10 `VertexAiSessionService`, Next.js 16 route handlers (nodejs runtime), `jose` 6.x, pytest + httpx `ASGITransport`, Vitest, Cloud Run + direct IAP.

---

## Context

Today anyone behind IAP can poll, resume or read any session by typing its `userId`/`sessionId`. The frontend mints `user_${Date.now()}` in the browser, and the backend trusts whatever userId the client sends. P3 was proposed on 2026-09-29 and P2 is complete (main 140f9e8), so this is next.

**User decisions (2026-09-29):**
- The user id is the **normalized email**.
- **Legacy `user_<ts>` sessions are orphaned.** There is no grace period, because there are no live users.
- **Include the live rollout** (Phases A→B→C).
- **Add the `hd` check** in the proxy.

The P3 doc is the source of truth for the per-task code and tests. **This plan adds execution order, the rollout mechanics we actually use, and the corrections below.** Read the P3 doc's task section before each task.

## Corrections to the P3 doc (verified against current code, 2026-09-29)

1. **The proxy's ID token has no `email` claim (blocker for Task 2 as written).**
   - `frontend/src/lib/gcp-auth.ts:25-37` fetches `…/service-accounts/default/identity?audience=…` without `format=full`. The default-format token carries only `aud/azp/sub/iss/iat/exp`.
   - The backend check `email == TRUSTED_PROXY_SA and email_verified is True` would therefore reject every request.
   - **Fix (Task 6):** add `&format=full` to the metadata URL, and add a Vitest case asserting it is in the URL. The `gcloud auth print-identity-token` fallback is local-only and already includes the email for user accounts.
   - For manual curls that impersonate `tt-web-sa`, add `--include-email`.
2. **The current line refs have moved.**
   - `runserver/async_runs.py`: `configure` is at `:647-651`, the globals at `:644-645`, `_StartRunBody` at `:654-657`, `router` at `:671`, `http_start_run` at `:694`, poll at `:712`, resume at `:728`.
   - `deployment/async_app.py`: `configure(...)` is at `:96` and `include_router` at `:97`. It adds no middleware of its own; ADK adds CORS/Origin/DefaultApp.
   - The frontend userId sits at `page.tsx:85`, `run/[sessionId]/page.tsx:69` (not 74) and `results/[sessionId]/page.tsx:130`. The fallback is `"default_user"`, which becomes `SELF_USER_ID`.
3. **Foreign-session behavior today:**
   - Poll returns 200 `not_found`, because the broad `except` at `async_runs.py:169-180` catches it.
   - **Start and resume return unhandled 500s.** `_get_session_or_none` (`:36-51`) re-raises the Vertex `ValueError('Session … does not belong to user …')`.
   - The ownership handler from P3 Task 3 fixes both. Add router tests that the start and resume paths return 404, using a fake session service that raises this `ValueError`.
4. **The blocked-route regex must also cover `POST /agent-identity/finalize`.** The UI doesn't use it. ADK 2.10 `web=False` registers exactly 23 routes (`api_server.py:1392+`); none of the dev/eval routes exist. Also:
   - Make sure `UserAuthzMiddleware` handles the `websocket` scope, so `/run_live` gets 404. For a websocket, reject with a close frame and never pass it through.
   - Add a test that a percent-encoded path user (`alice%40x.com`) matches `X-TT-User: alice@x.com`. Starlette's `scope["path"]` is already decoded.
5. **`jose` is only transitive** (6.2.2, via `@modelcontextprotocol/sdk`). Make it a direct dependency with `cd frontend && npm install jose@^6`. Vitest defaults to `jsdom`, which breaks jose's `Uint8Array` realm checks, so `iap-identity.test.ts` starts with `// @vitest-environment node`.
6. **`google-auth` is only transitive** (2.59.0). Declare it with `uv add google-auth` as the P3 doc says, then run `uv export --format requirements-txt --no-hashes --no-dev -o requirements.txt`.
7. **Add the `hd` check (user decision).**
   - `verifyIapJwt` takes an optional `allowedHd`. The route reads `process.env.IAP_ALLOWED_HD`.
   - When `K_SERVICE` is set and `IAP_ALLOWED_HD` is unset, the proxy **fails closed** with 401 and logs `IAP_ALLOWED_HD unset`.
   - Tests: matching `hd` passes, wrong `hd` or missing `hd` rejects, and there is no hd requirement locally.
   - Phase B sets `IAP_ALLOWED_HD=jordantotten.altostrat.com` via `--update-env-vars`, which merges, so `ADK_API_BASE` is preserved.
8. **Rollout mechanics are stale.**
   - The rollback targets are now **api `trend-trawler-api-00114-dor`** and **web `trend-trawler-web-00021-jmn`**. The `main-current` tag is on 00017-sqg. Task 0 re-reads them anyway.
   - Tags use our `verify`/`prev` convention, not `main-clean`.
   - Phase A becomes **one** `--no-traffic --tag verify` deploy that carries `--update-env-vars` (below), not "env first, then code". A new-code revision booting without `TRUSTED_PROXY_*` in enforce mode raises at startup, so the env must ride along with the code.
9. **`TRUSTED_PROXY_AUDIENCES` must equal the proxy's audience.** That is `new URL(ADK_API_BASE).origin` (route.ts:63).
   - Task 0 reads the web's `ADK_API_BASE`.
   - Set the audiences to that origin **plus** the other Cloud Run URL form, e.g. both `https://trend-trawler-api-qqzji3hyoa-uc.a.run.app` and `https://trend-trawler-api-934903580331.us-central1.run.app`, comma-separated.
10. **The screenshot harness** (`frontend/scripts/capture-screenshots.mjs:58`, `USER="demo_user"`) passes `?userId=` explicitly, so the pages still use it. No change is needed. Don't add harness work.

## Conventions (restate in every subagent dispatch)

- Branch `feat/p3-per-user-authz` off `main`: **one PR**, both CI workflows (python-ci + frontend-tests). Squash-merge after CI is green.
- **Never add `Co-Authored-By` trailers or any AI attribution to commits, and never put "Generated with Claude Code" in PR bodies.**
- Python loop: `uv run ruff format --check . && uv run ruff check . && uv run ty check && uv run pytest tests/ -q`.
- Frontend loop: `cd frontend && npm run lint && npm test && npm run build`.
- TDD per the P3 doc: failing test first, one commit per task using the P3 doc's commit message.
- Don't hand-edit `.env`. No BigQuery writes. The only live writes are the probe sessions in the `trend-trawler-sessions` store, which are deleted afterwards.

---

## Task 0: Commit the plan + pre-flight (read-only apart from the probe session)

1. Create the branch. Copy this file to `docs/plans/2026-09-29-p3-execution.md` and update the P3 doc's Status line. Commit `docs(p3): execution plan`.
2. Record the rollback targets:
   - `gcloud run services describe trend-trawler-api --region us-central1 --format='value(status.traffic)'`, expected 00114-dor @100.
   - The same for trend-trawler-web, expected latestRevision 00021-jmn.
   - Also save the web's `ADK_API_BASE` (`--format='value(spec.template.spec.containers[0].env)'`) and the api URL(s) (`status.url`, `metadata.annotations."run.googleapis.com/urls"`).
   - Write them all to `/tmp/p3-rollback.txt`.
3. Probe email `user_id`s using the P3 doc Task 0 snippet, with project `hybrid-vertex` and the engine ID parsed from the api's `SESSION_SERVICE_URI`. Expect `ok <id> p3-probe@jordantotten.altostrat.com`. If it is rejected, apply the P3 doc's `_at_` mapping on both sides.

## Tasks 1–3: Backend (P3 doc Tasks 1, 2, 3)

- **Task 1**, `runserver/authz.py` pure helpers + `tests/test_authz.py`: as in the doc, plus the `agent-identity/finalize` block (Correction 4).
- **Task 2**, `verify_proxy_caller`: as in the doc, plus `uv add google-auth` and the requirements export (Correction 6).
  - Keep the `email_verified is True` check; Correction 1 makes it satisfiable.
  - Also test a token whose `aud` isn't in the allowlist, and one with the wrong `email`.
- **Task 3**, `UserAuthzMiddleware` + `install_ownership_handler` + router body check + `async_app.py` wiring: as in the doc, plus the websocket scope, percent-encoded path, and start/resume→404 tests (Corrections 3 and 4).
  - `async_runs.configure` gains `authz_mode`, which defaults to `TRUST_CLIENT`. The existing `test_router_maps_run_already_active_to_409_on_start_and_resume` (`tests/test_async_runs.py:1168`) must stay green unchanged.
- Boot checks:
  - `TRUST_CLIENT_USER_ID=1 uv run uvicorn deployment.async_app:app --port 8000` + `curl localhost:8000/list-apps` returns 3 apps.
  - `K_SERVICE=x TRUST_CLIENT_USER_ID=1 …` refuses to boot.
  - `USER_AUTHZ_MODE=enforce` without `TRUSTED_PROXY_*` refuses to boot.

## Tasks 4–6: Frontend (P3 doc Tasks 4, 5, 6)

- **Task 4**, `frontend/src/lib/iap-identity.ts`: as in the doc, plus `npm install jose@^6`, the node-env pragma, and `allowedHd` (Corrections 5 and 7).
- **Task 5**, `frontend/src/lib/user-scoping.ts`: exactly as in the doc. The allowlist matches every call in `src/lib/api.ts`, which was verified: `list-apps`, create/get/list sessions, list/get artifacts, `POST runs/{a}`, `GET runs/{a}/{u}/{sid}`, and resume.
- **Task 6**, wire `route.ts`:
  - `resolveUser` runs before `stripInboundCredentials`.
  - Add `x-tt-user` to `INBOUND_CREDENTIAL_HEADERS`.
  - Scope the path and body, then build `target` from the scoped path while still appending `request.nextUrl.search`.
  - Add `format=full` in `gcp-auth.ts` (Correction 1) with a test.
  - Add `SELF_USER_ID` in `api.ts`. Use it in `page.tsx:85`, and as the fallback in `run/[sessionId]/page.tsx:69` and `results/[sessionId]/page.tsx:130`.
  - Local smoke: run the backend with `TRUST_CLIENT_USER_ID=1` and the frontend with `npm run dev`. A trend_scout run polls to completion, because locally there is no JWT and requests pass through.

## Task 7: Docs (P3 doc Task 7)

Covers:
- `CLAUDE.md`: the local-dev command gets `TRUST_CLIENT_USER_ID=1`, plus a trust-model paragraph under Frontend/Deployment.
- `deployment/README.md`: the env vars `USER_AUTHZ_MODE`, `TRUSTED_PROXY_SA`, `TRUSTED_PROXY_AUDIENCES` and `IAP_ALLOWED_HD`, and the runbook.
- `tests/README.md`.
- `frontend/.env.example`: `IAP_AUDIENCE` and `IAP_ALLOWED_HD`, both optional and local-irrelevant.

Then open the PR, request a final whole-diff review, and merge on green.

## Task 8: Live rollout (from a clean `main` worktree)

Before each phase, confirm no run is in flight (no `running` `__run_status` in recent api logs).

**Phase A: api in observe mode (one revision).**
```
gcloud run deploy trend-trawler-api --source . --region us-central1 --no-allow-unauthenticated \
  --service-account tt-api-sa@hybrid-vertex.iam.gserviceaccount.com --memory 8Gi --cpu 4 \
  --min-instances 1 --timeout 900 --no-cpu-throttling --no-traffic --tag verify --quiet \
  --update-env-vars "^|^USER_AUTHZ_MODE=observe|TRUSTED_PROXY_SA=tt-web-sa@hybrid-vertex.iam.gserviceaccount.com|TRUSTED_PROXY_AUDIENCES=<origin1>,<origin2>"
```
1. Find the new revision by newest `creationTimestamp`; the success line prints the old one.
2. Verify on the tag URL with `TOK=$(gcloud auth print-identity-token --impersonate-service-account=tt-web-sa@hybrid-vertex.iam.gserviceaccount.com --audiences=<api url> --include-email)`:
   - `/list-apps` returns 200.
   - `/run_sse` returns 404.
   - A user path without `X-TT-User` returns 200, and the logs show an `authz observe: would deny 401` line.
3. Pin traffic: `gcloud run services update-traffic trend-trawler-api --region us-central1 --to-revisions <new>=100 --update-tags prev=trend-trawler-api-00114-dor --remove-tags verify`.
4. The UI must keep working on the old web.

**Phase B: web proxy.**
```
gcloud run deploy trend-trawler-web --source ./frontend --region us-central1 \
  --service-account tt-web-sa@hybrid-vertex.iam.gserviceaccount.com --no-traffic --tag p3 --quiet \
  --update-env-vars IAP_ALLOWED_HD=jordantotten.altostrat.com
```
Never pass `--allow-unauthenticated` or `--set-env-vars`.
1. Unauthenticated `curl -sI <p3 tag url>` must return 302 (IAP).
2. Ask the user to open the `p3` tag URL in their browser and run a `trend_scout` campaign end to end. I can't pass IAP myself.
3. On a 401, read the proxy's `IAP JWT rejected:` log line. On an audience mismatch, set `IAP_AUDIENCE` via `--update-env-vars`. Also check P3 open question 4 (the admin's direct invoker path).
4. The api logs must show **no** `would deny` lines for this traffic.
5. Promote with `gcloud run services update-traffic trend-trawler-web --region us-central1 --to-latest --remove-tags p3`.

**Phase C: enforce.**
- `gcloud run services update trend-trawler-api --region us-central1 --update-env-vars USER_AUTHZ_MODE=enforce`.
- Then find the new revision and pin it (`--to-revisions <new>=100`), keeping `prev` on 00114-dor.

**Live verification** (P3 doc's list, with our values):
1. Create probe user B's session with the Task 0 snippet (`p3-probe-b@jordantotten.altostrat.com`, no delete) and note `B_ID`.
2. Browser devtools steps: the user runs these. I'll give the exact snippets. Expected results:
   - GET B's session returns 404.
   - Poll returns `not_found`.
   - Resume returns 404.
   - `users/me/sessions` lists only the user's own email.
   - `run_sse` returns 404.
3. Proxy bypass (I run these with curl and the `--include-email` token):
   - A mismatched `X-TT-User` returns 403.
   - No `X-TT-User` returns 401.
   - A plain user token is rejected by Cloud Run IAM.
4. Delete the B probe session.
5. Update memory: `trend-trawler-api-traffic-pin` gets the new api/web revisions, and `adk-pipe-work-status` gets "P3 COMPLETE".
6. Mark the P3 doc's Status complete in a small docs PR.

**Rollback** (P3 doc, with our revisions):
- Fastest: set `USER_AUTHZ_MODE=observe` and pin the new revision.
- Code: `--to-revisions trend-trawler-api-00114-dor=100`.
- Web: `--to-revisions trend-trawler-web-00021-jmn=100`, then restore `--to-latest` later.
- Always move the api to observe **before** rolling back the web.
- Never re-add `allUsers`, and never disable IAP.

## Stop conditions (report to the user; don't improvise)

- The Task 0 probe rejects both the email form and the `_at_` form.
- Phase A tag verification shows the app can't verify the tt-web-sa token (e.g. the signature is stripped or the email is missing even with `--include-email`). Don't pin traffic. Report and propose trusting Cloud Run's IAM-validated claims instead.
- The Phase B proxy returns 401 for the real user after one `IAP_AUDIENCE` fix attempt.
- Any live-verification check leaks another user's data. Leave the api on enforce, since observe would widen the hole. Report to the user with the failing request and response. The pre-P3 state was equally open, so a rollback gains nothing.

## Verification (end to end)

- CI is green on the PR (python-ci + frontend-tests). The local Python and frontend loops pass.
- Boot guards behave as in Task 3; the local trend_scout smoke passes through the proxy.
- In production:
  - An unauthenticated api returns 403.
  - The web returns 302 to IAP.
  - A campaign run via the web completes, and its session `userId` is the caller's email.
  - All live-verification checks give the expected 404/403/401/`not_found`.
  - The api is pinned to the new revision with `prev`=00114-dor.
  - The web is auto-routed to the new revision.
