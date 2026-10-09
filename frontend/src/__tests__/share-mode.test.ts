import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { proxy } from "@/proxy";
import robots from "@/app/robots";
import {
  buildShareCsp,
  decideRoute,
  isShareModeEnabled,
  isSharePath,
  SHARE_PAGE_HEADERS,
} from "@/lib/share-mode";

const TOKEN = "abc_DEF-123456789"; // 17 chars, URL-safe base64 alphabet

describe("isShareModeEnabled", () => {
  it("is on only for SHARE_MODE=1", () => {
    expect(isShareModeEnabled({ SHARE_MODE: "1" })).toBe(true);
    for (const v of [undefined, "", "0", "true", "yes", " 1"]) {
      expect(isShareModeEnabled({ SHARE_MODE: v })).toBe(false);
    }
  });
});

describe("isSharePath", () => {
  it.each([
    [`/s/${TOKEN}`, true],
    [`/s/${TOKEN}/img/0`, true],
    [`/s/${TOKEN}/img/3`, true],
    [`/s/${TOKEN}/img/12`, true],
    [`/s/${"a".repeat(16)}`, true],
    [`/s/${"a".repeat(64)}`, true],
    [`/s/${"a".repeat(15)}`, false],
    [`/s/${"a".repeat(65)}`, false],
    ["/s/", false],
    ["/s", false],
    [`/s/${TOKEN}/`, false],
    [`/s/${TOKEN}/img/`, false],
    [`/s/${TOKEN}/img/0/`, false],
    [`/s/${TOKEN}/img/x`, false],
    [`/s/${TOKEN}/img/-1`, false],
    [`/s/${TOKEN}/img/123`, false],
    [`/s/${TOKEN}/img/0.png`, false],
    [`/s/${TOKEN}/other`, false],
    [`/s/${TOKEN}%2F..%2F..%2Fapi`, false],
    [`/s/abcdefghijklmnop%2Fimg%2F0`, false],
    [`/s/${TOKEN}/img/0%2F..`, false],
    ["/s/../api/gcs", false],
    ["/s/..%2Fapi%2Fgcs%3Fbucket", false],
    [`/s/${TOKEN}.json`, false],
    [`/S/${TOKEN}`, false],
    [`//s/${TOKEN}`, false],
    [`/s//${TOKEN}`, false],
  ])("%s -> %s", (path, expected) => {
    expect(isSharePath(path)).toBe(expected);
  });
});

describe("decideRoute in share mode", () => {
  const share = (p: string) => decideRoute(p, { shareMode: true, dev: false });

  it.each([
    `/s/${TOKEN}`,
    `/s/${TOKEN}/img/0`,
  ])("serves share pages: %s", (p) => {
    expect(share(p)).toBe("share");
  });

  it.each([
    "/_next/static/chunks/main.js",
    "/_next/static/media/archivo.woff2",
    "/_next/static/css/app.css",
    "/robots.txt",
    "/favicon.ico",
  ])("passes static assets: %s", (p) => {
    expect(share(p)).toBe("allow");
  });

  it.each([
    "/",
    "/runs",
    "/runs/",
    "/run/abc",
    "/results/x",
    "/results/x/",
    "/experiments",
    "/experiments/e1",
    "/api",
    "/api/",
    "/api/gcs",
    "/api/gcs?bucket=x&path=y",
    "/api/adk/list-apps",
    "/api/adk/apps/creative_agent/users/me/sessions",
    "/API/gcs",
    "/s",
    "/s/",
    "/s/short",
    `/s/${TOKEN}/`,
    `/s/${TOKEN}/img/x`,
    `/s/${TOKEN}%2Fimg%2F0`,
    "/s/../api",
    "/s/%2E%2E/api/gcs",
    "/_next/image",
    "/_next/image?url=%2Fapi%2Fgcs",
    "/_next/data/build/x.json",
    "/_next/webpack-hmr",
    "/_next/static/../../api/gcs",
    "/_next/static/%2E%2E/api",
    "/_next",
    "/trend_trawler_banner.png",
    "/robots.txt/",
    "/favicon.ico/x",
    "/sitemap.xml",
    "/.env",
    "/%2Fapi%2Fgcs",
  ])("denies everything else: %s", (p) => {
    expect(share(p)).toBe("deny");
  });

  it("only treats the pathname, never the query, as the route", () => {
    // Pathnames never carry a query; a literal "?" in the path is not a share path.
    expect(share(`/s/${TOKEN}?view=feed`)).toBe("deny");
  });

  it("lets Next's dev-only internals through in development", () => {
    const dev = (p: string) => decideRoute(p, { shareMode: true, dev: true });
    expect(dev("/_next/webpack-hmr")).toBe("allow");
    expect(dev("/__nextjs_original-stack-frames")).toBe("allow");
    expect(dev("/_next/image")).toBe("deny");
    expect(dev("/api/gcs")).toBe("deny");
    expect(dev("/")).toBe("deny");
  });
});

describe("decideRoute in normal mode", () => {
  const normal = (p: string) => decideRoute(p, { shareMode: false, dev: false });

  it("leaves every existing route alone", () => {
    for (const p of ["/", "/runs", "/results/x", "/api/gcs", "/api/adk/list-apps", "/_next/image"]) {
      expect(normal(p)).toBe("allow");
    }
  });

  it("serves share pages (owner preview behind IAP) with share headers", () => {
    expect(normal(`/s/${TOKEN}`)).toBe("share");
    expect(normal(`/s/${TOKEN}/img/1`)).toBe("share");
    expect(normal("/s/short")).toBe("allow"); // falls through to Next's own 404
  });
});

describe("share headers", () => {
  it("set noindex, no referrer and a short private cache", () => {
    expect(SHARE_PAGE_HEADERS).toEqual({
      "X-Robots-Tag": "noindex, nofollow",
      "Referrer-Policy": "no-referrer",
      "Cache-Control": "private, max-age=60",
      "X-Content-Type-Options": "nosniff",
    });
  });

  it("build a nonce-based CSP with no framing and same-origin images", () => {
    const csp = buildShareCsp("NONCE123", false);
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("script-src 'self' 'nonce-NONCE123' 'strict-dynamic'");
    expect(csp).toContain("img-src 'self' data:");
    expect(csp).toContain("style-src 'self' 'unsafe-inline'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).toContain("base-uri 'none'");
    expect(csp).toContain("form-action 'none'");
    expect(csp).not.toContain("unsafe-eval");
    expect(csp).not.toMatch(/\s{2,}|\n/);
  });

  it("allow eval only in development (React dev tooling)", () => {
    expect(buildShareCsp("N", true)).toContain("'unsafe-eval'");
  });
});

describe("proxy", () => {
  const req = (path: string) => new NextRequest(new URL(path, "https://share.example"));

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("404s non-share routes in share mode, read at request time", () => {
    vi.stubEnv("SHARE_MODE", "1");
    for (const p of ["/", "/api/gcs?bucket=b&path=x", "/api/adk/list-apps", "/runs"]) {
      const res = proxy(req(p));
      expect(res.status).toBe(404);
      expect(res.headers.get("X-Robots-Tag")).toBe("noindex, nofollow");
      expect(res.headers.get("x-middleware-next")).toBeNull();
    }
    vi.stubEnv("SHARE_MODE", "");
    expect(proxy(req("/api/gcs?bucket=b&path=x")).headers.get("x-middleware-next")).toBe("1");
  });

  it("serves share pages with headers and a fresh nonce CSP", () => {
    vi.stubEnv("SHARE_MODE", "1");
    const a = proxy(req(`/s/${TOKEN}?view=feed`));
    const b = proxy(req(`/s/${TOKEN}`));
    expect(a.status).toBe(200);
    expect(a.headers.get("x-middleware-next")).toBe("1");
    for (const [k, v] of Object.entries(SHARE_PAGE_HEADERS)) {
      expect(a.headers.get(k)).toBe(v);
    }
    const csp = a.headers.get("Content-Security-Policy") ?? "";
    expect(csp).toMatch(/'nonce-[A-Za-z0-9+/=]+'/);
    expect(csp).not.toBe(b.headers.get("Content-Security-Policy"));
    // Next reads the CSP (and so the nonce) from the forwarded request headers.
    expect(a.headers.get("x-middleware-request-content-security-policy")).toBe(csp);
  });

  it("robots.txt disallows everything", () => {
    expect(robots()).toEqual({ rules: { userAgent: "*", disallow: "/" } });
  });

  it("passes static assets through in share mode", () => {
    vi.stubEnv("SHARE_MODE", "1");
    const res = proxy(req("/_next/static/chunks/app.js"));
    expect(res.headers.get("x-middleware-next")).toBe("1");
  });
});
