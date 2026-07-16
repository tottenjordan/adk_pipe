import { describe, it, expect } from "vitest";
import { parseRawGtrends } from "@/app/run/[sessionId]/run-helpers";

describe("parseRawGtrends", () => {
  it("returns the trimmed string terms", () => {
    expect(parseRawGtrends(["Trend A", "  Trend B  ", "Trend C"])).toEqual([
      "Trend A",
      "Trend B",
      "Trend C",
    ]);
  });

  it("drops empty and whitespace-only terms", () => {
    expect(parseRawGtrends(["Trend A", "", "   ", "Trend B"])).toEqual([
      "Trend A",
      "Trend B",
    ]);
  });

  it("drops non-string entries", () => {
    expect(
      parseRawGtrends(["Trend A", 42, null, undefined, { x: 1 }, "Trend B"])
    ).toEqual(["Trend A", "Trend B"]);
  });

  it("returns an empty array for undefined", () => {
    expect(parseRawGtrends(undefined)).toEqual([]);
  });

  it("returns an empty array for a non-array value", () => {
    expect(parseRawGtrends("Trend A")).toEqual([]);
    expect(parseRawGtrends({ raw_gtrends: ["a"] })).toEqual([]);
  });

  it("returns an empty array for an empty array", () => {
    expect(parseRawGtrends([])).toEqual([]);
  });
});
