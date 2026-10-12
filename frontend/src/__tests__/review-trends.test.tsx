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
