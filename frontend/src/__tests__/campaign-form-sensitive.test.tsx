// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// A stable object per test: the form's prefill effect depends on its identity.
let search = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useSearchParams: () => search,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import Home from "@/app/page";

const params = (trendContext?: Record<string, unknown>) => {
  const p = new URLSearchParams({
    agent: "creative_agent",
    brand: "PRS",
    targetSearchTrend: "Hurricane Milton",
  });
  if (trendContext) p.set("trendContext", JSON.stringify(trendContext));
  return p;
};

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }))
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("campaign form: sensitive prefilled trend", () => {
  it("warns when the prefilled trend context is sensitive", async () => {
    search = params({ context: "c", sensitive: true, sensitive_reason: "natural disaster" });
    render(<Home />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Sensitive trend: natural disaster. Creatives will never reference the event, so they may connect only loosely; many brands avoid trends like this."
    );
    expect(screen.getByText(/Pre-filled with trend/)).toBeInTheDocument();
  });

  it("clears the warning when the trend is edited", async () => {
    search = params({ sensitive: true, sensitive_reason: "natural disaster" });
    render(<Home />);
    await screen.findByRole("alert");
    fireEvent.change(screen.getByLabelText("Target search trend"), {
      target: { value: "World Series" },
    });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows no warning for a non-sensitive context", async () => {
    search = params({ context: "c", hook: "h" });
    render(<Home />);
    await screen.findByText(/Pre-filled with trend/);
    expect(screen.queryByText(/Sensitive trend/)).not.toBeInTheDocument();
  });
});
