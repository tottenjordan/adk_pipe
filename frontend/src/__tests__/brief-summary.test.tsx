// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BriefSummary } from "@/app/run/[sessionId]/run-sections";

describe("BriefSummary headings", () => {
  it("is an h2 disclosure, so its Visual direction h3 never skips a level under the page h1", () => {
    render(
      <BriefSummary
        campaignFields={[{ key: "brand", label: "Brand", value: "PRS" }]}
        visualDirectionFields={[{ key: "visual_intent", label: "Art direction", value: "noir" }]}
      />
    );
    const h2 = screen.getByRole("heading", { level: 2, name: /Brief/ });
    const toggle = screen.getByRole("button", { name: /Brief/ });
    expect(h2).toContainElement(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(toggle);
    expect(screen.getByRole("heading", { level: 3, name: "Visual direction" })).toBeInTheDocument();
  });
});
