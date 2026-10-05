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

/** Inbound credential headers that must NOT be forwarded to the private backend.
 *
 *  When the frontend is behind IAP, IAP authenticates the user at the edge and injects its
 *  own credentials on the request that reaches this container: `Authorization` (the IAP
 *  JWT, audience = IAP OAuth client ID), the `X-Goog-*` identity headers, and — critically —
 *  `X-Serverless-Authorization`, which Cloud Run's ingress checks *in preference to*
 *  `Authorization`. If any of these reach the backend, Cloud Run verifies the IAP token
 *  (wrong audience) and returns `401 "The access token could not be verified"`, ignoring the
 *  service-account token we set. Stripping them all guarantees the backend sees only our
 *  minted token. The `cookie` (large IAP session cookie) is dropped too — the backend has no
 *  use for it. A client-supplied `x-tt-user` is dropped so only the proxy can assert the
 *  (IAP-verified) user to the backend. */
export const INBOUND_CREDENTIAL_HEADERS = [
  "authorization",
  "x-serverless-authorization",
  "cookie",
  "x-goog-iap-jwt-assertion",
  "x-goog-authenticated-user-email",
  "x-goog-authenticated-user-id",
  "x-tt-user",
] as const;

/** Remove every inbound credential header so IAP's edge credentials never leak to the
 *  backend. Mutates and returns `headers`. */
export function stripInboundCredentials(headers: Headers): Headers {
  for (const name of INBOUND_CREDENTIAL_HEADERS) headers.delete(name);
  return headers;
}

async function proxy(
  request: NextRequest,
  ctx: { params: Promise<{ path: string[] }> }
): Promise<Response> {
  const { path } = await ctx.params;

  // Resolve the caller from the IAP assertion BEFORE stripInboundCredentials deletes it.
  // On Cloud Run a missing/invalid assertion (or unset IAP_ALLOWED_HD) fails closed; locally
  // there is no assertion and the request passes through unscoped.
  const who = await resolveUser(request.headers, {
    onCloudRun: !!process.env.K_SERVICE,
    allowedHd: process.env.IAP_ALLOWED_HD || undefined,
  });
  if (who.kind === "reject") return new Response("Unauthenticated", { status: who.status });

  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("connection");
  headers.delete("content-length");

  // Drop IAP's edge-injected credentials so only our minted token reaches the backend
  // (see INBOUND_CREDENTIAL_HEADERS for why — X-Serverless-Authorization is the key one).
  stripInboundCredentials(headers);

  const audience = new URL(BACKEND).origin;
  if (backendNeedsAuth(BACKEND)) {
    const token = await getIdentityToken(audience);
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
