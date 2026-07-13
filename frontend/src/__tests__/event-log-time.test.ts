import { describe, it, expect } from "vitest";
import { formatEventTime } from "@/components/event-log";

describe("formatEventTime", () => {
  it("formats a valid epoch-seconds timestamp as a local time string", () => {
    // A real ADK event timestamp (epoch seconds, float). Just assert it renders
    // some non-empty time string rather than the literal "Invalid Date".
    const out = formatEventTime(1_700_000_000);
    expect(out).not.toBe("");
    expect(out).not.toBe("Invalid Date");
  });

  it("returns empty string for undefined (missing timestamp)", () => {
    // The trigger for the "Invalid Date" bug: some events (e.g. the final
    // streaming event) arrive with no numeric `timestamp`.
    expect(formatEventTime(undefined)).toBe("");
  });

  it("returns empty string for NaN", () => {
    expect(formatEventTime(NaN)).toBe("");
  });

  it("returns empty string for null", () => {
    expect(formatEventTime(null)).toBe("");
  });

  it("never returns the literal 'Invalid Date'", () => {
    for (const bad of [undefined, null, NaN, Infinity, -Infinity]) {
      expect(formatEventTime(bad as number)).not.toBe("Invalid Date");
    }
  });
});
