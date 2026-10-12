import { NextRequest } from "next/server";
import { afterEach, describe, it, expect, vi } from "vitest";
import {
  backendNeedsAuth,
  FORWARDED_REQUEST_HEADERS,
  upstreamRequestHeaders,
} from "@/app/api/adk/[...path]/route";

describe("backendNeedsAuth", () => {
  it("is true for a remote https backend (private Cloud Run)", () => {
    expect(backendNeedsAuth("https://trend-trawler-api-abc.run.app")).toBe(true);
  });
  it("is false for localhost dev backend", () => {
    expect(backendNeedsAuth("http://localhost:8000")).toBe(false);
  });
});

/** Simulates the request IAP forwards to the container, plus client-spoofed headers. */
const inbound = () =>
  new Headers({
    authorization: "Bearer iap-jwt-wrong-audience",
    "x-serverless-authorization": "Bearer iap-serverless-token",
    cookie: "GCP_IAP_XSRF_NONCE=abc; GCP_IAPAuth=xyz",
    "x-goog-iap-jwt-assertion": "assertion",
    "x-goog-authenticated-user-email": "accounts.google.com:mallory@example.com",
    "x-goog-authenticated-user-id": "accounts.google.com:12345",
    "x-tt-user": "bob@x.com",
    forwarded: "for=1.2.3.4",
    "x-forwarded-for": "1.2.3.4",
    "x-foo": "bar",
    host: "app.example",
    "content-length": "12",
    "content-type": "application/json",
    accept: "application/json",
    traceparent: "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
    tracestate: "congo=t61rcWkgMzE",
    "x-cloud-trace-context": "105445aa7843bc8bf206b120001000/1;o=1",
  });

describe("upstreamRequestHeaders (allowlist)", () => {
  it("forwards only content-type, accept and the trace headers", () => {
    const out = upstreamRequestHeaders(inbound());
    expect([...out.keys()].sort()).toEqual([...FORWARDED_REQUEST_HEADERS].sort());
    expect(out.get("content-type")).toBe("application/json");
    expect(out.get("traceparent")).toMatch(/^00-/);
    expect(out.get("x-cloud-trace-context")).toContain("/1;o=1");
  });

  it.each([
    "authorization",
    "x-serverless-authorization",
    "cookie",
    "x-goog-iap-jwt-assertion",
    "x-goog-authenticated-user-email",
    "x-goog-authenticated-user-id",
    "x-tt-user",
    "forwarded",
    "x-forwarded-for",
    "x-foo",
    "host",
    "content-length",
  ])("never forwards %s", (name) => {
    expect(upstreamRequestHeaders(inbound()).get(name)).toBeNull();
  });

  it("omits allowlisted headers the client did not send", () => {
    const out = upstreamRequestHeaders(new Headers({ "x-foo": "bar" }));
    expect([...out.keys()]).toEqual([]);
  });
});

type Route = typeof import("@/app/api/adk/[...path]/route");

describe("proxy handler headers", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.doUnmock("@/lib/gcp-auth");
    vi.doUnmock("@/lib/iap-identity");
    vi.resetModules();
  });

  async function loadRoute(base: string) {
    vi.stubEnv("ADK_API_BASE", base);
    vi.stubEnv("K_SERVICE", "");
    vi.resetModules();
    vi.doMock("@/lib/gcp-auth", () => ({
      getIdentityToken: vi.fn(async () => "minted-id-token"),
    }));
    // The IAP assertion verified to Alice; the spoofed identity headers must not matter.
    vi.doMock("@/lib/iap-identity", () => ({
      resolveUser: vi.fn(async () => ({ kind: "user", userId: "alice@example.com" })),
    }));
    const fetchMock = vi.fn(
      async (_url: string | URL | Request, _init?: RequestInit) =>
        new Response("{}", { status: 200, headers: { "content-type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const route: Route = await import("@/app/api/adk/[...path]/route");
    return { route, fetchMock };
  }

  const call = (route: Route) =>
    route.GET(new NextRequest("https://app.example/api/adk/list-apps", { headers: inbound() }), {
      params: Promise.resolve({ path: ["list-apps"] }),
    });

  it("replaces the client's authorization with the minted ID token", async () => {
    const { route, fetchMock } = await loadRoute("https://api.example.run.app");
    const res = await call(route);
    expect(res.status).toBe(200);
    const sent = new Headers(fetchMock.mock.calls[0][1]?.headers);
    expect(sent.get("authorization")).toBe("Bearer minted-id-token");
    expect(sent.get("x-serverless-authorization")).toBeNull();
    expect(sent.get("x-goog-authenticated-user-email")).toBeNull();
    expect(sent.get("forwarded")).toBeNull();
    expect(sent.get("x-forwarded-for")).toBeNull();
    expect(sent.get("x-foo")).toBeNull();
    expect(sent.get("x-tt-user")).toBe("alice@example.com");
    expect(sent.get("content-type")).toBe("application/json");
    expect(sent.get("traceparent")).toMatch(/^00-/);
  });

  it("sends no authorization at all to a localhost backend", async () => {
    const { route, fetchMock } = await loadRoute("http://localhost:8000");
    await call(route);
    const sent = new Headers(fetchMock.mock.calls[0][1]?.headers);
    expect(sent.get("authorization")).toBeNull();
    expect(sent.get("cookie")).toBeNull();
  });
});
