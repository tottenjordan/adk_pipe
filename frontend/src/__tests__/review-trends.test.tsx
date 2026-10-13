// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ReviewPanel } from "@/app/run/[sessionId]/ReviewPanel";

const RAW = ["Taylor Swift", "Hurricane Milton", "World Series"];
const FLAGS = {
  flags: [
    { term: "hurricane  milton", sensitive: true, reason: "natural disaster with casualties" },
    { term: "Taylor Swift", sensitive: false, reason: "" },
  ],
};

function renderPick(state: Record<string, unknown> = {}) {
  const onResume = vi.fn();
  const view = render(
    <ReviewPanel
      functionName="review_trends"
      sessionState={{ raw_gtrends: RAW, ...state }}
      onResume={onResume}
    />
  );
  return { onResume, ...view };
}

const candidate = (name: RegExp) => screen.getByRole("button", { name });

describe("ReviewTrends: sensitive trends", () => {
  it("marks a flagged candidate with a Sensitive chip and its reason", () => {
    renderPick({ trend_flags: JSON.stringify(FLAGS) });
    const button = candidate(/Hurricane Milton/);
    expect(button).toHaveAccessibleDescription(/natural disaster with casualties/);
    const chip = screen.getByText("Sensitive");
    expect(button).toContainElement(chip);
    expect(chip).toHaveAttribute("title", "natural disaster with casualties");
    // Unflagged candidates carry no chip.
    expect(candidate(/Taylor Swift/)).not.toHaveTextContent("Sensitive");
    expect(screen.getAllByText("Sensitive")).toHaveLength(1);
  });

  it("keeps flagged candidates selectable and notes the risk once selected", () => {
    const { onResume } = renderPick({ trend_flags: FLAGS });
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
    fireEvent.click(candidate(/Taylor Swift/));
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
    fireEvent.click(candidate(/Hurricane Milton/));
    expect(candidate(/Hurricane Milton/)).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("note")).toHaveTextContent(
      "This trend is about a sensitive event (natural disaster with casualties). Creatives will never reference the event, so they may connect only loosely; many brands avoid trends like this."
    );
    fireEvent.click(screen.getByRole("button", { name: /confirm selection/i }));
    expect(onResume.mock.calls.at(-1)?.[0]).toMatchObject({
      selected_trends: ["Taylor Swift", "Hurricane Milton"],
    });
    fireEvent.click(candidate(/Hurricane Milton/));
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
  });

  it("renders the same markup without flags", () => {
    const plain = renderPick().container.innerHTML;
    const malformed = renderPick({ trend_flags: "not json" }).container.innerHTML;
    expect(malformed).toBe(plain);
    expect(screen.queryAllByText("Sensitive")).toHaveLength(0);
  });
});

const SUMMARIES = {
  refresh_date: "2026-10-13",
  source: "fresh",
  items: [
    { term: "taylor  swift", summary: "Taylor Swift announced new tour dates on Monday." },
    { term: "Hurricane Milton", summary: "Hurricane Milton is approaching Florida's Gulf Coast." },
  ],
};

describe("ReviewTrends: trend summaries", () => {
  it("shows a summary under a candidate and links it as the description", () => {
    renderPick({ trend_summaries: JSON.stringify(SUMMARIES) });
    const button = candidate(/Taylor Swift/);
    // The summary is the description, not part of the name.
    expect(button).toHaveAccessibleName(/^\W*Taylor Swift$/);
    expect(button).toHaveAccessibleDescription(
      "Taylor Swift announced new tour dates on Monday."
    );
    const summary = screen.getByText("Taylor Swift announced new tour dates on Monday.");
    expect(button).toContainElement(summary);
    expect(summary).toHaveClass("text-xs", "text-muted-foreground", "line-clamp-2");
    // A candidate without a summary has no description.
    expect(candidate(/World Series/)).not.toHaveAccessibleDescription();
  });

  it("shows both the Sensitive chip and the summary on a flagged candidate", () => {
    renderPick({ trend_flags: FLAGS, trend_summaries: SUMMARIES });
    const button = candidate(/Hurricane Milton/);
    expect(button).toContainElement(screen.getByText("Sensitive"));
    expect(button).toHaveTextContent("Hurricane Milton is approaching Florida's Gulf Coast.");
    expect(button).toHaveAccessibleDescription(
      "Sensitive: natural disaster with casualties Hurricane Milton is approaching Florida's Gulf Coast."
    );
    fireEvent.click(button);
    expect(screen.getByRole("note")).toHaveTextContent(/natural disaster with casualties/);
  });

  it("shows the Google Trends date when the refresh date parses", () => {
    renderPick({ gtrends_refresh_date: "2026-10-13" });
    expect(screen.getByText("Google Trends for Oct 13")).toBeInTheDocument();
  });

  it("renders the same markup without summaries", () => {
    const plain = renderPick().container.innerHTML;
    for (const state of [
      { trend_summaries: "not json" },
      { trend_summaries: { source: "unavailable", items: [] } },
      { trend_summaries: { items: [{ term: "Unknown", summary: "Not a candidate." }] } },
      { gtrends_refresh_date: "not a date" },
    ]) {
      expect(renderPick(state).container.innerHTML).toBe(plain);
    }
    expect(screen.queryByText(/Google Trends for/)).not.toBeInTheDocument();
  });
});
