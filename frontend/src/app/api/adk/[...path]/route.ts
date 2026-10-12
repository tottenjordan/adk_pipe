import { NextRequest } from "next/server";
import { getIdentityToken } from "@/lib/gcp-auth";
import { resolveUser } from "@/lib/iap-identity";
import { scopeQuery, scopeRequestToUser } from "@/lib/user-scoping";

// Same-origin proxy to the ADK api_server. The browser calls /api/adk/* (same origin
// as the app, so no CORS and no Cloud Workstations port-auth), and Next forwards
// server-side to the api_server on localhost. Request/response bodies are small JSON:
// the async-job run model replaced the long-lived SSE stream (/run_sse) with short
// GET polls (/runs/...), so the proxy no longer needs undici's idle-timeout override.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const BACKEND = process.env.ADK_API_BASE ?? "http://localhost:8000";

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

async function proxy(
  request: NextRequest,
  ctx: { params: Promise<{ path: string[] }> }
): Promise<Response> {
  const { path } = await ctx.params;

  // Resolve the caller from the IAP assertion (never forwarded upstream).
  // On Cloud Run a missing/invalid assertion (or unset IAP_ALLOWED_HD) fails closed; locally
  // there is no assertion and the request passes through unscoped.
  const who = await resolveUser(request.headers, {
    onCloudRun: !!process.env.K_SERVICE,
    allowedHd: process.env.IAP_ALLOWED_HD || undefined,
  });
  if (who.kind === "reject") return new Response("Unauthenticated", { status: who.status });

  // Allowlist only: IAP's edge credentials and any client-spoofed header never reach the
  // backend (see FORWARDED_REQUEST_HEADERS).
  const headers = upstreamRequestHeaders(request.headers);

  const audience = new URL(BACKEND).origin;
  if (backendNeedsAuth(BACKEND)) {
    let token = "";
    try {
      token = await getIdentityToken(audience);
    } catch (err) {
      console.error("/api/adk: could not mint a backend identity token", err);
    }
    // Never forward a request to a private backend without a real token — that surfaces as
    // an opaque backend 401. Fail fast with a clear signal instead.
    if (!token) {
      return new Response(
        "Proxy could not obtain a backend identity token",
        { status: 502 },
      );
    }
    headers.set("authorization", `Bearer ${token}`);
  }

  const method = request.method;
  const body =
    method === "GET" || method === "HEAD" ? undefined : await request.text();

  // Only allowlisted routes reach the backend, with every userId (path segment or kick-off
  // body) rewritten to the verified caller, who is also asserted via x-tt-user, and only
  // allowlisted query params (since/version; run on the experiment metrics/creatives routes) forwarded.
  // Local mode (no IAP assertion, no K_SERVICE) is intentionally fail-open: the request
  // passes through unscoped, and the backend's 401 on a missing X-TT-User (unless it runs
  // with TRUST_CLIENT_USER_ID=1) is the backstop.
  let upstreamPath = path.join("/"), upstreamBody = body, upstreamQuery = request.nextUrl.search;
  if (who.kind === "user") {
    let scoped;
    try { scoped = scopeRequestToUser(method, path, body, who.userId); }
    catch { return new Response("Bad JSON body", { status: 400 }); }
    if (!scoped) return new Response("Not found", { status: 404 });
    ({ path: upstreamPath, body: upstreamBody } = scoped);
    upstreamQuery = scopeQuery(request.nextUrl.searchParams, path);
    headers.set("x-tt-user", who.userId);
  }
  const target = `${BACKEND}/${upstreamPath}${upstreamQuery}`;

  const init: RequestInit = {
    method,
    headers,
    body: upstreamBody,
    redirect: "manual",
  };
  const upstream = await fetch(target, init);

  // Strip hop-by-hop / length headers that would conflict with the proxied body.
  const respHeaders = new Headers(upstream.headers);
  respHeaders.delete("content-encoding");
  respHeaders.delete("content-length");
  respHeaders.delete("transfer-encoding");

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: respHeaders,
  });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
export const OPTIONS = proxy;
