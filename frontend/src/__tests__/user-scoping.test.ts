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
  it("keeps run only on the experiment metrics and creatives routes, as a positive integer", () => {
    const metrics = ["experiments", "me", "exp-123", "metrics"];
    const creatives = ["experiments", "me", "exp-123", "creatives"];
    expect(scopeQuery(new URLSearchParams("run=2"), metrics)).toBe("?run=2");
    expect(scopeQuery(new URLSearchParams("run=12&userId=bob"), creatives)).toBe("?run=12");
    for (const bad of ["0", "-1", "1.5", "2a", "", " 2", "1e3", "0012", "1234567"]) {
      expect(scopeQuery(new URLSearchParams({ run: bad }), metrics), bad).toBe("");
    }
    expect(scopeQuery(new URLSearchParams("run=2"))).toBe("");
    expect(scopeQuery(new URLSearchParams("run=2"), ["experiments", "me", "exp-123"])).toBe("");
    expect(scopeQuery(new URLSearchParams("run=2"), ["experiments", "me", "exp-123", "traffic"])).toBe("");
    expect(scopeQuery(new URLSearchParams("run=2"), ["runs", "app", "me", "42"])).toBe("");
  });
  it("returns an empty string when nothing is allowed", () => {
    expect(scopeQuery(new URLSearchParams("userId=bob"))).toBe("");
    expect(scopeQuery(new URLSearchParams(""))).toBe("");
  });
});

describe("scopeRequestToUser: experiments", () => {
  const ID = "exp-3f9a2c1d";
  it("overwrites the create body userId", () => {
    const body = JSON.stringify({ userId: "bob", appName: "creative_agent", sessionId: "42", creativeIndices: [0, 1] });
    const r = scopeRequestToUser("POST", ["experiments"], body, U);
    expect(r?.path).toBe("experiments");
    expect(JSON.parse(r!.body!)).toEqual({ userId: U, appName: "creative_agent", sessionId: "42", creativeIndices: [0, 1] });
  });
  it("rewrites the user segment on list, detail and metrics", () => {
    expect(scopeRequestToUser("GET", ["experiments", "me"], undefined, U)?.path).toBe("experiments/alice%40x.com");
    expect(scopeRequestToUser("GET", ["experiments", "bob@x.com", ID], undefined, U)?.path)
      .toBe(`experiments/alice%40x.com/${ID}`);
    expect(scopeRequestToUser("GET", ["experiments", "me", ID, "metrics"], undefined, U)?.path)
      .toBe(`experiments/alice%40x.com/${ID}/metrics`);
  });
  it("rewrites the user segment on the per-creative series (contracts §8)", () => {
    expect(scopeRequestToUser("GET", ["experiments", "bob@x.com", ID, "creatives"], undefined, U)?.path)
      .toBe(`experiments/alice%40x.com/${ID}/creatives`);
    expect(scopeRequestToUser("POST", ["experiments", "me", ID, "creatives"], "{}", U)).toBeNull();
    expect(scopeRequestToUser("GET", ["experiments", "me", ID, "creatives", "x"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["experiments", "me", "Bad_Id", "creatives"], undefined, U)).toBeNull();
  });
  it("rewrites the user segment on traffic and stop", () => {
    expect(scopeRequestToUser("POST", ["experiments", "bob", ID, "traffic"], '{"episodes":20}', U))
      .toEqual({ path: `experiments/alice%40x.com/${ID}/traffic`, body: '{"episodes":20}' });
    expect(scopeRequestToUser("POST", ["experiments", "me", ID, "stop"], "{}", U)?.path)
      .toBe(`experiments/alice%40x.com/${ID}/stop`);
  });
  it("refuses malformed experiment ids", () => {
    for (const bad of ["Exp-1", "-abc", "ab", "a_b_c", "x".repeat(65), "abc.def", "abc%2Fdef"]) {
      expect(scopeRequestToUser("GET", ["experiments", "me", bad], undefined, U)).toBeNull();
      expect(scopeRequestToUser("POST", ["experiments", "me", bad, "stop"], "{}", U)).toBeNull();
    }
    expect(scopeRequestToUser("POST", ["experiments", "me", "../x", "stop"], "{}", U)).toBeNull();
    expect(scopeRequestToUser("GET", ["experiments", "me", "a/b/c"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["experiments", "a/b"], undefined, U)).toBeNull();
  });
  it("blocks methods and shapes the UI never uses", () => {
    expect(scopeRequestToUser("DELETE", ["experiments", "me", ID], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["experiments"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("POST", ["experiments", "me"], "{}", U)).toBeNull();
    expect(scopeRequestToUser("POST", ["experiments", "me", ID, "delete"], "{}", U)).toBeNull();
    expect(scopeRequestToUser("GET", ["experiments", "me", ID, "traffic"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["experiments", "me", ID, "metrics", "x"], undefined, U)).toBeNull();
  });
});

describe("scopeRequestToUser: ratings", () => {
  const SID = "3f2a9c1e-7b4d-4e1a-9f00-1234567890ab";
  const body = '{"app_name":"creative_agent","creative_key":"visual:A","kind":"visual","verdict":"pass"}';
  it("rewrites the user segment on the session list, the upsert and calibration", () => {
    expect(scopeRequestToUser("GET", ["ratings", "me", SID], undefined, U)?.path)
      .toBe(`ratings/alice%40x.com/${SID}`);
    expect(scopeRequestToUser("PUT", ["ratings", "bob@x.com", SID], body, U))
      .toEqual({ path: `ratings/alice%40x.com/${SID}`, body });
    expect(scopeRequestToUser("GET", ["ratings", "bob@x.com", "calibration"], undefined, U)?.path)
      .toBe("ratings/alice%40x.com/calibration");
  });
  it("refuses other methods, shapes and malformed session ids", () => {
    expect(scopeRequestToUser("PUT", ["ratings", "me", "calibration"], body, U)).toBeNull();
    expect(scopeRequestToUser("POST", ["ratings", "me", SID], body, U)).toBeNull();
    expect(scopeRequestToUser("DELETE", ["ratings", "me", SID], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["ratings", "me"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["ratings"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["ratings", "me", SID, "x"], undefined, U)).toBeNull();
    for (const bad of ["a b", "a.b", "..", "a/b", "x".repeat(129), "a%2Fb"]) {
      expect(scopeRequestToUser("GET", ["ratings", "me", bad], undefined, U)).toBeNull();
      expect(scopeRequestToUser("PUT", ["ratings", "me", bad], body, U)).toBeNull();
    }
  });
  it("drops every query param on ratings routes", () => {
    expect(scopeQuery(new URLSearchParams("run=2&x=1"), ["ratings", "me", SID])).toBe("");
  });
});

describe("scopeRequestToUser: shares", () => {
  const SID = "3f2a9c1e-7b4d-4e1a-9f00-1234567890ab";
  const TOKEN = "AbCdEfGh_ijkl-MNOP";
  const body = '{"concept_names":["A"],"include_eval":true}';
  it("rewrites the user segment on create, list and revoke", () => {
    expect(scopeRequestToUser("POST", ["shares", "me", "creative_agent", SID], body, U))
      .toEqual({ path: `shares/alice%40x.com/creative_agent/${SID}`, body });
    expect(scopeRequestToUser("GET", ["shares", "bob@x.com"], undefined, U)?.path)
      .toBe("shares/alice%40x.com");
    expect(scopeRequestToUser("DELETE", ["shares", "bob@x.com", TOKEN], undefined, U)?.path)
      .toBe(`shares/alice%40x.com/${TOKEN}`);
  });
  it("refuses a bad app name or session id on create", () => {
    for (const app of ["1abc", "a-b", "a.b", "a b", ""]) {
      expect(scopeRequestToUser("POST", ["shares", "me", app, SID], body, U)).toBeNull();
    }
    for (const bad of ["a b", "a.b", "..", "a/b", "x".repeat(129), "a%2Fb"]) {
      expect(scopeRequestToUser("POST", ["shares", "me", "creative_agent", bad], body, U)).toBeNull();
    }
  });
  it("refuses a malformed token on revoke", () => {
    for (const bad of ["short", "x".repeat(65), "a.b.c.d.e.f.g.h.i.j", "abcdefghijklmnop/q", "abcdefgh ijklmnop", ".."]) {
      expect(scopeRequestToUser("DELETE", ["shares", "me", bad], undefined, U)).toBeNull();
    }
    expect(scopeRequestToUser("DELETE", ["shares", "me", "x".repeat(16)], undefined, U)).not.toBeNull();
    expect(scopeRequestToUser("DELETE", ["shares", "me", "x".repeat(64)], undefined, U)).not.toBeNull();
  });
  it("allows DELETE only on the shares revoke path and refuses other shapes", () => {
    expect(scopeRequestToUser("DELETE", ["ratings", "me", TOKEN], undefined, U)).toBeNull();
    expect(scopeRequestToUser("DELETE", ["experiments", "me", "exp-123"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("DELETE", ["shares", "me"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("DELETE", ["shares", "me", "creative_agent", SID], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["shares", "me", TOKEN], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["shares"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("PUT", ["shares", "me", "creative_agent", SID], body, U)).toBeNull();
    expect(scopeRequestToUser("POST", ["shares", "me", "creative_agent"], body, U)).toBeNull();
    expect(scopeRequestToUser("POST", ["shares", "me"], body, U)).toBeNull();
    expect(scopeRequestToUser("POST", ["shares", "me", "creative_agent", SID, "x"], body, U)).toBeNull();
  });
});

describe("scopeRequestToUser: person-refs", () => {
  const CID = "AbCdEfGh_ijkl-MN";
  const body = '{"photo_uri":"gs://b/person-refs/alice_x_com/me.jpg"}';
  it("rewrites the user segment on register, list and revoke", () => {
    expect(scopeRequestToUser("POST", ["person-refs", "me"], body, U))
      .toEqual({ path: "person-refs/alice%40x.com", body });
    expect(scopeRequestToUser("GET", ["person-refs", "bob@x.com"], undefined, U)?.path)
      .toBe("person-refs/alice%40x.com");
    expect(scopeRequestToUser("DELETE", ["person-refs", "bob@x.com", CID], undefined, U)?.path)
      .toBe(`person-refs/alice%40x.com/${CID}`);
  });
  it("refuses a malformed consent id on revoke", () => {
    for (const bad of ["short", "x".repeat(65), "abcdefgh.ijk", "abcdefgh/ijk", "abcd efgh", ".."]) {
      expect(scopeRequestToUser("DELETE", ["person-refs", "me", bad], undefined, U)).toBeNull();
    }
    expect(scopeRequestToUser("DELETE", ["person-refs", "me", "x".repeat(8)], undefined, U)).not.toBeNull();
    expect(scopeRequestToUser("DELETE", ["person-refs", "me", "x".repeat(64)], undefined, U)).not.toBeNull();
  });
  it("refuses other shapes", () => {
    expect(scopeRequestToUser("DELETE", ["person-refs", "me"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["person-refs", "me", CID], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["person-refs"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("POST", ["person-refs", "me", CID], body, U)).toBeNull();
    expect(scopeRequestToUser("PUT", ["person-refs", "me"], body, U)).toBeNull();
    expect(scopeRequestToUser("DELETE", ["person-refs", "me", CID, "x"], undefined, U)).toBeNull();
  });
  it("drops every query param", () => {
    expect(scopeQuery(new URLSearchParams("run=2&x=1"), ["person-refs", "me"])).toBe("");
  });
});

describe("scopeRequestToUser: variants", () => {
  const VSID = "a1b2c3d4-e5f6";
  const body = '{"concept_name":"The Encore","consent_id":"consent-1234"}';
  it("rewrites the user segment on render and list", () => {
    expect(scopeRequestToUser("POST", ["variants", "me", "creative_agent", VSID], body, U)).toEqual({
      path: `variants/alice%40x.com/creative_agent/${VSID}`,
      body,
    });
    expect(
      scopeRequestToUser("GET", ["variants", "bob@x.com", "interactive_creative", VSID], undefined, U)?.path,
    ).toBe(`variants/alice%40x.com/interactive_creative/${VSID}`);
  });
  it("refuses bad app names, session ids and other shapes", () => {
    expect(scopeRequestToUser("POST", ["variants", "me", "bad-app", VSID], body, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["variants", "me", "creative_agent", "a b"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["variants", "me", "creative_agent"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("GET", ["variants", "me"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("DELETE", ["variants", "me", "creative_agent", VSID], undefined, U)).toBeNull();
    expect(scopeRequestToUser("PUT", ["variants", "me", "creative_agent", VSID], body, U)).toBeNull();
    expect(scopeRequestToUser("POST", ["variants", "me", "creative_agent", VSID, "x"], body, U)).toBeNull();
  });
});
