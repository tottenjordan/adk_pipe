// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentEvent } from "@/lib/types";

vi.mock("@/lib/api", async (importActual) => {
  const actual = await importActual<typeof import("@/lib/api")>();
  return {
    ...actual,
    pollRun: vi.fn(),
    getRunStatus: vi.fn(),
    resumeRun: vi.fn(),
    startRun: vi.fn(),
    getSession: vi.fn(),
  };
});

import {
  getRunStatus,
  getSession,
  pollRun,
  resumeRun,
  ResumeNotAppliedError,
  ResumeRejectedError,
  RunConnectionLostError,
  type PollResult,
} from "@/lib/api";
import { useRunFollower } from "@/app/run/[sessionId]/use-run-follower";
import {
  PAUSE_WATCH_INTERVAL_MS,
  stallTimeoutFor,
} from "@/app/run/[sessionId]/run-config";

// The run page's follower: kick-off, poll consumption (dedupe across replays),
// pause detection, stall watchdog, stale-tab watcher, resume / continue.

const textEvent = (id: string): AgentEvent => ({
  id,
  invocationId: "inv",
  author: "root_agent",
  timestamp: 1,
  content: { role: "model", parts: [{ text: `text ${id}` }] },
});

/** A checkpoint call (long-running, unanswered until `answer(callId)`). */
const pauseEvent = (id: string, callId: string): AgentEvent => ({
  id,
  invocationId: "inv",
  author: "root_agent",
  timestamp: 1,
  longRunningToolIds: [callId],
  content: {
    role: "model",
    parts: [{ functionCall: { id: callId, name: "review_research", args: {} } }],
  },
});

const answerEvent = (id: string, callId: string): AgentEvent => ({
  id,
  invocationId: "inv",
  author: "user",
  timestamp: 1,
  content: {
    role: "user",
    parts: [{ functionResponse: { id: callId, name: "review_research", response: {} } }],
  },
});

/** A poll stream the test drives: push events, then end or fail it. */
function stream() {
  const queue: AgentEvent[] = [];
  let wake: (() => void) | null = null;
  let done = false;
  let error: unknown = null;
  const poke = () => {
    wake?.();
    wake = null;
  };
  async function* iter(): AsyncGenerator<AgentEvent> {
    while (true) {
      while (queue.length) yield queue.shift()!;
      if (error) throw error;
      if (done) return;
      await new Promise<void>((resolve) => (wake = resolve));
    }
  }
  return {
    iter,
    push(...events: AgentEvent[]) {
      queue.push(...events);
      poke();
    },
    end() {
      done = true;
      poke();
    },
    fail(err: unknown) {
      error = err;
      poke();
    },
  };
}

/** A finished poll segment yielding `events`. */
function segment(...events: AgentEvent[]) {
  const s = stream();
  s.push(...events);
  s.end();
  return s.iter();
}

const poll = (events: AgentEvent[], nextCursor = events.length): PollResult => ({
  status: "running",
  events,
  nextCursor,
  state: {},
  error: null,
});

const mockPollRun = vi.mocked(pollRun);
const mockGetRunStatus = vi.mocked(getRunStatus);
const mockResumeRun = vi.mocked(resumeRun);

/** Let pending promises / timers run inside act(). */
async function flush(ms = 0) {
  await act(() => vi.advanceTimersByTimeAsync(ms));
}

/** Advance fake time in 1 s steps, each in its own act() (see run-page tests). */
async function advance(ms: number) {
  for (let left = ms; left > 0; left -= 1_000) {
    await flush(Math.min(1_000, left));
  }
}

const ids = (events: AgentEvent[]) => events.map((e) => e.id);

function follow(app = "interactive_creative") {
  return renderHook(() => useRunFollower(app, "me", "s1"));
}

beforeEach(() => {
  sessionStorage.clear(); // no kick-off message: view mode
  vi.useFakeTimers();
  vi.mocked(getSession).mockResolvedValue({ id: "s1", state: {} } as never);
  // The seed (since=0) shows a started run.
  mockGetRunStatus.mockResolvedValue(poll([textEvent("e0")]));
});
afterEach(() => {
  vi.useRealTimers();
  vi.resetAllMocks();
});

describe("useRunFollower: events and pauses", () => {
  it("consumes events, pauses at a checkpoint and dedupes the resume's replay", async () => {
    const e1 = textEvent("e1");
    const e2 = textEvent("e2");
    const p1 = pauseEvent("p1", "fc-1");
    mockPollRun.mockReturnValueOnce(segment(e1, e2, p1));
    const { result } = follow();
    await flush();

    expect(ids(result.current.events)).toEqual(["e1", "e2", "p1"]);
    expect(result.current.status).toBe("paused");
    expect(result.current.pauseContext).toEqual({
      functionCallId: "fc-1",
      functionName: "review_research",
      eventId: "p1",
    });

    // The resumed poll replays from since=0: the old events must not repeat.
    mockResumeRun.mockResolvedValueOnce(undefined as never);
    mockPollRun.mockReturnValueOnce(segment(e1, e2, p1, answerEvent("r1", "fc-1"), textEvent("e3")));
    await act(() => result.current.resume({ status: "approved", edits: [{ field: "x" }] }));
    await flush();

    expect(mockResumeRun).toHaveBeenCalledWith(
      "interactive_creative",
      "me",
      "s1",
      "fc-1",
      "review_research",
      { status: "approved" },
      "p1",
      [{ field: "x" }]
    );
    expect(ids(result.current.events)).toEqual(["e1", "e2", "p1", "r1", "e3"]);
    expect(result.current.pauseContext).toBeNull();
    expect(result.current.status).toBe("completed");
  });

  it("does not re-pause when a replay contains an answered checkpoint", async () => {
    mockPollRun.mockReturnValueOnce(
      segment(pauseEvent("p1", "fc-1"), answerEvent("r1", "fc-1"), textEvent("e3"))
    );
    const { result } = follow();
    await flush();
    expect(result.current.status).toBe("completed");
    expect(result.current.pauseContext).toBeNull();
  });

  it("merges state deltas into session state", async () => {
    const ev = { ...textEvent("e1"), actions: { stateDelta: { brand: "PRS" } } };
    mockPollRun.mockReturnValueOnce(segment(ev));
    const { result } = follow();
    await flush();
    expect(result.current.sessionState.brand).toBe("PRS");
  });
});

describe("useRunFollower: stall watchdog", () => {
  it("flags a quiet run as stalled after stallTimeoutFor(app) and clears it on the next event", async () => {
    const s = stream();
    mockPollRun.mockReturnValueOnce(s.iter());
    const { result } = follow("trend_scout");
    await flush();
    s.push(textEvent("e1"));
    await flush();

    await advance(stallTimeoutFor("trend_scout") - 20_000);
    expect(result.current.status).toBe("running");
    await advance(40_000);
    expect(result.current.status).toBe("stalled");

    s.push(textEvent("e2"));
    await flush();
    expect(result.current.status).toBe("running");
  });
});

describe("useRunFollower: stale-tab watcher", () => {
  it("clears the pause only once another tab answered it, then follows the run", async () => {
    mockPollRun.mockReturnValueOnce(segment(textEvent("e1"), pauseEvent("p1", "fc-1")));
    const { result } = follow();
    await flush();
    expect(result.current.status).toBe("paused");

    // First check: new activity, but not the answer → still paused.
    mockGetRunStatus.mockResolvedValueOnce(poll([textEvent("x1")], 3));
    await flush(PAUSE_WATCH_INTERVAL_MS);
    expect(result.current.status).toBe("paused");
    expect(result.current.pauseContext?.functionCallId).toBe("fc-1");
    expect(mockGetRunStatus).toHaveBeenLastCalledWith("interactive_creative", "me", "s1", 0);

    // Second check (from the advanced cursor): the answer arrived.
    const follow2 = stream();
    mockPollRun.mockReturnValueOnce(follow2.iter());
    mockGetRunStatus.mockResolvedValueOnce(poll([answerEvent("r1", "fc-1")], 4));
    await flush(PAUSE_WATCH_INTERVAL_MS);

    expect(mockGetRunStatus).toHaveBeenLastCalledWith("interactive_creative", "me", "s1", 3);
    expect(result.current.pauseContext).toBeNull();
    expect(result.current.status).toBe("running");
    expect(result.current.notice).toBe("This review was answered in another tab or window.");
    expect(mockPollRun).toHaveBeenCalledTimes(2);

    follow2.push(answerEvent("r1", "fc-1"), textEvent("e2"));
    follow2.end();
    await flush();
    expect(result.current.status).toBe("completed");
  });

  it("keeps waiting through a failed check", async () => {
    mockPollRun.mockReturnValueOnce(segment(pauseEvent("p1", "fc-1")));
    const { result } = follow();
    await flush();
    mockGetRunStatus.mockRejectedValueOnce(new Error("503"));
    await flush(PAUSE_WATCH_INTERVAL_MS);
    expect(result.current.status).toBe("paused");
    expect(result.current.errorMsg).toBeNull();
  });
});

describe("useRunFollower: failures", () => {
  it("reads a lost connection as 'disconnected', not a failed run", async () => {
    const s = stream();
    mockPollRun.mockReturnValueOnce(s.iter());
    const { result } = follow();
    await flush();
    s.fail(new RunConnectionLostError());
    await flush();
    expect(result.current.status).toBe("disconnected");
    expect(result.current.errorMsg).toBeNull();
  });

  it("fails the run on any other poll error", async () => {
    const s = stream();
    mockPollRun.mockReturnValueOnce(s.iter());
    const { result } = follow();
    await flush();
    s.fail(new Error("Run failed: boom"));
    await flush();
    expect(result.current.status).toBe("error");
    expect(result.current.errorMsg).toBe("Run failed: boom");
  });

  it("re-shows the review panel when a resume is not applied (409)", async () => {
    mockPollRun.mockReturnValueOnce(segment(pauseEvent("p1", "fc-1")));
    const { result } = follow();
    await flush();
    const ctx = result.current.pauseContext;

    mockResumeRun.mockRejectedValueOnce(new ResumeNotAppliedError());
    await act(() => result.current.resume({ status: "approved" }));

    expect(result.current.status).toBe("paused");
    expect(result.current.pauseContext).toEqual(ctx);
    expect(result.current.notice).toBe(new ResumeNotAppliedError().message);
    expect(mockPollRun).toHaveBeenCalledTimes(1); // nothing followed
  });

  it("shows a rejected resume's field errors (400) and keeps the pause", async () => {
    mockPollRun.mockReturnValueOnce(segment(pauseEvent("p1", "fc-1")));
    const { result } = follow();
    await flush();

    mockResumeRun.mockRejectedValueOnce(
      new ResumeRejectedError("Invalid brief", { "angles.0.name": "Required" })
    );
    await act(() => result.current.resume({ status: "approved" }));

    expect(result.current.status).toBe("paused");
    expect(result.current.pauseContext?.functionCallId).toBe("fc-1");
    expect(result.current.reviewErrors).toEqual({ "angles.0.name": "Required" });
  });

  it("reads a resume whose response was lost as 'disconnected'", async () => {
    mockPollRun.mockReturnValueOnce(segment(pauseEvent("p1", "fc-1")));
    const { result } = follow();
    await flush();
    mockResumeRun.mockRejectedValueOnce(new RunConnectionLostError());
    await act(() => result.current.resume({ status: "approved" }));
    expect(result.current.status).toBe("disconnected");
  });
});
