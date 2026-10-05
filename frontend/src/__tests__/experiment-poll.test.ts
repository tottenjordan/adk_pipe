import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ExperimentApiError,
  isTransientError,
  PollTimeoutError,
  PollWaker,
  pollBackoffMs,
  pollExperiment,
  statusLooksStale,
  wakeOnPageReturn,
  type ExperimentSummary,
  type PollOptions,
} from "@/lib/experiments";

const ID = "exp-3f9a2c1d";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function summary(status: ExperimentSummary["status"]): ExperimentSummary {
  return {
    experimentId: ID,
    userId: "u",
    sessionId: "s",
    appName: "creative_agent",
    createdAt: "2026-10-05T14:00:00Z",
    updatedAt: "2026-10-05T14:00:00Z",
    status,
    scenario: "drift",
    ctrMode: "demo",
    rewardMode: "click",
    ttlExpiresAt: null,
    arms: [],
    endpointId: null,
    trafficExecution: null,
    progress: { episodesDone: 0, episodesTotal: 20 },
    error: null,
  };
}

/** Drive the poll in the background, recording yields, onError calls and the outcome. */
function drive(opts: PollOptions) {
  const seen: string[] = [];
  const errors: [string, number][] = [];
  const ctrl = new AbortController();
  const result = { done: false, error: null as unknown };
  const run = (async () => {
    try {
      for await (const s of pollExperiment(ID, {
        signal: ctrl.signal,
        onError: (e, n) => errors.push([e.message, n]),
        ...opts,
      })) {
        seen.push(s.status);
      }
      result.done = true;
    } catch (err) {
      result.error = err;
    }
  })();
  return { seen, errors, ctrl, result, run };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("pollExperiment resilience", () => {
  it("times out a hung request and retries instead of stopping", async () => {
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) => {
        call += 1;
        if (call === 1) {
          // hangs until aborted (the old poll had no timeout and stalled here)
          return new Promise<Response>((_, reject) =>
            init?.signal?.addEventListener("abort", () => reject(init.signal?.reason))
          );
        }
        return Promise.resolve(jsonResponse(summary("ready")));
      })
    );
    const p = drive({ intervalMs: 5000, requestTimeoutMs: 15_000 });
    await vi.advanceTimersByTimeAsync(14_999);
    expect(p.seen).toEqual([]);
    await vi.advanceTimersByTimeAsync(1); // timeout fires
    await vi.advanceTimersByTimeAsync(5000); // first backoff
    await p.run;
    expect(p.seen).toEqual(["ready"]);
    expect(p.result.done).toBe(true);
    expect(call).toBe(2);
  });

  it("times out even when fetch ignores the abort signal", async () => {
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => {
        call += 1;
        return call === 1
          ? new Promise<Response>(() => {})
          : Promise.resolve(jsonResponse(summary("ready")));
      })
    );
    const p = drive({ intervalMs: 1000, requestTimeoutMs: 2000 });
    await vi.advanceTimersByTimeAsync(3000);
    await p.run;
    expect(p.seen).toEqual(["ready"]);
  });

  it("retries 5xx and network errors with backoff, reporting after N in a row", async () => {
    const outcomes: (() => Promise<Response>)[] = [
      () => Promise.resolve(jsonResponse(summary("running_traffic"))),
      () => Promise.resolve(jsonResponse({ detail: "boom" }, 503)),
      () => Promise.reject(new TypeError("Failed to fetch")),
      () => Promise.resolve(jsonResponse({ detail: "boom" }, 500)),
      () => Promise.resolve(jsonResponse({ detail: "boom" }, 502)),
      () => Promise.resolve(jsonResponse(summary("running_traffic"))),
      () => Promise.resolve(jsonResponse(summary("ready"))),
    ];
    const fetchMock = vi.fn(() => outcomes.shift()!());
    vi.stubGlobal("fetch", fetchMock);
    const p = drive({ intervalMs: 1000, errorAfter: 3, maxBackoffMs: 60_000 });
    await vi.advanceTimersByTimeAsync(0);
    expect(p.seen).toEqual(["running_traffic"]);
    await vi.advanceTimersByTimeAsync(1000); // poll → 503 (failure 1, backoff 1 s)
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1000); // network error (failure 2, backoff 2 s)
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(p.errors).toEqual([]);
    await vi.advanceTimersByTimeAsync(1999);
    expect(fetchMock).toHaveBeenCalledTimes(3); // still backing off
    await vi.advanceTimersByTimeAsync(1); // 500 (failure 3 → reported, backoff 4 s)
    expect(p.errors).toHaveLength(1);
    expect(p.errors[0][1]).toBe(3);
    expect(p.errors[0][0]).toMatch(/500/);
    await vi.advanceTimersByTimeAsync(4000); // 502 (failure 4 → reported again)
    expect(p.errors.map(([, n]) => n)).toEqual([3, 4]);
    await vi.advanceTimersByTimeAsync(8000); // recovers
    expect(p.seen).toEqual(["running_traffic", "running_traffic"]);
    await vi.advanceTimersByTimeAsync(1000); // normal interval again (failures reset)
    await p.run;
    expect(p.seen).toEqual(["running_traffic", "running_traffic", "ready"]);
    expect(p.result.error).toBeNull();
  });

  it("still throws on a non-transient error such as 404", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "not found" }, 404)));
    const p = drive({ intervalMs: 1000 });
    await p.run;
    expect(p.result.error).toBeInstanceOf(ExperimentApiError);
    expect(p.errors).toEqual([]);
  });

  it("re-polls immediately when woken during the interval", async () => {
    const seq = ["running_traffic", "ready"] as const;
    let i = 0;
    const fetchMock = vi.fn(async () => jsonResponse(summary(seq[i++])));
    vi.stubGlobal("fetch", fetchMock);
    const waker = new PollWaker();
    const p = drive({ intervalMs: 60_000, waker });
    await vi.advanceTimersByTimeAsync(10);
    expect(p.seen).toEqual(["running_traffic"]);
    waker.wake();
    await vi.advanceTimersByTimeAsync(0);
    await p.run;
    expect(p.seen).toEqual(["running_traffic", "ready"]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("cuts a retry backoff short when woken", async () => {
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        call += 1;
        return call === 1 ? jsonResponse({}, 503) : jsonResponse(summary("ready"));
      })
    );
    const waker = new PollWaker();
    const p = drive({ intervalMs: 30_000, waker });
    await vi.advanceTimersByTimeAsync(10);
    waker.wake();
    await vi.advanceTimersByTimeAsync(0);
    await p.run;
    expect(p.seen).toEqual(["ready"]);
  });

  it("a wake that lands mid-request makes the next wait zero", async () => {
    let resolveFirst: (r: Response) => void = () => {};
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => {
        call += 1;
        if (call === 1) return new Promise<Response>((r) => (resolveFirst = r));
        return Promise.resolve(jsonResponse(summary("ready")));
      })
    );
    const waker = new PollWaker();
    const p = drive({ intervalMs: 60_000, waker });
    await vi.advanceTimersByTimeAsync(10);
    waker.wake(); // while the first GET is in flight
    resolveFirst(jsonResponse(summary("running_traffic")));
    await vi.advanceTimersByTimeAsync(0);
    await p.run;
    expect(p.seen).toEqual(["running_traffic", "ready"]);
  });

  it("stops on abort, including mid-backoff, without calling onError", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({}, 503)));
    const p = drive({ intervalMs: 1000, errorAfter: 1 });
    await vi.advanceTimersByTimeAsync(10);
    expect(p.errors).toHaveLength(1);
    p.ctrl.abort();
    await p.run;
    expect(p.result.error).toBeDefined();
    expect(p.errors).toHaveLength(1);
  });
});

describe("poll helpers", () => {
  it("classifies transient errors", () => {
    expect(isTransientError(new ExperimentApiError("x", 503))).toBe(true);
    expect(isTransientError(new ExperimentApiError("x", 429))).toBe(true);
    expect(isTransientError(new ExperimentApiError("x", 408))).toBe(true);
    expect(isTransientError(new ExperimentApiError("x", 404))).toBe(false);
    expect(isTransientError(new ExperimentApiError("x", 401))).toBe(false);
    expect(isTransientError(new TypeError("Failed to fetch"))).toBe(true);
    expect(isTransientError(new PollTimeoutError(15_000))).toBe(true);
    expect(isTransientError(new Error("other"))).toBe(false);
  });

  it("backs off exponentially from the interval, capped", () => {
    expect([1, 2, 3, 4, 5].map((n) => pollBackoffMs(n, 5000, 60_000))).toEqual([
      5000, 10_000, 20_000, 40_000, 60_000,
    ]);
    expect(pollBackoffMs(1, 0, 60_000)).toBe(1000); // never a hot loop
  });

  it("flags a stale running_traffic status once every episode has landed", () => {
    const exp = summary("running_traffic");
    expect(statusLooksStale(exp, { episodes: 19 })).toBe(false);
    expect(statusLooksStale(exp, { episodes: 20 })).toBe(true);
    expect(statusLooksStale({ ...exp, status: "ready" }, { episodes: 20 })).toBe(false);
    expect(statusLooksStale({ ...exp, progress: null }, { episodes: 20 })).toBe(false);
    expect(statusLooksStale(exp, null)).toBe(false);
  });

  it("wakes on visibilitychange to visible and on window focus", () => {
    const waker = new PollWaker();
    const wake = vi.spyOn(waker, "wake");
    const cleanup = wakeOnPageReturn(waker);
    const visibility = vi.spyOn(document, "visibilityState", "get");
    visibility.mockReturnValue("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(wake).not.toHaveBeenCalled();
    visibility.mockReturnValue("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(wake).toHaveBeenCalledTimes(1);
    window.dispatchEvent(new Event("focus"));
    expect(wake).toHaveBeenCalledTimes(2);
    cleanup();
    window.dispatchEvent(new Event("focus"));
    expect(wake).toHaveBeenCalledTimes(2);
    visibility.mockRestore();
  });
});
