// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ResultsSummary } from "@/app/results/[sessionId]/results-summary";
import { classifyWarnings } from "@/lib/run-warnings";

const RESEARCH_TITLE = "Some research steps produced no output";
const SAVES_TITLE = "Some outputs could not be saved";
const QUALITY_TITLE = "Quality checks flagged issues";

function renderSummary(warnings: string[]) {
  return render(
    <ResultsSummary
      report={null}
      status="idle"
      onRefresh={() => {}}
      refreshDisabled={false}
      noImages={false}
      runWarnings={classifyWarnings(warnings)}
    />,
  );
}

describe("ResultsSummary run notes", () => {
  it("shows a quality-only note without the research banner (the WWE false alarm)", () => {
    const note =
      "Image qa has unresolved issues: 1 (e.g. The Lonely Tech Apron: WWE logo on road case sticker)";
    renderSummary([note]);
    expect(screen.queryByText(RESEARCH_TITLE)).toBeNull();
    expect(screen.queryByText(/incomplete research/)).toBeNull();
    expect(screen.queryByText(SAVES_TITLE)).toBeNull();
    expect(screen.getByText(QUALITY_TITLE)).toBeTruthy();
    expect(
      screen.getByText(/Automated checks flagged these items; review them before using the creatives\./),
    ).toBeTruthy();
    expect(screen.getByText(note)).toBeTruthy();
  });

  it("shows the research banner only for research gaps", () => {
    const note = "Step 'gs_web_search_insights' exhausted retries and produced no output.";
    renderSummary([note]);
    expect(screen.getByText(RESEARCH_TITLE)).toBeTruthy();
    expect(screen.getByText(/incomplete research/)).toBeTruthy();
    expect(screen.getByText(note)).toBeTruthy();
    expect(screen.queryByText(QUALITY_TITLE)).toBeNull();
  });

  it("shows a saves notice for persistence failures", () => {
    const note = "Gallery save has unresolved issues: 1 (e.g. HTML gallery failed: x)";
    renderSummary([note]);
    expect(screen.getByText(SAVES_TITLE)).toBeTruthy();
    expect(screen.getByText(note)).toBeTruthy();
    expect(screen.queryByText(RESEARCH_TITLE)).toBeNull();
    expect(screen.queryByText(QUALITY_TITLE)).toBeNull();
  });

  it("renders all three notices for a mixed list, and none when clean", () => {
    const { unmount } = renderSummary([
      "Step 'creative_brief' exhausted retries and produced no output.",
      "Eval row save has unresolved issues: 1 (e.g. x)",
      "Ad copy check has unresolved issues: 1 (e.g. y)",
    ]);
    expect(screen.getByText(RESEARCH_TITLE)).toBeTruthy();
    expect(screen.getByText(SAVES_TITLE)).toBeTruthy();
    expect(screen.getByText(QUALITY_TITLE)).toBeTruthy();
    unmount();

    renderSummary(["Step '_images_generated' exhausted retries and produced no output."]);
    expect(screen.queryAllByRole("status")).toHaveLength(0);
  });
});
