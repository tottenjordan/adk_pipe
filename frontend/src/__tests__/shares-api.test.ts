import { afterEach, describe, expect, it, vi } from "vitest";
import { createShare, listShares, revokeShare } from "@/lib/api";
import { hasRenderedImage, isAbsoluteUrl, ShareError, shareErrorMessage, sharesForSession, type Share } from "@/lib/shares";

const share = (over: Partial<Share> = {}): Share => ({
  token: "AbCdEfGhIjKlMnOp", url: "/s/AbCdEfGhIjKlMnOp", title: "Acme x Trend", scope: "slate",
  include_eval: false, concept_names: [], app_name: "creative_agent", session_id: "s1",
  created_at: "2026-10-09T10:00:00Z", ...over,
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => vi.unstubAllGlobals());

describe("createShare", () => {
  it("POSTs the payload to shares/me/{app}/{session}", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => json(share()));
    vi.stubGlobal("fetch", fetchMock);
    const out = await createShare("creative_agent", "s 1", { include_eval: true });
    expect(out.token).toBe("AbCdEfGhIjKlMnOp");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/adk/shares/me/creative_agent/s%201");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({ include_eval: true });
  });
  it("throws a ShareError carrying the backend reason and a friendly message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      json({ detail: { reason: "too_many_shares", message: "at most 200" } }, 429)));
    const err = await createShare("creative_agent", "s1", {}).catch((e) => e);
    expect(err).toBeInstanceOf(ShareError);
    expect(err.reason).toBe("too_many_shares");
    expect(err.status).toBe(429);
    expect(err.message).toMatch(/too many active links/i);
  });
  it("tolerates a non-JSON error body", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("Not found", { status: 404 })));
    const err = await createShare("creative_agent", "s1", {}).catch((e) => e);
    expect(err.reason).toBeNull();
    expect(err.status).toBe(404);
  });
});

describe("listShares", () => {
  it("GETs shares/me and returns the list", async () => {
    const fetchMock = vi.fn(async (_url: string) => json({ shares: [share()] }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await listShares()).toHaveLength(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/adk/shares/me");
  });
  it("returns [] on a malformed body", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({})));
    expect(await listShares()).toEqual([]);
  });
});

describe("revokeShare", () => {
  it("DELETEs shares/me/{token} and resolves on 204", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(revokeShare("AbCdEfGhIjKlMnOp")).resolves.toBeUndefined();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/adk/shares/me/AbCdEfGhIjKlMnOp");
    expect(fetchMock.mock.calls[0][1]?.method).toBe("DELETE");
  });
  it("throws share_not_found on 404", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      json({ detail: { reason: "share_not_found", message: "share not found" } }, 404)));
    await expect(revokeShare("AbCdEfGhIjKlMnOp")).rejects.toMatchObject({ reason: "share_not_found" });
  });
});

describe("share helpers", () => {
  it("maps every backend reason to a friendly sentence", () => {
    for (const reason of [
      "invalid_app_name", "invalid_concept_names", "invalid_include_eval", "unknown_concept", "no_images",
      "image_outside_bucket", "session_not_found", "share_not_found", "too_many_shares", "share_failed",
      "store_failed", "revoke_incomplete", "shares_unconfigured",
    ]) {
      const msg = shareErrorMessage(reason, 400);
      expect(msg).not.toMatch(/_/);
      expect(msg).not.toMatch(/Something went wrong/);
    }
    expect(shareErrorMessage("whatever", 500)).toMatch(/500/);
  });
  it("detects absolute urls", () => {
    expect(isAbsoluteUrl("https://share.example.com/s/x")).toBe(true);
    expect(isAbsoluteUrl("/s/x")).toBe(false);
  });
  it("detects a rendered image per concept", () => {
    const gi = { A: { gcs_uri: "gs://b/a.png" }, B: { gcs_uri: "" }, C: {} };
    expect(hasRenderedImage(gi, "A")).toBe(true);
    expect(hasRenderedImage(gi, "B")).toBe(false);
    expect(hasRenderedImage(gi, "C")).toBe(false);
    expect(hasRenderedImage(gi, "D")).toBe(false);
    expect(hasRenderedImage(undefined, "A")).toBe(false);
  });
  it("filters shares by session", () => {
    expect(sharesForSession([share(), share({ token: "b".repeat(16), session_id: "s2" })], "s2"))
      .toEqual([share({ token: "b".repeat(16), session_id: "s2" })]);
  });
});
