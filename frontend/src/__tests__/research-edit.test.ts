import { describe, it, expect } from "vitest";
import { buildResearchEdit } from "@/app/run/[sessionId]/run-helpers";

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
