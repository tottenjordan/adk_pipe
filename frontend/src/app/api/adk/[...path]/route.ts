import { NextRequest } from "next/server";
import { backendNeedsAuth, upstreamRequestHeaders } from "@/lib/adk-proxy";
import { BACKEND_FETCH_TIMEOUT_MS, isTimeoutError } from "@/lib/fetch-timeout";
import { getIdentityToken } from "@/lib/gcp-auth";
import { resolveUser } from "@/lib/iap-identity";
import { scopeQuery, scopeRequestToUser } from "@/lib/user-scoping";

// Same-origin proxy to the ADK api_server. The browser calls /api/adk/* (same origin
// as the app, so no CORS and no Cloud Workstations port-auth), and Next forwards
// server-side to the api_server on localhost. Request/response bodies are small JSON:
// the async-job run model replaced the long-lived SSE stream (/run_sse) with short
// GET polls (/runs/...), so the proxy no longer needs undici's idle-timeout override.
// No route segment config: route handlers are uncached and run on Node.js by default.

const BACKEND = process.env.ADK_API_BASE ?? "http://localhost:8000";

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
    signal: AbortSignal.timeout(BACKEND_FETCH_TIMEOUT_MS),
  };
  let upstream: Response;
  try {
    upstream = await fetch(target, init);
  } catch (err) {
    if (isTimeoutError(err)) return new Response("Backend timed out", { status: 504 });
    throw err;
  }

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
