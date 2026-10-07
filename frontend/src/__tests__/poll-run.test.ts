import { describe, it, expect, vi, afterEach } from "vitest";
import {
  startRun,
  pollRun,
  getRunStatus,
  resumeRun,
  ResumeNotAppliedError,
  ResumeRejectedError,
} from "@/lib/api";
import type { AgentEvent } from "@/lib/types";
import { jsonResponse } from "./helpers";

// The async-job run model replaces the SSE async-generator with a REST job:
// POST /runs/{app} starts a background run, and GET /runs/{app}/{user}/{sid}?since=N
// returns only the NEW events since the cursor plus a coarse status. pollRun drives
// that GET loop until the run reaches a terminal state ("done"/"error").

const API_BASE = "/api/adk";

function ev(id: string): AgentEvent {
  return { id, author: "a", timestamp: 1 } as AgentEvent;
}

afterEach(() => vi.restoreAllMocks());

describe("startRun", () => {
  it("posts correct body and returns parsed {runId,status}", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({ runId: "run-1", status: "running" })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await startRun("creative_agent", "u1", "s1", "hello");

    expect(result).toEqual({ runId: "run-1", status: "running" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${API_BASE}/runs/creative_agent`);
    expect(init?.method).toBe("POST");
    expect(init?.headers).toEqual({ "Content-Type": "application/json" });
    expect(JSON.parse(init?.body as string)).toEqual({
      userId: "u1",
      sessionId: "s1",
      message: "hello",
    });
  });

  it("throws when the start request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("nope", { status: 500 }))
    );
    await expect(startRun("creative_agent", "u1", "s1", "hi")).rejects.toThrow(
      /Failed to start run \(500\)/
    );
  });

  it("treats 409 run-already-active as a running run (caller just polls)", async () => {
    // The server's duplicate-run guard returns 409 when a run is already active
    // for this session (e.g. the first POST landed but its response was lost and
    // the page reloaded before markRunStarted). The run IS live, so startRun must
    // resolve and let the page poll it instead of showing a start error.
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: "Run already active" }), {
            status: 409,
          })
      )
    );
    await expect(startRun("creative_agent", "u1", "s1", "hi")).resolves.toEqual({
      runId: "s1",
      status: "running",
    });
  });
});

describe("pollRun", () => {
  it("yields only new events and advances the cursor", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ status: "running", events: [ev("e1"), ev("e2")], nextCursor: 2 })
      )
      .mockResolvedValueOnce(
        jsonResponse({ status: "done", events: [ev("e3")], nextCursor: 3 })
      );
    vi.stubGlobal("fetch", fetchMock);

    const yielded: AgentEvent[] = [];
    for await (const e of pollRun("creative_agent", "u1", "s1", { intervalMs: 0 })) {
      yielded.push(e);
    }

    expect(yielded.map((e) => e.id)).toEqual(["e1", "e2", "e3"]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][0]).toContain("since=0");
    expect(fetchMock.mock.calls[1][0]).toContain("since=2");
  });

  it("stops on done", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ status: "done", events: [ev("e1")], nextCursor: 1 })
      );
    vi.stubGlobal("fetch", fetchMock);

    const yielded: AgentEvent[] = [];
    for await (const e of pollRun("creative_agent", "u1", "s1", { intervalMs: 0 })) {
      yielded.push(e);
    }

    expect(yielded.map((e) => e.id)).toEqual(["e1"]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps waiting on not_found", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ status: "not_found", events: [], nextCursor: 0 })
      )
      .mockResolvedValueOnce(
        jsonResponse({ status: "done", events: [ev("e1")], nextCursor: 1 })
      );
    vi.stubGlobal("fetch", fetchMock);

    const yielded: AgentEvent[] = [];
    for await (const e of pollRun("creative_agent", "u1", "s1", { intervalMs: 0 })) {
      yielded.push(e);
    }

    expect(yielded.map((e) => e.id)).toEqual(["e1"]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("throws on error status with the backend error message", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ status: "error", events: [], nextCursor: 0, error: "model 429" })
      );
    vi.stubGlobal("fetch", fetchMock);

    async function drain() {
      for await (const _ of pollRun("creative_agent", "u1", "s1", { intervalMs: 0 })) {
        void _;
      }
    }

    await expect(drain()).rejects.toThrow(/model 429/);
  });
});

describe("getRunStatus", () => {
  it("returns the full payload including state", async () => {
    const payload = {
      status: "running",
      events: [ev("e1")],
      nextCursor: 1,
      state: { brand: "PRS" },
      error: null,
    };
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse(payload)
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await getRunStatus("creative_agent", "u1", "s1", 0);

    expect(result.status).toBe("running");
    expect(result.events.map((e) => e.id)).toEqual(["e1"]);
    expect(result.nextCursor).toBe(1);
    expect(result.state).toEqual({ brand: "PRS" });
    expect(fetchMock.mock.calls[0][0]).toBe(
      `${API_BASE}/runs/creative_agent/u1/s1?since=0`
    );
  });
});

describe("resumeRun", () => {
  it("posts the function response to the resume endpoint", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({ runId: "run-2", status: "running" })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await resumeRun(
      "interactive_creative",
      "u1",
      "s1",
      "fc-123",
      "review_research",
      { status: "approved", feedback: "Looks good" },
      "evt-1"
    );

    expect(result).toEqual({ runId: "run-2", status: "running" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${API_BASE}/runs/interactive_creative/u1/s1/resume`);
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({
      functionCallId: "fc-123",
      functionName: "review_research",
      response: { status: "approved", feedback: "Looks good" },
      functionCallEventId: "evt-1",
    });
  });

  it("posts the review_trends selection to the resume endpoint", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({ runId: "run-3", status: "running" })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await resumeRun(
      "trend_scout",
      "u1",
      "s1",
      "fc-trends",
      "review_trends",
      { status: "selected", selected_trends: ["Trend A", "Trend C"], instruction: "" },
      "evt-8"
    );

    expect(result).toEqual({ runId: "run-3", status: "running" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${API_BASE}/runs/trend_scout/u1/s1/resume`);
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({
      functionCallId: "fc-trends",
      functionName: "review_trends",
      response: {
        status: "selected",
        selected_trends: ["Trend A", "Trend C"],
        instruction: "",
      },
      functionCallEventId: "evt-8",
    });
  });

  function conflict(reason: string | null): Response {
    const detail =
      reason === null ? "Run already active" : { reason, message: "Run already active" };
    return new Response(JSON.stringify({ detail }), { status: 409 });
  }

  const call = () =>
    resumeRun(
      "interactive_creative",
      "u1",
      "s1",
      "fc-1",
      "review_research",
      { status: "approved" }
    );

  it("treats 409 resume_in_progress as running (a duplicate resume is already being served)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => conflict("resume_in_progress")));
    await expect(call()).resolves.toEqual({ runId: "s1", status: "running" });
  });

  it("throws ResumeNotAppliedError on 409 prior_segment_active (response NOT applied)", async () => {
    // The previous segment was still finishing after the server's grace wait, so
    // the approval was dropped. Polling would NOT re-show the review panel (the
    // pause event is already deduped) and the prior segment's 'done' would read
    // as completed — so the caller must re-offer the review instead.
    vi.stubGlobal("fetch", vi.fn(async () => conflict("prior_segment_active")));
    const err = await call().catch((e) => e);
    expect(err).toBeInstanceOf(ResumeNotAppliedError);
    expect((err as Error).message).toMatch(/still finishing/i);
  });

  it("treats an unrecognised 409 as not applied (safe: the user can re-submit)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => conflict(null)));
    await expect(call()).rejects.toBeInstanceOf(ResumeNotAppliedError);
  });

  it("throws ResumeRejectedError (a not-applied error) on 400 invalid edits", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              detail: {
                reason: "invalid_brief",
                message: "The edited brief is invalid: angles: too short",
                errors: [{ loc: "angles", msg: "too short" }],
              },
            }),
            { status: 400 }
          )
      )
    );
    const err = await call().catch((e) => e);
    expect(err).toBeInstanceOf(ResumeRejectedError);
    expect(err).toBeInstanceOf(ResumeNotAppliedError); // the page re-offers the review
    expect((err as Error).message).toMatch(/angles: too short/);
  });

  it("still throws a generic error on other failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("boom", { status: 500 }))
    );
    const err = await call().catch((e) => e);
    expect(err).not.toBeInstanceOf(ResumeNotAppliedError);
    expect((err as Error).message).toMatch(/Failed to resume run \(500\)/);
  });
});
