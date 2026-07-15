import { describe, it, expect, vi, afterEach } from "vitest";
import { startRun, pollRun, getRunStatus, resumeRun } from "@/lib/api";
import type { AgentEvent } from "@/lib/types";

// The async-job run model replaces the SSE async-generator with a REST job:
// POST /runs/{app} starts a background run, and GET /runs/{app}/{user}/{sid}?since=N
// returns only the NEW events since the cursor plus a coarse status. pollRun drives
// that GET loop until the run reaches a terminal state ("done"/"error").

const API_BASE = "/api/adk";

function ev(id: string): AgentEvent {
  return { id, author: "a", timestamp: 1 } as AgentEvent;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
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
});
