import { afterEach, describe, expect, it, vi } from "vitest";
import { buildShareMetadata, MISSING_SHARE_METADATA, shareBaseUrl } from "@/lib/share-metadata";
import { SHARE_TOKEN, singleEvalSnapshot, slateSnapshot } from "./fixtures/share-snapshot";

vi.mock("@/lib/gcp-auth", () => ({ getAccessToken: vi.fn(async () => "ya29.token") }));

const BASE = "https://trend-trawler-share-abc-uc.a.run.app";

describe("shareBaseUrl", () => {
  it("accepts an http(s) origin and drops trailing slashes", () => {
    expect(shareBaseUrl({ SHARE_BASE_URL: `${BASE}/` })).toBe(BASE);
    expect(shareBaseUrl({ SHARE_BASE_URL: BASE })).toBe(BASE);
    expect(shareBaseUrl({})).toBeNull();
    expect(shareBaseUrl({ SHARE_BASE_URL: "javascript:alert(1)" })).toBeNull();
    expect(shareBaseUrl({ SHARE_BASE_URL: "not a url" })).toBeNull();
  });
});

describe("buildShareMetadata", () => {
  it("titles a slate by brand × trend with the creative count", () => {
    const md = buildShareMetadata(slateSnapshot(), BASE);
    expect(md.title).toBe("PRS Guitars × Powerball jackpot");
    expect(md.openGraph?.title).toBe("PRS Guitars × Powerball jackpot — 4 creatives");
    expect(md.twitter?.title).toBe("PRS Guitars × Powerball jackpot — 4 creatives");
  });

  it("titles a single creative by its headline", () => {
    const md = buildShareMetadata(singleEvalSnapshot(), BASE);
    expect(md.openGraph?.title).toBe("Headline 0: play it loud");
  });

  it("describes with the first caption trimmed to 160 characters", () => {
    const snap = slateSnapshot();
    snap.creatives[0].caption = "x ".repeat(200);
    const md = buildShareMetadata(snap, BASE);
    const desc = md.openGraph?.description as string;
    expect(desc.length).toBeLessThanOrEqual(160);
    expect(desc.endsWith("…")).toBe(true);
    expect(md.description).toBe(desc);
    const short = buildShareMetadata(slateSnapshot(), BASE);
    expect(short.description).toBe(slateSnapshot().creatives[0].caption);
  });

  it("points og:image and twitter at the absolute first image", () => {
    const md = buildShareMetadata(slateSnapshot(), BASE);
    const og = md.openGraph as { images: { url: string; alt: string }[]; type: string };
    expect(og.images[0].url).toBe(`${BASE}/s/${SHARE_TOKEN}/img/0`);
    expect(og.images[0].alt).toBe(slateSnapshot().creatives[0].alt);
    expect(md.twitter).toMatchObject({
      card: "summary_large_image",
      images: [`${BASE}/s/${SHARE_TOKEN}/img/0`],
    });
    expect(md.metadataBase?.toString()).toBe(`${BASE}/`);
  });

  it("omits the image (never a localhost URL) without a base URL", () => {
    const md = buildShareMetadata(slateSnapshot(), null);
    expect(md.openGraph?.images).toBeUndefined();
    expect(md.twitter).toMatchObject({ card: "summary_large_image" });
    expect((md.twitter as { images?: unknown }).images).toBeUndefined();
  });

  it("is never indexed", () => {
    expect(buildShareMetadata(slateSnapshot(), BASE).robots).toEqual({
      index: false,
      follow: false,
    });
    expect(MISSING_SHARE_METADATA.robots).toEqual({ index: false, follow: false });
  });
});

describe("generateMetadata (page)", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("builds metadata from the stored snapshot", async () => {
    vi.stubEnv("GOOGLE_CLOUD_STORAGE_BUCKET", "tt-bucket");
    vi.stubEnv("SHARE_BASE_URL", BASE);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify(slateSnapshot()), { status: 200 }))
    );
    const { generateMetadata } = await import("@/app/s/[token]/page");
    const md = await generateMetadata({
      params: Promise.resolve({ token: SHARE_TOKEN }),
      searchParams: Promise.resolve({}),
    } as unknown as Parameters<typeof generateMetadata>[0]);
    expect(md.openGraph?.title).toBe("PRS Guitars × Powerball jackpot — 4 creatives");
    expect(md.robots).toEqual({ index: false, follow: false });
  });

  it("falls back to a noindex 'unavailable' title for a missing link", async () => {
    vi.stubEnv("GOOGLE_CLOUD_STORAGE_BUCKET", "tt-bucket");
    vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 404 })));
    const { generateMetadata } = await import("@/app/s/[token]/page");
    const md = await generateMetadata({
      params: Promise.resolve({ token: "MissingToken_1234567" }),
      searchParams: Promise.resolve({}),
    } as unknown as Parameters<typeof generateMetadata>[0]);
    expect(md).toEqual(MISSING_SHARE_METADATA);
  });
});
