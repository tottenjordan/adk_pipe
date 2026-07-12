# Frontend notes

Written 2026-07-12 on the `creative-eval` branch.

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
The crash remounted the run page, which re-fired **3 concurrent `POST /run_sse`**
calls onto the same session. That produced `KeyError: raw_gtrends` and
stale-session `ValueError`s in the api_server logs — which look like agent bugs
but are downstream symptoms of the React crash. Fixing `formatStateValue` made
both disappear. If you see multiple concurrent runs on one session or
`raw_gtrends` KeyErrors, suspect a frontend render crash first.

## Same-origin proxy (why it exists)

The browser talks to the ADK api_server through a same-origin Next.js proxy
(`frontend/src/app/api/adk/[...path]/route.ts`, base `/api/adk`), not directly.
This bypasses the Cloud Workstations port-auth redirect + CORS and streams SSE
through. `NEXT_PUBLIC_API_BASE` can override it to hit an api_server directly.
Note this proxy does NOT bypass the ~12-min HTTP request timeout that kills long
runs — see `local-testing.md`.
