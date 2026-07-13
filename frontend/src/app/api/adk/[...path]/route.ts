import { NextRequest } from "next/server";
import { Agent } from "undici";
import { getIdentityToken } from "@/lib/gcp-auth";

// Same-origin proxy to the ADK api_server. The browser calls /api/adk/* (same origin
// as the app, so no CORS and no Cloud Workstations port-auth), and Next forwards
// server-side to the api_server on localhost. The RESPONSE body is streamed through
// untouched so SSE (/run_sse) keeps streaming; the request body is small JSON so it
// is buffered.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// ADK `/run_sse` streams for many minutes. undici (Node's global fetch) defaults
// `bodyTimeout`/`headersTimeout` to 300_000ms (5 min) — an *idle* timeout that aborts the
// response body when a quota-paced run pauses longer than 5 min between SSE events,
// surfacing as `UND_ERR_BODY_TIMEOUT` and a truncated / blank run. 0 disables both.
export const PROXY_STREAM_TIMEOUTS = { bodyTimeout: 0, headersTimeout: 0 };
const streamDispatcher = new Agent(PROXY_STREAM_TIMEOUTS);

const BACKEND = process.env.ADK_API_BASE ?? "http://localhost:8000";

/** A remote https backend is a private Cloud Run service → attach an ID token.
 *  A localhost backend (dev) is unauthenticated → skip. */
export function backendNeedsAuth(base: string): boolean {
  return base.startsWith("https://");
}

async function proxy(
  request: NextRequest,
  ctx: { params: Promise<{ path: string[] }> }
): Promise<Response> {
  const { path } = await ctx.params;
  const target = `${BACKEND}/${path.join("/")}${request.nextUrl.search}`;

  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("connection");
  headers.delete("content-length");

  if (backendNeedsAuth(BACKEND)) {
    const token = await getIdentityToken(new URL(BACKEND).origin);
    headers.set("authorization", `Bearer ${token}`);
  }

  const method = request.method;
  const body =
    method === "GET" || method === "HEAD" ? undefined : await request.text();

  const init: RequestInit & { dispatcher: Agent } = {
    method,
    headers,
    body,
    redirect: "manual",
    // undici honors `dispatcher` on Node's global fetch; disables the 5-min idle timeout
    // so long SSE streams are not aborted mid-run.
    dispatcher: streamDispatcher,
  };
  const upstream = await fetch(target, init);

  // Strip hop-by-hop / length headers that would conflict with the streamed body.
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
