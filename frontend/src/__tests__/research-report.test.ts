import { describe, it, expect } from "vitest";
import {
  canonicalizeCitations,
  formatReportCitations,
  fromEditableReport,
  toEditableReport,
} from "@/lib/research-report";

const SOURCES = {
  "src-1": { title: "powerball.com", url: "https://r.example/1" },
  "src-2": { title: "", domain: "phish.net", url: "https://r.example/2" },
  "src-3": { url: "https://r.example/3" },
  "src-4": { title: "no-url.com" },
};

describe("formatReportCitations", () => {
  it("numbers sources by first appearance and reuses numbers", () => {
    const raw =
      'A <cite source="src-2" /> b <cite source="src-1" /> c <cite source="src-2" />.';
    const { markdown, sources } = formatReportCitations(raw, SOURCES);
    expect(markdown).toBe(
      "A[1](https://r.example/2) b[2](https://r.example/1) c[1](https://r.example/2)."
    );
    expect(sources).toEqual([
      { n: 1, id: "src-2", label: "phish.net", url: "https://r.example/2" },
      { n: 2, id: "src-1", label: "powerball.com", url: "https://r.example/1" },
    ]);
  });

  it("joins adjacent citations with no spaces between markers", () => {
    const raw = 'text <cite source="src-1" /><cite source="src-2" />.';
    expect(formatReportCitations(raw, SOURCES).markdown).toBe(
      "text[1](https://r.example/1)[2](https://r.example/2)."
    );
    const spaced = 'text <cite source="src-1" /> <cite source="src-2" /> next';
    expect(formatReportCitations(spaced, SOURCES).markdown).toBe(
      "text[1](https://r.example/1)[2](https://r.example/2) next"
    );
  });

  it("accepts whitespace and quote variants of the tag", () => {
    const raw = "x<cite  source = 'src-1'/> y<cite source=src-2/>";
    expect(formatReportCitations(raw, SOURCES).markdown).toBe(
      "x[1](https://r.example/1) y[2](https://r.example/2)"
    );
  });

  it("drops tags for unknown sources or sources without a url", () => {
    const raw = 'a <cite source="src-9" />. b <cite source="src-4" /> c';
    const out = formatReportCitations(raw, SOURCES);
    expect(out.markdown).toBe("a. b c");
    expect(out.sources).toEqual([]);
    expect(formatReportCitations('a <cite source="src-1" />.', undefined).markdown).toBe("a.");
  });

  it("labels fall back title → domain → id", () => {
    const { sources } = formatReportCitations('<cite source="src-3"/>', SOURCES);
    expect(sources[0].label).toBe("src-3");
  });

  it("escapes parentheses and spaces in urls", () => {
    const { markdown } = formatReportCitations('a<cite source="src-1"/>', {
      "src-1": { title: "t", url: "https://x.com/a (b)" },
    });
    expect(markdown).toBe("a[1](https://x.com/a%20%28b%29)");
  });

  it("leaves text without citations untouched", () => {
    expect(formatReportCitations("# Title\n\nplain [link](https://a.b)", SOURCES).markdown).toBe(
      "# Title\n\nplain [link](https://a.b)"
    );
  });
});

describe("editable report round trip", () => {
  it("converts tags to [src-N] markers", () => {
    expect(toEditableReport('a <cite source="src-12" /><cite source="src-3"/>.')).toBe(
      "a [src-12][src-3]."
    );
  });

  it("converts markers back to canonical tags", () => {
    expect(fromEditableReport("a [src-12][src-3].")).toBe(
      'a <cite source="src-12"/><cite source="src-3"/>.'
    );
  });

  it("round-trips to the canonical form, normalising variants", () => {
    const raw =
      "# T\n\nOne <cite source=\"src-1\" />, two<cite  source = 'src-2'/><cite source=src-3/>.";
    const back = fromEditableReport(toEditableReport(raw));
    expect(back).toBe(
      '# T\n\nOne <cite source="src-1"/>, two<cite source="src-2"/><cite source="src-3"/>.'
    );
    expect(back).toBe(canonicalizeCitations(raw));
    expect(fromEditableReport(toEditableReport(back))).toBe(back);
  });

  it("leaves user text without markers untouched", () => {
    const text = "New paragraph with [a link](https://x.y) and [brackets].";
    expect(fromEditableReport(text)).toBe(text);
    expect(toEditableReport(text)).toBe(text);
  });

  it("does not treat a markdown link labelled src-N as a marker", () => {
    expect(fromEditableReport("[src-1](https://x.y)")).toBe("[src-1](https://x.y)");
  });
});
