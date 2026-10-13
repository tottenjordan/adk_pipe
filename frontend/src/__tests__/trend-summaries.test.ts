import { describe, expect, it } from "vitest";
import {
  MAX_SUMMARY_CHARS,
  formatRefreshDate,
  parseRefreshDate,
  parseTrendSummaries,
} from "@/lib/trend-summaries";

const DIGEST = {
  refresh_date: "2026-10-13",
  source: "fresh",
  items: [
    { term: "Texas Football", summary: "Texas plays Oklahoma on Saturday." },
    { term: "hurricane  milton", summary: "  A hurricane is   approaching Florida. " },
  ],
};

describe("parseTrendSummaries", () => {
  it("maps normalised terms to summaries", () => {
    const map = parseTrendSummaries(DIGEST);
    expect(map.get("texas football")).toBe("Texas plays Oklahoma on Saturday.");
    expect(map.get("hurricane milton")).toBe("A hurricane is approaching Florida.");
    expect(map.size).toBe(2);
  });

  it("accepts a JSON string and a fenced JSON string", () => {
    expect(parseTrendSummaries(JSON.stringify(DIGEST)).size).toBe(2);
    const fenced = "```json\n" + JSON.stringify(DIGEST) + "\n```";
    expect(parseTrendSummaries(fenced).get("texas football")).toBe(
      "Texas plays Oklahoma on Saturday."
    );
  });

  it.each([
    ["missing", undefined],
    ["null", null],
    ["not json", "not json"],
    ["an array", [DIGEST]],
    ["a number", 42],
    ["no items", { source: "unavailable" }],
    ["empty items", { source: "unavailable", items: [] }],
    ["items not a list", { items: { term: "a", summary: "b" } }],
  ])("returns an empty map for %s", (_label, raw) => {
    expect(parseTrendSummaries(raw).size).toBe(0);
  });

  it("skips bad items, blank summaries and duplicates", () => {
    const map = parseTrendSummaries({
      items: [
        null,
        "texas football",
        { term: 7, summary: "number term" },
        { term: "   ", summary: "blank term" },
        { term: "a", summary: 3 },
        { term: "b", summary: "   " },
        { term: "c" },
        { term: "World Series", summary: "first" },
        { term: "world series", summary: "second" },
      ],
    });
    expect([...map.entries()]).toEqual([["world series", "first"]]);
  });

  it("caps long summaries", () => {
    const long = "word ".repeat(200);
    const summary = parseTrendSummaries({ items: [{ term: "x", summary: long }] }).get("x");
    expect(summary).toBeDefined();
    expect(summary!.length).toBeLessThanOrEqual(MAX_SUMMARY_CHARS);
    expect(summary!.endsWith("…")).toBe(true);
    const exact = "a".repeat(MAX_SUMMARY_CHARS);
    expect(parseTrendSummaries({ items: [{ term: "y", summary: exact }] }).get("y")).toBe(exact);
  });
});

describe("parseRefreshDate / formatRefreshDate", () => {
  it("accepts a valid ISO date", () => {
    expect(parseRefreshDate("2026-10-13")).toBe("2026-10-13");
    expect(parseRefreshDate(" 2026-10-13 ")).toBe("2026-10-13");
    expect(formatRefreshDate("2026-10-13")).toBe("Oct 13");
    expect(formatRefreshDate("2026-01-01")).toBe("Jan 1");
  });

  it.each([undefined, null, 20261013, "", "2026-10", "13/10/2026", "2026-02-30", "2026-13-01", "2026-10-13T00:00:00Z"])(
    "rejects %s",
    (raw) => {
      expect(parseRefreshDate(raw)).toBeNull();
      expect(formatRefreshDate(raw)).toBe("");
    }
  );
});
