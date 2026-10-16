import { afterEach, describe, expect, it, vi } from "vitest";
import { listPersonRefs, listSessions, listShares } from "@/lib/api";
import { listExperiments, getExperiment } from "@/lib/experiments";
import {
  apiFetch,
  isSessionExpired,
  SESSION_EXPIRED_MESSAGE,
  SessionExpiredError,
} from "@/lib/session-expired";

afterEach(() => vi.unstubAllGlobals());

/** What fetch resolves to when IAP answers an expired session with a 302 to Google
 *  sign-in and the request used `redirect: "manual"`: an opaque redirect, status 0. */
function opaqueRedirect(): Response {
  const res = new Response(null, { status: 200 });
  Object.defineProperty(res, "type", { value: "opaqueredirect" });
  Object.defineProperty(res, "status", { value: 0 });
  Object.defineProperty(res, "ok", { value: false });
  return res;
}

const LOADERS: [string, () => Promise<unknown>][] = [
  ["listSessions", () => listSessions("creative_agent", "me")],
  ["listPersonRefs", () => listPersonRefs()],
  ["listShares", () => listShares()],
  ["listExperiments", () => listExperiments()],
  ["getExperiment", () => getExperiment("exp-1")],
];

describe("session expiry in the API layer", () => {
  it("sends every request with redirect: manual, so an IAP sign-in redirect is visible", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => new Response("[]"));
    vi.stubGlobal("fetch", fetchMock);
    await apiFetch("/api/adk/x", { method: "POST" });
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "POST", redirect: "manual" });
  });

  it.each(LOADERS)("%s maps a 401 to the session-expired error", async (_name, load) => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("no", { status: 401 })));
    const err = await load().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(SessionExpiredError);
    expect(isSessionExpired(err)).toBe(true);
    expect((err as Error).message).toBe(SESSION_EXPIRED_MESSAGE);
  });

  it.each(LOADERS)("%s maps an IAP redirect to the session-expired error", async (_name, load) => {
    vi.stubGlobal("fetch", vi.fn(async () => opaqueRedirect()));
    const err = await load().catch((e: unknown) => e);
    expect(isSessionExpired(err)).toBe(true);
  });

  it("leaves a 403 (signed in, not allowed) and a 500 as ordinary errors", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("no", { status: 403 })));
    expect(isSessionExpired(await listShares().catch((e: unknown) => e))).toBe(false);
    vi.stubGlobal("fetch", vi.fn(async () => new Response("no", { status: 500 })));
    expect(isSessionExpired(await listSessions("a", "me").catch((e: unknown) => e))).toBe(false);
  });

  it("says how to recover", () => {
    expect(SESSION_EXPIRED_MESSAGE).toBe("Your sign-in expired. Reload to continue.");
  });
});
