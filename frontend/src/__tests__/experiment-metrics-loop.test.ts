import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { keepIfUnchanged, startMetricsLoop } from "@/lib/experiment-view";
import type { ExperimentSummary } from "@/lib/experiments";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

/** A fetch whose calls resolve only when the test says so (and ignore the abort signal). */
function controlledFetch() {
  const calls: { signal: AbortSignal; resolve: (v: string) => void }[] = [];
  const fetch = vi.fn(
    (signal: AbortSignal) =>
      new Promise<string>((resolve) => {
        calls.push({ signal, resolve });
      })
  );
  return { fetch, calls };
}

describe("startMetricsLoop", () => {
  it("never overlaps requests when a response takes longer than the interval", async () => {
    const { fetch, calls } = controlledFetch();
    const applied: string[] = [];
    const stop = startMetricsLoop({
      fetch,
      apply: (v) => applied.push(v),
      repeat: true,
      intervalMs: 1000,
      timeoutMs: 60_000,
    });
    expect(fetch).toHaveBeenCalledTimes(1);
    // Five intervals pass while the first request is still in flight: no second request.
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetch).toHaveBeenCalledTimes(1);
    calls[0].resolve("one");
    await vi.advanceTimersByTimeAsync(0);
    expect(applied).toEqual(["one"]);
    // The next request waits a full interval after the previous one finished.
    await vi.advanceTimersByTimeAsync(999);
    expect(fetch).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetch).toHaveBeenCalledTimes(2);
    stop();
  });

  it("abandons a request after the timeout and ignores its late (stale) response", async () => {
    const { fetch, calls } = controlledFetch();
    const applied: string[] = [];
    const stop = startMetricsLoop({
      fetch,
      apply: (v) => applied.push(v),
      repeat: true,
      intervalMs: 1000,
      timeoutMs: 3000,
    });
    await vi.advanceTimersByTimeAsync(3000);
    expect(calls[0].signal.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetch).toHaveBeenCalledTimes(2);
    calls[1].resolve("new");
    await vi.advanceTimersByTimeAsync(0);
    // The first request finally answers, after a newer one already landed.
    calls[0].resolve("old");
    await vi.advanceTimersByTimeAsync(0);
    expect(applied).toEqual(["new"]);
    stop();
  });

  it("drops a response that lands after stop (unmount / status change)", async () => {
    const { fetch, calls } = controlledFetch();
    const applied: string[] = [];
    const stop = startMetricsLoop({ fetch, apply: (v) => applied.push(v), repeat: true, intervalMs: 1000 });
    stop();
    expect(calls[0].signal.aborted).toBe(true);
    calls[0].resolve("late");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(applied).toEqual([]);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("loads once without repeat, and keeps polling after a failed request", async () => {
    const once = vi.fn(async () => "x");
    const stopOnce = startMetricsLoop({ fetch: once, apply: () => {}, repeat: false, intervalMs: 1000 });
    await vi.advanceTimersByTimeAsync(5000);
    expect(once).toHaveBeenCalledTimes(1);
    stopOnce();

    let n = 0;
    const flaky = vi.fn(async () => {
      n += 1;
      if (n === 1) throw new Error("boom");
      return `ok${n}`;
    });
    const applied: string[] = [];
    const stop = startMetricsLoop({ fetch: flaky, apply: (v) => applied.push(v), repeat: true, intervalMs: 1000 });
    await vi.advanceTimersByTimeAsync(1000);
    expect(applied).toEqual(["ok2"]);
    stop();
  });
});

function summary(over: Partial<ExperimentSummary> = {}): ExperimentSummary {
  return {
    experimentId: "exp-1",
    userId: "u",
    sessionId: "s",
    appName: "creative_agent",
    createdAt: "2026-10-05T14:00:00Z",
    updatedAt: "2026-10-05T14:00:00Z",
    status: "running_traffic",
    scenario: "drift",
    ctrMode: "demo",
    rewardMode: "click",
    ttlExpiresAt: null,
    arms: [],
    endpointId: null,
    trafficExecution: null,
    progress: { episodesDone: 3, episodesTotal: 20 },
    error: null,
    ...over,
  };
}

describe("keepIfUnchanged", () => {
  it("keeps the previous object for an identical poll payload", () => {
    const prev = summary();
    expect(keepIfUnchanged(prev, summary())).toBe(prev);
  });

  it("takes the new object when updatedAt, status or progress changed", () => {
    const prev = summary();
    for (const next of [
      summary({ updatedAt: "2026-10-05T14:00:05Z" }),
      summary({ status: "ready" }),
      summary({ progress: { episodesDone: 4, episodesTotal: 20 } }),
      summary({ progress: null }),
    ]) {
      expect(keepIfUnchanged(prev, next)).toBe(next);
    }
    const first = summary();
    expect(keepIfUnchanged(null, first)).toBe(first);
  });
});
