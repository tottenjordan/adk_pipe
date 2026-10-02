import { describe, it, expect } from "vitest";
import {
  buildEditableResearchEdit,
  buildResearchEdit,
} from "@/app/run/[sessionId]/run-helpers";
import { toEditableReport } from "@/lib/research-report";

describe("buildResearchEdit", () => {
  it("returns an edit only when the report changed", () => {
    expect(buildResearchEdit("a", "a ")).toBeNull();
    expect(buildResearchEdit("a", "   ")).toBeNull();
    expect(buildResearchEdit("a", "b")).toEqual([
      { field: "combined_final_cited_report", value: "b" },
    ]);
  });

  it("keeps the edited value verbatim (untrimmed)", () => {
    expect(buildResearchEdit("", "  new report\n")).toEqual([
      { field: "combined_final_cited_report", value: "  new report\n" },
    ]);
  });
});

describe("buildEditableResearchEdit", () => {
  const report =
    'Jackpot hits $1.8B <cite source="src-1" /><cite source="src-20" />.\n\nMore.';

  it("produces no edit when the editable text is unchanged", () => {
    expect(buildEditableResearchEdit(report, toEditableReport(report))).toBeNull();
  });

  it("sends the edit with canonical cite tags", () => {
    const edited = toEditableReport(report).replace("More.", "More, edited.");
    expect(buildEditableResearchEdit(report, edited)).toEqual([
      {
        field: "combined_final_cited_report",
        value:
          'Jackpot hits $1.8B <cite source="src-1"/><cite source="src-20"/>.\n\nMore, edited.',
      },
    ]);
  });

  it("keeps deleted markers deleted", () => {
    const edited = toEditableReport(report).replace("[src-20]", "");
    expect(buildEditableResearchEdit(report, edited)?.[0].value).toBe(
      'Jackpot hits $1.8B <cite source="src-1"/>.\n\nMore.'
    );
  });
});
