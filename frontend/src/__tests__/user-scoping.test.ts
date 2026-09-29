import { describe, it, expect } from "vitest";
import { scopeQuery, scopeRequestToUser } from "@/lib/user-scoping";

const U = "alice@x.com";
describe("scopeRequestToUser", () => {
  it("rewrites the path user segment on canned session routes", () => {
    expect(scopeRequestToUser("GET", ["apps", "trend_scout", "users", "me", "sessions", "42"], undefined, U))
      .toEqual({ path: "apps/trend_scout/users/alice%40x.com/sessions/42", body: undefined });
  });
  it("rewrites the create-session and list-sessions paths", () => {
    expect(scopeRequestToUser("POST", ["apps", "a", "users", "me", "sessions"], "{}", U))
      .toEqual({ path: "apps/a/users/alice%40x.com/sessions", body: "{}" });
    expect(scopeRequestToUser("GET", ["apps", "a", "users", "bob@x.com", "sessions"], undefined, U)?.path)
      .toBe("apps/a/users/alice%40x.com/sessions");
  });
  it("rewrites poll and resume paths", () => {
    expect(scopeRequestToUser("GET", ["runs", "a", "bob@x.com", "42"], undefined, U)?.path).toBe("runs/a/alice%40x.com/42");
    expect(scopeRequestToUser("POST", ["runs", "a", "me", "42", "resume"], "{}", U)?.path).toBe("runs/a/alice%40x.com/42/resume");
  });
  it("overwrites the kick-off body userId", () => {
    const r = scopeRequestToUser("POST", ["runs", "a"], JSON.stringify({ userId: "bob", sessionId: "42", message: "m" }), U);
    expect(JSON.parse(r!.body!)).toEqual({ userId: U, sessionId: "42", message: "m" });
  });
  it("throws on a malformed kick-off body", () => {
    expect(() => scopeRequestToUser("POST", ["runs", "a"], "{not json", U)).toThrow();
  });
  it("blocks routes the UI never uses", () => {
    expect(scopeRequestToUser("POST", ["run_sse"], "{}", U)).toBeNull();
    expect(scopeRequestToUser("DELETE", ["apps", "a", "users", "me", "sessions", "42"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("PATCH", ["apps", "a", "users", "me", "memory"], "{}", U)).toBeNull();
  });
  it("scopes the artifacts listing and artifact fetch", () => {
    expect(scopeRequestToUser("GET", ["apps", "a", "users", "me", "sessions", "42", "artifacts"], undefined, U)?.path)
      .toBe("apps/a/users/alice%40x.com/sessions/42/artifacts");
    expect(scopeRequestToUser("GET", ["apps", "a", "users", "me", "sessions", "42", "artifacts", "img.png"], undefined, U)).not.toBeNull();
  });
  it("refuses dot segments and any segment holding a decoded '/' or '\\'", () => {
    // Next decodes %2F inside a catch-all segment, and re-encoding is undone by uvicorn
    // upstream — so such a segment must be refused, not re-encoded.
    expect(scopeRequestToUser("GET", ["runs", "a", "me", "../../apps/a/users/bob@x.com/sessions/42"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["apps", "a", "users", "me", "sessions", ".."], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["apps", "a", "users", "me", "sessions", "4\\2"], undefined, U)).toBeNull();
  });
  it("refuses a kick-off whose app segment smuggles another user's resume route", () => {
    // POST /api/adk/runs/a%2Fbob%40x.com%2F42%2Fresume → ["runs", "a/bob@x.com/42/resume"]
    expect(scopeRequestToUser("POST", ["runs", "a/bob@x.com/42/resume"], "{}", U)).toBeNull();
  });
  it("requires the app segment to be an identifier", () => {
    expect(scopeRequestToUser("GET", ["apps", "a/b", "users", "me", "sessions"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["apps", "my-app", "users", "me", "sessions"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["runs", "9a", "me", "42"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("POST", ["runs", "trend_scout"], "{}", U)).not.toBeNull();
  });
  it("allows list-apps untouched", () => {
    expect(scopeRequestToUser("GET", ["list-apps"], undefined, U)).toEqual({ path: "list-apps", body: undefined });
  });
});

describe("scopeQuery", () => {
  it("keeps only since and version", () => {
    expect(scopeQuery(new URLSearchParams("since=3&version=2&userId=bob&x=1"))).toBe("?since=3&version=2");
    expect(scopeQuery(new URLSearchParams("since=0"))).toBe("?since=0");
  });
  it("returns an empty string when nothing is allowed", () => {
    expect(scopeQuery(new URLSearchParams("userId=bob"))).toBe("");
    expect(scopeQuery(new URLSearchParams(""))).toBe("");
  });
});
