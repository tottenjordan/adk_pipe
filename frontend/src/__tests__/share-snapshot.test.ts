import { afterEach, describe, expect, it, vi } from "vitest";
import {
  isSnapshotV1,
  loadShareImage,
  loadSnapshot,
  shareBucket,
  shareObjectUrl,
  TOKEN_RE,
} from "@/lib/share-snapshot";
import { SHARE_TOKEN, singleEvalSnapshot, slateSnapshot } from "./fixtures/share-snapshot";

const BUCKET = "tt-bucket";

function deps(fetchImpl: (url: string) => Response | Promise<Response>) {
  const fetchMock = vi.fn(async (url: string | URL | Request) => fetchImpl(String(url)));
  return {
    fetchMock,
    deps: {
      fetch: fetchMock as unknown as typeof fetch,
      getToken: vi.fn(async () => "ya29.token"),
      bucket: BUCKET,
    },
  };
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

describe("TOKEN_RE", () => {
  it("accepts URL-safe tokens of 16–64 chars only", () => {
    expect(TOKEN_RE.test(SHARE_TOKEN)).toBe(true);
    expect(TOKEN_RE.test("a".repeat(16))).toBe(true);
    expect(TOKEN_RE.test("a".repeat(64))).toBe(true);
    for (const bad of ["a".repeat(15), "a".repeat(65), "abc/defghijklmnopq", "abc%2Fdefghijklmnop", "../../../../etc/passwd", "abcdefghijklmnop\n", "abcdefghijklmno.p"]) {
      expect(TOKEN_RE.test(bad)).toBe(false);
    }
  });
});

describe("shareBucket", () => {
  it("reads GOOGLE_CLOUD_STORAGE_BUCKET and validates the name", () => {
    expect(shareBucket({ GOOGLE_CLOUD_STORAGE_BUCKET: "tt-bucket" })).toBe("tt-bucket");
    expect(shareBucket({ GOOGLE_CLOUD_STORAGE_BUCKET: "gs://tt-bucket" })).toBe("tt-bucket");
    expect(() => shareBucket({})).toThrow();
    expect(() => shareBucket({ GOOGLE_CLOUD_STORAGE_BUCKET: "a/b" })).toThrow();
    expect(() => shareBucket({ GOOGLE_CLOUD_STORAGE_BUCKET: "Bad Bucket" })).toThrow();
  });
});

describe("shareObjectUrl", () => {
  it("always addresses shares/<token>/<file> as one encoded object name", () => {
    expect(shareObjectUrl(BUCKET, SHARE_TOKEN, "snapshot.json")).toBe(
      `https://storage.googleapis.com/storage/v1/b/${BUCKET}/o/shares%2F${SHARE_TOKEN}%2Fsnapshot.json?alt=media`
    );
    expect(shareObjectUrl(BUCKET, SHARE_TOKEN, 2)).toBe(
      `https://storage.googleapis.com/storage/v1/b/${BUCKET}/o/shares%2F${SHARE_TOKEN}%2F2.png?alt=media`
    );
  });

  it("refuses a bad token or file", () => {
    expect(() => shareObjectUrl(BUCKET, "../x", "snapshot.json")).toThrow();
    expect(() => shareObjectUrl(BUCKET, SHARE_TOKEN, -1)).toThrow();
    expect(() => shareObjectUrl(BUCKET, SHARE_TOKEN, 1.5)).toThrow();
  });
});

describe("isSnapshotV1", () => {
  it("accepts the slate and single fixtures", () => {
    expect(isSnapshotV1(slateSnapshot())).toBe(true);
    expect(isSnapshotV1(singleEvalSnapshot())).toBe(true);
  });

  it("accepts a null score", () => {
    const s = singleEvalSnapshot();
    s.creatives[0].eval!.score = null;
    expect(isSnapshotV1(s)).toBe(true);
  });

  const bad: [string, (s: Record<string, unknown>) => unknown][] = [
    ["not an object", () => "nope"],
    ["null", () => null],
    ["wrong version", (s) => ({ ...s, version: 2 })],
    ["unknown scope", (s) => ({ ...s, scope: "all" })],
    ["missing brand", (s) => ({ ...s, brand: undefined })],
    ["non-string trend", (s) => ({ ...s, trend: 3 })],
    ["over-long brand", (s) => ({ ...s, brand: "x".repeat(301) })],
    ["include_eval not boolean", (s) => ({ ...s, include_eval: "true" })],
    ["no creatives", (s) => ({ ...s, creatives: [] })],
    ["creatives not an array", (s) => ({ ...s, creatives: {} })],
    ["too many creatives", (s) => ({ ...s, creatives: [...slateSnapshot().creatives, { ...slateSnapshot().creatives[0], index: 4, image: "4.png" }] })],
  ];
  it.each(bad)("rejects %s", (_name, mutate) => {
    expect(isSnapshotV1(mutate(slateSnapshot() as unknown as Record<string, unknown>))).toBe(false);
  });

  const badCreative: [string, (c: Record<string, unknown>) => unknown][] = [
    ["a non-integer index", (c) => ({ ...c, index: 0.5 })],
    ["an index out of position", (c) => ({ ...c, index: 2 })],
    ["a string index", (c) => ({ ...c, index: "0" })],
    ["an over-long body", (c) => ({ ...c, body: "x".repeat(4001) })],
    ["a missing headline", (c) => ({ ...c, headline: undefined })],
    ["a bad aspect ratio", (c) => ({ ...c, aspect_ratio: "1:1; background:url(x)" })],
    ["an object caption", (c) => ({ ...c, caption: { a: 1 } })],
    ["a malformed eval", (c) => ({ ...c, eval: { passed: "yes", score: 1, checks: [] } })],
    ["eval checks not an array", (c) => ({ ...c, eval: { passed: true, score: 1, checks: "x" } })],
    ["an out-of-range score", (c) => ({ ...c, eval: { passed: true, score: 7, checks: [] } })],
    ["a malformed check", (c) => ({ ...c, eval: { passed: true, score: 1, checks: [{ gate: "g", label: 3, passed: true, advisory: false }] } })],
  ];
  it.each(badCreative)("rejects a creative with %s", (_name, mutate) => {
    const s = slateSnapshot() as unknown as { creatives: Record<string, unknown>[] };
    s.creatives[0] = mutate(s.creatives[0]) as Record<string, unknown>;
    expect(isSnapshotV1(s)).toBe(false);
  });

  it("rejects eval on a snapshot that did not opt in", () => {
    const s = singleEvalSnapshot();
    expect(isSnapshotV1({ ...s, include_eval: false })).toBe(false);
  });
});

describe("loadSnapshot", () => {
  afterEach(() => vi.restoreAllMocks());

  it("never fetches for a bad token", async () => {
    const { deps: d, fetchMock } = deps(() => json(slateSnapshot()));
    for (const t of ["", "short", "../../etc/passwd", "a/b/c/d/e/f/g/h/i", `${SHARE_TOKEN}/../x`]) {
      expect(await loadSnapshot(t, d)).toBeNull();
    }
    expect(fetchMock).not.toHaveBeenCalled();
    expect(d.getToken).not.toHaveBeenCalled();
  });

  it("fetches only shares/<token>/snapshot.json with a bearer token", async () => {
    const { deps: d, fetchMock } = deps(() => json(slateSnapshot()));
    const snap = await loadSnapshot(SHARE_TOKEN, d);
    expect(snap?.brand).toBe("PRS Guitars");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(
      `https://storage.googleapis.com/storage/v1/b/${BUCKET}/o/shares%2F${SHARE_TOKEN}%2Fsnapshot.json?alt=media`
    );
    expect(new Headers(init.headers).get("authorization")).toBe("Bearer ya29.token");
    expect(init.cache).toBe("no-store");
  });

  it("is null when the snapshot is gone (revoked or never existed)", async () => {
    const { deps: d } = deps(() => new Response("nope", { status: 404 }));
    expect(await loadSnapshot(SHARE_TOKEN, d)).toBeNull();
  });

  it("throws on other storage errors instead of claiming the link is gone", async () => {
    const { deps: d } = deps(() => new Response("boom", { status: 503 }));
    await expect(loadSnapshot(SHARE_TOKEN, d)).rejects.toThrow();
  });

  it("rejects malformed JSON, invalid shapes and a token mismatch", async () => {
    for (const body of [
      new Response("{not json", { status: 200 }),
      json({ version: 1 }),
      json(slateSnapshot({ token: "OtherToken_123456789" })),
    ]) {
      const { deps: d } = deps(() => body);
      expect(await loadSnapshot(SHARE_TOKEN, d)).toBeNull();
    }
  });

  it("rejects an oversized snapshot", async () => {
    const { deps: d } = deps(() => new Response("x".repeat(600_000), { status: 200 }));
    expect(await loadSnapshot(SHARE_TOKEN, d)).toBeNull();
  });
});

describe("loadShareImage", () => {
  const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47]);

  function imageDeps() {
    return deps((url) =>
      url.includes("snapshot.json")
        ? json(slateSnapshot())
        : new Response(png, { status: 200, headers: { "content-type": "text/html" } })
    );
  }

  it("streams shares/<token>/<n>.png as image/png", async () => {
    const { deps: d, fetchMock } = imageDeps();
    const res = await loadShareImage(SHARE_TOKEN, "2", d);
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("image/png");
    expect(res.headers.get("x-content-type-options")).toBe("nosniff");
    expect(res.headers.get("cache-control")).toBe("private, max-age=60");
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(png);
    const urls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(urls).toEqual([
      `https://storage.googleapis.com/storage/v1/b/${BUCKET}/o/shares%2F${SHARE_TOKEN}%2Fsnapshot.json?alt=media`,
      `https://storage.googleapis.com/storage/v1/b/${BUCKET}/o/shares%2F${SHARE_TOKEN}%2F2.png?alt=media`,
    ]);
  });

  it.each([
    ["a non-numeric n", SHARE_TOKEN, "x"],
    ["a negative n", SHARE_TOKEN, "-1"],
    ["an n with a suffix", SHARE_TOKEN, "0.png"],
    ["an encoded traversal n", SHARE_TOKEN, "0%2F..%2F..%2Fsecret"],
    ["a too-long n", SHARE_TOKEN, "123"],
    ["a bad token", "../../private", "0"],
  ])("404s %s without touching storage", async (_name, token, n) => {
    const { deps: d, fetchMock } = imageDeps();
    const res = await loadShareImage(token, n, d);
    expect(res.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("404s an index past the snapshot's creatives", async () => {
    const { deps: d, fetchMock } = imageDeps();
    const res = await loadShareImage(SHARE_TOKEN, "4", d);
    expect(res.status).toBe(404);
    expect(fetchMock).toHaveBeenCalledTimes(1); // the snapshot only
  });

  it("404s when the snapshot or the image is gone", async () => {
    const gone = deps(() => new Response("", { status: 404 }));
    expect((await loadShareImage(SHARE_TOKEN, "0", gone.deps)).status).toBe(404);
    const imgGone = deps((url) =>
      url.includes("snapshot.json") ? json(slateSnapshot()) : new Response("", { status: 404 })
    );
    expect((await loadShareImage(SHARE_TOKEN, "0", imgGone.deps)).status).toBe(404);
  });
});
