import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";

const execFileMock = vi.hoisted(() => vi.fn());
vi.mock("node:child_process", () => ({ execFile: execFileMock }));

import {
  getAccessToken,
  getIdentityToken,
  resetTokenCacheForTests,
} from "@/lib/gcp-auth";

const T0 = new Date("2026-10-12T12:00:00Z").getTime();

/** An unsigned JWT whose payload carries `exp` (seconds). */
function jwt(expSeconds: number, tag = "x"): string {
  const b64url = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64url({ alg: "RS256" })}.${b64url({ exp: expSeconds, tag })}.sig`;
}

const accessResponse = (token: string, expiresIn = 3599) =>
  new Response(JSON.stringify({ access_token: token, expires_in: expiresIn }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

/** Make the gcloud fallback answer `stdout`. */
function gcloudAnswers(stdout: string) {
  execFileMock.mockImplementation(
    (_cmd: string, _args: string[], _opts: object, cb: (e: Error | null, out: string) => void) =>
      cb(null, `${stdout}\n`),
  );
}

beforeEach(() => {
  resetTokenCacheForTests();
  execFileMock.mockReset();
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  vi.stubEnv("K_SERVICE", "");
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("getAccessToken", () => {
  it("reads access_token from the metadata server with a timeout", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => accessResponse("ya29.test")));
    expect(await getAccessToken()).toBe("ya29.test");
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/service-accounts/default/token"),
      expect.objectContaining({
        headers: { "Metadata-Flavor": "Google" },
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("reuses the cached token until expires_in - 60 s, then refreshes", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(accessResponse("ya29.one", 3600))
      .mockResolvedValueOnce(accessResponse("ya29.two", 3600));
    vi.stubGlobal("fetch", fetchMock);

    expect(await getAccessToken()).toBe("ya29.one");
    vi.setSystemTime(T0 + (3600 - 61) * 1000);
    expect(await getAccessToken()).toBe("ya29.one");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    vi.setSystemTime(T0 + (3600 - 59) * 1000);
    expect(await getAccessToken()).toBe("ya29.two");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("falls back to gcloud (async execFile) locally", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("no metadata"); }));
    gcloudAnswers("ya29.local");
    expect(await getAccessToken()).toBe("ya29.local");
    expect(execFileMock).toHaveBeenCalledWith(
      "gcloud",
      ["auth", "application-default", "print-access-token"],
      expect.any(Object),
      expect.any(Function),
    );
  });

  it("never runs gcloud on Cloud Run: throws a clear error instead", async () => {
    vi.stubEnv("K_SERVICE", "trend-trawler-web");
    vi.stubGlobal("fetch", vi.fn(async () => new Response("nope", { status: 500 })));
    await expect(getAccessToken()).rejects.toThrow(/metadata server/i);
    expect(execFileMock).not.toHaveBeenCalled();
  });
});

describe("getIdentityToken", () => {
  it("requests a format=full ID token for the given audience", async () => {
    const token = jwt(T0 / 1000 + 3600);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(token, { status: 200 })));
    expect(await getIdentityToken("https://api.example.run.app")).toBe(token);
    // The backend trusts X-TT-User only from the proxy SA, identified by the token's email.
    expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/audience=https%3A%2F%2Fapi\.example\.run\.app.*format=full/),
      expect.objectContaining({
        headers: { "Metadata-Flavor": "Google" },
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("caches per audience until the JWT's exp - 60 s", async () => {
    const exp = T0 / 1000 + 3600;
    const fetchMock = vi.fn(async (url: string) => {
      const aud = new URL(url).searchParams.get("audience");
      return new Response(jwt(exp, `${aud}-${fetchMock.mock.calls.length}`), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const a1 = await getIdentityToken("https://a.run.app");
    const b1 = await getIdentityToken("https://b.run.app");
    expect(a1).not.toBe(b1);
    expect(await getIdentityToken("https://a.run.app")).toBe(a1);
    expect(await getIdentityToken("https://b.run.app")).toBe(b1);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    vi.setSystemTime(T0 + (3600 - 59) * 1000);
    const a2 = await getIdentityToken("https://a.run.app");
    expect(a2).not.toBe(a1);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("does not cache a token without a readable exp", async () => {
    const fetchMock = vi.fn(async () => new Response("not-a-jwt", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await getIdentityToken("https://a.run.app");
    await getIdentityToken("https://a.run.app");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("falls back to gcloud locally and caches its token", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("no metadata"); }));
    const token = jwt(T0 / 1000 + 3600);
    gcloudAnswers(token);
    expect(await getIdentityToken("https://a.run.app")).toBe(token);
    expect(await getIdentityToken("https://a.run.app")).toBe(token);
    expect(execFileMock).toHaveBeenCalledTimes(1);
    expect(execFileMock.mock.calls[0][1]).toEqual(["auth", "print-identity-token"]);
  });

  it("never runs gcloud on Cloud Run", async () => {
    vi.stubEnv("K_SERVICE", "trend-trawler-web");
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("timeout"); }));
    await expect(getIdentityToken("https://a.run.app")).rejects.toThrow(/metadata server/i);
    expect(execFileMock).not.toHaveBeenCalled();
  });
});
