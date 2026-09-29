// @vitest-environment node
// jose's key/Uint8Array checks break under jsdom's realm, so this suite runs in node.
import { describe, it, expect, vi, afterEach } from "vitest";
import { generateKeyPair, SignJWT, exportJWK, createLocalJWKSet } from "jose";
import { normalizeUserId, verifyIapJwt, resolveUser, IAP_ISSUER } from "@/lib/iap-identity";

const AUD = "/projects/PROJECT_NUMBER/locations/us-central1/services/trend-trawler-web";
const HD = "x.com";

afterEach(() => vi.restoreAllMocks());

async function setup() {
  const { publicKey, privateKey } = await generateKeyPair("ES256");
  const keys = createLocalJWKSet({ keys: [{ ...(await exportJWK(publicKey)), kid: "k1", alg: "ES256" }] });
  const sign = (claims: Record<string, unknown>, aud = AUD, iss = IAP_ISSUER) =>
    new SignJWT(claims).setProtectedHeader({ alg: "ES256", kid: "k1" }).setIssuer(iss)
      .setAudience(aud).setIssuedAt().setExpirationTime("5m").sign(privateKey);
  return { keys, sign };
}

describe("normalizeUserId", () => {
  it("strips the IAP prefix and lowercases", () => {
    expect(normalizeUserId("accounts.google.com:Alice@X.com")).toBe("alice@x.com");
    expect(normalizeUserId("  Alice@X.com \n")).toBe("alice@x.com");
    expect(() => normalizeUserId("user_1727600000000")).toThrow();
    expect(() => normalizeUserId("me")).toThrow();
  });
});

describe("verifyIapJwt", () => {
  it("returns the normalized email for a valid assertion", async () => {
    const { keys, sign } = await setup();
    expect(await verifyIapJwt(await sign({ email: "Alice@X.com" }), { audience: AUD, keys })).toBe("alice@x.com");
  });
  it("rejects wrong audience or issuer", async () => {
    const { keys, sign } = await setup();
    await expect(verifyIapJwt(await sign({ email: "a@x.com" }, "/projects/1/global/backendServices/2"), { audience: AUD, keys })).rejects.toThrow();
    await expect(verifyIapJwt(await sign({ email: "a@x.com" }, AUD, "https://evil"), { audience: AUD, keys })).rejects.toThrow();
  });
  it("rejects a token signed by a different key", async () => {
    const { keys } = await setup();
    const other = await setup();
    await expect(verifyIapJwt(await other.sign({ email: "a@x.com" }), { audience: AUD, keys })).rejects.toThrow();
  });
  it("rejects an assertion without an email", async () => {
    const { keys, sign } = await setup();
    await expect(verifyIapJwt(await sign({ sub: "123" }), { audience: AUD, keys })).rejects.toThrow(/email/);
  });
  it("accepts a matching hosted domain", async () => {
    const { keys, sign } = await setup();
    expect(await verifyIapJwt(await sign({ email: "a@x.com", hd: HD }), { audience: AUD, keys, allowedHd: HD })).toBe("a@x.com");
  });
  it("rejects a wrong or missing hosted domain when one is required", async () => {
    const { keys, sign } = await setup();
    await expect(verifyIapJwt(await sign({ email: "a@evil.com", hd: "evil.com" }), { audience: AUD, keys, allowedHd: HD })).rejects.toThrow(/hd/);
    await expect(verifyIapJwt(await sign({ email: "a@gmail.com" }), { audience: AUD, keys, allowedHd: HD })).rejects.toThrow(/hd/);
  });
  it("does not require hd when no allowedHd is given", async () => {
    const { keys, sign } = await setup();
    expect(await verifyIapJwt(await sign({ email: "a@gmail.com" }), { audience: AUD, keys })).toBe("a@gmail.com");
  });
});

describe("resolveUser", () => {
  const ok = async () => "alice@x.com";
  const jwt = () => new Headers({ "x-goog-iap-jwt-assertion": "jwt" });
  it("rejects a missing assertion on Cloud Run", async () => {
    expect(await resolveUser(new Headers(), { onCloudRun: true, allowedHd: HD, verify: ok })).toEqual({ kind: "reject", status: 401 });
  });
  it("passes through locally with no assertion", async () => {
    expect(await resolveUser(new Headers(), { onCloudRun: false, verify: ok })).toEqual({ kind: "local" });
  });
  it("ignores the spoofable email header, uses the JWT", async () => {
    const h = new Headers({ "x-goog-iap-jwt-assertion": "jwt", "x-goog-authenticated-user-email": "accounts.google.com:bob@x.com" });
    expect(await resolveUser(h, { onCloudRun: true, allowedHd: HD, verify: ok })).toEqual({ kind: "user", userId: "alice@x.com" });
  });
  it("rejects an invalid assertion and logs the reason, never the token", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const bad = async () => { throw new Error("bad sig"); };
    expect(await resolveUser(jwt(), { onCloudRun: true, allowedHd: HD, verify: bad })).toEqual({ kind: "reject", status: 401 });
    expect(warn).toHaveBeenCalledWith("IAP JWT rejected:", "bad sig");
    expect(JSON.stringify(warn.mock.calls)).not.toContain("\"jwt\"");
  });
  it("forwards allowedHd to the verifier", async () => {
    const verify = vi.fn(async () => "alice@x.com");
    await resolveUser(jwt(), { onCloudRun: true, allowedHd: HD, verify });
    expect(verify).toHaveBeenCalledWith("jwt", HD);
  });
  it("needs no hd locally", async () => {
    const verify = vi.fn(async () => "alice@gmail.com");
    expect(await resolveUser(jwt(), { onCloudRun: false, verify })).toEqual({ kind: "user", userId: "alice@gmail.com" });
    expect(verify).toHaveBeenCalledWith("jwt", undefined);
  });
  it("fails closed on Cloud Run when no allowed hd is configured", async () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const verify = vi.fn(ok);
    expect(await resolveUser(jwt(), { onCloudRun: true, verify })).toEqual({ kind: "reject", status: 401 });
    expect(error).toHaveBeenCalledWith("IAP_ALLOWED_HD unset");
    expect(verify).not.toHaveBeenCalled();
  });
  it("end-to-end: a real assertion with the wrong hd is rejected", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const { keys, sign } = await setup();
    const h = new Headers({ "x-goog-iap-jwt-assertion": await sign({ email: "a@evil.com", hd: "evil.com" }) });
    const verify = (t: string, hd?: string) => verifyIapJwt(t, { audience: AUD, keys, allowedHd: hd });
    expect(await resolveUser(h, { onCloudRun: true, allowedHd: HD, verify })).toEqual({ kind: "reject", status: 401 });
  });
});
