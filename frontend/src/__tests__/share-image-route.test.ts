import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SHARE_TOKEN, slateSnapshot } from "./fixtures/share-snapshot";

vi.mock("@/lib/gcp-auth", () => ({ getAccessToken: vi.fn(async () => "ya29.token") }));

import { GET } from "@/app/s/[token]/img/[n]/route";

const call = (token: string, n: string) =>
  GET(new NextRequest(`https://share.example/s/${token}/img/${n}`), {
    params: Promise.resolve({ token, n }),
  });

describe("share image route", () => {
  const fetchMock = vi.fn(async (url: string | URL | Request) =>
    String(url).includes("snapshot.json")
      ? new Response(JSON.stringify(slateSnapshot()), { status: 200 })
      : new Response(new Uint8Array([1, 2, 3]), { status: 200 })
  );

  beforeEach(() => {
    vi.stubEnv("GOOGLE_CLOUD_STORAGE_BUCKET", "tt-bucket");
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    fetchMock.mockClear();
  });

  it("streams a valid image as image/png from the env bucket", async () => {
    const res = await call(SHARE_TOKEN, "0");
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("image/png");
    expect(String(fetchMock.mock.calls[1][0])).toBe(
      `https://storage.googleapis.com/storage/v1/b/tt-bucket/o/shares%2F${SHARE_TOKEN}%2F0.png?alt=media`
    );
  });

  it.each([
    [SHARE_TOKEN, "a"],
    [SHARE_TOKEN, "1e1"],
    [SHARE_TOKEN, "00"],
    [SHARE_TOKEN, " 1"],
    ["short", "0"],
    ["..%2F..%2Fsessions", "0"],
  ])("refuses token=%s n=%s without fetching", async (token, n) => {
    const res = await call(token, n);
    expect(res.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("answers 502 (not a stack trace) when storage is unreachable", async () => {
    fetchMock.mockImplementationOnce(async () => {
      throw new Error("network down");
    });
    const res = await call(SHARE_TOKEN, "0");
    expect(res.status).toBe(502);
  });
});
