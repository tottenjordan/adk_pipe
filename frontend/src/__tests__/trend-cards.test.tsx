// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

import { TrendCards, type ParsedTrend } from "@/components/trend-cards";

const trend = (term: string): ParsedTrend => ({
  term,
  hook: "",
  context: "",
  whyItFits: "",
  strategicBridge: "",
});

describe("TrendCards: sensitive trends", () => {
  it("shows a Sensitive chip with its reason on flagged cards only", () => {
    render(
      <TrendCards
        trends={[trend("Hurricane Milton"), trend("World Series")]}
        campaignState={{
          trend_flags: { flags: [{ term: "hurricane milton", sensitive: true, reason: "natural disaster" }] },
        }}
      />
    );
    const chips = screen.getAllByText("Sensitive");
    expect(chips).toHaveLength(1);
    expect(chips[0]).toHaveAttribute("title", "natural disaster");
    const [flagged, plain] = screen.getAllByRole("button", { name: /generate creatives/i });
    expect(flagged).toHaveAccessibleDescription("Sensitive: natural disaster");
    expect(plain).not.toHaveAccessibleDescription();
  });

  it("shows no chip without flags", () => {
    render(<TrendCards trends={[trend("World Series")]} campaignState={{}} />);
    expect(screen.queryByText("Sensitive")).not.toBeInTheDocument();
  });
});

describe("TrendCards: trend summaries", () => {
  it("shows the matching summary under the hook", () => {
    render(
      <TrendCards
        trends={[{ ...trend("World Series"), hook: "October baseball" }, trend("Taylor Swift")]}
        campaignState={{
          trend_summaries: JSON.stringify({
            source: "cache",
            items: [{ term: "world  series", summary: "The Dodgers lead the World Series 2-1." }],
          }),
        }}
      />
    );
    const hook = screen.getByText("October baseball");
    const summary = screen.getByText("The Dodgers lead the World Series 2-1.");
    expect(hook.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(summary).toHaveClass("text-muted-foreground");
    expect(screen.getAllByText(/Dodgers/)).toHaveLength(1);
  });

  it("renders the same markup without summaries", () => {
    const plain = render(<TrendCards trends={[trend("World Series")]} campaignState={{}} />)
      .container.innerHTML;
    const malformed = render(
      <TrendCards trends={[trend("World Series")]} campaignState={{ trend_summaries: "nope" }} />
    ).container.innerHTML;
    expect(malformed).toBe(plain);
  });
});

