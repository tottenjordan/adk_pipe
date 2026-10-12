import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ResolvedUser } from "@/lib/iap-identity";

vi.mock("@/lib/gcp-auth", () => ({ getAccessToken: vi.fn(async () => "ya29.token") }));
const resolveUser = vi.fn<() => Promise<ResolvedUser>>();
vi.mock("@/lib/iap-identity", () => ({ resolveUser: () => resolveUser() }));

import { GET } from "@/app/api/gcs/route";
import { BUCKET_RE } from "@/lib/gcs";
import { gcsMediaUrl, isAllowedBucket } from "@/lib/gcs-server";

const ALICE = "alice.smith@example.com";
const ALICE_SLUG = "alice_smith_example_com-7dcd3a39ad";

/** Build the request with properly encoded query params (raw values in, URLSearchParams out). */
const call = (bucket: string, path: string) =>
  GET(
    new NextRequest(
      `https://app.example/api/gcs?${new URLSearchParams({ bucket, path }).toString()}`
    )
  );

let upstream: { body: BodyInit; contentType: string | null } = {
  body: new Uint8Array([1, 2, 3]),
  contentType: "image/png",
};

const fetchMock = vi.fn(
  async (_url: string | URL | Request) =>
    new Response(upstream.body, {
      status: 200,
      headers: upstream.contentType ? { "content-type": upstream.contentType } : {},
    })
);

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  upstream = { body: new Uint8Array([1, 2, 3]), contentType: "image/png" };
  resolveUser.mockResolvedValue({ kind: "user", userId: ALICE });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  fetchMock.mockClear();
  resolveUser.mockReset();
});

describe("bucket helpers", () => {
  it("BUCKET_RE accepts GCS-style names and rejects URL syntax", () => {
    expect(BUCKET_RE.test("trend-trawler-deploy-ae")).toBe(true);
    expect(BUCKET_RE.test("a.b_c-d")).toBe(true);
    for (const bad of ["b/o", "b?x", "b#x", "b%2Fx", "Bucket", "bucket.", "", "a"]) {
      expect(BUCKET_RE.test(bad)).toBe(false);
    }
  });

  it("isAllowedBucket honours GCS_ALLOWED_BUCKETS (trimmed, comma-separated)", () => {
    expect(isAllowedBucket("tt-bucket", {})).toBe(true);
    expect(isAllowedBucket("tt-bucket", { GCS_ALLOWED_BUCKETS: "" })).toBe(true);
    expect(isAllowedBucket("tt-bucket", { GCS_ALLOWED_BUCKETS: " x-b , tt-bucket " })).toBe(true);
    expect(isAllowedBucket("other-b", { GCS_ALLOWED_BUCKETS: "tt-bucket" })).toBe(false);
    expect(isAllowedBucket("b/o", {})).toBe(false);
  });

  it("gcsMediaUrl encodes both bucket and object", () => {
    expect(gcsMediaUrl("tt-bucket", "a/b c.png")).toBe(
      "https://storage.googleapis.com/storage/v1/b/tt-bucket/o/a%2Fb%20c.png?alt=media"
    );
    expect(gcsMediaUrl("b/o/x?y#", "z")).toBe(
      "https://storage.googleapis.com/storage/v1/b/b%2Fo%2Fx%3Fy%23/o/z?alt=media"
    );
  });
});

describe("/api/gcs bucket validation", () => {
  it.each([
    "b/o",
    "b?alt=media",
    "b#frag",
    "b%2Fo",
    "TT-Bucket",
    "tt-bucket.",
  ])("rejects bucket %j with 400 and never fetches", async (bucket) => {
    const res = await call(bucket, "2026/creative_output/hero.png");
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "Invalid bucket" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects the bucket-injection that would read another user's person photo", async () => {
    // Raw query from the report: the bucket smuggles its own /o/<object>?alt=media#.
    const res = await GET(
      new NextRequest(
        "https://app.example/api/gcs?bucket=b%2Fo%2Fperson-refs%252Fvictim-slug%252Fphoto.jpg%3Falt%3Dmedia%23&path=x.png"
      )
    );
    expect(res.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(resolveUser).not.toHaveBeenCalled();
  });

  it("fetches an allowlisted bucket with the exact encoded URL", async () => {
    vi.stubEnv("GCS_ALLOWED_BUCKETS", "tt-bucket, other-b");
    const res = await call("tt-bucket", "2026/out/hero.png");
    expect(res.status).toBe(200);
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      "https://storage.googleapis.com/storage/v1/b/tt-bucket/o/2026%2Fout%2Fhero.png?alt=media"
    );
  });

  it("rejects a valid bucket that is not in GCS_ALLOWED_BUCKETS", async () => {
    vi.stubEnv("GCS_ALLOWED_BUCKETS", "a-bucket");
    const res = await call("b-bucket", "2026/out/hero.png");
    expect(res.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fails closed for person paths on Cloud Run without an allowlist", async () => {
    vi.stubEnv("K_SERVICE", "trend-trawler-web");
    vi.stubEnv("GCS_ALLOWED_BUCKETS", "");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const res = await call("tt-bucket", `person-refs/${ALICE_SLUG}/me.jpg`);
    expect(res.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it("still serves non-person paths on Cloud Run without an allowlist", async () => {
    vi.stubEnv("K_SERVICE", "trend-trawler-web");
    vi.stubEnv("GCS_ALLOWED_BUCKETS", "");
    const res = await call("tt-bucket", "2026/out/hero.png");
    expect(res.status).toBe(200);
  });

  it("serves person paths on Cloud Run when the bucket is allowlisted", async () => {
    vi.stubEnv("K_SERVICE", "trend-trawler-web");
    vi.stubEnv("GCS_ALLOWED_BUCKETS", "tt-bucket");
    upstream.contentType = "image/jpeg";
    const res = await call("tt-bucket", `person-refs/${ALICE_SLUG}/me.jpg`);
    expect(res.status).toBe(200);
    expect(res.headers.get("cache-control")).toBe("private, no-store");
  });
});

describe("/api/gcs response headers", () => {
  it("serves HTML sandboxed, nosniff, with the link rewrite", async () => {
    upstream = {
      body: '<img src="https://storage.mtls.cloud.google.com/tt-bucket/2026/out/a.png?authuser=3">',
      contentType: "text/html; charset=utf-8",
    };
    const res = await call("tt-bucket", "2026/out/gallery.html");
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("text/html; charset=utf-8");
    expect(res.headers.get("x-content-type-options")).toBe("nosniff");
    const csp = res.headers.get("content-security-policy") ?? "";
    expect(csp).toMatch(/(^|;\s*)sandbox(;|$)/);
    expect(csp).toContain("default-src 'none'");
    expect(csp).toContain("img-src 'self' data:");
    expect(await res.text()).toContain(
      'src="/api/gcs?bucket=tt-bucket&path=2026%2Fout%2Fa.png"'
    );
  });

  it("sandboxes an object served as text/html even when the path is not .html", async () => {
    upstream = { body: "<script>alert(1)</script>", contentType: "text/html" };
    const res = await call("tt-bucket", "2026/out/x.png");
    expect(res.headers.get("content-type")).toBe("text/html");
    expect(res.headers.get("content-security-policy")).toContain("sandbox");
  });

  it("sandboxes other script-capable documents (SVG) too", async () => {
    upstream.contentType = "image/svg+xml";
    const res = await call("tt-bucket", "2026/out/logo.svg");
    expect(res.headers.get("content-security-policy")).toContain("sandbox");
    upstream.contentType = "image/svg+xml";
    expect((await call("tt-bucket", `person-refs/${ALICE_SLUG}/x.svg`)).status).toBe(404);
  });

  it("never serves HTML from a person path (by path or content type)", async () => {
    upstream = { body: "<p>x</p>", contentType: "text/html" };
    expect((await call("tt-bucket", `person-refs/${ALICE_SLUG}/x.html`)).status).toBe(404);
    expect((await call("tt-bucket", `person-refs/${ALICE_SLUG}/x.jpg`)).status).toBe(404);
  });

  it("passes the upstream content type through with nosniff", async () => {
    upstream.contentType = "image/webp";
    const res = await call("tt-bucket", "2026/out/hero.html.png");
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("image/webp");
    expect(res.headers.get("x-content-type-options")).toBe("nosniff");
    expect(res.headers.get("content-security-policy")).toBeNull();
  });

  it("does not take the content type from the path", async () => {
    upstream.contentType = "image/png";
    const res = await call("tt-bucket", "2026/out/evil.html");
    expect(res.headers.get("content-type")).toBe("image/png");
  });

  it("falls back to application/pdf for .pdf and octet-stream otherwise", async () => {
    upstream.contentType = null;
    expect((await call("tt-bucket", "2026/out/r.pdf")).headers.get("content-type")).toBe(
      "application/pdf"
    );
    expect((await call("tt-bucket", "2026/out/blob")).headers.get("content-type")).toBe(
      "application/octet-stream"
    );
  });

  it("fetches GCS with a timeout signal", async () => {
    await call("tt-bucket", "2026/out/hero.png");
    const init = (fetchMock.mock.calls[0] as unknown[])[1] as RequestInit | undefined;
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });

  it("maps a GCS timeout to 504 with a generic message", async () => {
    fetchMock.mockRejectedValueOnce(Object.assign(new Error("The operation was aborted due to timeout"), { name: "TimeoutError" }));
    const res = await call("tt-bucket", "2026/out/hero.png");
    expect(res.status).toBe(504);
    expect(await res.json()).toEqual({ error: "Storage timed out" });
  });

  it("never returns the raw error message on a storage failure", async () => {
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    fetchMock.mockRejectedValueOnce(new Error("connect ECONNREFUSED 10.0.0.1:443 secret-detail"));
    const res = await call("tt-bucket", "2026/out/hero.png");
    expect(res.status).toBe(500);
    expect(await res.json()).toEqual({ error: "Storage error" });
    expect(log).toHaveBeenCalled();
    log.mockRestore();
  });
});
