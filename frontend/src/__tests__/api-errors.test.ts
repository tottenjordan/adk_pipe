import { describe, it, expect, vi, afterEach } from "vitest";
import * as api from "@/lib/api";
import {
  createSession,
  getArtifact,
  getEventError,
  getRunStatus,
  getSession,
  listArtifacts,
  listSessions,
  pollRun,
  resumeRun,
  SELF_USER_ID,
  startRun,
} from "@/lib/api";
import type { AgentEvent } from "@/lib/types";
import { jsonResponse } from "./helpers";

// Session/artifact errors used to read `res.statusText`, which is empty over
// HTTP/2 and HTTP/3 (IAP / Cloud Run), so users saw "Failed to get session: ".
// They now carry the status code plus FastAPI's string `detail`, when there is one.

afterEach(() => {
  vi.restoreAllMocks();
});

/** An HTTP/2-style response: no reason phrase. */
function h2Response(body: string, status: number, contentType = "application/json"): Response {
  return new Response(body, { status, statusText: "", headers: { "content-type": contentType } });
}

function stubFetch(res: () => Response) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async () => res());
}

describe("session/artifact error messages", () => {
  it("names the status and FastAPI's string detail (empty statusText)", async () => {
    stubFetch(() => h2Response(JSON.stringify({ detail: "Session not found" }), 404));
    await expect(getSession("creative_agent", SELF_USER_ID, "s1")).rejects.toThrow(
      new Error("Failed to get session (404): Session not found")
    );
  });

  it("gives the status only for a non-JSON body", async () => {
    stubFetch(() => h2Response("<html>Bad Gateway</html>", 502, "text/html"));
    await expect(getSession("creative_agent", SELF_USER_ID, "s1")).rejects.toThrow(
      new Error("Failed to get session (502)")
    );
  });

  it("gives the status only when detail is not a string (validation errors)", async () => {
    stubFetch(() => h2Response(JSON.stringify({ detail: [{ loc: ["body"], msg: "bad" }] }), 422));
    await expect(createSession("creative_agent", SELF_USER_ID, {})).rejects.toThrow(
      new Error("Failed to create session (422)")
    );
  });

  it("gives the status only for an empty body", async () => {
    stubFetch(() => h2Response("", 500));
    await expect(listSessions("creative_agent", SELF_USER_ID)).rejects.toThrow(
      new Error("Failed to list sessions (500)")
    );
  });

  it("covers listArtifacts and getArtifact too", async () => {
    stubFetch(() => h2Response(JSON.stringify({ detail: "nope" }), 403));
    await expect(listArtifacts("creative_agent", SELF_USER_ID, "s1")).rejects.toThrow(
      new Error("Failed to list artifacts (403): nope")
    );
    await expect(
      getArtifact("creative_agent", SELF_USER_ID, "s1", "video_bumper_6s_9x16.mp4")
    ).rejects.toThrow(new Error("Failed to get artifact (403): nope"));
  });
});

describe("path segments are URL-encoded", () => {
  const urls = () =>
    (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.map((c) => String(c[0]));

  it("encodes app, user and session on every session/artifact/run URL", async () => {
    stubFetch(() => jsonResponse({ status: "done", events: [], nextCursor: 0, id: "x" }));
    const app = "../gcs";
    const user = "a@b.com";
    const sid = "s/1?x";
    await createSession(app, user);
    await getSession(app, user, sid);
    await listSessions(app, user);
    await listArtifacts(app, user, sid);
    await getArtifact(app, user, sid, "v/1?y.mp4");
    await startRun(app, user, sid, "hi");
    await getRunStatus(app, user, sid);
    for await (const _ of pollRun(app, user, sid)) {
      // drain
    }
    await resumeRun(app, user, sid, "fc", "review", {});

    const seen = urls();
    expect(seen).toEqual([
      "/api/adk/apps/..%2Fgcs/users/a%40b.com/sessions",
      "/api/adk/apps/..%2Fgcs/users/a%40b.com/sessions/s%2F1%3Fx",
      "/api/adk/apps/..%2Fgcs/users/a%40b.com/sessions",
      "/api/adk/apps/..%2Fgcs/users/a%40b.com/sessions/s%2F1%3Fx/artifacts",
      "/api/adk/apps/..%2Fgcs/users/a%40b.com/sessions/s%2F1%3Fx/artifacts/v%2F1%3Fy.mp4",
      "/api/adk/runs/..%2Fgcs",
      "/api/adk/runs/..%2Fgcs/a%40b.com/s%2F1%3Fx?since=0",
      "/api/adk/runs/..%2Fgcs/a%40b.com/s%2F1%3Fx?since=0",
      "/api/adk/runs/..%2Fgcs/a%40b.com/s%2F1%3Fx/resume",
    ]);
    for (const u of seen) expect(u).not.toContain("../");
  });

  it("leaves the 'me' placeholder and plain ids unchanged", async () => {
    stubFetch(() => jsonResponse({ id: "s1" }));
    await getSession("creative_agent", SELF_USER_ID, "s1");
    expect(urls()).toEqual(["/api/adk/apps/creative_agent/users/me/sessions/s1"]);
  });
});

describe("quota error detection", () => {
  const err = (text: string) =>
    getEventError({ id: "", author: "a", timestamp: 1, error: text } as AgentEvent);

  it("does not treat a 429 inside a longer number as a quota error", () => {
    expect(err("session 14290 failed")).toBe("session 14290 failed");
    expect(err("used 4291 tokens")).toBe("used 4291 tokens");
  });

  it("treats a standalone 429 as a quota error", () => {
    expect(err("429 Too Many Requests")).toMatch(/quota/i);
  });

  it("treats RESOURCE_EXHAUSTED alone as a quota error", () => {
    expect(err("RESOURCE_EXHAUSTED: try later")).toMatch(/quota/i);
  });
});

describe("dead code", () => {
  it("no longer exports listApps", () => {
    expect("listApps" in api).toBe(false);
  });
});
