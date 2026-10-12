# Frontend notes

Written 2026-07-12 on the `creative-eval` branch; run-model section refreshed 2026-10-12.

## React crash from nested session-state values (and its backend cascade)

**Symptom:** `Objects are not valid as a React child (found: object with keys
{target_search_trends})` on the run/results pages.

**Root cause:** session-state values aren't all strings. Some keys (notably
`target_search_trends`) are stored as nested objects/arrays, e.g.
`{target_search_trends: [...]}`. The run/results pages rendered them via an
`as string` cast, so React tried to render a raw object.

**Fix:** `formatStateValue(value: unknown): string` in
`frontend/src/lib/utils.ts` flattens any value (string / array / object) to a
display string. Applied in both
`frontend/src/app/run/[sessionId]/page.tsx` and
`frontend/src/app/results/[sessionId]/page.tsx` where campaign/state fields are
mapped for display. (This fix already existed on `main-baseline-fixes` but was
missing on `creative-eval` — port it when moving between branches.)

**Non-obvious cascade:** this frontend crash caused confusing *backend* errors.
The crash remounted the run page, which (in the SSE era) re-fired **3 concurrent
`POST /run_sse`** calls onto the same session. That produced `KeyError: raw_gtrends` and
stale-session `ValueError`s in the api_server logs — which look like agent bugs
but are downstream symptoms of the React crash. Fixing `formatStateValue` made
both disappear. If you see multiple concurrent runs on one session or
`raw_gtrends` KeyErrors, suspect a frontend render crash first.

## Leaving the run page no longer cancels the run (async `/runs` jobs)

*Historical (2026-07-12):* the run used to be a long-lived `POST /run_sse`
stream held open by the run page, so navigating away or refreshing aborted the
fetch and the server cancelled the in-flight agent (`Root node ... was
cancelled`), e.g. an `interactive_creative` final leg killed seconds before its
eval report / gallery / BQ writes.

*Current (since the async-job run model, 2026-07-15):* the run page kicks off a
**detached backend task** (`POST /runs/{app}`, `runserver/async_runs.py`) and
**polls** `GET /runs/{app}/{user}/{session}?since=N` (`pollRun` in
`frontend/src/lib/api.ts`). Progress is read from the persistent session log,
so leaving the page, reloading or an IAP re-auth does **not** cancel the run;
reopening it re-polls from `since=0` and replays without re-sending the kick-off.
Interactive checkpoints resume via `POST …/resume`. Status comes only from the
terminal `__run_status` marker; a contained step error in the log is a muted
"A step reported an error and was skipped" notice (`lib/step-errors.ts`), not a
failure (#322). The bare ADK `/run`, `/run_sse` and `/run_live` routes are
blocked (404) by the backend authz middleware.

## Same-origin proxy (why it exists)

The browser talks to the ADK api_server through a same-origin Next.js proxy
(`frontend/src/app/api/adk/[...path]/route.ts`, base `/api/adk`), not directly.
This bypasses the Cloud Workstations port-auth redirect + CORS. On Cloud Run it
is also the P3 authz boundary: it verifies the IAP JWT, allowlists routes,
rewrites every `userId` to the caller and sets `X-TT-User`
(`lib/iap-identity.ts`, `lib/user-scoping.ts`). `NEXT_PUBLIC_API_BASE` can
override it to hit a backend directly. Long runs are no longer affected by the
~12-min HTTP request timeout, since each poll is a short request.
