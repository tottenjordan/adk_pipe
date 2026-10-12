import { describe, expect, it } from "vitest";
import { contextSensitivity, normalizeTerm, parseTrendFlags } from "@/lib/trend-flags";

const FLAGS = {
  flags: [
    { term: "Hurricane  Milton", sensitive: true, reason: "natural disaster" },
    { term: "taylor swift", sensitive: false, reason: "" },
    { term: "Plane Crash", sensitive: true, reason: "fatal accident" },
  ],
};

describe("normalizeTerm", () => {
  it("lowercases, collapses whitespace and trims (like trend_context._norm_term)", () => {
    expect(normalizeTerm("  Hurricane   MILTON \n")).toBe("hurricane milton");
    expect(normalizeTerm('"Plane Crash"')).toBe("plane crash");
    expect(normalizeTerm(3)).toBe("");
  });
});

describe("parseTrendFlags", () => {
  it("keeps only sensitive flags, keyed by normalised term", () => {
    const flags = parseTrendFlags(FLAGS);
    expect([...flags.entries()]).toEqual([
      ["hurricane milton", "natural disaster"],
      ["plane crash", "fatal accident"],
    ]);
  });

  it("accepts a JSON string and a fenced JSON string", () => {
    const json = JSON.stringify(FLAGS);
    expect(parseTrendFlags(json).get("plane crash")).toBe("fatal accident");
    expect(parseTrendFlags("```json\n" + json + "\n```").get("hurricane milton")).toBe(
      "natural disaster",
    );
  });

  it("ignores malformed input and entries", () => {
    expect(parseTrendFlags(undefined).size).toBe(0);
    expect(parseTrendFlags("not json").size).toBe(0);
    expect(parseTrendFlags([]).size).toBe(0);
    expect(parseTrendFlags({ flags: "x" }).size).toBe(0);
    const flags = parseTrendFlags({
      flags: [
        null,
        "x",
        { term: "", sensitive: true, reason: "r" },
        { term: "quake", sensitive: "true", reason: "r" },
        { term: "fire", sensitive: true, reason: 5 },
      ],
    });
    expect([...flags.entries()]).toEqual([["fire", ""]]);
  });
});

describe("contextSensitivity", () => {
  it("reads sensitive + sensitive_reason from trend_context JSON", () => {
    expect(
      contextSensitivity(
        JSON.stringify({ context: "c", sensitive: true, sensitive_reason: "natural disaster" }),
      ),
    ).toEqual({ sensitive: true, reason: "natural disaster" });
  });

  it("is not sensitive for plain, missing or malformed context", () => {
    const no = { sensitive: false, reason: "" };
    expect(contextSensitivity(JSON.stringify({ context: "c", hook: "h" }))).toEqual(no);
    expect(contextSensitivity(undefined)).toEqual(no);
    expect(contextSensitivity("")).toEqual(no);
    expect(contextSensitivity("{bad")).toEqual(no);
    expect(contextSensitivity(JSON.stringify({ sensitive: "yes" }))).toEqual(no);
  });
});
