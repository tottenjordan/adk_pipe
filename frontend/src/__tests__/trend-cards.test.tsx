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
