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

## Leaving the run page mid-run cancels the run (SSE abort)

The run is driven by a long-lived `POST /run_sse` stream held open by the run
page. If you **navigate away or refresh before the run completes** — e.g. click
through to `/results` while the final leg is still working — the browser aborts
that fetch, and the server logs `Root node ... was cancelled` and kills the
in-flight agent. Observed 2026-07-12: an `interactive_creative` run that had
already finished all 12 eval scores was cancelled ~4 s later when the results
page loaded (the log shows the cancellation immediately followed by
`GET .../results/.../artifacts/*` requests), so the eval report / gallery / BQ
writes never ran. This is distinct from the ~12-min request timeout (that run was
only ~6.5 min in). Interactive checkpoints split the run into separate requests,
but the **final leg after checkpoint 3 has no checkpoint**, so it only survives
while the run page stays open. Practical rule: don't leave the run page until it
reports completion. (The concurrent-eval fix — see `local-testing.md` — shrinks
this vulnerable window from ~5.5 min to ~30 s.)

## Same-origin proxy (why it exists)

The browser talks to the ADK api_server through a same-origin Next.js proxy
(`frontend/src/app/api/adk/[...path]/route.ts`, base `/api/adk`), not directly.
This bypasses the Cloud Workstations port-auth redirect + CORS and streams SSE
through. `NEXT_PUBLIC_API_BASE` can override it to hit an api_server directly.
Note this proxy does NOT bypass the ~12-min HTTP request timeout that kills long
runs — see `local-testing.md`.
