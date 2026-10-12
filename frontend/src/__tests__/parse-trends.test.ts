import { describe, it, expect } from "vitest";
import {
  parseTrendsMarkdown,
  trendContextJson,
  trendRunParams,
  type ParsedTrend,
} from "@/components/trend-cards";

describe("parseTrendsMarkdown", () => {
  it("parses a single trend with all fields", () => {
    const md = `### Taylor Swift Engaged
* **The "Hook":** Love is in the air
* **Context:** The pop star just got engaged.
* **Why it fits:** Our audience loves pop culture moments.
* **The Strategic Bridge:** Position the product as a celebration essential.`;

    const result = parseTrendsMarkdown(md);
    expect(result).toHaveLength(1);
    expect(result[0].term).toBe("Taylor Swift Engaged");
    expect(result[0].hook).toBe("Love is in the air");
    expect(result[0].context).toBe("The pop star just got engaged.");
    expect(result[0].whyItFits).toBe(
      "Our audience loves pop culture moments."
    );
    expect(result[0].strategicBridge).toBe(
      "Position the product as a celebration essential."
    );
  });

  it("parses multiple trends", () => {
    const md = `### Trend A
* **The "Hook":** Hook A
* **Context:** Context A
* **Why it fits:** Fits A
* **The Strategic Bridge:** Bridge A

### Trend B
* **The "Hook":** Hook B
* **Context:** Context B
* **Why it fits:** Fits B
* **The Strategic Bridge:** Bridge B

### Trend C
* **The "Hook":** Hook C
* **Context:** Context C
* **Why it fits:** Fits C
* **The Strategic Bridge:** Bridge C`;

    const result = parseTrendsMarkdown(md);
    expect(result).toHaveLength(3);
    expect(result[0].term).toBe("Trend A");
    expect(result[1].term).toBe("Trend B");
    expect(result[2].term).toBe("Trend C");
  });

  it("returns empty array for empty string", () => {
    expect(parseTrendsMarkdown("")).toEqual([]);
  });

  it("returns no cards for text without a ### trend heading", () => {
    // pick_trends_agent's "no trend research available" one-liner (and any
    // other headerless text) is not a trend: a card would launch a creative run
    // for that literal sentence.
    expect(parseTrendsMarkdown("No trend research was available.")).toEqual([]);
  });

  it("drops the preamble before the first ### heading", () => {
    // PICK_TRENDS_INSTR's output format starts with `## Selected Trends Strategy`.
    const md = `## Selected Trends Strategy

### Trend A
* **The "Hook":** Hook A
* **Context:** Context A`;

    const result = parseTrendsMarkdown(md);
    expect(result.map((t) => t.term)).toEqual(["Trend A"]);
    expect(result[0].hook).toBe("Hook A");
  });

  it("handles The Hook without quotes", () => {
    const md = `### Some Trend
* **The Hook:** Unquoted hook text
* **Context:** Some context`;

    const result = parseTrendsMarkdown(md);
    expect(result).toHaveLength(1);
    expect(result[0].hook).toBe("Unquoted hook text");
  });

  it("returns empty strings for missing fields", () => {
    const md = `### Bare Trend`;
    const result = parseTrendsMarkdown(md);
    expect(result).toHaveLength(1);
    expect(result[0].term).toBe("Bare Trend");
    expect(result[0].hook).toBe("");
    expect(result[0].context).toBe("");
    expect(result[0].whyItFits).toBe("");
    expect(result[0].strategicBridge).toBe("");
  });
});

const TREND: ParsedTrend = {
  term: "Texas Football",
  hook: "Game day, your way",
  context: "The Red River Rivalry is this weekend.",
  whyItFits: "Fans gather.",
  strategicBridge: "Tailgate tone.",
};

describe("trendContextJson", () => {
  it("carries the hook and context as compact JSON (backend shape)", () => {
    expect(JSON.parse(trendContextJson(TREND))).toEqual({
      context: "The Red River Rivalry is this weekend.",
      hook: "Game day, your way",
    });
  });

  it("drops blank fields and is empty when nothing is left", () => {
    expect(JSON.parse(trendContextJson({ ...TREND, hook: " " }))).toEqual({
      context: "The Red River Rivalry is this weekend.",
    });
    expect(trendContextJson({ ...TREND, hook: "", context: "" })).toBe("");
  });
});

describe("trendRunParams", () => {
  const campaign = {
    brand: "PRS",
    target_audience: "fans",
    target_product: "SE CE24",
    key_selling_points: "tone",
    current_date: "2026-10-10",
  };

  it("passes the trend context and the scout run's date to the creative form", () => {
    const params = trendRunParams(TREND, campaign);
    expect(params.get("agent")).toBe("creative_agent");
    expect(params.get("targetSearchTrend")).toBe("Texas Football");
    expect(JSON.parse(params.get("trendContext") ?? "")).toEqual({
      context: "The Red River Rivalry is this weekend.",
      hook: "Game day, your way",
    });
    expect(params.get("trendDetectedOn")).toBe("2026-10-10");
  });

  it("omits what the scout run doesn't have", () => {
    const params = trendRunParams({ ...TREND, hook: "", context: "" }, { brand: "PRS" });
    expect(params.has("trendContext")).toBe(false);
    expect(params.has("trendDetectedOn")).toBe(false);
    expect(trendRunParams(TREND, { current_date: 20261010 }).has("trendDetectedOn")).toBe(false);
  });
});
