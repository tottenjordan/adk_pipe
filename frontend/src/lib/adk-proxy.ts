/**
 * Pure helpers for the `/api/adk` proxy (`app/api/adk/[...path]/route.ts`).
 *
 * Kept out of the route file: a route module may export only HTTP method handlers and
 * segment config (`next build --webpack` type-checks this and fails on anything else).
 */

import "server-only";

/** A remote https backend is a private Cloud Run service → attach an ID token.
 *  A localhost backend (dev) is unauthenticated → skip. */
export function backendNeedsAuth(base: string): boolean {
  return base.startsWith("https://");
}

/** The only inbound request headers forwarded to the private backend (allowlist).
 *
 *  Everything else is dropped: IAP's edge credentials (`Authorization` = the IAP JWT,
 *  `X-Serverless-Authorization`, which Cloud Run ingress checks in preference to
 *  `Authorization` and would 401 on the wrong audience, the `X-Goog-*` identity headers),
 *  the IAP session cookie, a client-supplied `x-tt-user`, `Forwarded`/`X-Forwarded-*`, and
 *  any header the backend or Cloud Run might start trusting later. `authorization` (our
 *  minted ID token) and `x-tt-user` (the IAP-verified caller) are set explicitly by the
 *  proxy. The W3C / Cloud trace headers keep request traces joined across the hop. */
export const FORWARDED_REQUEST_HEADERS = [
  "content-type",
  "accept",
  "traceparent",
  "tracestate",
  "x-cloud-trace-context",
] as const;

/** Fresh upstream headers holding only the allowlisted inbound headers. */
export function upstreamRequestHeaders(inbound: Headers): Headers {
  const out = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = inbound.get(name);
    if (value !== null) out.set(name, value);
  }
  return out;
}
