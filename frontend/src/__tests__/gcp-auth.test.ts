import { describe, it, expect, vi, afterEach } from "vitest";
import { getAccessToken, getIdentityToken } from "@/lib/gcp-auth";

afterEach(() => vi.restoreAllMocks());

describe("gcp-auth", () => {
  it("getAccessToken reads access_token from the metadata server", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ access_token: "ya29.test" }),
      { status: 200, headers: { "content-type": "application/json" } },
    )));
    expect(await getAccessToken()).toBe("ya29.test");
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/service-accounts/default/token"),
      expect.objectContaining({ headers: { "Metadata-Flavor": "Google" } }),
    );
  });

  it("getIdentityToken requests an ID token for the given audience (plain-text JWT)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("eyJ.jwt.token", { status: 200 })));
    const token = await getIdentityToken("https://api.example.run.app");
    expect(token).toBe("eyJ.jwt.token");
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("audience=https%3A%2F%2Fapi.example.run.app"),
      expect.objectContaining({ headers: { "Metadata-Flavor": "Google" } }),
    );
  });
});
