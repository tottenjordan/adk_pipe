// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  ensureRunStarted,
  hasStartedRun,
  isUnstartedRun,
  markRunStarted,
  readRunMessage,
} from "@/lib/run-kickoff";

// The run page mounts fresh on every browser reload (and twice under React
// StrictMode), so the kickoff guard must be durable across mounts
// (sessionStorage) and shared between overlapping effect runs (in-flight map).
// This is what prevents a reload or a dev double-mount from spawning a
// duplicate detached run.

describe("run-kickoff guard", () => {
  beforeEach(() => sessionStorage.clear());

  it("reports not-started for a fresh session", () => {
    expect(hasStartedRun("s1")).toBe(false);
  });

  it("reports started after marking (survives a simulated reload)", () => {
    markRunStarted("s1");
    // A reload re-imports the module but sessionStorage persists within the tab.
    expect(hasStartedRun("s1")).toBe(true);
  });

  it("is scoped per session id", () => {
    markRunStarted("s1");
    expect(hasStartedRun("s1")).toBe(true);
    expect(hasStartedRun("s2")).toBe(false);
  });
});

describe("ensureRunStarted", () => {
  beforeEach(() => sessionStorage.clear());

  it("starts once and marks the session started", async () => {
    const start = vi.fn().mockResolvedValue(undefined);
    await ensureRunStarted("s1", start);
    expect(start).toHaveBeenCalledTimes(1);
    expect(hasStartedRun("s1")).toBe(true);
  });

  it("shares one start between overlapping calls (StrictMode double effect)", async () => {
    let resolve!: () => void;
    const start = vi.fn(
      () => new Promise<void>((r) => {
        resolve = r;
      })
    );
    const a = ensureRunStarted("s1", start);
    const b = ensureRunStarted("s1", start);
    expect(start).toHaveBeenCalledTimes(1);
    expect(hasStartedRun("s1")).toBe(false);
    resolve();
    await Promise.all([a, b]);
    expect(start).toHaveBeenCalledTimes(1);
    expect(hasStartedRun("s1")).toBe(true);
  });

  it("skips start when the tab already started the run (reload)", async () => {
    markRunStarted("s1");
    const start = vi.fn();
    await ensureRunStarted("s1", start);
    expect(start).not.toHaveBeenCalled();
  });

  it("does not mark a failed start, and a later attempt retries", async () => {
    const failing = vi.fn().mockRejectedValue(new Error("boom"));
    await expect(ensureRunStarted("s1", failing)).rejects.toThrow("boom");
    expect(hasStartedRun("s1")).toBe(false);

    const ok = vi.fn().mockResolvedValue(undefined);
    await ensureRunStarted("s1", ok);
    expect(ok).toHaveBeenCalledTimes(1);
    expect(hasStartedRun("s1")).toBe(true);
  });

  it("keeps sessions independent", async () => {
    const start = vi.fn().mockResolvedValue(undefined);
    await Promise.all([ensureRunStarted("s1", start), ensureRunStarted("s2", start)]);
    expect(start).toHaveBeenCalledTimes(2);
  });
});

describe("readRunMessage", () => {
  beforeEach(() => sessionStorage.clear());

  it("reads the stored kick-off message", () => {
    sessionStorage.setItem("run:s1", JSON.stringify({ message: "hello" }));
    expect(readRunMessage("s1")).toBe("hello");
  });

  it("returns empty for a missing, corrupt or malformed entry", () => {
    expect(readRunMessage("none")).toBe("");
    sessionStorage.setItem("run:bad", "{not json");
    expect(readRunMessage("bad")).toBe("");
    sessionStorage.setItem("run:num", JSON.stringify({ message: 3 }));
    expect(readRunMessage("num")).toBe("");
  });
});

describe("isUnstartedRun", () => {
  it("treats an unknown session as unstarted", () => {
    expect(isUnstartedRun({ status: "not_found", events: [], nextCursor: 0 })).toBe(true);
  });

  it("treats an existing session with an empty log as unstarted", () => {
    expect(isUnstartedRun({ status: "running", events: [], nextCursor: 0 })).toBe(true);
  });

  it("treats any run with events as known", () => {
    expect(isUnstartedRun({ status: "running", events: [{}], nextCursor: 1 })).toBe(false);
    expect(isUnstartedRun({ status: "not_found", events: [{}], nextCursor: 1 })).toBe(false);
    expect(isUnstartedRun({ status: "done", events: [], nextCursor: 12 })).toBe(false);
  });

  it("treats terminal statuses as known even without new events", () => {
    expect(isUnstartedRun({ status: "done", events: [], nextCursor: 0 })).toBe(false);
    expect(isUnstartedRun({ status: "error", events: [] })).toBe(false);
  });
});
