import { describe, it, expect } from "vitest";
import { parseTrendsMarkdown } from "@/components/trend-cards";

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

  it("treats plain text as a single term with empty fields", () => {
    // The parser splits on sections after filtering empty strings,
    // so plain text without ### becomes one section with the text as term.
    const result = parseTrendsMarkdown("No trends here");
    expect(result).toHaveLength(1);
    expect(result[0].term).toBe("No trends here");
    expect(result[0].hook).toBe("");
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
