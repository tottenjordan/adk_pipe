import { describe, it, expect } from "vitest";
import { scopeRequestToUser } from "@/lib/user-scoping";

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
  it("re-encodes segments and refuses dot segments so a decoded '/' can't escape the scoped path", () => {
    // Next decodes %2F inside a catch-all segment; re-joined raw it would let `..` walk
    // out of the caller's scope into another user's path.
    expect(scopeRequestToUser("GET", ["runs", "a", "me", "../../apps/a/users/bob@x.com/sessions/42"], undefined, U)?.path)
      .toBe("runs/a/alice%40x.com/..%2F..%2Fapps%2Fa%2Fusers%2Fbob%40x.com%2Fsessions%2F42");
    expect(scopeRequestToUser("GET", ["apps", "a", "users", "me", "sessions", ".."], undefined, U)).toBeNull();
  });
  it("allows list-apps untouched", () => {
    expect(scopeRequestToUser("GET", ["list-apps"], undefined, U)).toEqual({ path: "list-apps", body: undefined });
  });
});
