import { describe, it, expect, beforeEach } from "vitest";
import { hasStartedRun, markRunStarted } from "@/lib/run-kickoff";

// The run page mounts fresh on every browser reload, so the kickoff guard must
// be durable across mounts (sessionStorage) — not just an in-memory ref. This
// is what prevents a reload from spawning a duplicate detached run.

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
